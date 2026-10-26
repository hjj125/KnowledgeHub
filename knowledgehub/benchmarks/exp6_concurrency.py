"""实验六（§15.6 可选强化）：系统整体并发测试。

- 混合场景：浏览笔记详情 60% / 更新笔记 15% / 关键词搜索 15% / 列表+新建 10%
  （搜索用 metadata 模式隔离 Embedding API 的外网延迟；上传/语义检索的延迟特性
  已在实验四/阶段 5 记录，不混入本实验）
- 负载：并发 1/4/16/32/64 递增，每级 600 请求
- 指标：吞吐、P50/P95/P99 延迟、错误率、实验后 Outbox/Streams 积压

运行：backend/.venv/Scripts/python benchmarks/exp6_concurrency.py
前置：后端已启动；脚本自行拉起 publisher/workers 消化异步事件
"""

from __future__ import annotations

import asyncio
import os
import statistics
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import httpx  # noqa: E402
from pymongo import MongoClient  # noqa: E402

from benchmarks import common  # noqa: E402
from benchmarks.common import append_csv, line_chart, save_environment  # noqa: E402

PROJECT = Path(__file__).resolve().parents[1]
BACKEND = PROJECT / "backend"
SUBPROCESS_ENV = {**os.environ, "PYTHONPATH": f"{PROJECT}{os.pathsep}{BACKEND}"}
CONCURRENCY_LEVELS = [1, 4, 16, 32, 64]
REQUESTS_PER_LEVEL = 600
csv_path = common.ensure_results_dir() / "exp6_concurrency.csv"


def start_worker(module: str, tag: str) -> subprocess.Popen:
    logs = PROJECT / "tmp"
    logs.mkdir(exist_ok=True)
    log_file = open(logs / f"exp6_{tag}.log", "w", encoding="utf-8")  # noqa: SIM115
    return subprocess.Popen([sys.executable, "-m", module], cwd=str(BACKEND),
                            env=SUBPROCESS_ENV, stdout=log_file, stderr=subprocess.STDOUT)


