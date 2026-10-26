"""实验四 B v2（§15.4-B，P1）：真实文档语料上的检索质量对比。

修订说明（重要）：合成模板语料版本（exp4b_quality_synthetic_negative.csv）得出
负结果——模板句大量重复导致 chunk 嵌入被稀释，任何方法都无法在 chunk 级区分目标
（精确余弦下目标仍排不进 Top-5）。这印证了设计文档 §15.7 "不要把随机生成的文本
当作高质量语义检索评测资料" 的警告，故本版本改用真实文档语料：

- 语料：KnowledgeHub.md（设计文档）、README.md、PROGRESS.md 的章节正文，
  按标题切分后分块（约 400 chunk 真实中文技术文本）
- 评测集：以章节标题构造自然语言查询（标题即作者写下的主题标注，相关集 = 该章节全部
  chunk），约 100 条；标题行不进入 chunk 正文，检索必须凭语义匹配
- 对照：Dense（Milvus FLAT 精确检索）/ BM25（jieba）/ RRF 混合 / Reranker 精排
- 指标：Precision@5、Recall@5、MRR；延迟区分查询嵌入与检索两段

运行：backend/.venv/Scripts/python benchmarks/exp4b_retrieval_quality.py
"""

from __future__ import annotations

import asyncio
import json
import re
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import httpx  # noqa: E402
import jieba  # noqa: E402
from pymilvus import DataType, MilvusClient  # noqa: E402
from rank_bm25 import BM25Okapi  # noqa: E402

from benchmarks import common  # noqa: E402
from benchmarks.common import append_csv, bar_chart, save_environment  # noqa: E402
from app.services.chunking import chunk_text  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.services.embedding import get_embedding_client  # noqa: E402

TOP_K = 5
REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCES = [
    REPO_ROOT / "KnowledgeHub.md" if (REPO_ROOT.parent / "KnowledgeHub.md").exists() is False
    else REPO_ROOT.parent / "KnowledgeHub.md",
    REPO_ROOT / "README.md",
    REPO_ROOT / "PROGRESS.md",
]
cache_path = common.ensure_results_dir() / "exp4b_real_vectors.json"

SECTION_SPLIT = re.compile(r"\n(?=#{2,3} )")  # ## / ### 二三级标题


async def load_real_corpus() -> tuple[list[str], list[list[float]], list[int], list[str]]:
    """切分真实文档 → (chunk 文本, 向量, chunk→章节号, 章节标题列表)。"""
    sections: list[tuple[str, str]] = []  # (标题, 正文)
    for path in SOURCES:
        text = path.read_text(encoding="utf-8")
        for part in SECTION_SPLIT.split(text):
            lines = part.lstrip("\n").split("\n", 1)
            heading = re.sub(r"^#+\s*", "", lines[0]).strip()
            body = lines[1] if len(lines) > 1 else ""
            if heading and body.strip():
                sections.append((heading, body))
    texts: list[str] = []
    sec_of_chunk: list[int] = []
    for si, (heading, body) in enumerate(sections):
        chunks = chunk_text(body)
        if not chunks:
            chunks = [heading]
        for chunk in chunks:
            texts.append(chunk)
            sec_of_chunk.append(si)
    titles = [h for h, _ in sections]
    print(f"real corpus: {len(sections)} sections, {len(texts)} chunks")

    if cache_path.exists():
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        return texts, data["vectors"], sec_of_chunk, titles

    async def _embed() -> list[list[float]]:
        vectors: list[list[float]] = []
        for i in range(0, len(texts), 16):
            vectors.extend(await get_embedding_client().embed_texts(texts[i : i + 16]))
        return vectors

    vectors = await _embed()
    cache_path.write_text(json.dumps({"vectors": vectors}, ensure_ascii=False), encoding="utf-8")
    return texts, vectors, sec_of_chunk, titles


def build_flat(client: MilvusClient, vectors: list[list[float]]) -> None:
    name = "bench_flat_qa"
    client.drop_collection(name)
    schema = MilvusClient.create_schema(auto_id=False, enable_dynamic_field=False)
    schema.add_field("pk", DataType.INT64, is_primary=True)
    schema.add_field("dense", DataType.FLOAT_VECTOR, dim=1024)
    index_params = client.prepare_index_params()
    index_params.add_index(field_name="dense", index_type="FLAT", metric_type="COSINE")
    client.create_collection(collection_name=name, schema=schema, index_params=index_params)
    for i in range(0, len(vectors), 2000):
        rows = [{"pk": i + j, "dense": v} for j, v in enumerate(vectors[i : i + 2000])]
        client.insert(collection_name=name, data=rows)
    client.flush(name)
    client.load_collection(name)


def mrr(rankings: list[int]) -> float:
    return statistics.mean(1.0 / r if r > 0 else 0.0 for r in rankings)


def try_rerank(query: str, docs: list[str]) -> list[float] | None:
    try:
        r = httpx.post(
            settings.siliconflow_api_base.rstrip("/") + "/rerank",
            headers={"Authorization": f"Bearer {settings.siliconflow_api_key}"},
            json={"model": settings.reranker_model, "query": query, "documents": docs,
                  "top_n": len(docs), "return_documents": False},
            timeout=60,
        )
        if r.status_code != 200:
            print(f"  (rerank HTTP {r.status_code}: {r.text[:80]})")
            return None
        results = sorted(r.json()["results"], key=lambda x: x["index"])
        return [item["relevance_score"] for item in results]
    except Exception as exc:  # noqa: BLE001
        print(f"  (rerank unavailable: {str(exc)[:80]})")
        return None


