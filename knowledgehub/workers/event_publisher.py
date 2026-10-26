"""Outbox 发布器（§10.3）：扫描待发布事件 → XADD 到 Redis Streams → 标记已发布。

崩溃窗口说明：XADD 成功后、标记 published 前崩溃会导致重复投递，
消费端幂等保证无副作用（这是设计允许的 at-least-once）。

运行：backend/.venv/Scripts/python -m workers.event_publisher
"""

import asyncio
import json
import logging
from datetime import datetime, timezone

from app.core import clients
from app.core.config import settings
from workers.runner import STREAM

POLL_INTERVAL_SECONDS = 1.0
BATCH_SIZE = 100
MAX_PUBLISH_ATTEMPTS = 10

log = logging.getLogger("publisher")


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def publish_pending_batch() -> int:
    docs = await (
        clients.outbox_events()
        .find({"status": "pending"})
        .sort("created_at", 1)
        .to_list(length=BATCH_SIZE)
    )
    published = 0
    for doc in docs:
        try:
            fields = {
                "event_id": doc["_id"],
                "event_type": doc["event_type"],
                "entity_type": doc["entity_type"],
                "entity_id": doc["entity_id"],
                "entity_version": str(doc["entity_version"]),
            }
            if doc.get("payload"):
                fields["payload"] = json.dumps(doc["payload"], ensure_ascii=False, default=str)
            await clients.redis_client().xadd(STREAM, fields)
        except Exception as exc:  # noqa: BLE001 — Redis 不可用时事件保留，下次重试
            await clients.outbox_events().update_one(
                {"_id": doc["_id"]},
                {"$inc": {"attempts": 1}, "$set": {"last_error": str(exc)[:300]}},
            )
            log.warning("xadd failed for %s: %s", doc["_id"], exc)
            continue
        await clients.outbox_events().update_one(
            {"_id": doc["_id"]}, {"$set": {"status": "published", "published_at": _now(), "last_error": None}}
        )
        published += 1
    return published


async def main() -> None:
    log.info("outbox publisher started (stream=%s)", STREAM)
    while True:
        try:
            n = await publish_pending_batch()
            if n:
                log.info("published %d event(s)", n)
                continue  # 还有积压时立刻继续
        except Exception as exc:  # noqa: BLE001 — 任何异常都不退出发布循环
            log.warning("publish round failed: %s", exc)
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    asyncio.run(main())
