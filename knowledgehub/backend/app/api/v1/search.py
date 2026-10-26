"""统一检索 API（§5.6 / §10.5）。

semantic：services/retrieval.py 共用召回（Milvus TopK + MongoDB 回填校验）；
metadata：MongoDB 正则关键词检索（笔记标题+正文、文档文件名），无需向量服务；
Embedding 或 Milvus 故障时自动降级 metadata 模式并置 degraded（§10.5）。
"""

import re

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import get_current_user
from app.core import clients
from app.repositories import knowledge_bases as kb_repo
from app.repositories import milvus as milvus_repo
from app.schemas.search import SearchRequest
from app.services import retrieval
from app.services.embedding import EmbeddingError

router = APIRouter(tags=["search"])

_SNIPPET_MAX = 300


def _result(source_type: str, source_id: str, kb_id: str, title: str, snippet: str, page: int, score: float | None) -> dict:
    return {
        "source_type": source_type,
        "source_id": source_id,
        "kb_id": kb_id,
        "title": title,
        "snippet": snippet,
        "page": page,
        "score": score,
    }


async def _metadata_search(
    owner_id: str, query: str, kb_id: str | None, source_type: str | None, top_k: int
) -> list[dict]:
    """降级/显式元数据模式：正则关键词检索（无向量依赖）。"""
    rx = {"$regex": re.escape(query), "$options": "i"}
    results: list[dict] = []

    if source_type in (None, "note"):
        note_query: dict = {"owner_id": owner_id, "deleted_at": None, "$or": [{"title": rx}, {"content_md": rx}]}
        if kb_id:
            note_query["kb_id"] = kb_id
        cursor = clients.notes().find(note_query).sort("updated_at", -1).limit(top_k)
        async for note in cursor:
            results.append(
                _result("note", note["_id"], note["kb_id"], note["title"],
                        (note.get("content_md") or "")[:_SNIPPET_MAX], 0, None)
            )

    if source_type in (None, "document"):
        doc_query: dict = {"owner_id": owner_id, "deleted_at": None, "$or": [{"title": rx}, {"file_name": rx}]}
        if kb_id:
            doc_query["kb_id"] = kb_id
        cursor = clients.documents().find(doc_query).sort("updated_at", -1).limit(top_k)
        async for doc in cursor:
            results.append(
                _result("document", doc["_id"], doc["kb_id"], doc["title"], "", 0, None)
            )
    return results[:top_k]


async def _semantic_search(
    owner_id: str, query: str, kb_id: str | None, source_type: str | None, top_k: int
) -> list[dict]:
    hits = await retrieval.retrieve(owner_id, query, kb_id=kb_id, source_type=source_type, top_k=top_k)
    return [
        _result(h["source_type"], h["document_id"], h["kb_id"], h["title"],
                h["content"], h["page"], h["score"])
        for h in hits
    ]


@router.post("/search")
async def search(payload: SearchRequest, user: dict = Depends(get_current_user)) -> dict:
    if payload.kb_id and await kb_repo.get_owned(payload.kb_id, user["id"]) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "knowledge base not found")

    if payload.mode == "semantic":
        try:
            results = await _semantic_search(
                user["id"], payload.query, payload.kb_id, payload.source_type, payload.top_k
            )
            return {"mode": "semantic", "degraded": False, "reason": None, "results": results}
        except (EmbeddingError, milvus_repo.MilvusError) as exc:
            # §10.5 自动降级：向量服务不可用时不阻断检索
            results = await _metadata_search(
                user["id"], payload.query, payload.kb_id, payload.source_type, payload.top_k
            )
            return {"mode": "metadata", "degraded": True, "reason": str(exc)[:200], "results": results}

    results = await _metadata_search(
        user["id"], payload.query, payload.kb_id, payload.source_type, payload.top_k
    )
    return {"mode": "metadata", "degraded": False, "reason": None, "results": results}
