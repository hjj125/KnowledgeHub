"""笔记草稿生成（交付后增强）：文档 → LLM 抽取要点 → 单篇结构化笔记草稿。

半自动建图的关键：Prompt 注入该知识库已有笔记标题清单，约束 LLM 只引用清单内
标题写 [[链接]]；草稿被用户保存为笔记后，现有 links→graph 链路自动解析成边。

- 资料来源：MongoDB chunks（权威分块存储，带页码/幻灯片号）；
- **map-reduce 两段式**：实测单请求超过约 3k 字符会被硅基流动 429 拒绝（50609
  System is too busy，与账号 TPM/容量相关），故先按批提取要点（每次小请求），
  再把各批要点汇总成最终笔记；
- 幂等：以事件 payload 的 request_id 为准（同 request_id 已生成则跳过）；
- 失败随事件重试（runner 死信机制），草稿状态机 generating→ready/failed 落文档记录。
"""

import logging

from app.core import clients
from app.core.config import settings
from app.repositories import documents as documents_repo
from app.services.llm import LLMError, get_llm_client

log = logging.getLogger("worker.draft")

SYSTEM_PROMPT = (
    "你是个人知识库助教。根据给定的文档资料，撰写一篇结构化的要点笔记。\n"
    "硬性要求：\n"
    "1. 只依据资料内容撰写，不编造事实；资料不足的部分直接不写。\n"
    "2. 正文中可以用 [[标题]] 引用其他笔记；可引用的标题 ONLY 来自"
    "\"已有笔记清单\"，必须逐字使用清单中的标题；清单为空或清单中没有合适标题时，"
    "绝对不要写任何 [[..]]。\n"
    "3. 输出为 Markdown：第一行必须是 `# 笔记标题`（标题不超过 30 字）；\n"
    "正文分 2-4 个小节（用 `## 小节名`），每节 2-5 个要点（用列表）；\n"
    "末尾可加一个 `## 疑问与待办` 小节列出资料中未讲清的问题。\n"
    "4. 不要输出任何解释性文字，只输出笔记本身。"
)

MAP_PROMPT = (
    "从以下文档片段中提取关键要点。要求：\n"
    "1. 每条要点一行，保留其中的页码标注（如【第 3 页】）便于回溯；\n"
    "2. 只输出要点列表本身，不要解释、不要标题；\n"
    "3. 事实性内容优先，忽略纯排版/装饰文字。"
)


def _piece(chunk: dict) -> str:
    page = chunk.get("page") or 0
    loc = f"【第 {page} 页】" if page else "【正文】"
    return f"{loc}{chunk['content']}"


def build_batches(chunks: list[dict], batch_budget: int, max_batches: int) -> list[str]:
    """把 chunk 按字符预算切成 N 批资料（每批将作为一次独立的小请求）。"""
    batches: list[str] = []
    parts: list[str] = []
    total = 0
    for chunk in chunks:
        piece = _piece(chunk)
        if parts and total + len(piece) > batch_budget:
            batches.append("\n\n".join(parts))
            parts, total = [], 0
            if len(batches) >= max_batches:
                break
        parts.append(piece)
        total += len(piece)
    if parts and len(batches) < max_batches:
        batches.append("\n\n".join(parts))
    return batches


def build_material(chunks: list[dict], char_budget: int) -> str:
    """把带页码的 chunk 拼装为资料文本；超预算截断并标注（单请求路径与测试用）。"""
    parts: list[str] = []
    total = 0
    truncated = False
    for chunk in chunks:
        piece = _piece(chunk)
        if total + len(piece) > char_budget:
            truncated = True
            break
        parts.append(piece)
        total += len(piece)
    material = "\n\n".join(parts)
    if truncated:
        material += "\n\n（资料过长已截断，仅以上述部分为准）"
    return material


async def load_note_titles(kb_id: str) -> list[str]:
    """该知识库现存笔记标题（双链引用的合法清单），按最近更新排序取前 N。"""
    cursor = (
        clients.notes()
        .find({"kb_id": kb_id, "deleted_at": None}, {"title": 1})
        .sort("updated_at", -1)
        .limit(settings.draft_max_note_titles)
    )
    return [doc["title"] async for doc in cursor]


