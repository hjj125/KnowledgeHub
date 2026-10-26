"""RAG 问答 API（§12）。

- POST /qa/retrieve（R1 检索服务化）：不接 LLM，返回召回 + 回填后的资料列表；
- POST /qa（R2 生成与引用）：SSE 流式输出 answer，事件序列
  sources → delta* → done；拒答分支（空资料不调用 LLM）发 refusal；
  LLM 故障发 llm_unavailable（仅返回检索结果，§12.6 降级）。
  sources[] 为引用硬约束数据源：只含真正送入 LLM 上下文的 chunk。
"""

import json
import time

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse

from app.api.deps import get_current_user
from app.core import clients
from app.core.config import settings
from app.repositories import knowledge_bases as kb_repo
from app.schemas.qa import QARequest, QARetrieveRequest
from app.services import qa as qa_service
from app.services import retrieval
from app.services.embedding import EmbeddingError
from app.services.llm import LLMError, get_llm_client

router = APIRouter(tags=["qa"])


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


async def _validate_kbs(user_id: str, kb_ids: list[str]) -> list[str]:
    """过滤出当前用户拥有的知识库；不存在的 id 直接剔除（不泄露存在性）。"""
    owned = []
    for kb_id in kb_ids:
        if await kb_repo.get_owned(kb_id, user_id) is not None:
            owned.append(kb_id)
    return owned


@router.post("/qa/retrieve")
async def qa_retrieve(payload: QARetrieveRequest, user: dict = Depends(get_current_user)) -> dict:
    """R1：召回 + 回填校验后的资料列表（无 LLM 参与）。"""
    kb_ids = await _validate_kbs(user["id"], payload.kb_ids)
    try:
        hits = await retrieval.retrieve(
            user["id"], payload.question,
            kb_ids=kb_ids or None, top_k=payload.top_k,
        )
    except (EmbeddingError, retrieval.milvus_repo.MilvusError) as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            {"reason": "vector_service_unavailable", "detail": str(exc)[:200]},
        )
    hits = qa_service.dedupe(hits)
    return {
        "total": len(hits),
        "results": [
            {
                "source_type": h["source_type"],
                "source_id": h["document_id"],
                "kb_id": h["kb_id"],
                "title": h["title"],
                "file_name": h.get("file_name"),
                "page": h["page"],
                "score": h["score"],
                "content": h["content"],
            }
            for h in hits
        ],
    }


@router.post("/qa")
async def qa(payload: QARequest, user: dict = Depends(get_current_user)) -> StreamingResponse:
    """R2：SSE 流式问答。限流 429 / kb 越权剔除在开流前完成。"""
    try:
        await qa_service.check_rate_limit(user["id"])
    except qa_service.RateLimitError as exc:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            {"reason": "rate_limited", "limit_per_min": exc.limit},
        )
    owned_kb_ids = await _validate_kbs(user["id"], payload.kb_ids)
    session_id = payload.session_id or qa_service.new_session_id()
    started = time.perf_counter()

    async def event_stream():
        history = await qa_service.load_history(session_id)
        question = payload.question
        rewritten, did_rewrite = await qa_service.rewrite_question(
            get_llm_client(), question, history
        )

        degraded = False
        hits: list[dict] = []
        try:
            hits = await retrieval.retrieve(
                user["id"], rewritten, kb_ids=owned_kb_ids or None, top_k=settings.qa_top_k,
                # §12.2 决策 2：TopK 对无关问题也会返回低分命中，
                # 分数地板之下的召回视为"证据不足"，落入拒答分支
                min_score=settings.qa_score_floor,
            )
        except (EmbeddingError, retrieval.milvus_repo.MilvusError):
            # §12.6：Embedding/Milvus 不可用 → 降级元数据检索，用正则命中的笔记正文兜底
            degraded = True
            hits = await _metadata_fallback(user["id"], rewritten, owned_kb_ids)

        hits = qa_service.dedupe(hits)
        if not hits:
            yield _sse({"type": "refusal", "session_id": session_id,
                        "message": qa_service.REFUSAL_MESSAGE, "sources": [], "degraded": degraded})
            yield _sse({"type": "done"})
            return

        blocks, used = qa_service.assemble(hits)
        sources = qa_service.sources_view(used)
        yield _sse({"type": "sources", "session_id": session_id,
                    "rewritten_question": rewritten if did_rewrite else None,
                    "degraded": degraded, "sources": sources})

        answer_parts: list[str] = []
        llm_failed: str | None = None
        try:
            async for delta in get_llm_client().stream(qa_service.build_messages(rewritten, blocks)):
                answer_parts.append(delta)
                yield _sse({"type": "delta", "text": delta})
        except LLMError as exc:
            llm_failed = str(exc)[:200]
        answer = "".join(answer_parts)
        latency_ms = int((time.perf_counter() - started) * 1000)

        if llm_failed:
            # §12.6：LLM 不可用 → 仅返回检索结果
            yield _sse({"type": "llm_unavailable", "reason": llm_failed, "sources": sources})
        else:
            await qa_service.append_history(session_id, payload.question, answer)
            try:
                await qa_service.log_qa(
                    user["id"], session_id, payload.question, rewritten, answer,
                    sources, refused=False, degraded=degraded, latency_ms=latency_ms,
                )
            except Exception:  # noqa: BLE001 — 日志失败不影响回答
                pass
        yield _sse({"type": "done", "refused": False, "degraded": degraded,
                    "llm_unavailable": llm_failed is not None, "latency_ms": latency_ms})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _metadata_fallback(owner_id: str, query: str, kb_ids: list[str]) -> list[dict]:
    """降级召回：正则命中笔记正文，构造与 retrieve 相同结构的富命中。"""
    import re

    rx = {"$regex": re.escape(query), "$options": "i"}
    query_filter: dict = {"owner_id": owner_id, "deleted_at": None, "content_md": rx}
    if kb_ids:
        query_filter["kb_id"] = {"$in": kb_ids}
    results = []
    cursor = clients.notes().find(query_filter).sort("updated_at", -1).limit(8)
    async for note in cursor:
        content = (note.get("content_md") or "").strip()
        if not content:
            continue
        results.append(
            {
                "chunk_id": f"fallback:{note['_id']}",
                "document_id": note["_id"],
                "kb_id": note["kb_id"],
                "source_type": "note",
                "title": note["title"],
                "file_name": None,
                "page": 0,
                "score": 0.0,
                "content": content[:1500],
                "content_version": note["content_version"],
            }
        )
    return results
