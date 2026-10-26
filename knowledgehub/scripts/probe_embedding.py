"""阶段 0 Spike：硅基流动 Embedding API 探测脚本。

对应设计文档 §9.8.3 维度定稿流程第 1 步：
  1. 验证 API Key 有效；
  2. 实测 EMBEDDING_MODEL 原生返回维度；
  3. 验证 dimensions 参数（MRL 离散档位）对 VL 模型是否生效；
  4. 验证 VL 多模态请求格式（图片 base64 data URL）是否可用（best-effort）。

结果保存到 docs/experiments/embedding_probe_<时间戳>.json，作为维度定稿与实验记录依据。

用法：
    python scripts/probe_embedding.py
前置条件：项目根目录 .env 中已填写 SILICONFLOW_API_KEY。
"""

from __future__ import annotations

import base64
import json
import os
import struct
import sys
import time
import zlib
from datetime import datetime, timezone
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPERIMENT_DIR = PROJECT_ROOT / "docs" / "experiments"

# 硅基流动文档给出的 MRL 离散档位；重点验证建议档 1024 / 2048
DIMS_TO_TEST = [1024, 2048]
REQUEST_TIMEOUT_SECONDS = 60.0


def load_env() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(PROJECT_ROOT / ".env")
    except ImportError:
        pass  # 未安装 python-dotenv 时直接读系统环境变量


def env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def tiny_png_base64() -> str:
    """生成 1x1 RGB PNG 的 base64，不依赖 Pillow。"""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    idat = zlib.compress(b"\x00\xff\x00\x00")
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")
    return base64.b64encode(png).decode("ascii")


