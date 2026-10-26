"""实验一（§15.1）：MongoDB 索引优化。

研究问题：不同索引对知识库查询的影响如何？
- 数据规模：1 万 / 5 万 / 10 万笔记（bench_notes 独立集合，与业务数据隔离）
- 查询：A 按用户+知识库时间倒序分页（列表页）；B 标签筛选；C 时间范围过滤
- 对照：无业务索引 / 单字段 / 复合索引（应用索引）/ 顺序不当的复合索引
- 指标：P50/P95 延迟、totalDocsExamined、totalKeysExamined、索引构建后的写入开销

运行：backend/.venv/Scripts/python benchmarks/exp1_mongo_index.py
输出：benchmarks/results/exp1_*.csv、exp1_*.png、exp1_environment.json
"""

from __future__ import annotations

import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from pymongo import ASCENDING, DESCENDING, MongoClient  # noqa: E402

from benchmarks import common  # noqa: E402
from benchmarks.common import append_csv, line_chart, save_environment, stats  # noqa: E402
from benchmarks.data_gen import gen_notes  # noqa: E402

SCALES = [10_000, 50_000, 100_000]
ROUNDS = 30  # 每种查询每配置测 30 次取分位
SEED = 42
TAG = "期末复习"

INDEX_CONFIGS = {
    "no_index": [],
    "single_field": [[("kb_id", ASCENDING)]],
    "composite_app": [[("owner_id", ASCENDING), ("kb_id", ASCENDING), ("deleted_at", ASCENDING),
                       ("updated_at", DESCENDING)]],
    # 顺序不当：等值字段排在排序字段之后，无法同时服务等值过滤与倒序排序
    "composite_bad_order": [[("updated_at", DESCENDING), ("kb_id", ASCENDING), ("owner_id", ASCENDING)]],
}


def main() -> None:
    client = MongoClient("mongodb://localhost:27017/?directConnection=true", tz_aware=True)
    db = client["knowledgehub"]
    coll = db["bench_notes"]
    results_dir = common.ensure_results_dir()
    csv_path = results_dir / "exp1_index_latency.csv"
    plan_csv = results_dir / "exp1_execution_plans.csv"
    insert_csv = results_dir / "exp1_insert_overhead.csv"
    save_environment("exp1", {"mongo_collection": "bench_notes", "scales": SCALES, "rounds": ROUNDS})

    for scale in SCALES:
        print(f"\n=== 规模 {scale} ===")
        coll.drop()
        notes = gen_notes(scale, seed=SEED)
        for i in range(0, len(notes), 10_000):
            coll.insert_many(notes[i : i + 10_000])
        print(f"  inserted {scale} docs")

        rng = random.Random(SEED)
        kbs = sorted({n["kb_id"] for n in notes})
        owners = sorted({n["owner_id"] for n in notes})
        timestamps = sorted(n["created_at"] for n in notes)

        for config_name, indexes in INDEX_CONFIGS.items():
            # 索引切换：drop 所有 _id 之外的索引后重建
            for idx in coll.list_indexes():
                if idx["name"] != "_id_":
                    coll.drop_index(idx["name"])
            for keys in indexes:
                coll.create_index(keys)

            for query_type in ("list_by_kb", "tag_filter", "time_range"):
                latencies: list[float] = []
                for r in range(ROUNDS):
                    if query_type == "list_by_kb":
                        q = {"owner_id": owners[0], "kb_id": kbs[rng.randrange(len(kbs))],
                             "deleted_at": None}
                        sort, limit = [("updated_at", DESCENDING)], 20
                    elif query_type == "tag_filter":
                        q = {"owner_id": owners[0], "kb_id": kbs[rng.randrange(len(kbs))],
                             "tags": TAG}
                        sort, limit = [("updated_at", DESCENDING)], 20
                    else:
                        lo, hi = timestamps[rng.randrange(len(timestamps) - 1)], None
                        t0, t1 = timestamps[0], timestamps[len(timestamps) // 2]
                        q = {"owner_id": owners[0], "kb_id": kbs[rng.randrange(len(kbs))],
                             "created_at": {"$gte": t0, "$lte": t1}}
                        sort, limit = [("created_at", DESCENDING)], 20

                    start = time.perf_counter()
                    cursor = coll.find(q)
                    if sort:
                        cursor = cursor.sort(sort)
                    if limit:
                        cursor = cursor.limit(limit)
                    cursor.to_list(length=limit or 100)
                    latencies.append((time.perf_counter() - start) * 1000)

                    if r == 0:  # 执行计划只采样一次
                        cmd = {"find": coll.name, "filter": q, "sort": dict(sort) or None,
                               "limit": limit}
                        plan = db.command("explain", cmd, verbosity="executionStats")
                        stats_doc = plan["executionStats"]
                        append_csv(plan_csv,
                                   ["scale", "index_config", "query_type", "total_keys_examined",
                                    "total_docs_examined", "n_returned", "execution_time_ms"],
                                   [{"scale": scale, "index_config": config_name,
                                     "query_type": query_type,
                                     "total_keys_examined": stats_doc.get("totalKeysExamined"),
                                     "total_docs_examined": stats_doc.get("totalDocsExamined"),
                                     "n_returned": stats_doc.get("nReturned"),
                                     "execution_time_ms": round(stats_doc.get("executionTimeMillis", 0), 3)}])

                s = stats(latencies)
                append_csv(csv_path,
                           ["scale", "index_config", "query_type", "n", "p50_ms", "p95_ms",
                            "p99_ms", "mean_ms", "stdev_ms"],
                           [{"scale": scale, "index_config": config_name, "query_type": query_type, **s}])
                print(f"  {config_name:22s} {query_type:12s} p50={s['p50_ms']:8.2f} p95={s['p95_ms']:8.2f} ms")

            # 写入开销：本索引配置下再插入 1000 篇的耗时
            extra = gen_notes(1000, seed=SEED + scale)
            for n_doc in extra:
                n_doc["_id"] = n_doc["_id"] + "x"
            start = time.perf_counter()
            for i in range(0, len(extra), 500):
                coll.insert_many(extra[i : i + 500])
            insert_ms = (time.perf_counter() - start) * 1000
            append_csv(insert_csv, ["scale", "index_config", "docs", "insert_1000_ms"],
                       [{"scale": scale, "index_config": config_name, "docs": 1000,
                         "insert_1000_ms": round(insert_ms, 2)}])
            print(f"  {config_name:22s} insert 1000: {insert_ms:8.1f} ms")

        # 出图：list_by_kb 的 P95 随配置对比（三规模汇总后再画）
    for query_type in ("list_by_kb", "tag_filter", "time_range"):
        import csv as _csv

        series: dict[str, list[tuple[float, float]]] = {}
        with open(csv_path, encoding="utf-8") as f:
            for row in _csv.DictReader(f):
                if row["query_type"] == query_type:
                    series.setdefault(row["index_config"], []).append(
                        (int(row["scale"]), float(row["p95_ms"]))
                    )
        series = {k: sorted(v) for k, v in series.items()}
        line_chart(results_dir / f"exp1_p95_{query_type}.png", series,
                   f"MongoDB 索引对比：{query_type} P95 延迟（越低越好）",
                   "笔记规模", "P95 延迟 (ms)", logy=True)

    print(f"\nCSV/图输出：{results_dir}")


if __name__ == "__main__":
    main()
