"""Transactional Outbox（§10.3）：业务变更与事件在同一 MongoDB 事务内落库。

事件由独立 publisher 进程扫描发布到 Redis Streams；XADD 成功但标记发布前崩溃
会造成重复投递，因此消费端必须幂等（见 workers/runner.py）。
"""

import uuid
from datetime import datetime, timezone

from typing import Any

from app.core import clients


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def add_event(
    session: Any,
    event_type: str,
    entity_type: str,
    entity_id: str,
    entity_version: int,
    payload: dict | None = None,
) -> str:
    """在给定事务会话内写入一条待发布事件；session 传 None 时独立写入（仅测试用）。"""
    event_id = str(uuid.uuid4())
    doc = {
        "_id": event_id,
        "event_type": event_type,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "entity_version": entity_version,
        "payload": payload or {},
        "status": "pending",
        "attempts": 0,
        "last_error": None,
        "created_at": _now(),
        "published_at": None,
    }
    kwargs = {"session": session} if session is not None else {}
    await clients.outbox_events().insert_one(doc, **kwargs)
    return event_id
