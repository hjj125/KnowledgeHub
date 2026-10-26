"""实验四 A（§15.4 核心）：Milvus 多索引检索性能对比。

研究问题：FLAT 精确检索与 HNSW / IVF_FLAT 近似索引在相同向量数据上的
召回率、延迟有什么差异？

- 数据：合成笔记分块后经真实 Qwen3-VL-Embedding-8B 嵌入（约 1 万条，1024 维），
  首次运行缓存到 results/exp4_vectors.json，复跑不再调用 API
- 对照索引：FLAT（精确基线）/ HNSW(M=16, efC=200, ef=128) / IVF_FLAT(nlist=128, nprobe=32)
- 指标：索引构建时间、P50/P95 延迟、QPS、ANN Recall@10（与 FLAT 真值对比）
- 声明：ANN Recall@K 是与 FLAT 精确最近邻比较，不是信息检索召回率（§15.4 提示）

运行：backend/.venv/Scripts/python benchmarks/exp4a_milvus_index.py
"""

from __future__ import annotations

import asyncio
import json
import random
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from pymilvus import DataType, MilvusClient  # noqa: E402

from benchmarks import common  # noqa: E402
from benchmarks.common import append_csv, bar_chart, save_environment  # noqa: E402
from benchmarks.data_gen import gen_notes  # noqa: E402
from app.services.chunking import chunk_text  # noqa: E402
from app.services.embedding import get_embedding_client  # noqa: E402

N_NOTES = 5000  # → 约 1 万 chunk
N_QUERIES = 50
TOP_K = 10
SEED = 42
DIM = 1024
COLLECTIONS = {
    "FLAT": {"index_type": "FLAT", "params": {}},
    "HNSW": {"index_type": "HNSW", "params": {"M": 16, "efConstruction": 200}, "search": {"ef": 128}},
    "IVF_FLAT": {"index_type": "IVF_FLAT", "params": {"nlist": 128}, "search": {"nprobe": 32}},
}

cache_path = common.ensure_results_dir() / "exp4_vectors.json"


