"""统一召回与回填（§5.4 / §12.1 第 3—4 步）：/search 与 /qa 共用。

流程：查询向量 → Milvus Top-K（owner/kb/source_type 标量过滤）→ MongoDB chunks 回填
→ 父实体校验（归属、未删除、版本一致）→ 带标题/页码/原文的富命中列表。
调用方决定如何装配（search 直接返回、qa 继续去重/精排/组装上下文）。
"""

from app.core import clients
from app.repositories import milvus as milvus_repo
from app.services.embedding import EmbeddingError, get_embedding_client


class RetrievalError(RuntimeError):
    pass


async def retrieve(
    owner_id: str,
    query: str,
    kb_id: str | None = None,
    kb_ids: list[str] | None = None,
    source_type: str | None = None,
    top_k: int = 10,
    min_score: float | None = None,
) -> list[dict]:
    """语义召回 + 回填校验。抛出 EmbeddingError / milvus_repo.MilvusError 表示向量服务不可用。"""
    query_vector = await get_embedding_client().embed_query(query)
    hits = await milvus_repo.search(
        query_vector, owner_id, kb_id=kb_id, kb_ids=kb_ids, source_type=source_type, top_k=top_k
    )
    if not hits:
        return []

    chunk_rows = await clients.chunks().find(
        {"_id": {"$in": [h["chunk_id"] for h in hits]}}
    ).to_list(length=len(hits))
    chunk_map = {row["_id"]: row for row in chunk_rows}

    note_ids = list({h["document_id"] for h in hits if h["source_type"] == "note"})
    doc_ids = list({h["document_id"] for h in hits if h["source_type"] == "document"})
    notes_map = {
        d["_id"]: d
        async for d in clients.notes().find(
            {"_id": {"$in": note_ids}, "owner_id": owner_id, "deleted_at": None}
        )
    } if note_ids else {}
    docs_map = {
        d["_id"]: d
        async for d in clients.documents().find(
            {"_id": {"$in": doc_ids}, "owner_id": owner_id, "deleted_at": None}
        )
    } if doc_ids else {}

    results: list[dict] = []
    for hit in hits:
        if min_score is not None and hit["score"] < min_score:
            continue
        row = chunk_map.get(hit["chunk_id"])
        if row is None:
            continue  # Milvus 有向量但分块行缺失：等待 worker 对账，不出残缺结果
        parent = (notes_map if hit["source_type"] == "note" else docs_map).get(hit["document_id"])
        if parent is None:
            continue  # 已删除或不属于当前用户
        # 版本护栏：父实体当前版本与命中向量版本不一致 → 陈旧索引，跳过（§10.5）
        if (
            parent.get("content_version") != hit["content_version"]
            or row["content_version"] != hit["content_version"]
        ):
            continue
        results.append(
            {
                "chunk_id": hit["chunk_id"],
                "document_id": hit["document_id"],
                "kb_id": parent["kb_id"],
                "source_type": hit["source_type"],
                "title": parent["title"],
                "file_name": parent.get("file_name") if hit["source_type"] == "document" else None,
                "page": hit["page"],
                "score": hit["score"],
                "content": row["content"],
                "content_version": hit["content_version"],
            }
        )
    return results


__all__ = ["retrieve", "RetrievalError", "EmbeddingError", "milvus_repo"]
