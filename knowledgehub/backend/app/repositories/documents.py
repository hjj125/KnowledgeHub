import json
import uuid
from datetime import datetime, timedelta, timezone

from pymongo import ReturnDocument

from app.core import clients
from app.core import outbox
from app.core.config import settings


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def create(
    owner_id: str,
    kb_id: str,
    file_name: str,
    file_type: str,
    size_bytes: int,
    sha256: str,
    file_path: str,
) -> dict:
    """上传登记（与 outbox 同事务）。文件原件已先落 GridFS；这里只写元数据。"""
    now = _now()
    doc = {
        "_id": str(uuid.uuid4()),
        "owner_id": owner_id,
        "kb_id": kb_id,
        "title": file_name.rsplit(".", 1)[0] if "." in file_name else file_name,
        "file_name": file_name,
        "file_type": file_type,
        "size_bytes": size_bytes,
        "sha256": sha256,
        "file_path": file_path,
        "status": "uploaded",
        "error": None,
        "content_version": 1,
        "indexed_version": 0,
        "draft_status": None,
        "note_draft": None,
        "deleted_at": None,
        "created_at": now,
        "updated_at": now,
    }
    async with clients.mongo_client().start_session() as session:
        async with await session.start_transaction():
            await clients.documents().insert_one(doc, session=session)
            await outbox.add_event(
                session, "document.created", "document", doc["_id"], 1,
                payload={"file_path": file_path, "file_type": file_type},
            )
    return doc


async def get_owned(document_id: str, owner_id: str, include_deleted: bool = False) -> dict | None:
    query: dict = {"_id": document_id, "owner_id": owner_id}
    if not include_deleted:
        query["deleted_at"] = None
    return await clients.documents().find_one(query)


async def list_for_kb(kb_id: str, owner_id: str) -> list[dict]:
    cursor = (
        clients.documents()
        .find({"kb_id": kb_id, "owner_id": owner_id, "deleted_at": None})
        .sort("created_at", -1)
    )
    return await cursor.to_list(length=1000)


async def mark_status(
    document_id: str, status: str, error: str | None = None, indexed_version: int | None = None
) -> None:
    """流水线内部状态机（uploaded→parsing→indexing→ready / failed），不发 outbox 事件。"""
    fields: dict = {"status": status, "updated_at": _now()}
    if error is not None:
        fields["error"] = error
    if indexed_version is not None:
        fields["indexed_version"] = indexed_version
    await clients.documents().update_one({"_id": document_id}, {"$set": fields})


async def reindex(document_id: str, owner_id: str) -> dict | None:
    """重新索引：版本号 +1 触发全量重建向量；metadata 不变也走一遍流水线。

    内容将变化，旧草稿一并重置（提示用户重新生成）。
    """
    async with clients.mongo_client().start_session() as session:
        async with await session.start_transaction():
            doc = await clients.documents().find_one_and_update(
                {"_id": document_id, "owner_id": owner_id, "deleted_at": None},
                {
                    "$set": {
                        "status": "uploaded",
                        "error": None,
                        "draft_status": None,
                        "note_draft": None,
                        "updated_at": _now(),
                    },
                    "$inc": {"content_version": 1},
                },
                return_document=ReturnDocument.AFTER,
                session=session,
            )
            if doc is None:
                return None
            await outbox.add_event(
                session, "document.updated", "document", document_id, doc["content_version"],
                payload={"file_path": doc["file_path"], "file_type": doc["file_type"]},
            )
    return doc


async def mark_draft(
    document_id: str, status: str, error: str | None = None, note_draft: dict | None = None
) -> None:
    """草稿状态机（None→generating→ready/failed），由 draft worker 落库。"""
    fields: dict = {"draft_status": status, "updated_at": _now()}
    fields["draft_error"] = error  # generating 时清空旧错误
    if note_draft is not None:
        fields["note_draft"] = note_draft
    await clients.documents().update_one({"_id": document_id}, {"$set": fields})


async def request_draft(document_id: str, owner_id: str) -> dict | None:
    """用户触发生成草稿：置 generating + 发布 document.draft_requested 事件。

    generating 未超过 draft_stale_seconds 视为仍在生成（防重复触发）；
    超时视为卡死（进程崩溃残留），允许重新触发。返回 None 表示文档不可用。
    """
    doc = await clients.documents().find_one(
        {"_id": document_id, "owner_id": owner_id, "deleted_at": None}
    )
    if doc is None:
        return None
    if doc.get("draft_status") == "generating":
        stale_at = doc.get("updated_at")
        if (
            isinstance(stale_at, datetime)
            and _now() - stale_at < timedelta(seconds=settings.draft_stale_seconds)
        ):
            return {"result": "in_progress"}
        # 卡死的 generating：直接放行重新触发
    request_id = uuid.uuid4().hex
    await mark_draft(document_id, "generating", error=None)
    # 用户显式重试 = 全新的失败预算：清零 draft 任务的历史 attempts，
    # 否则死信记录会让后续每次失败立即再次进入死信（attempts 永不归零）
    await clients.indexing_jobs().update_one(
        {"entity_id": document_id, "job_type": "draft"},
        {"$set": {"attempts": 0, "status": "pending", "last_error": None}},
    )
    event_id = uuid.uuid4().hex
    await clients.outbox_events().insert_one(
        {
            "_id": event_id,
            "event_type": "document.draft_requested",
            "entity_type": "document",
            "entity_id": document_id,
            "entity_version": doc["content_version"],
            "payload": {"request_id": request_id},
            "status": "pending",
            "attempts": 0,
            "last_error": None,
            "created_at": _now(),
            "published_at": None,
        }
    )
    await clients.redis_client().xadd(
        "stream:knowledge_events",
        {
            "event_id": event_id,
            "event_type": "document.draft_requested",
            "entity_type": "document",
            "entity_id": document_id,
            "entity_version": str(doc["content_version"]),
            "payload": json.dumps({"request_id": request_id}),
        },
    )
    await clients.outbox_events().update_one(
        {"_id": event_id}, {"$set": {"status": "published", "published_at": _now()}}
    )
    return {"result": "requested", "request_id": request_id}


async def soft_delete(document_id: str, owner_id: str) -> dict | None:
    """软删除并触发向量/原件清理（worker 消费 document.deleted）。"""
    now = _now()
    async with clients.mongo_client().start_session() as session:
        async with await session.start_transaction():
            doc = await clients.documents().find_one_and_update(
                {"_id": document_id, "owner_id": owner_id, "deleted_at": None},
                {"$set": {"deleted_at": now, "status": "deleted", "updated_at": now}},
                session=session,
            )
            if doc is None:
                return None
            await outbox.add_event(
                session, "document.deleted", "document", document_id, doc["content_version"],
                payload={"file_path": doc["file_path"]},
            )
    return doc


def public_view(doc: dict, include_draft: bool = False) -> dict:
    view = {
        "id": doc["_id"],
        "kb_id": doc["kb_id"],
        "title": doc["title"],
        "file_name": doc["file_name"],
        "file_type": doc["file_type"],
        "size_bytes": doc["size_bytes"],
        "status": doc["status"],
        "error": doc.get("error"),
        "content_version": doc["content_version"],
        "indexed_version": doc.get("indexed_version", 0),
        "draft_status": doc.get("draft_status"),
        "created_at": doc["created_at"].isoformat(),
        "updated_at": doc["updated_at"].isoformat(),
    }
    if include_draft:
        view["note_draft"] = doc.get("note_draft")
        view["draft_error"] = doc.get("draft_error")
    return view
