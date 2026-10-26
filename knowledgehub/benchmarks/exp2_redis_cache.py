"""实验二（§15.2）：Redis 缓存收益。

研究问题：缓存在热点笔记访问中提供怎样的收益？
- 对照：全部读 MongoDB / Redis Cache-Aside（与应用 services/cache.py 相同的键规则与 TTL）
- 自变量：请求并发（1/4/16/64）、热点访问比例（均匀 / 80/20 Zipf 式热点）
- 指标：缓存命中率、吞吐量（req/s）、P50/P95/P99 延迟、缓存失效后恢复开销

运行：backend/.venv/Scripts/python benchmarks/exp2_redis_cache.py
前置：MongoDB/Redis healthy；实验数据写入 bench_notes（1000 篇，独立于业务数据）
"""

from __future__ import annotations

import asyncio
import random
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import redis.asyncio as aioredis  # noqa: E402
from pymongo import AsyncMongoClient, MongoClient  # noqa: E402

from benchmarks import common  # noqa: E402
from benchmarks.common import append_csv, line_chart, save_environment, stats  # noqa: E402
from benchmarks.data_gen import gen_notes  # noqa: E402

N_NOTES = 1000
SEED = 42
TTL_SECONDS = 300
CONCURRENCY_LEVELS = [1, 4, 16, 64]
REQUESTS_PER_RUN = 2000
HOT_FRACTION = 0.8  # 80% 请求落在 20% 热点笔记上

csv_path = common.ensure_results_dir() / "exp2_cache_latency.csv"
hit_csv = common.ensure_results_dir() / "exp2_cache_hitrate.csv"


class CacheAside:
    """与 app/services/cache.py 相同语义的最小实现（键规则一致，MongoDB 走异步驱动）。"""

    def __init__(self, rds: aioredis.Redis, mongo_coll, enabled: bool):
        self.redis = rds
        self.coll = mongo_coll
        self.enabled = enabled
        self.hits = 0
        self.misses = 0

    def _key(self, owner: str, note_id: str) -> str:
        return f"bench:note:{owner}:{note_id}"

    async def get_note(self, owner: str, note_id: str) -> dict | None:
        query = {"_id": note_id, "owner_id": owner, "deleted_at": None}
        if not self.enabled:
            return await self.coll.find_one(query)
        key = self._key(owner, note_id)
        cached = await self.redis.get(key)
        if cached:
            self.hits += 1
            return {**__import__("json").loads(cached), "_id": note_id}
        self.misses += 1
        doc = await self.coll.find_one(query)
        if doc is not None:
            view = {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in doc.items()}
            await self.redis.set(key, __import__("json").dumps(view, ensure_ascii=False), ex=TTL_SECONDS)
        return doc