def call_embeddings(
    base_url: str,
    api_key: str,
    model: str,
    input_payload: list | str,
    dimensions: int | None = None,
) -> tuple[int, dict]:
    body: dict = {"model": model, "input": input_payload, "encoding_format": "float"}
    if dimensions is not None:
        body["dimensions"] = dimensions
    response = httpx.post(
        f"{base_url.rstrip('/')}/embeddings",
        headers={"Authorization": f"Bearer {api_key}"},
        json=body,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    try:
        payload = response.json()
    except json.JSONDecodeError:
        payload = {"raw": response.text[:500]}
    return response.status_code, payload


def analyze(payload: dict) -> dict:
    data = payload.get("data")
    if not isinstance(data, list) or not data:
        return {"ok": False, "error": "response has no data[]", "usage": payload.get("usage")}
    dims = [len(item.get("embedding", [])) for item in data]
    first = data[0].get("embedding", [])
    sample_norm = round(sum(v * v for v in first) ** 0.5, 6)
    return {
        "ok": True,
        "n_vectors": len(data),
        "dims": sorted(set(dims)),
        "dim_consistent": len(set(dims)) == 1,
        "sample_vector_head": [round(v, 6) for v in first[:4]],
        "sample_vector_norm": sample_norm,
        "usage": payload.get("usage"),
        "model": payload.get("model"),
    }


def record(name: str, started: float, http_status: int | None, payload: dict, error: str | None = None) -> dict:
    entry: dict = {
        "test": name,
        "status": "ok" if error is None else "error",
        "latency_ms": round((time.time() - started) * 1000),
        "http_status": http_status,
    }
    if error is None:
        entry.update(analyze(payload) if "data" in payload or "usage" in payload else {"raw": payload})
        if not entry.get("ok", True):
            entry["status"] = "error"
            entry["error"] = entry.get("error")
    else:
        entry["error"] = error
    return entry


def main() -> int:
    load_env()
    base_url = env("SILICONFLOW_API_BASE", "https://api.siliconflow.cn/v1")
    api_key = env("SILICONFLOW_API_KEY")
    model = env("EMBEDDING_MODEL", "Qwen/Qwen3-VL-Embedding-8B")

    if not api_key:
        print("× 未找到 SILICONFLOW_API_KEY")
        print(f"  请在 {PROJECT_ROOT / '.env'} 中填写硅基流动 API Key 后重新运行。")
        return 2

    results: list[dict] = []

    # --- 测试 A：文本批量（Classic 格式，多个短文本一次调用） ---
    started = time.time()
    try:
        http_status, payload = call_embeddings(
            base_url, api_key, model,
            ["什么是进程调度？", "死锁的四个必要条件是什么？"],
        )
        entry = record("A_text_batch", started, http_status, payload)
        if entry["status"] == "error" and http_status != 200:
            err = payload.get("message") or payload.get("error") or payload
            entry["error"] = f"HTTP {http_status}: {str(err)[:300]}"
    except Exception as exc:  # noqa: BLE001 — 探测脚本需要收集任何失败
        entry = record("A_text_batch", started, None, {}, error=repr(exc)[:300])
    results.append(entry)

    native_dim = None
    if entry["status"] == "ok":
        native_dim = entry.get("dims", [None])[0]

    # --- 测试 B：dimensions 参数（MRL 档位）是否生效 ---
    dim_results: dict[str, dict] = {}
    for dim in DIMS_TO_TEST:
        started = time.time()
        try:
            http_status, payload = call_embeddings(
                base_url, api_key, model, ["维度定稿测试"], dimensions=dim,
            )
            dim_results[str(dim)] = record(f"B_dimensions_{dim}", started, http_status, payload)
        except Exception as exc:  # noqa: BLE001
            dim_results[str(dim)] = record(f"B_dimensions_{dim}", started, None, {}, error=repr(exc)[:300])
        results.append(dim_results[str(dim)])

    dimensions_param_effective = all(
        r.get("status") == "ok" and int(r.get("dims", [0])[0] or 0) == int(k)
        for k, r in dim_results.items()
    )

    # --- 测试 C：VL 图片格式（best-effort，失败不影响其余结论） ---
    started = time.time()
    try:
        vl_input = [
            {"type": "image_url", "image_url": f"data:image/png;base64,{tiny_png_base64()}"},
            {"type": "text", "text": "测试图片"},
        ]
        http_status, payload = call_embeddings(base_url, api_key, model, vl_input)
        entry_c = record("C_vl_image", started, http_status, payload)
        if entry_c["status"] == "error" and http_status != 200:
            err = payload.get("message") or payload.get("error") or payload
            entry_c["error"] = f"HTTP {http_status}: {str(err)[:300]}（VL schema 可能需按官方 EmbeddingsVLRequest 校准）"
    except Exception as exc:  # noqa: BLE001
        entry_c = record("C_vl_image", started, None, {}, error=repr(exc)[:300])
    results.append(entry_c)

    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "purpose": "阶段0 Embedding API 探测（§9.8.3 维度定稿第 1 步）",
        "base_url": base_url,
        "model": model,
        "python": sys.version.split()[0],
        "native_dim": native_dim,
        "dimensions_param_effective": dimensions_param_effective,
        "results": results,
    }

    EXPERIMENT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = EXPERIMENT_DIR / f"embedding_probe_{stamp}.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    # --- 人类可读摘要 ---
    print(f"\n=== Embedding API 探测报告（model: {model}） ===")
    for r in results:
        flag = "√" if r["status"] == "ok" else "×"
        line = f"{flag} {r['test']:<20} {r['latency_ms']:>6}ms"
        if r["status"] == "ok":
            line += f"  n={r.get('n_vectors')}  dim={r.get('dims')}"
        else:
            line += f"  ERROR: {r.get('error', '')}"
        print(line)
    print(f"\n原生维度 native_dim = {native_dim}")
    print(f"dimensions 参数生效 = {dimensions_param_effective}")
    print(f"报告已保存: {out_path}")
    print("\n下一步（§9.8.3）：结合 Milvus 内存预算从 1024/2048 档定稿，")
    print("写入 .env 的 EMBEDDING_DIM 并冻结；变更需全量重建 Collection。")
    return 0 if results[0]["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
