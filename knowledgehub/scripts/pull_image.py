"""从容器镜像源（m.daocloud.io 或上游 registry）直接拉取镜像为 docker-archive tar。

背景：本机网络下 dockerd 直连/代理拉取均不稳定，但宿主机可直连 daocloud（或经本地
代理访问上游）。WSL 内装 skopeo 需要交互 sudo，故用 Registry HTTP API 自行拉取。
- 认证：从 401 challenge 的 WWW-Authenticate 动态解析 realm/service；
- blob：307 跟随到 CDN（如 daocloud R2 / Docker Hub cloudflare），仅带 Range 头；
- 大 blob：24MiB 分块 Range 下载 + 断点续传（实测单连接约 60MiB 会被截断）；
- 校验：整 blob sha256；失败整文件重下。

用法：
    python scripts/pull_image.py --image docker.io/library/mongo:7.0
    python scripts/pull_image.py --image docker.io/minio/minio:RELEASE.x --proxy http://127.0.0.1:7897 --mirror https://registry-1.docker.io --tag m.daocloud.io/docker.io/minio/minio:RELEASE.x

产出：tmp/pull/<slug>.tar（docker-archive，layer 保留 gzip，docker load 自动识别）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tarfile
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

DEFAULT_MIRROR = "https://m.daocloud.io"
OUT_DIR = Path(__file__).resolve().parents[1] / "tmp" / "pull"
CHUNK = 24 << 20  # 单连接 Range 块上限 24MiB：实测 CDN 会在单连接约 60MiB 处截断
MAX_TRIES = 20

ACCEPT = ",".join(
    [
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
    ]
)


def log(msg: str) -> None:
    print(msg, flush=True)


def request_with_retry(client: httpx.Client, method: str, url: str, **kwargs) -> httpx.Response:
    last_exc: Exception | None = None
    for attempt in range(1, MAX_TRIES + 1):
        try:
            response = client.request(method, url, **kwargs)
            if response.status_code < 500 and response.status_code != 403:
                return response
            last_exc = RuntimeError(f"HTTP {response.status_code}")
            if response.status_code == 403:
                time.sleep(min(2 ** attempt, 20))  # 上游限流时退避后重试
                continue
        except httpx.HTTPError as exc:
            last_exc = exc
        wait = min(2 ** attempt, 30)
        log(f"  retry {attempt}/{MAX_TRIES} after error: {last_exc} (sleep {wait}s)")
        time.sleep(wait)
    raise RuntimeError(f"request failed after {MAX_TRIES} tries: {url} ({last_exc})")


def get_token(client: httpx.Client, mirror: str, repository: str) -> str | None:
    """按 401 challenge 动态获取 token；匿名可读时返回 None。"""
    probe = f"{mirror}/v2/{repository}/manifests/latest"
    response = request_with_retry(
        client, "GET", probe, headers={"Accept": ACCEPT}, follow_redirects=False, timeout=30
    )
    if response.status_code == 200:
        return None
    if response.status_code != 401:
        raise RuntimeError(f"unexpected probe status {response.status_code} for {probe}")
    www = response.headers.get("www-authenticate", "")
    realm_match = re.search(r'realm="([^"]+)"', www)
    if not realm_match:
        raise RuntimeError(f"no realm in WWW-Authenticate: {www[:200]}")
    realm = realm_match.group(1)
    service_match = re.search(r'service="([^"]+)"', www)
    service = service_match.group(1) if service_match else urlparse(mirror).netloc
    params = {"service": service, "scope": f"repository:{repository}:pull"}
    token_response = request_with_retry(client, "GET", realm, params=params, timeout=30)
    token_response.raise_for_status()
    payload = token_response.json()
    token = payload.get("token") or payload.get("access_token")
    if not token:
        raise RuntimeError(f"no token in response from {realm}: {str(payload)[:200]}")
    return token


def auth_headers(token: str | None) -> dict:
    return {"Authorization": f"Bearer {token}"} if token else {}


def fetch_manifest(client: httpx.Client, mirror: str, repository: str, reference: str, token: str | None) -> dict:
    headers = {**auth_headers(token), "Accept": ACCEPT}
    url = f"{mirror}/v2/{repository}/manifests/{reference}"
    response = request_with_retry(client, "GET", url, headers=headers, timeout=60)
    response.raise_for_status()
    manifest = response.json()

    media = manifest.get("mediaType", "")
    if "list" in media or "index" in media or "manifests" in manifest:
        for entry in manifest.get("manifests", []):
            platform = entry.get("platform", {})
            if platform.get("architecture") == "amd64" and platform.get("os") == "linux":
                digest = entry["digest"]
                log(f"  multi-arch -> linux/amd64 {digest[:19]}")
                return fetch_manifest(client, mirror, repository, digest, token)
        raise RuntimeError("no linux/amd64 manifest in index")
    return manifest


def resolve_blob_url(client: httpx.Client, mirror: str, repository: str, digest: str, token: str | None) -> str:
    """解析 blob 的最终下载地址（mirror 307 到 CDN，签名 URL 通常有效数小时）。"""
    url = f"{mirror}/v2/{repository}/blobs/{digest}"
    headers = auth_headers(token)
    for _ in range(6):
        response = client.get(url, headers=headers, follow_redirects=False, timeout=60)
        if response.status_code in (301, 302, 307, 308):
            url = response.headers["location"]
            continue
        response.raise_for_status()
        return url
    raise RuntimeError(f"too many redirects resolving {digest[:19]}")


def _fetch_range(client: httpx.Client, url: str, start: int, end: int) -> bytes:
    """拉取 [start, end] 闭区间；服务端不支持 Range（返回 200 全量）时视为错误。"""
    last_exc: Exception | None = None
    for attempt in range(1, 16):
        try:
            with client.stream(
                "GET", url, headers={"Range": f"bytes={start}-{end}"}, timeout=90
            ) as response:
                if response.status_code == 416:
                    return b""  # 起点已在 EOF（如前一块恰好在文件末尾结束）
                if response.status_code == 200:
                    raise RuntimeError("server ignored Range (200 instead of 206)")
                response.raise_for_status()
                return response.read()
        except (httpx.HTTPError, RuntimeError) as exc:
            last_exc = exc
            time.sleep(min(2 ** attempt, 15))
    raise RuntimeError(f"range fetch failed after 15 tries: {last_exc}")


def download_blob(
    client: httpx.Client, mirror: str, repository: str, digest: str, token: str | None, dest: Path
) -> None:
    if dest.exists() and dest.stat().st_size > 0:
        return  # 断点续跑：已有完整 blob 直接跳过
    expected = digest.split(":", 1)[1]
    tmp = dest.with_suffix(dest.suffix + ".part")
    tmp.parent.mkdir(parents=True, exist_ok=True)

    for round_ in range(1, MAX_TRIES + 1):
        try:
            pos = tmp.stat().st_size if tmp.exists() else 0
            blob_url = resolve_blob_url(client, mirror, repository, digest, token)
            hasher = hashlib.sha256()
            if pos:
                with open(tmp, "rb") as existing:
                    for piece in iter(lambda: existing.read(CHUNK), b""):
                        hasher.update(piece)
            with open(tmp, "ab" if pos else "wb") as fh:
                while True:
                    end = pos + CHUNK - 1
                    data = _fetch_range(client, blob_url, pos, end)
                    if not data:
                        break  # 流结束
                    fh.write(data)
                    hasher.update(data)
                    pos += len(data)
                    if len(data) < CHUNK:
                        break
            if hasher.hexdigest() != expected:
                tmp.unlink(missing_ok=True)
                raise RuntimeError(f"sha256 mismatch for {digest[:19]}")
            tmp.rename(dest)
            return
        except (httpx.HTTPError, RuntimeError) as exc:
            part_size = tmp.stat().st_size if tmp.exists() else 0
            wait = min(2 ** round_, 30)
            log(f"  blob {digest[:19]} round {round_}/{MAX_TRIES} failed: {exc} (have {part_size}B, sleep {wait}s)")
            time.sleep(wait)
    raise RuntimeError(f"blob download failed: {digest}")


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit in ("B", "KB") else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True, help="上游引用，如 docker.io/library/mongo:7.0")
    parser.add_argument("--mirror", default=DEFAULT_MIRROR, help="registry 源，默认 daocloud")
    parser.add_argument("--proxy", default=None, help="HTTP 代理，如 http://127.0.0.1:7897")
    parser.add_argument("--tag", default=None, help="docker load 后的镜像名，默认 <mirror 域名>/<image>")
    args = parser.parse_args()

    repository, _, reference = args.image.rpartition(":")
    mirror_host = urlparse(args.mirror).netloc
    repo_tag = args.tag or f"{mirror_host}/{args.image}"
    slug = repo_tag.replace("/", "_").replace(":", "_")
    work = OUT_DIR / slug
    work.mkdir(parents=True, exist_ok=True)
    tar_path = OUT_DIR / f"{slug}.tar"

    client = httpx.Client(follow_redirects=True, proxy=args.proxy) if args.proxy else httpx.Client(follow_redirects=True)

    with client:
        log(f"[{repo_tag}] token (mirror={args.mirror}, proxy={args.proxy or 'none'})...")
        token = get_token(client, args.mirror, repository)
        log(f"[{repo_tag}] manifest...")
        manifest = fetch_manifest(client, args.mirror, repository, reference, token)

        config_digest = manifest["config"]["digest"]
        layers = manifest["layers"]
        total = sum(layer["size"] for layer in layers)
        log(f"[{repo_tag}] config {config_digest[:19]}, {len(layers)} layers, {human(total)}")

        config_path = work / "config.json"
        download_blob(client, args.mirror, repository, config_digest, token, config_path)

        layer_files: list[str] = []
        for i, layer in enumerate(layers):
            dest = work / f"L{i:03d}.tar"
            if not dest.exists():
                download_blob(client, args.mirror, repository, layer["digest"], token, dest)
            else:
                log(f"  layer {i} cached")
            layer_files.append(dest.name)
            log(f"  layer {i + 1}/{len(layers)} ok ({human(layer['size'])})")

    docker_manifest = [{"Config": "config.json", "RepoTags": [repo_tag], "Layers": layer_files}]
    (work / "manifest.json").write_text(json.dumps(docker_manifest), encoding="utf-8")

    with tarfile.open(tar_path, "w") as tar:
        for name in ["manifest.json", "config.json", *layer_files]:
            tar.add(work / name, arcname=name)
    log(f"[{repo_tag}] archive ready: {tar_path} ({human(tar_path.stat().st_size)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