def load_or_embed_corpus() -> tuple[list[str], list[list[float]], list[list[float]]]:
    """返回 (chunk 文本, 文档向量, 查询向量)；磁盘缓存避免重复计费。"""
    if cache_path.exists():
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        print(f"corpus cache hit: {len(data['texts'])} vectors")
        return data["texts"], data["vectors"], data["query_vectors"]

    notes = gen_notes(N_NOTES, seed=SEED)
    texts: list[str] = []
    for note in notes:
        for chunk in chunk_text(note["content_md"]):
            texts.append(chunk)

    async def _embed() -> tuple[list[list[float]], list[list[float]]]:
        print(f"embedding {len(texts)} chunks via SiliconFlow ...")
        vectors: list[list[float]] = []
        batch = 16
        for i in range(0, len(texts), batch):
            vectors.extend(await get_embedding_client().embed_texts(texts[i : i + batch]))
            if (i // batch) % 100 == 0:
                print(f"  {i}/{len(texts)}")
        query_texts = [t[:60] for t in texts[:: len(texts) // N_QUERIES]][:N_QUERIES]
        query_vectors = await get_embedding_client().embed_texts(query_texts)
        return vectors, query_vectors

    vectors, query_vectors = asyncio.run(_embed())
    cache_path.write_text(
        json.dumps({"texts": texts, "vectors": vectors, "query_vectors": query_vectors},
                   ensure_ascii=False),
        encoding="utf-8",
    )
    return texts, vectors, query_vectors


def hit_ids(res) -> set:
    """pymilvus 3.x 命中主键键名跟随 Schema（auto_id 时为 pk）。"""
    return {hit.get("pk", hit.get("id")) for hit in res[0]}


def build_collection(client: MilvusClient, name: str, index_type: str, params: dict,
                     vectors: list[list[float]]) -> float:
    client.drop_collection(name)
    # 显式主键（行号）：跨 Collection 对齐向量身份是 ANN Recall 对比的前提
    # （auto_id 的主键由 Milvus 内部生成器分配，不同集合互不相同）
    schema = MilvusClient.create_schema(auto_id=False, enable_dynamic_field=False)
    schema.add_field("pk", DataType.INT64, is_primary=True)
    schema.add_field("dense", DataType.FLOAT_VECTOR, dim=DIM)
    index_params = client.prepare_index_params()
    kwargs: dict = {"field_name": "dense", "index_type": index_type, "metric_type": "COSINE"}
    if params:
        kwargs["params"] = params
    index_params.add_index(**kwargs)
    start = time.perf_counter()
    client.create_collection(collection_name=name, schema=schema, index_params=index_params)
    for i in range(0, len(vectors), 2000):
        rows = [{"pk": i + j, "dense": v} for j, v in enumerate(vectors[i : i + 2000])]
        client.insert(collection_name=name, data=rows)
    client.flush(name)
    build_ms = (time.perf_counter() - start) * 1000
    return build_ms


def main() -> None:
    texts, vectors, query_vectors = load_or_embed_corpus()
    client = MilvusClient(uri="http://localhost:19530")
    rng = random.Random(SEED)
    save_environment("exp4a", {"vectors": len(vectors), "dim": DIM, "queries": len(query_vectors),
                               "top_k": TOP_K, "collections": list(COLLECTIONS)})

    rows = []
    ground_truth: list[set[int]] = []  # FLAT 真值，跨索引复用
    for name, cfg in COLLECTIONS.items():
        build_ms = build_collection(client, name, cfg["index_type"], cfg["params"], vectors)
        client.load_collection(name)
        search_kwargs: dict = {}
        if "search" in cfg:
            search_kwargs["search_params"] = {"params": cfg["search"]}

        latencies = []
        for qv in query_vectors:
            start = time.perf_counter()
            res = client.search(collection_name=name, data=[qv], limit=TOP_K, **search_kwargs)
            latencies.append((time.perf_counter() - start) * 1000)
            if name == "FLAT":
                ground_truth.append(hit_ids(res))

        # QPS：连续 200 次顺序检索
        start = time.perf_counter()
        for qv in query_vectors * 4:
            client.search(collection_name=name, data=[qv], limit=TOP_K, **search_kwargs)
        qps = len(query_vectors) * 4 / (time.perf_counter() - start)

        # ANN Recall@10：与 FLAT 真值对比
        recalls = []
        for qi, qv in enumerate(query_vectors):
            res = client.search(collection_name=name, data=[qv], limit=TOP_K, **search_kwargs)
            got = hit_ids(res)
            recalls.append(len(got & ground_truth[qi]) / TOP_K)

        row = {
            "index_type": name,
            "build_ms": round(build_ms, 1),
            "p50_ms": round(common.percentile(latencies, 50), 3),
            "p95_ms": round(common.percentile(latencies, 95), 3),
            "mean_ms": round(statistics.mean(latencies), 3),
            "qps": round(qps, 1),
            "recall_at_10": round(statistics.mean(recalls), 4),
        }
        rows.append(row)
        print(f"  {name:9s} build={build_ms:8.0f} ms p50={row['p50_ms']:7.2f} "
              f"p95={row['p95_ms']:7.2f} qps={row['qps']:8.0f} recall@10={row['recall_at_10']}")

    append_csv(common.ensure_results_dir() / "exp4a_index_compare.csv",
               ["index_type", "build_ms", "p50_ms", "p95_ms", "mean_ms", "qps", "recall_at_10"], rows)
    bar_chart(common.ensure_results_dir() / "exp4a_p95.png",
              [r["index_type"] for r in rows], [r["p95_ms"] for r in rows],
              f"Milvus 索引对比：Top-{TOP_K} 检索 P95（{len(vectors)} 向量）", "P95 延迟 (ms)")
    bar_chart(common.ensure_results_dir() / "exp4a_recall.png",
              [r["index_type"] for r in rows], [r["recall_at_10"] * 100 for r in rows],
              "ANN Recall@10（以 FLAT 精确检索为真值）", "Recall@10 (%)")

    for name, _ in COLLECTIONS.items():
        client.drop_collection(name)
    print(f"\nCSV/图输出：{common.ensure_results_dir()}")


if __name__ == "__main__":
    main()
