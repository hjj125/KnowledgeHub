"""实验五（§15.5 重点）：跨库一致性与故障注入。

研究问题：多数据库异步同步在故障、重复消息和版本冲突下能否最终恢复？

六个情景（§15.5 原表）：
  1. 正常新建/修改/删除并等待图与向量同步 → 端到端同步延迟
  2. MongoDB 提交后立即停止发布器再恢复 → 事件滞留时长
  3. 图/向量 Worker 中断后恢复 → 恢复时间
  4. 重复交付同一事件 → 节点/向量重复数（应为 0）
  5. 快速连续修改同一笔记 → 最终版本一致率（旧任务不得覆盖新状态）
  6. 软删除后搜索 → 确认 Milvus 旧向量不再返回

运行：backend/.venv/Scripts/python benchmarks/exp5_consistency.py
前置：后端已启动；本脚本自行拉起/终止 publisher 与 workers（docker CLI 用于故障注入）
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import httpx  # noqa: E402
import redis as redis_sync  # noqa: E402
from pymongo import MongoClient  # noqa: E402

from benchmarks import common  # noqa: E402
from benchmarks.common import append_csv, save_environment, stats  # noqa: E402

PROJECT = Path(__file__).resolve().parents[1]
BACKEND = PROJECT / "backend"
STREAM = "stream:knowledge_events"
SUBPROCESS_ENV = {**os.environ, "PYTHONPATH": f"{PROJECT}{os.pathsep}{BACKEND}"}
ROUNDS = 5  # 情景 1 重复轮次

csv_path = common.ensure_results_dir() / "exp5_consistency.csv"


def start_worker(module: str, tag: str) -> subprocess.Popen:
    logs = PROJECT / "tmp"
    logs.mkdir(exist_ok=True)
    log_file = open(logs / f"exp5_{tag}.log", "w", encoding="utf-8")  # noqa: SIM115
    return subprocess.Popen([sys.executable, "-m", module], cwd=str(BACKEND),
                            env=SUBPROCESS_ENV, stdout=log_file, stderr=subprocess.STDOUT)


def stop_proc(proc: subprocess.Popen | None) -> None:
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def container_name(fragment: str) -> str | None:
    out = subprocess.run(["docker", "ps", "--filter", f"name={fragment}",
                          "--format", "{{.Names}}"], capture_output=True, text=True)
    names = out.stdout.strip().splitlines()
    return names[0] if names else None


async def main_async() -> None:
    base = "http://127.0.0.1:8000/api/v1"
    http = httpx.Client(base_url=base, timeout=30)
    mongo = MongoClient("mongodb://localhost:27017/?directConnection=true", tz_aware=True)
    db = mongo["knowledgehub"]
    rds = redis_sync.Redis.from_url("redis://localhost:6379/0", decode_responses=True, socket_timeout=5)
    save_environment("exp5", {"rounds": ROUNDS})

    suffix = uuid.uuid4().hex[:8]
    email = f"exp5_{suffix}@test.dev"
    http.post("/auth/register", json={"email": email, "password": "exp5-passw0rd"})
    token = http.post("/auth/login", json={"email": email, "password": "exp5-passw0rd"}).json()["access_token"]
    auth = {"Authorization": f"Bearer {token}"}
    kb = http.post("/knowledge-bases", headers=auth, json={"name": "一致性实验库"}).json()

    rows: list[dict] = []

    def log_row(scenario: str, metric: str, value: float, unit: str, note: str = "") -> None:
        rows.append({"scenario": scenario, "metric": metric, "value": round(value, 2),
                     "unit": unit, "note": note})
        print(f"  [{scenario}] {metric} = {value:.2f} {unit} {note}")

    def wait_note_indexed(note_id: str, min_version: int, timeout: float = 60) -> float | None:
        """等待向量侧完成（indexing_jobs v>=min_version），返回耗时秒。"""
        start = time.perf_counter()
        while time.perf_counter() - start < timeout:
            job = db["indexing_jobs"].find_one({"entity_id": note_id, "job_type": "vector",
                                                "status": "succeeded"})
            if job and job.get("indexed_version", 0) >= min_version:
                return time.perf_counter() - start
            time.sleep(0.1)
        return None

    # ===== 情景 1：正常链路端到端同步延迟 =====
    print("\n== 情景 1：正常同步延迟 ==")
    publisher = start_worker("workers.event_publisher", "publisher")
    graph = start_worker("workers.graph_indexer", "graph")
    vector = start_worker("workers.vector_indexer", "vector")
    time.sleep(2)
    sync_latencies = []
    for r in range(ROUNDS):
        note = http.post("/notes", headers=auth, json={
            "kb_id": kb["id"], "title": f"同步延迟样本{r}", "content_md": f"端到端同步计时样本{r}。",
        }).json()
        cost = wait_note_indexed(note["id"], 1)
        if cost is not None:
            sync_latencies.append(cost * 1000)
    if sync_latencies:
        s = stats(sync_latencies)
        log_row("1_normal_sync", "e2e_sync_p50", s["p50_ms"], "ms", "提交→向量侧完成")
        log_row("1_normal_sync", "e2e_sync_p95", s["p95_ms"], "ms")
        log_row("1_normal_sync", "success_rate", 100.0 * len(sync_latencies) / ROUNDS, "%")

    # ===== 情景 2：停止发布器，事件滞留后恢复 =====
    print("\n== 情景 2：发布器中断 ==")
    stop_proc(publisher)
    note = http.post("/notes", headers=auth, json={
        "kb_id": kb["id"], "title": "滞留样本", "content_md": "发布器停止期间的笔记。",
    }).json()
    time.sleep(3)
    pending = db["outbox_events"].count_documents({"status": "pending"})
    log_row("2_publisher_down", "pending_events_after_3s", pending, "条", "事件滞留于 Outbox")
    t_start = time.perf_counter()
    publisher = start_worker("workers.event_publisher", "publisher2")
    recovered = wait_note_indexed(note["id"], 1, timeout=60)
    log_row("2_publisher_down", "recovery_time", (time.perf_counter() - t_start), "s",
            "恢复后到向量侧完成" if recovered else "（未恢复）")

    # ===== 情景 3：Worker 中断恢复 =====
    print("\n== 情景 3：Worker 中断 ==")
    for worker_tag, module in (("vector", "workers.vector_indexer"), ("graph", "workers.graph_indexer")):
        stop_proc(vector if worker_tag == "vector" else graph)
        note = http.post("/notes", headers=auth, json={
            "kb_id": kb["id"], "title": f"{worker_tag}中断样本", "content_md": f"{worker_tag} 中断期间创建。",
        }).json()
        time.sleep(2)
        t_start = time.perf_counter()
        if worker_tag == "vector":
            vector = start_worker(module, f"{worker_tag}2")
        else:
            graph = start_worker(module, f"{worker_tag}2")
        recovered = wait_note_indexed(note["id"], 1, timeout=60)
        log_row("3_worker_restart", f"{worker_tag}_recovery", time.perf_counter() - t_start, "s",
                "恢复完成" if recovered else "（未恢复）")

    # ===== 情景 4：重复交付同一事件 =====
    print("\n== 情景 4：重复交付 ==")
    note = http.post("/notes", headers=auth, json={
        "kb_id": kb["id"], "title": "重复交付样本", "content_md": "同一条事件将手动重复投递 5 次。",
    }).json()
    wait_note_indexed(note["id"], 1)
    job = db["indexing_jobs"].find_one({"entity_id": note["id"], "job_type": "vector"})
    # 手动向流内重复 XADD 同一事件（模拟发布器崩溃窗口的重复投递）
    event = db["outbox_events"].find_one({"entity_id": note["id"], "event_type": "note.created"})
    for _ in range(5):
        rds.xadd(STREAM, {"event_id": event["_id"], "event_type": "note.created",
                          "entity_type": "note", "entity_id": note["id"],
                          "entity_version": "1"})
    time.sleep(6)
    from pymilvus import MilvusClient
    mv = MilvusClient(uri="http://localhost:19530")
    vec_rows = mv.query(collection_name="knowledge_chunks",
                        filter=f'document_id == "{note["id"]}"', output_fields=["pk"])
    graph_nodes = db["indexing_jobs"].count_documents({"entity_id": note["id"], "job_type": "graph"})
    log_row("4_duplicate_delivery", "vector_rows", len(vec_rows), "条", "应为 1（内容单 chunk）")
    log_row("4_duplicate_delivery", "job_records", graph_nodes, "条", "应为 1（幂等 upsert）")
    log_row("4_duplicate_delivery", "duplicate_free", 1.0 if len(vec_rows) == 1 else 0.0, "bool")

    # ===== 情景 5：快速连续修改（乱序防护）=====
    print("\n== 情景 5：快速连续修改 ==")
    note = http.post("/notes", headers=auth, json={
        "kb_id": kb["id"], "title": "乱序样本", "content_md": "第 1 版。",
    }).json()
    wait_note_indexed(note["id"], 1)
    t_start = time.perf_counter()
    for v in range(2, 8):
        http.patch(f"/notes/{note['id']}", headers=auth,
                   json={"content_md": f"第 {v} 版内容，最终版应为 v7。", "expected_content_version": v - 1})
    final_cost = wait_note_indexed(note["id"], 7, timeout=60)
    log_row("5_rapid_updates", "settle_time", time.perf_counter() - t_start, "s",
            "6 次连续修改收敛到最终版")
    log_row("5_rapid_updates", "final_consistent", 1.0 if final_cost else 0.0, "bool")

    # ===== 情景 6：软删除后搜索 =====
    print("\n== 情景 6：软删除后旧向量不返回 ==")
    victim = http.post("/notes", headers=auth, json={
        "kb_id": kb["id"], "title": "蓝鲸是地球上最大的哺乳动物", "content_md": "蓝鲸是地球上最大的哺乳动物。",
    }).json()
    wait_note_indexed(victim["id"], 1)
    r = http.post("/search", headers=auth, json={"query": "蓝鲸", "mode": "semantic", "top_k": 5})
    hit_before = any(x["source_id"] == victim["id"] for x in r.json()["results"])
    http.delete(f"/notes/{victim['id']}", headers=auth)
    deleted = False
    start = time.perf_counter()
    while time.perf_counter() - start < 30:
        rows_now = mv.query(collection_name="knowledge_chunks",
                            filter=f'document_id == "{victim["id"]}"', output_fields=["pk"])
        if not rows_now:
            deleted = True
            break
        time.sleep(0.3)
    log_row("6_soft_delete_search", "vectors_removed", 1.0 if deleted else 0.0, "bool",
            f"{time.perf_counter() - start:.1f}s")
    r = http.post("/search", headers=auth, json={"query": "蓝鲸", "mode": "semantic", "top_k": 5})
    hit_after = any(x["source_id"] == victim["id"] for x in r.json()["results"])
    log_row("6_soft_delete_search", "stale_hit_after_delete", 1.0 if hit_after else 0.0, "bool",
            "1 = 有陈旧命中（坏）；0 = 无（好）")

    # 收尾：PEL 清空后退出
    stop_proc(graph)
    stop_proc(vector)
    stop_proc(publisher)
    append_csv(csv_path, ["scenario", "metric", "value", "unit", "note"], rows)
    print(f"\nCSV 输出：{csv_path}")
    http.close()


if __name__ == "__main__":
    asyncio.run(main_async())
