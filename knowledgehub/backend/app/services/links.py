"""双向链接解析（§5.2）：[[标题]] → 同知识库内稳定 note_id。

同名多篇取最近更新的一篇；解析不到的标题（不存在/已删除/跨库）直接跳过。
自引用（笔记链接自己）排除。
"""

import re

from app.core import clients

LINK_PATTERN = re.compile(r"\[\[([^\[\]]+?)\]\]")


def extract_link_titles(content_md: str) -> list[str]:
    titles: list[str] = []
    for match in LINK_PATTERN.finditer(content_md or ""):
        title = match.group(1).strip()
        if title and title not in titles:
            titles.append(title)
    return titles


async def resolve_linked_note_ids(kb_id: str, content_md: str, exclude_note_id: str | None = None) -> list[str]:
    titles = extract_link_titles(content_md)
    if not titles:
        return []
    docs = await (
        clients.notes()
        .find({"kb_id": kb_id, "title": {"$in": titles}, "deleted_at": None}, {"_id": 1, "title": 1, "updated_at": 1})
        .sort("updated_at", -1)
        .to_list(length=500)
    )
    by_title: dict[str, str] = {}
    for doc in docs:  # 已按 updated_at 降序，同名取最新
        by_title.setdefault(doc["title"], doc["_id"])

    resolved: list[str] = []
    for title in titles:
        note_id = by_title.get(title)
        if note_id and note_id != exclude_note_id and note_id not in resolved:
            resolved.append(note_id)
    return resolved
