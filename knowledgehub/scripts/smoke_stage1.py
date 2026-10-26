"""阶段 1 冒烟测试：对运行中的后端执行完整业务链路。

前置：后端已启动（默认 http://127.0.0.1:8000），MongoDB/Redis 容器 healthy。
覆盖：注册/登录/错误密码 → 知识库 CRUD → 笔记 CRUD + 乐观锁 409 →
     版本历史 → 缓存 MISS/HIT + TTL → 标签筛选 → 未授权访问 →
     知识库事务级联软删除。

用法：backend/.venv/Scripts/python scripts/smoke_stage1.py [--base http://127.0.0.1:8000]
"""

from __future__ import annotations

import argparse
import sys
import time
import uuid

import httpx

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  √ {name}" + (f"  [{detail}]" if detail else ""))
    else:
        FAILED.append(name)
        print(f"  × {name}  [{detail}]")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    base = args.base.rstrip("/")
    api = f"{base}/api/v1"

    client = httpx.Client(timeout=15)
    email = f"smoke_{uuid.uuid4().hex[:8]}@test.dev"
    password = "smoke-passw0rd"
    token = ""

    print(f"== 阶段 1 冒烟测试 ({api}) ==")

    # --- 认证 ---
    r = client.post(f"{api}/auth/register", json={"email": email, "password": password})
    check("注册 201", r.status_code == 201, f"got {r.status_code}")
    user_id = r.json().get("id", "")
    r = client.post(f"{api}/auth/register", json={"email": email, "password": password})
    check("重复注册 409", r.status_code == 409, f"got {r.status_code}")
    r = client.post(f"{api}/auth/login", json={"email": email, "password": "wrong-password"})
    check("错误密码 401", r.status_code == 401, f"got {r.status_code}")
    r = client.post(f"{api}/auth/login", json={"email": email, "password": password})
    token = r.json().get("access_token", "")
    check("登录 200 + token", r.status_code == 200 and len(token) > 20)
    auth = {"Authorization": f"Bearer {token}"}

    # --- 知识库 ---
    r = client.post(f"{api}/knowledge-bases", headers=auth, json={"name": "操作系统", "category": "课程"})
    check("创建知识库 201", r.status_code == 201, f"got {r.status_code}")
    kb_id = r.json().get("id", "")
    r = client.get(f"{api}/knowledge-bases/{kb_id}", headers=auth)
    check("知识库详情 note_count=0", r.json().get("note_count") == 0)

    # --- 笔记 CRUD ---
    r = client.post(
        f"{api}/notes",
        headers=auth,
        json={"kb_id": kb_id, "title": "进程调度", "content_md": "# 进程调度\n核心是 CPU 分时复用。", "tags": ["os", "期末复习"]},
    )
    check("创建笔记 201 v=1", r.status_code == 201 and r.json().get("content_version") == 1, f"got {r.status_code}")
    note_id = r.json().get("id", "")

    r = client.get(f"{api}/notes/{note_id}", headers=auth)
    miss = r.headers.get("x-cache") == "MISS"
    r = client.get(f"{api}/notes/{note_id}", headers=auth)
    hit = r.headers.get("x-cache") == "HIT"
    check("Cache-Aside MISS→HIT", miss and hit)

    redis_ok = False
    try:
        import redis as redis_sync

        rc = redis_sync.Redis.from_url("redis://localhost:6379/0", socket_timeout=3)
        ttl = rc.ttl(f"cache:note:{user_id}:{note_id}")
        redis_ok = 0 < ttl <= 300
        check("缓存键 TTL 生效", redis_ok, f"ttl={ttl}")
    except Exception as exc:  # noqa: BLE001 — 冒烟脚本容忍本机 redis 检查失败
        check("缓存键 TTL 生效", False, repr(exc))

    r = client.patch(
        f"{api}/notes/{note_id}",
        headers=auth,
        json={"title": "进程调度（修订）", "expected_content_version": 99},
    )
    conflict_ok = r.status_code == 409 and r.json()["detail"]["reason"] == "content_version_conflict"
    check("乐观锁冲突 409", conflict_ok, f"got {r.status_code}")

    r = client.patch(
        f"{api}/notes/{note_id}",
        headers=auth,
        json={"title": "进程调度（修订）", "expected_content_version": 1},
    )
    check("正确版本更新 → v=2", r.status_code == 200 and r.json().get("content_version") == 2, f"got {r.status_code}")

    r = client.get(f"{api}/notes/{note_id}/versions", headers=auth)
    versions = [v["version"] for v in r.json()]
    check("版本历史 [2,1]", r.status_code == 200 and versions == [2, 1], str(versions))

    r = client.get(f"{api}/notes", headers=auth, params={"tag": "os"})
    check("标签筛选 total=1", r.json().get("total") == 1)
    r = client.get(f"{api}/notes", headers=auth, params={"q": "修订"})
    check("标题搜索 total=1", r.json().get("total") == 1)

    r = client.get(f"{api}/notes/{note_id}")
    check("未授权 401", r.status_code == 401, f"got {r.status_code}")
    r = client.get(f"{api}/notes", headers={"Authorization": "Bearer fake.token.here"})
    check("伪造 token 401", r.status_code == 401, f"got {r.status_code}")

    r = client.get(f"{api}/knowledge-bases/{kb_id}", headers=auth)
    check("知识库 note_count=1", r.json().get("note_count") == 1)

    # --- 登出后 token 失效（Redis 黑名单） ---
    r = client.post(f"{api}/auth/logout", headers=auth)
    check("登出 204", r.status_code == 204, f"got {r.status_code}")
    r = client.get(f"{api}/notes", headers=auth)
    check("登出后 token 401", r.status_code == 401, f"got {r.status_code}")

    # 重新登录继续
    r = client.post(f"{api}/auth/login", json={"email": email, "password": password})
    auth = {"Authorization": f"Bearer {r.json()['access_token']}"}

    # --- 知识库事务级联软删除 ---
    r = client.delete(f"{api}/knowledge-bases/{kb_id}", headers=auth)
    check("删除知识库 204", r.status_code == 204, f"got {r.status_code}")
    r = client.get(f"{api}/knowledge-bases/{kb_id}", headers=auth)
    check("知识库 404", r.status_code == 404, f"got {r.status_code}")
    r = client.get(f"{api}/notes/{note_id}", headers=auth)
    check("级联后笔记 404", r.status_code == 404, f"got {r.status_code}")
    r = client.get(f"{api}/notes/{note_id}/versions", headers=auth)
    check("级联后版本记录仍归属笔记", r.status_code == 200 and len(r.json()) == 2, f"got {r.status_code}")

    print(f"\n结果：{len(PASSED)} 通过 / {len(FAILED)} 失败")
    if FAILED:
        print("失败项：", "、".join(FAILED))
        return 1
    print("阶段 1 冒烟测试全部通过 ✔")
    return 0


if __name__ == "__main__":
    sys.exit(main())