def pick_note(rng: random.Random, note_ids: list[str], hot: bool) -> str:
    if hot:
        hot_ids = note_ids[: max(1, len(note_ids) // 5)]  # 前 20% 为热点
        return rng.choice(hot_ids)
    return rng.choice(note_ids)


async def run_load(cache: CacheAside, note_ids: list[str], concurrency: int,
                   zipf_hot: bool, requests: int) -> tuple[list[float], float]:
    rng = random.Random(SEED + concurrency + int(zipf_hot))
    latencies: list[float] = []
    counter = Counter()
    lock = asyncio.Lock()
    owner = "bench"

    async def worker(n_requests: int) -> None:
        nonlocal latencies
        local = []
        for _ in range(n_requests):
            if zipf_hot:
                note_id = pick_note(rng, note_ids, hot=rng.random() < HOT_FRACTION)
            else:
                note_id = pick_note(rng, note_ids, hot=False)
            start = time.perf_counter()
            doc = await cache.get_note(owner, note_id)
            local.append((time.perf_counter() - start) * 1000)
            if doc is not None:
                counter[note_id] += 1
        async with lock:
            latencies.extend(local)

    per_worker = requests // concurrency
    start = time.perf_counter()
    await asyncio.gather(*(worker(per_worker) for _ in range(concurrency)))
    wall = time.perf_counter() - start
    return latencies, requests / wall


async def main_async() -> None:
    seed_client = MongoClient("mongodb://localhost:27017/?directConnection=true", tz_aware=True)
    coll_sync = seed_client["knowledgehub"]["bench_cache_notes"]
    coll_sync.drop()
    notes = gen_notes(N_NOTES, seed=SEED, kb_id="bench-cache-kb")
    coll_sync.insert_many(notes)
    note_ids = [n["_id"] for n in notes]
    seed_client.close()

    async_client = AsyncMongoClient("mongodb://localhost:27017/?directConnection=true", tz_aware=True)
    coll = async_client["knowledgehub"]["bench_cache_notes"]

    rds = aioredis.from_url("redis://localhost:6379/0", decode_responses=True)
    save_environment("exp2", {"notes": N_NOTES, "concurrency": CONCURRENCY_LEVELS,
                              "requests_per_run": REQUESTS_PER_RUN, "hot_fraction": HOT_FRACTION,
                              "ttl_seconds": TTL_SECONDS})

    for mode_label, enabled in (("mongo_only", False), ("cache_aside", True)):
        for zipf_hot in (False, True):
            for concurrency in CONCURRENCY_LEVELS:
                await rds.flushdb()  # 冷启动
                cache = CacheAside(rds, coll, enabled)
                latencies, throughput = await run_load(cache, note_ids, concurrency, zipf_hot,
                                                       REQUESTS_PER_RUN)
                s = stats(latencies)
                hit_rate = cache.hits / max(1, cache.hits + cache.misses)
                append_csv(csv_path,
                           ["mode", "zipf_hot", "concurrency", "n", "p50_ms", "p95_ms", "p99_ms",
                            "mean_ms", "throughput_rps"],
                           [{"mode": mode_label, "zipf_hot": zipf_hot, "concurrency": concurrency,
                             **s, "throughput_rps": round(throughput, 1)}])
                append_csv(hit_csv, ["mode", "zipf_hot", "concurrency", "hits", "misses", "hit_rate"],
                           [{"mode": mode_label, "zipf_hot": zipf_hot, "concurrency": concurrency,
                             "hits": cache.hits, "misses": cache.misses,
                             "hit_rate": round(hit_rate, 4)}])
                print(f"{mode_label:12s} zipf={zipf_hot!s:5s} c={concurrency:3d} "
                      f"p50={s['p50_ms']:7.2f} p95={s['p95_ms']:7.2f} ms "
                      f"{throughput:8.0f} req/s hit={hit_rate:5.1%}")

    # 缓存失效后恢复开销：清空缓存后单次回填 vs 命中
    await rds.flushdb()
    cache = CacheAside(rds, coll, True)
    note_id = note_ids[0]
    t0 = time.perf_counter()
    await cache.get_note("bench", note_id)
    fill_ms = (time.perf_counter() - t0) * 1000
    t0 = time.perf_counter()
    await cache.get_note("bench", note_id)
    hit_ms = (time.perf_counter() - t0) * 1000
    print(f"miss(回填+读)={fill_ms:.2f} ms  hit={hit_ms:.2f} ms  放大 {fill_ms / max(hit_ms, 0.01):.1f}x")

    # 出图：P95 与吞吐（均匀 vs 热点）
    import csv as _csv

    for metric, ylabel in (("p95_ms", "P95 延迟 (ms)"), ("throughput_rps", "吞吐 (req/s)")):
        series: dict[str, list[tuple[float, float]]] = {}
        with open(csv_path, encoding="utf-8") as f:
            for row in _csv.DictReader(f):
                label = f"{row['mode']}/{row['zipf_hot']}"
                series.setdefault(label, []).append((int(row["concurrency"]), float(row[metric])))
        line_chart(common.ensure_results_dir() / f"exp2_{metric}.png", series,
                   f"Redis Cache-Aside 收益：{ylabel}", "并发数", ylabel, logy=(metric == "p95_ms"))

    await rds.aclose()
    await async_client.close()
    print(f"CSV/图输出：{common.ensure_results_dir()}")


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