def questionize(heading: str) -> str:
    """标题 → 自然问句：去掉编号前缀，按需加疑问后缀。"""
    clean = re.sub(r"^[\d.]+\s*", "", heading).strip()
    if clean.endswith(("？", "吗")):
        return clean
    return f"{clean}是怎么回事？"


async def main_async() -> None:
    from app.services.embedding import get_embedding_client

    texts, vectors, sec_of_chunk, titles = await load_real_corpus()
    client = MilvusClient(uri="http://localhost:19530")
    build_flat(client, vectors)

    # 评测集：每个章节一条查询（标题问句化）；相关集 = 该章节全部 chunk
    sec_chunks: dict[int, list[int]] = {}
    for ci, si in enumerate(sec_of_chunk):
        sec_chunks.setdefault(si, []).append(ci)
    eval_set = []
    for si, chunks in sec_chunks.items():
        if si == 0:  # 文档开头无标题章节跳过
            continue
        eval_set.append({"query": questionize(titles[si]), "relevant": set(chunks)})
    print(f"eval queries: {len(eval_set)}")

    tokenized = [[t for t in jieba.lcut(t) if re.match(r"[\w\u4e00-\u9fff]", t)] for t in texts]
    bm25 = BM25Okapi(tokenized)

    save_environment("exp4b", {"corpus": "real docs (KnowledgeHub.md/README/PROGRESS)",
                               "chunks": len(texts), "queries": len(eval_set), "top_k": TOP_K})

    rows = {name: {"p": [], "r": [], "mrr": [], "lat": [], "embed_lat": []} for name in
            ("dense", "bm25", "hybrid_rrf", "reranker")}
    rerank_available = True

    for item in eval_set:
        query, relevant = item["query"], item["relevant"]

        start = time.perf_counter()
        qv = (await get_embedding_client().embed_texts([query]))[0]
        embed_lat = (time.perf_counter() - start) * 1000
        start = time.perf_counter()
        res = client.search(collection_name="bench_flat_qa", data=[qv], limit=TOP_K)
        dense_lat = (time.perf_counter() - start) * 1000
        dense_top = [hit["pk"] for hit in res[0]]

        start = time.perf_counter()
        scores = bm25.get_scores([t for t in jieba.lcut(query) if re.match(r"[\w\u4e00-\u9fff]", t)])
        bm25_top = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:TOP_K]
        bm25_lat = (time.perf_counter() - start) * 1000

        rrf: dict[int, float] = {}
        for rank, idx in enumerate(dense_top):
            rrf[idx] = rrf.get(idx, 0) + 1 / (60 + rank + 1)
        for rank, idx in enumerate(bm25_top):
            rrf[idx] = rrf.get(idx, 0) + 1 / (60 + rank + 1)
        hybrid_top = sorted(rrf, key=rrf.get, reverse=True)[:TOP_K]

        rerank_top: list[int] = []
        if rerank_available:
            res20 = client.search(collection_name="bench_flat_qa", data=[qv], limit=20)
            cand_idx = [hit["pk"] for hit in res20[0]]
            scores_r = try_rerank(query, [texts[i] for i in cand_idx])
            if scores_r is None:
                rerank_available = False
            else:
                rerank_top = [cand_idx[i] for i in sorted(range(len(cand_idx)),
                                                          key=lambda k: scores_r[k], reverse=True)[:TOP_K]]

        def metrics(top: list[int]) -> tuple[float, float, int]:
            hits = sum(1 for i in top if i in relevant)
            return hits / TOP_K, hits / len(relevant), next(
                (rank + 1 for rank, i in enumerate(top) if i in relevant), 0)

        for name, top, lat in (("dense", dense_top, dense_lat), ("bm25", bm25_top, bm25_lat),
                               ("hybrid_rrf", hybrid_top, None), ("reranker", rerank_top, None)):
            if not top and name == "reranker":
                continue
            p, r, first = metrics(top)
            rows[name]["p"].append(p)
            rows[name]["r"].append(r)
            rows[name]["mrr"].append(first)
            if lat is not None:
                rows[name]["lat"].append(lat)
                rows[name]["embed_lat"].append(embed_lat)

    out_rows = []
    for name, data in rows.items():
        if not data["p"]:
            continue
        out_rows.append({
            "method": name,
            "precision_at_5": round(statistics.mean(data["p"]), 4),
            "recall_at_5": round(statistics.mean(data["r"]), 4),
            "mrr": round(mrr(data["mrr"]), 4),
            "p95_search_ms": round(common.percentile(data["lat"], 95), 2) if data["lat"] else "",
            "p95_embed_ms": round(common.percentile(data["embed_lat"], 95), 2) if data["embed_lat"] else "",
        })
        print(f"  {name:12s} P@5={out_rows[-1]['precision_at_5']:.4f} "
              f"R@5={out_rows[-1]['recall_at_5']:.4f} MRR={out_rows[-1]['mrr']:.4f} "
              f"search_p95={out_rows[-1]['p95_search_ms']}ms embed_p95={out_rows[-1]['p95_embed_ms']}ms")
    if not rerank_available:
        print("  (reranker 不可用，已跳过)")

    append_csv(common.ensure_results_dir() / "exp4b_quality.csv",
               ["method", "precision_at_5", "recall_at_5", "mrr", "p95_search_ms", "p95_embed_ms"],
               out_rows)
    bar_chart(common.ensure_results_dir() / "exp4b_mrr.png",
              [r["method"] for r in out_rows], [r["mrr"] for r in out_rows],
              "检索质量对比：MRR（真实文档语料，标题→章节任务）", "MRR")
    client.drop_collection("bench_flat_qa")
    print(f"\nCSV/图输出：{common.ensure_results_dir()}")


if __name__ == "__main__":
    asyncio.run(main_async())
