"""RAG 问答流水线（§12.1 八步中的装配与治理，检索复用 services/retrieval.py）。

- 上下文装配：编号 [1]—[n]、按相似度排序、字符预算控制（§12.1 第 6 步）；
- 引用硬约束：sources[] 只包含真正送入 LLM 的 chunk（§12.2 决策 1）；
- 拒答分支：有效 chunk 为 0 时不调用 LLM，返回模板（§12.2 决策 2）；
- 多轮：会话历史存 Redis（短 TTL），第二轮起用 LLM 把追问改写为独立检索问题（§12.5）；
- 限流：Redis 固定窗口计数器（§12.6）。
"""

import json
import uuid
from datetime import datetime, timezone
from typing import AsyncIterator

from app.core import clients
from app.core.config import settings

REFUSAL_MESSAGE = "当前知识库缺少足够资料，无法回答该问题。"
HISTORY_PREFIX = "session:"
HISTORY_SUFFIX = ":qa_history"
RATE_PREFIX = "rate:qa:"


def dedupe(hits: list[dict]) -> list[dict]:
    """同文档同内容的重复 chunk 去重（重叠分块可能产生），保留分数最高者。"""
    seen: dict[tuple, dict] = {}
    for hit in hits:
        key = (hit["document_id"], hit["content"])
        if key not in seen or hit["score"] > seen[key]["score"]:
            seen[key] = hit
    return sorted(seen.values(), key=lambda h: h["score"], reverse=True)


def source_label(hit: dict, index: int) -> str:
    if hit["source_type"] == "note":
        return f"笔记《{hit['title']}》"
    page = f"第 {hit['page']} 页" if hit.get("page", 0) else "（无页码）"
    name = hit.get("file_name") or hit["title"]
    return f"文档《{name}》{page}"


def assemble(hits: list[dict], max_chunks: int | None = None, char_budget: int | None = None) -> tuple[list[str], list[dict]]:
    """装配编号上下文块；返回 (编号文本块, 实际送入的 hits)。引用硬约束的依据。"""
    blocks: list[str] = []
    used: list[dict] = []
    total = 0
    limit = max_chunks or settings.qa_context_chunks
    budget = char_budget or settings.qa_context_char_budget
    for hit in hits:
        if len(blocks) >= limit:
            break
        text = f"[{len(blocks) + 1}] {source_label(hit, len(blocks) + 1)}：{hit['content']}"
        if blocks and total + len(text) > budget:
            break
        blocks.append(text)
        used.append(hit)
        total += len(text)
    return blocks, used


def sources_view(used: list[dict]) -> list[dict]:
    """sources[]：前端引用角标的唯一数据源（每条可点击回原文）。"""
    return [
        {
            "n": i + 1,
            "source_type": hit["source_type"],
            "source_id": hit["document_id"],
            "kb_id": hit["kb_id"],
            "title": hit["title"],
            "file_name": hit.get("file_name"),
            "page": hit["page"],
            "chunk_id": hit["chunk_id"],
            "score": hit["score"],
            "content": hit["content"],
        }
        for i, hit in enumerate(used)
    ]


def build_messages(question: str, blocks: list[str]) -> list[dict]:
    system = (
        "你是用户的个人知识库助手。仅根据下方资料回答问题，引用处标注 [编号]。\n"
        "资料不足以回答时，明确说明\"当前知识库缺少足够资料\"。\n"
        "禁止编造来源、页码或内容。"
    )
    context = "\n\n".join(blocks)
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": f"资料：\n{context}\n\n问题：{question}"},
    ]


REWRITE_SYSTEM = (
    "把用户的追问改写为一个不依赖对话历史的独立检索问题。"
    "只输出改写后的问题本身，不要任何解释。"
)


async def load_history(session_id: str) -> list[dict]:
    raw = await clients.redis_client().lrange(HISTORY_PREFIX + session_id + HISTORY_SUFFIX, 0, -1)
    return [json.loads(item) for item in raw]


async def append_history(session_id: str, question: str, answer: str) -> None:
    key = HISTORY_PREFIX + session_id + HISTORY_SUFFIX
    client = clients.redis_client()
    await client.rpush(key, json.dumps({"question": question, "answer": answer}, ensure_ascii=False))
    await client.ltrim(key, -settings.qa_session_max_turns * 2, -1)
    await client.expire(key, settings.qa_session_ttl_seconds)


async def rewrite_question(llm, question: str, history: list[dict]) -> tuple[str, bool]:
    """多轮追问改写；失败静默回退原问题（返回 (问题, 是否发生了改写)）。"""
    if not history:
        return question, False
    recent = history[-2:]
    dialogue = "\n".join(f"问：{t['question']}\n答：{t['answer'][:300]}" for t in recent)
    try:
        rewritten = await llm.chat(
            [
                {"role": "system", "content": REWRITE_SYSTEM},
                {"role": "user", "content": f"对话历史：\n{dialogue}\n\n追问：{question}"},
            ]
        )
        rewritten = rewritten.strip().strip('"')
        return (rewritten or question, True)
    except Exception:  # noqa: BLE001 — 改写失败不阻断问答（§12.6 降级）
        return question, False


async def check_rate_limit(user_id: str) -> int:
    """Redis 固定窗口限流；超限抛 RateLimitError。返回当前窗口计数。"""
    key = RATE_PREFIX + user_id
    client = clients.redis_client()
    count = await client.incr(key)
    if count == 1:
        await client.expire(key, 60)
    if count > settings.qa_rate_limit_per_min:
        raise RateLimitError(settings.qa_rate_limit_per_min)
    return count


class RateLimitError(Exception):
    def __init__(self, limit: int):
        self.limit = limit


async def log_qa(user_id: str, session_id: str, question: str, rewritten: str,
                 answer: str, sources: list[dict], refused: bool, degraded: bool, latency_ms: int) -> None:
    """qa_logs 问答日志（§12.5 可选项）：可追溯、可评测。"""
    await clients.db().get_collection("qa_logs").insert_one(
        {
            "user_id": user_id,
            "session_id": session_id,
            "question": question,
            "rewritten_question": rewritten,
            "answer": answer,
            "sources": [
                {"source_type": s["source_type"], "source_id": s["source_id"], "page": s["page"]}
                for s in sources
            ],
            "refused": refused,
            "degraded": degraded,
            "llm_model": settings.llm_model,
            "latency_ms": latency_ms,
            "created_at": datetime.now(timezone.utc),
        }
    )


def new_session_id() -> str:
    return uuid.uuid4().hex


__all__ = [
    "REFUSAL_MESSAGE", "RateLimitError", "assemble", "append_history", "build_messages",
    "check_rate_limit", "dedupe", "load_history", "log_qa", "new_session_id", "rewrite_question",
    "source_label", "sources_view",
]
