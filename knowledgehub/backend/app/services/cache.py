"""笔记详情 Cache-Aside（§7.2）：读时回填 TTL 缓存，写时删除缓存。

缓存键带 user_id，缓存内容为已通过所有权校验的 API 视图（ISO 时间字符串），
因此缓存命中不会绕过权限边界。
"""

import json

from app.core import clients
from app.core.config import settings


def _key(user_id: str, note_id: str) -> str:
    return f"cache:note:{user_id}:{note_id}"


async def get_note(user_id: str, note_id: str) -> dict | None:
    raw = await clients.redis_client().get(_key(user_id, note_id))
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


async def set_note(user_id: str, note_id: str, payload: dict) -> None:
    await clients.redis_client().set(
        _key(user_id, note_id),
        json.dumps(payload, ensure_ascii=False),
        ex=settings.note_cache_ttl_seconds,
    )


async def invalidate_note(user_id: str, note_id: str) -> None:
    await clients.redis_client().delete(_key(user_id, note_id))
