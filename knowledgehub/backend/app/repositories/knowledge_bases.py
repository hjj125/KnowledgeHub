import uuid
from datetime import datetime, timezone

from app.core import clients
from app.core import outbox


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def list_for_owner(owner_id: str) -> list[dict]:
    query = {"owner_id": owner_id, "deleted_at": None}
    cursor = clients.knowledge_bases().find(query).sort("created_at", 1)
    return await cursor.to_list(length=1000)


async def get_owned(kb_id: str, owner_id: str) -> dict | None:
    return await clients.knowledge_bases().find_one(
        {"_id": kb_id, "owner_id": owner_id, "deleted_at": None}
    )


async def create(owner_id: str, name: str, category: str, description: str) -> dict:
    now = _now()
    doc = {
        "_id": str(uuid.uuid4()),
        "owner_id": owner_id,
        "name": name,
        "category": category,
        "description": description,
        "is_archived": False,
        "deleted_at": None,
        "created_at": now,
        "updated_at": now,
    }
    async with clients.mongo_client().start_session() as session:
        async with await session.start_transaction():
            await clients.knowledge_bases().insert_one(doc, session=session)
            await outbox.add_event(session, "kb.created", "kb", doc["_id"], 0)
    return doc


async def update(kb_id: str, owner_id: str, fields: dict) -> None:
    await clients.knowledge_bases().update_one(
        {"_id": kb_id, "owner_id": owner_id, "deleted_at": None},
        {"$set": {**fields, "updated_at": _now()}},
    )


async def soft_delete_cascade(kb_id: str, owner_id: str) -> int:
    """单一事务内软删除知识库并级联软删除其全部笔记，逐笔记写删除事件；返回笔记数。

    知识库不属于该用户或已删除时抛出 KeyError，由端点转换为 404（事务自动中止）。
    """
    now = _now()
    async with clients.mongo_client().start_session() as session:
        async with await session.start_transaction():
            kb = await clients.knowledge_bases().find_one_and_update(
                {"_id": kb_id, "owner_id": owner_id, "deleted_at": None},
                {"$set": {"deleted_at": now, "updated_at": now}},
                session=session,
            )
            if kb is None:
                raise KeyError("kb_not_found")
            cursor = clients.notes().find(
                {"kb_id": kb_id, "owner_id": owner_id, "deleted_at": None},
                {"_id": 1, "content_version": 1},
                session=session,
            )
            affected = await cursor.to_list(length=10000)
            await clients.notes().update_many(
                {"kb_id": kb_id, "owner_id": owner_id, "deleted_at": None},
                {"$set": {"deleted_at": now, "updated_at": now}},
                session=session,
            )
            for note in affected:
                await outbox.add_event(
                    session, "note.deleted", "note", note["_id"], note.get("content_version", 0)
                )
            doc_cursor = clients.documents().find(
                {"kb_id": kb_id, "owner_id": owner_id, "deleted_at": None},
                {"_id": 1, "content_version": 1, "file_path": 1},
                session=session,
            )
            affected_docs = await doc_cursor.to_list(length=10000)
            await clients.documents().update_many(
                {"kb_id": kb_id, "owner_id": owner_id, "deleted_at": None},
                {"$set": {"deleted_at": now, "status": "deleted", "updated_at": now}},
                session=session,
            )
            for document in affected_docs:
                await outbox.add_event(
                    session,
                    "document.deleted",
                    "document",
                    document["_id"],
                    document.get("content_version", 0),
                    payload={"file_path": document.get("file_path")},
                )
            await outbox.add_event(session, "kb.deleted", "kb", kb_id, 0)
            return len(affected)


async def note_counts(owner_id: str) -> dict[str, int]:
    """按知识库聚合未删除笔记数（$group 聚合管道，§6.4）。"""
    pipeline = [
        {"$match": {"owner_id": owner_id, "deleted_at": None}},
        {"$group": {"_id": "$kb_id", "count": {"$sum": 1}}},
    ]
    cursor = await clients.notes().aggregate(pipeline)
    return {doc["_id"]: doc["count"] async for doc in cursor}


def public_view(doc: dict, note_count: int = 0) -> dict:
    return {
        "id": doc["_id"],
        "name": doc["name"],
        "category": doc.get("category", ""),
        "description": doc.get("description", ""),
        "is_archived": doc.get("is_archived", False),
        "note_count": note_count,
        "created_at": doc["created_at"].isoformat(),
        "updated_at": doc["updated_at"].isoformat(),
    }
