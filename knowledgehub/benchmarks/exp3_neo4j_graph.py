"""实验三（§15.3）：Neo4j 图查询。

研究问题：图规模和遍历深度如何影响关系检索？与 MongoDB 引用存储的等价查询对比。
- 图规模：1,000 / 10,000 / 50,000 节点，平均约 5—6 条关系/节点（BenchNote 独立 label）
- 查询：一跳邻居 / 两跳邻居 / 最短路径（6 跳内）
- 对照：MongoDB 将相同关系存为文档 linked_note_ids 数组，一跳=单读、两跳=两轮查询
- 指标：查询 P50/P95 延迟；讨论不预设 Neo4j 全场景更快

运行：backend/.venv/Scripts/python benchmarks/exp3_neo4j_graph.py
"""

from __future__ import annotations

import random
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from neo4j import GraphDatabase  # noqa: E402
from pymongo import MongoClient  # noqa: E402

from benchmarks import common  # noqa: E402
from benchmarks.common import append_csv, line_chart, save_environment  # noqa: E402
from benchmarks.data_gen import gen_graph_edges, gen_notes  # noqa: E402

SCALES = [1_000, 10_000, 50_000]
ROUNDS = 30
SEED = 42

csv_path = common.ensure_results_dir() / "exp3_graph_latency.csv"


def build_graph(driver, mongo_db, scale: int) -> tuple[list[str], list[tuple[str, str]]]:
    notes = gen_notes(scale, seed=SEED)
    edges = gen_graph_edges(notes, seed=SEED)
    ids = [n["_id"] for n in notes]

    # MongoDB 对照数据：linked_note_ids 数组 + 邻接表集合
    mongo_db["bench_notes"].drop()
    mongo_db["bench_edges"].drop()
    for i in range(0, len(notes), 10_000):
        mongo_db["bench_notes"].insert_many(notes[i : i + 10_000])
    adj: dict[str, list[str]] = {}
    for src, dst in edges:
        adj.setdefault(src, []).append(dst)
    mongo_db["bench_edges"].insert_many(
        ({"_id": src, "targets": targets} for src, targets in adj.items())
    )

    # Neo4j：批量 UNWIND 创建节点与边
    with driver.session() as session:
        session.run("MATCH (n:BenchNote) DETACH DELETE n")
        session.run(
            "CREATE CONSTRAINT bench_note_id IF NOT EXISTS FOR (n:BenchNote) REQUIRE n.note_id IS UNIQUE"
        )
        t0 = time.perf_counter()
        for i in range(0, len(ids), 10_000):
            session.run(
                "UNWIND $rows AS row MERGE (n:BenchNote {note_id: row.id})",
                rows=[{"id": x} for x in ids[i : i + 10_000]],
            )
        node_ms = (time.perf_counter() - t0) * 1000
        t0 = time.perf_counter()
        for i in range(0, len(edges), 10_000):
            session.run(
                "UNWIND $pairs AS pair "
                "MATCH (a:BenchNote {note_id: pair.s}), (b:BenchNote {note_id: pair.d}) "
                "MERGE (a)-[:LINKS_TO]->(b)",
                pairs=[{"s": s, "d": d} for s, d in edges[i : i + 10_000]],
            )
        edge_ms = (time.perf_counter() - t0) * 1000
    print(f"  built: {len(ids)} nodes ({node_ms:.0f} ms), {len(edges)} edges ({edge_ms:.0f} ms)")
    return ids, edges


def measure(fn, rounds: int) -> list[float]:
    out = []
    for _ in range(rounds):
        start = time.perf_counter()
        fn()
        out.append((time.perf_counter() - start) * 1000)
    return out


