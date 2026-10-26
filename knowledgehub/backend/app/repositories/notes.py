import re
import uuid
from datetime import datetime, timezone

from app.core import clients
from app.core import outbox
from app.services import links


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def get_owned(note_id: str, owner_id: str, include_deleted: bool = False) -> dict | None:
    query: dict = {"_id": note_id, "owner_id": owner_id}
    if not include_deleted:
        query["deleted_at"] = None
    return await clients.notes().find_one(query)


async def list_for_owner(
    owner_id: str,
    kb_id: str | None = None,
    tag: str | None = None,
    q: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[dict], int]:
    query: dict = {"owner_id": owner_id, "deleted_at": None}
    if kb_id:
        query["kb_id"] = kb_id
    if tag:
        query["tags"] = tag
    if q:
        query["title"] = {"$regex": re.escape(q), "$options": "i"}

    coll = clients.notes()
    total = await coll.count_documents(query)
    cursor = (
        coll.find(query)
        .sort("updated_at", -1)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return await cursor.to_list(length=page_size), total


async def create(
    owner_id: str, kb_id: str, title: str, content_md: str, tags: list[str], properties: dict,
    source_document_id: str | None = None,
) -> dict:
    now = _now()
    note_id = str(uuid.uuid4())
    linked_note_ids = await links.resolve_linked_note_ids(kb_id, content_md, exclude_note_id=note_id)
    doc = {
        "_id": note_id,
        "owner_id": owner_id,
        "kb_id": kb_id,
        "title": title,
        "content_md": content_md,
        "tags": tags,
        "properties": properties,
        "linked_note_ids": linked_note_ids,
        "source_document_id": source_document_id,
        "content_version": 1,
        "index_status": "pending",
        "deleted_at": None,
        "created_at": now,
        "updated_at": now,
    }
    version_doc = {
        "_id": str(uuid.uuid4()),
        "note_id": note_id,
        "version": 1,
        "title": title,
        "content_md": content_md,
        "tags": tags,
        "changed_at": now,
    }
    async with clients.mongo_client().start_session() as session:
        async with await session.start_transaction():
            await clients.notes().insert_one(doc, session=session)
            await clients.note_versions().insert_one(version_doc, session=session)
            await outbox.add_event(session, "note.created", "note", note_id, 1)
    return doc


async def update_with_version(
    note_id: str, owner_id: str, updates: dict, expected_version: int
) -> dict:
    """事务内完成：乐观锁版本校验 → 更新笔记 → 写入版本快照。

    返回 {"result": "not_found"} | {"result": "conflict", "current_version": n} | {"result": "ok", "content_version": n}
    """
    now = _now()
    async with clients.mongo_client().start_session() as session:
        async with await session.start_transaction():
            current = await clients.notes().find_one(
                {"_id": note_id, "owner_id": owner_id, "deleted_at": None}, session=session
            )
            if current is None:
                return {"result": "not_found"}
            if current["content_version"] != expected_version:
                return {"result": "conflict", "current_version": current["content_version"]}

            if "content_md" in updates:
                updates["linked_note_ids"] = await links.resolve_linked_note_ids(
                    current["kb_id"], updates["content_md"], exclude_note_id=note_id
                )

            new_version = expected_version + 1
            await clients.notes().update_one(
                {"_id": note_id},
                {
                    "$set": {
                        **updates,
                        "content_version": new_version,
                        "index_status": "pending",
                        "updated_at": now,
                    }
                },
                session=session,
            )
            await clients.note_versions().insert_one(
                {
                    "_id": str(uuid.uuid4()),
                    "note_id": note_id,
                    "version": new_version,
                    "title": updates.get("title", current["title"]),
                    "content_md": updates.get("content_md", current["content_md"]),
                    "tags": updates.get("tags", current["tags"]),
                    "changed_at": now,
                },
                session=session,
            )
            await outbox.add_event(session, "note.updated", "note", note_id, new_version)
            return {"result": "ok", "content_version": new_version}


async def soft_delete(note_id: str, owner_id: str) -> bool:
    now = _now()
    async with clients.mongo_client().start_session() as session:
        async with await session.start_transaction():
            doc = await clients.notes().find_one_and_update(
                {"_id": note_id, "owner_id": owner_id, "deleted_at": None},
                {"$set": {"deleted_at": now, "updated_at": now}},
                session=session,
            )
            if doc is None:
                return False
            await outbox.add_event(session, "note.deleted", "note", note_id, doc["content_version"])
    return True


async def list_versions(note_id: str) -> list[dict]:
    cursor = clients.note_versions().find({"note_id": note_id}).sort("version", -1)
    return await cursor.to_list(length=1000)


def public_view(doc: dict) -> dict:
    return {
        "id": doc["_id"],
        "kb_id": doc["kb_id"],
        "title": doc["title"],
        "content_md": doc["content_md"],
        "tags": doc.get("tags", []),
        "properties": doc.get("properties", {}),
        "linked_note_ids": doc.get("linked_note_ids", []),
        "content_version": doc["content_version"],
        "index_status": doc.get("index_status", "pending"),
        "created_at": doc["created_at"].isoformat(),
        "updated_at": doc["updated_at"].isoformat(),
    }


def version_view(doc: dict) -> dict:
    return {
        "version": doc["version"],
        "title": doc["title"],
        "content_md": doc["content_md"],
        "tags": doc.get("tags", []),
        "changed_at": doc["changed_at"].isoformat(),
    }