def stop_proc(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


async def main_async() -> None:
    base = "http://127.0.0.1:8000/api/v1"
    mongo = MongoClient("mongodb://localhost:27017/?directConnection=true", tz_aware=True)
    db = mongo["knowledgehub"]

    suffix = uuid.uuid4().hex[:8]
    async with httpx.AsyncClient(base_url=base, timeout=30) as setup:
        email = f"exp6_{suffix}@test.dev"
        await setup.post("/auth/register", json={"email": email, "password": "exp6-passw0rd"})
        token = (await setup.post("/auth/login", json={"email": email, "password": "exp6-passw0rd"})).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        kb = (await setup.post("/knowledge-bases", headers=headers,
                               json={"name": "压测知识库"})).json()
        note_ids = []
        for i in range(20):
            r = await setup.post("/notes", headers=headers,
                                 json={"kb_id": kb["id"], "title": f"压测笔记{i}",
                                       "content_md": f"压测语料第 {i} 篇，用于并发读与更新负载。"})
            note_ids.append(r.json()["id"])

    save_environment("exp6", {"concurrency_levels": CONCURRENCY_LEVELS,
                              "requests_per_level": REQUESTS_PER_LEVEL,
                              "mix": "read60/update15/search_metadata15/list+create10"})

    publisher = start_worker("workers.event_publisher", "publisher")
    graph = start_worker("workers.graph_indexer", "graph")
    vector = start_worker("workers.vector_indexer", "vector")
    time.sleep(2)

    rng_seed = 42
    results = []

    async def run_level(concurrency: int) -> None:
        nonlocal rng_seed
        rng_seed += 1
        import random

        rng = random.Random(rng_seed)
        latencies: list[float] = []
        errors = 0
        lock = asyncio.Lock()
        limit = asyncio.Semaphore(concurrency)

        async def one(client: httpx.AsyncClient, seq: int) -> None:
            nonlocal errors
            async with limit:
                roll = rng.random()
                try:
                    if roll < 0.60:  # 浏览笔记详情
                        note_id = note_ids[rng.randrange(len(note_ids))]
                        start = time.perf_counter()
                        r = await client.get(f"/notes/{note_id}", headers=headers)
                    elif roll < 0.75:  # 更新笔记（乐观锁需要先读版本——简化为创建后版本 1 失败可接受？
                        note_id = note_ids[rng.randrange(len(note_ids))]
                        detail = (await client.get(f"/notes/{note_id}", headers=headers)).json()
                        start = time.perf_counter()
                        r = await client.patch(f"/notes/{note_id}", headers=headers,
                                               json={"content_md": detail["content_md"] + f"\n压测更新 {seq}。",
                                                     "expected_content_version": detail["content_version"]})
                    elif roll < 0.90:  # 关键词搜索（metadata 模式）
                        start = time.perf_counter()
                        r = await client.post("/search", headers=headers,
                                              json={"query": "压测", "mode": "metadata", "top_k": 5})
                    else:  # 列表
                        start = time.perf_counter()
                        r = await client.get("/notes", headers=headers,
                                             params={"kb_id": kb["id"], "page_size": 5})
                    lat = (time.perf_counter() - start) * 1000
                    ok = r.status_code < 400
                except Exception:  # noqa: BLE001
                    lat = (time.perf_counter() - start) * 1000 if 'start' in dir() else 9999
                    ok = False
                async with lock:
                    latencies.append(lat)
                    if not ok:
                        errors += 1

        async with httpx.AsyncClient(base_url=base, timeout=30) as client:
            start = time.perf_counter()
            await asyncio.gather(*(one(client, i) for i in range(REQUESTS_PER_LEVEL)))
            wall = time.perf_counter() - start

        row = {
            "concurrency": concurrency,
            "n": len(latencies),
            "p50_ms": round(common.percentile(latencies, 50), 2),
            "p95_ms": round(common.percentile(latencies, 95), 2),
            "p99_ms": round(common.percentile(latencies, 99), 2),
            "mean_ms": round(statistics.mean(latencies), 2),
            "throughput_rps": round(len(latencies) / wall, 1),
            "error_rate": round(errors / len(latencies), 4),
        }
        results.append(row)
        print(f"  c={concurrency:3d} p50={row['p50_ms']:8.2f} p95={row['p95_ms']:8.2f} "
              f"p99={row['p99_ms']:8.2f} ms  {row['throughput_rps']:8.1f} req/s  err={row['error_rate']:.2%}")

    for level in CONCURRENCY_LEVELS:
        await run_level(level)

    append_csv(csv_path, ["concurrency", "n", "p50_ms", "p95_ms", "p99_ms", "mean_ms",
                          "throughput_rps", "error_rate"], results)

    # 异步链路积压（压测产生的更新事件应已被 worker 消化）
    time.sleep(5)
    backlog = db["outbox_events"].count_documents({"status": "pending"})
    print(f"  outbox 积压 after 5s: {backlog}")

    stop_proc(vector)
    stop_proc(graph)
    stop_proc(publisher)

    series = {
        "P50": [(r["concurrency"], r["p50_ms"]) for r in results],
        "P95": [(r["concurrency"], r["p95_ms"]) for r in results],
        "P99": [(r["concurrency"], r["p99_ms"]) for r in results],
    }
    line_chart(common.ensure_results_dir() / "exp6_latency.png", series,
               "混合负载下延迟随并发变化", "并发数", "延迟 (ms)", logy=True)
    line_chart(common.ensure_results_dir() / "exp6_throughput.png",
               {"吞吐": [(r["concurrency"], r["throughput_rps"]) for r in results]},
               "混合负载吞吐随并发变化", "并发数", "吞吐 (req/s)")
    print(f"\nCSV/图输出：{common.ensure_results_dir()}")


if __name__ == "__main__":
    asyncio.run(main_async())