def main() -> None:
    driver = GraphDatabase.driver("bolt://localhost:7687", auth=("neo4j", "knowledgehub123"))
    mongo = MongoClient("mongodb://localhost:27017/?directConnection=true", tz_aware=True)
    mongo_db = mongo["knowledgehub"]
    save_environment("exp3", {"scales": SCALES, "rounds": ROUNDS, "degree": "3-8/node"})
    rng = random.Random(SEED)

    for scale in SCALES:
        print(f"\n=== 规模 {scale} 节点 ===")
        ids, edges = build_graph(driver, mongo_db, scale)
        sample_src = [s for s, _ in edges[:100]]
        sample_pairs = edges[:100]

        with driver.session() as session:
            # 一跳
            lat = measure(lambda: session.run(
                "MATCH (n:BenchNote {note_id:$id})-[:LINKS_TO]->(m) RETURN m.note_id",
                id=rng.choice(sample_src)).data(), ROUNDS)
            append_csv(csv_path, ["scale", "store", "query", "n", "p50_ms", "p95_ms", "mean_ms"],
                       [{"scale": scale, "store": "neo4j", "query": "one_hop", **_s(lat)}])
            one_hop_neo = lat

            # 两跳
            lat = measure(lambda: session.run(
                "MATCH (n:BenchNote {note_id:$id})-[:LINKS_TO]->()-[:LINKS_TO]->(m) "
                "RETURN DISTINCT m.note_id LIMIT 200",
                id=rng.choice(sample_src)).data(), ROUNDS)
            append_csv(csv_path, ["scale", "store", "query", "n", "p50_ms", "p95_ms", "mean_ms"],
                       [{"scale": scale, "store": "neo4j", "query": "two_hop", **_s(lat)}])

            # 最短路径（6 跳内，取已知存在边相连的节点对）
            lat = measure(lambda: session.run(
                "MATCH (a:BenchNote {note_id:$s}), (b:BenchNote {note_id:$d}) "
                "MATCH p = shortestPath((a)-[:LINKS_TO*..6]-(b)) RETURN length(p) AS hops",
                s=rng.choice(sample_pairs)[0], d=rng.choice(sample_pairs)[1]).data(), ROUNDS)
            append_csv(csv_path, ["scale", "store", "query", "n", "p50_ms", "p95_ms", "mean_ms"],
                       [{"scale": scale, "store": "neo4j", "query": "shortest_path", **_s(lat)}])

        # MongoDB 对照
        notes_coll = mongo_db["bench_notes"]
        edges_coll = mongo_db["bench_edges"]

        def mongo_one_hop() -> None:
            notes_coll.find_one({"_id": rng.choice(sample_src)}, {"linked_note_ids": 1})

        lat = measure(mongo_one_hop, ROUNDS)
        append_csv(csv_path, ["scale", "store", "query", "n", "p50_ms", "p95_ms", "mean_ms"],
                   [{"scale": scale, "store": "mongodb", "query": "one_hop", **_s(lat)}])
        one_hop_mongo = lat

        def mongo_two_hop() -> None:
            first = edges_coll.find_one({"_id": rng.choice(sample_src)}, {"targets": 1})
            if first and first.get("targets"):
                nxt = first["targets"][:50]
                rows = list(edges_coll.find({"_id": {"$in": nxt}}, {"targets": 1}))
                _ = {t for r in rows for t in r.get("targets", [])[:20]}

        lat = measure(mongo_two_hop, ROUNDS)
        append_csv(csv_path, ["scale", "store", "query", "n", "p50_ms", "p95_ms", "mean_ms"],
                   [{"scale": scale, "store": "mongodb", "query": "two_hop", **_s(lat)}])

        print(f"  one_hop  neo4j p50={statistics.median(one_hop_neo):6.2f} ms | "
              f"mongo p50={statistics.median(one_hop_mongo):6.2f} ms")

    # 出图：neo4j 三种查询 P95 随规模变化 + mongo 对照
    import csv as _csv

    series: dict[str, list[tuple[float, float]]] = {}
    with open(csv_path, encoding="utf-8") as f:
        for row in _csv.DictReader(f):
            if row["store"] == "neo4j":
                series.setdefault(row["query"], []).append((int(row["scale"]), float(row["p95_ms"])))
    line_chart(common.ensure_results_dir() / "exp3_neo4j_p95.png", series,
               "Neo4j 图查询 P95 延迟随规模与深度的变化", "节点规模", "P95 延迟 (ms)", logy=True)

    series2: dict[str, list[tuple[float, float]]] = {}
    with open(csv_path, encoding="utf-8") as f:
        for row in _csv.DictReader(f):
            if row["query"] == "one_hop":
                series2.setdefault(row["store"], []).append((int(row["scale"]), float(row["p50_ms"])))
    line_chart(common.ensure_results_dir() / "exp3_one_hop_compare.png", series2,
               "一跳邻居查询：Neo4j vs MongoDB（P50）", "节点规模", "P50 延迟 (ms)")

    # 清理实验图数据：分批删除（50k 节点 / 32 万边单事务删除会撞事务内存上限）
    with driver.session() as session:
        while True:
            deleted = session.run(
                "MATCH (n:BenchNote) WITH n LIMIT 5000 DETACH DELETE n RETURN count(n) AS c"
            ).single()["c"]
            if deleted == 0:
                break
    print("bench graph cleaned")
    driver.close()
    print(f"\nCSV/图输出：{common.ensure_results_dir()}")


def _s(latencies: list[float]) -> dict:
    return {
        "n": len(latencies),
        "p50_ms": round(common.percentile(latencies, 50), 3),
        "p95_ms": round(common.percentile(latencies, 95), 3),
        "mean_ms": round(sum(latencies) / len(latencies), 3),
    }


if __name__ == "__main__":
    main()
