"""阶段 2 冒烟测试：Outbox → Redis Streams → 双 Worker 全链路 + 故障恢复。

前置：后端已启动（默认 http://127.0.0.1:8000）、MongoDB/Redis healthy。
本脚本自行拉起/终止 publisher 与两个 Worker 子进程。

场景：
  1. 正常链路：创建知识库+笔记 → outbox 发布 → 双消费组处理 → index_status=indexed
  2. 崩溃恢复：杀 Worker → 新事件被"幽灵消费者"读入 PEL（模拟读后崩溃）→
     重启 Worker → XAUTOCLAIM 认领闲置消息 → 处理完成
  3. 重复投递：手工重复 XADD 同一事件 → 幂等跳过，不产生重复副作用
  4. 乱序防护：快速连续修改（旧事件后到）→ 以 MongoDB 当前版本为准，最终一致

用法：backend/.venv/Scripts/python scripts/smoke_stage2.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
import redis as redis_sync
from pymongo import MongoClient

PROJECT = Path(__file__).resolve().parents[1]
BACKEND = PROJECT / "backend"
LOGS = PROJECT / "tmp"
STREAM = "stream:knowledge_events"
CLAIM_MIN_IDLE_MS = 10_000

# workers/ 在仓库根、app/ 在 backend/ 下，子进程需要同时可见两者
SUBPROCESS_ENV = {**os.environ, "PYTHONPATH": f"{PROJECT}{os.pathsep}{BACKEND}"}

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  √ {name}" + (f"  [{detail}]" if detail else ""))
    else:
        FAILED.append(name)
        print(f"  × {name}  [{detail}]")


def wait_until(desc: str, fn, timeout: float = 40.0, interval: float = 0.5) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if fn():
                return True
        except Exception:  # noqa: BLE001 — 轮询期间连接抖动属预期
            pass
        time.sleep(interval)
    print(f"    (timeout waiting: {desc})")
    return False


def start_worker(module: str) -> subprocess.Popen:
    LOGS.mkdir(exist_ok=True)
    log_path = LOGS / f"smoke2_{module.split('.')[-1]}.log"
    log_file = open(log_path, "w", encoding="utf-8")  # noqa: SIM115 — 进程生命周期与脚本一致
    return subprocess.Popen(
        [sys.executable, "-m", module],
        cwd=str(BACKEND),
        env=SUBPROCESS_ENV,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )


def stop_proc(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def main() -> int:
    base = "http://127.0.0.1:8000"
    api = f"{base}/api/v1"
    http = httpx.Client(timeout=15)
    mongo = MongoClient("mongodb://localhost:27017/?directConnection=true", tz_aware=True)
    db = mongo["knowledgehub"]
    rds = redis_sync.Redis.from_url("redis://localhost:6379/0", decode_responses=True, socket_timeout=5)

    print(f"== 阶段 2 冒烟测试 ({api}) ==")

    # --- 准备用户与数据 ---
    email = f"smoke2_{uuid.uuid4().hex[:8]}@test.dev"
    password = "smoke-passw0rd"
    http.post(f"{api}/auth/register", json={"email": email, "password": password})
    token = http.post(f"{api}/auth/login", json={"email": email, "password": password}).json()["access_token"]
    auth = {"Authorization": f"Bearer {token}"}
    kb_id = http.post(f"{api}/knowledge-bases", headers=auth, json={"name": "异步链路测试"}).json()["id"]
    note_a = http.post(
        f"{api}/notes",
        headers=auth,
        json={"kb_id": kb_id, "title": "笔记A", "content_md": "v1", "tags": []},
    ).json()
    print(f"  准备完成：note_a={note_a['id'][:8]}… v={note_a['content_version']}")

    # --- 1. 正常链路 ---
    publisher = start_worker("workers.event_publisher")
    graph = start_worker("workers.graph_indexer")
    vector = start_worker("workers.vector_indexer")
    try:
        ok = wait_until(
            "双 worker 处理 note A",
            lambda: (
                db.indexing_jobs.count_documents(
                    {"entity_id": note_a["id"], "job_type": {"$in": ["graph", "vector"]},
                     "status": "succeeded", "indexed_version": 1}
                )
                == 2
                and http.get(f"{api}/notes/{note_a['id']}", headers=auth).json()["index_status"] == "indexed"
            ),
        )
        check("正常链路：outbox 发布 + 双消费组处理 + index_status=indexed", ok)

        # --- 2. 杀 Worker → 幽灵消费者模拟"读后崩溃" → 重启恢复 ---
        stop_proc(graph)
        stop_proc(vector)
        time.sleep(1)

        r = http.patch(
            f"{api}/notes/{note_a['id']}",
            headers=auth,
            json={"content_md": "v2", "expected_content_version": 1},
        )
        check("Worker 停机期间修改笔记 → v2", r.status_code == 200 and r.json()["content_version"] == 2)
        note_b = http.post(
            f"{api}/notes",
            headers=auth,
            json={"kb_id": kb_id, "title": "笔记B", "content_md": "b1", "tags": []},
        ).json()
        check("Worker 停机期间新建笔记 B", note_b.get("content_version") == 1)

        ok = wait_until("publisher 已发布停机期间事件", lambda: db.outbox_events.count_documents(
            {"entity_id": {"$in": [note_a["id"], note_b["id"]]}, "status": "published"}) >= 3)
        check("publisher 独立存活并完成发布", ok)

        # 幽灵消费者把新事件读入 PEL 但永不 XACK（等价于 Worker 读后崩溃）
        for group in ("graph_indexer", "vector_indexer"):
            rds.xreadgroup(group, f"ghost.{group}", {STREAM: ">"}, count=10)
        pending = rds.xpending(STREAM, "graph_indexer")["pending"]
        check("幽灵消费者后 pending 积压 >= 2", pending >= 2, f"pending={pending}")

        time.sleep((CLAIM_MIN_IDLE_MS / 1000) + 2)  # 等待消息闲置超过认领阈值
        graph = start_worker("workers.graph_indexer")
        vector = start_worker("workers.vector_indexer")

        ok = wait_until(
            "恢复后 note A → v2 / note B → v1 全部被索引",
            lambda: (
                db.indexing_jobs.count_documents(
                    {"entity_id": note_a["id"], "job_type": "graph", "status": "succeeded", "indexed_version": 2}
                )
                == 1
                and db.indexing_jobs.count_documents(
                    {"entity_id": note_b["id"], "job_type": {"$in": ["graph", "vector"]},
                     "status": "succeeded", "indexed_version": 1}
                )
                == 2
            ),
        )
        check("XAUTOCLAIM 认领恢复：停机期间事件最终处理", ok)
        status_a = http.get(f"{api}/notes/{note_a['id']}", headers=auth).json()["index_status"]
        check("恢复后 note A index_status=indexed", status_a == "indexed", status_a)
        ok = wait_until(
            "恢复后 PEL 清空",
            lambda: rds.xpending(STREAM, "graph_indexer")["pending"] == 0
            and rds.xpending(STREAM, "vector_indexer")["pending"] == 0,
            timeout=20,
        )
        check("恢复后 PEL 清空（无未确认残留）", ok, f"pending={rds.xpending(STREAM, 'graph_indexer')['pending']}")

        # --- 3. 重复投递（模拟 publisher 在 XADD 后、标记前崩溃） ---
        dup_fields = {
            "event_id": "smoke-duplicate-event",
            "event_type": "note.created",
            "entity_type": "note",
            "entity_id": note_b["id"],
            "entity_version": "1",
        }
        job_before = db.indexing_jobs.find_one({"entity_id": note_b["id"], "job_type": "graph"})
        rds.xadd(STREAM, dup_fields)
        rds.xadd(STREAM, dup_fields)  # 重复两次
        ok = wait_until(
            "重复事件被消费",
            lambda: rds.xpending(STREAM, "graph_indexer")["pending"] == 0
            and rds.xpending(STREAM, "vector_indexer")["pending"] == 0,
        )
        job_after = db.indexing_jobs.find_one({"entity_id": note_b["id"], "job_type": "graph"})
        check(
            "重复投递幂等：job 无变化（重复事件被跳过）",
            ok and job_after["indexed_version"] == job_before["indexed_version"]
            and job_after["attempts"] == job_before["attempts"],
        )

        # --- 4. 乱序防护：快速连续修改 ---
        http.patch(f"{api}/notes/{note_b['id']}", headers=auth,
                   json={"content_md": "b2", "expected_content_version": 1})
        http.patch(f"{api}/notes/{note_b['id']}", headers=auth,
                   json={"content_md": "b3", "expected_content_version": 2})
        ok = wait_until(
            "note B 追赶到 v3",
            lambda: db.indexing_jobs.find_one(
                {"entity_id": note_b["id"], "job_type": "vector"})["indexed_version"] == 3,
        )
        job_b = db.indexing_jobs.find_one({"entity_id": note_b["id"], "job_type": "vector"})
        final_status = http.get(f"{api}/notes/{note_b['id']}", headers=auth).json()["index_status"]
        check("乱序防护：旧事件不回写，最终 indexed_version=3 且 index_status=indexed",
              ok and job_b["indexed_version"] == 3 and job_b["status"] == "succeeded" and final_status == "indexed",
              f"v={job_b['indexed_version']} status={job_b['status']}/{final_status}")

        # --- 收尾核对 ---
        ok = wait_until("outbox 全部发布",
                        lambda: db.outbox_events.count_documents({"status": "pending"}) == 0)
        check("outbox 无积压", ok)
    finally:
        stop_proc(graph)
        stop_proc(vector)
        stop_proc(publisher)

    print(f"\n结果：{len(PASSED)} 通过 / {len(FAILED)} 失败")
    if FAILED:
        print("失败项：", "、".join(FAILED))
        return 1
    print("阶段 2 冒烟测试全部通过 ✔")
    return 0


if __name__ == "__main__":
    sys.exit(main())