def parse_draft_output(raw: str, fallback_title: str) -> tuple[str, str]:
    """解析 LLM 输出：首个 `# ` 行为标题，其余为正文；缺失则回退标题。"""
    text = (raw or "").strip()
    lines = text.split("\n")
    title = fallback_title
    body_start = 0
    for i, line in enumerate(lines):
        if line.startswith("# "):
            title = line[2:].strip() or fallback_title
            body_start = i + 1
            break
    body = "\n".join(lines[body_start:]).strip()
    if not body:
        body = text  # 没有标题行时全文即正文
    return title[:60], body


def build_messages(title: str, file_name: str, material: str, note_titles: list[str]) -> list[dict]:
    listing = "\n".join(f"- {t}" for t in note_titles) if note_titles else "（暂无其他笔记）"
    user = (
        f"文档：《{title}》（{file_name}）\n\n"
        f"已有笔记清单（[[标题]] 只能引用以下标题）：\n{listing}\n\n"
        f"资料：\n{material}\n\n"
        "请按系统要求输出这篇要点笔记。"
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


async def process_draft(document: dict, request_id: str) -> str:
    """生成单篇要点草稿并落库；抛异常交给 runner 重试。

    map-reduce：资料总量超过单批预算时，先分批提取要点（每次小请求，
    规避硅基流动对大请求的 429），再把各批要点汇总成最终笔记。
    """
    doc_id = document["_id"]
    chunks = await (
        clients.chunks()
        .find({"document_id": doc_id})
        .sort("chunk_index", 1)
        .to_list(length=1000)
    )
    if not chunks or not "".join(c.get("content", "") for c in chunks).strip():
        await documents_repo.mark_draft(doc_id, "failed", error="文档无可提取文本")
        return "draft-failed(empty)"

    note_titles = await load_note_titles(document["kb_id"])
    try:
        batches = build_batches(chunks, settings.draft_map_char_budget, settings.draft_max_batches)
        if len(batches) == 1:
            material = batches[0]
        else:
            log.info("map-reduce: %d batches for %s", len(batches), doc_id[:8])
            bullet_parts: list[str] = []
            for i, batch in enumerate(batches, 1):
                messages = [
                    {"role": "system", "content": MAP_PROMPT},
                    {"role": "user", "content": f"（第 {i}/{len(batches)} 部分）\n{batch}"},
                ]
                raw = await get_llm_client().chat(
                    messages, max_tokens=settings.draft_map_max_tokens
                )
                if not raw.strip():
                    # 思考型模型可能把输出上限耗在 reasoning 段：加倍重试一次
                    raw = await get_llm_client().chat(
                        messages, max_tokens=settings.draft_map_max_tokens * 2
                    )
                if not raw.strip():
                    # 仍为空则用原文摘录兜底，保证 reduce 有该批素材
                    log.warning("map %d/%d empty after retry, falling back to raw excerpt", i, len(batches))
                    raw = batch[:1200]
                bullet_parts.append(f"## 资料第 {i} 部分要点\n{raw.strip()}")
                log.info("map %d/%d done (%d chars)", i, len(batches), len(raw))
            material = "\n\n".join(bullet_parts)

        messages = build_messages(document["title"], document["file_name"], material, note_titles)
        raw = await get_llm_client().chat(messages, max_tokens=settings.draft_max_tokens)
    except LLMError as exc:
        await documents_repo.mark_draft(doc_id, "failed", error=str(exc)[:300])
        raise

    title, body = parse_draft_output(raw, fallback_title=f"{document['title']} 要点笔记")
    await documents_repo.mark_draft(
        doc_id,
        "ready",
        note_draft={
            "title": title,
            "content_md": body,
            "model": settings.llm_model,
            "request_id": request_id,
            "source_chunks": len(chunks),
        },
    )
    log.info("draft ready for %s (title=%s, %d chars)", doc_id[:8], title, len(body))
    return f"draft-ready({len(body)} chars)"
