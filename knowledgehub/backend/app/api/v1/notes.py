from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from app.api.deps import get_current_user
from app.repositories import graph as graph_repo
from app.repositories import knowledge_bases as kb_repo
from app.repositories import notes as notes_repo
from app.schemas.note import NoteCreate, NoteUpdate
from app.services import cache

router = APIRouter(prefix="/notes", tags=["notes"])


@router.get("")
async def list_notes(
    kb_id: str | None = None,
    tag: str | None = None,
    q: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    user: dict = Depends(get_current_user),
) -> dict:
    if kb_id is not None and await kb_repo.get_owned(kb_id, user["id"]) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "knowledge base not found")
    items, total = await notes_repo.list_for_owner(
        user["id"], kb_id=kb_id, tag=tag, q=q, page=page, page_size=page_size
    )
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [notes_repo.public_view(doc) for doc in items],
    }


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_note(payload: NoteCreate, user: dict = Depends(get_current_user)) -> dict:
    if await kb_repo.get_owned(payload.kb_id, user["id"]) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "knowledge base not found")
    doc = await notes_repo.create(
        user["id"], payload.kb_id, payload.title, payload.content_md, payload.tags, payload.properties,
        source_document_id=payload.source_document_id,
    )
    return notes_repo.public_view(doc)


@router.get("/{note_id}")
async def get_note(
    note_id: str, response: Response, user: dict = Depends(get_current_user)
) -> dict:
    cached = await cache.get_note(user["id"], note_id)
    if cached is not None:
        response.headers["X-Cache"] = "HIT"
        return cached

    doc = await notes_repo.get_owned(note_id, user["id"])
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "note not found")
    view = notes_repo.public_view(doc)
    await cache.set_note(user["id"], note_id, view)
    response.headers["X-Cache"] = "MISS"
    return view


@router.patch("/{note_id}")
async def update_note(
    note_id: str, payload: NoteUpdate, user: dict = Depends(get_current_user)
) -> dict:
    updates = payload.model_dump(exclude_none=True)
    expected = updates.pop("expected_content_version")
    if not updates:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "no fields to update")

    outcome = await notes_repo.update_with_version(note_id, user["id"], updates, expected)
    if outcome["result"] == "not_found":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "note not found")
    if outcome["result"] == "conflict":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {
                "reason": "content_version_conflict",
                "expected_content_version": expected,
                "current_content_version": outcome["current_version"],
            },
        )

    await cache.invalidate_note(user["id"], note_id)
    doc = await notes_repo.get_owned(note_id, user["id"])
    return notes_repo.public_view(doc)


@router.delete("/{note_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_note(note_id: str, user: dict = Depends(get_current_user)) -> None:
    deleted = await notes_repo.soft_delete(note_id, user["id"])
    if not deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "note not found")
    await cache.invalidate_note(user["id"], note_id)


@router.get("/{note_id}/backlinks")
async def note_backlinks(note_id: str, user: dict = Depends(get_current_user)) -> list[dict]:
    """反向链接：由 Neo4j 图查询提供（§5.5 图谱必须来自实际图数据库）。"""
    if await notes_repo.get_owned(note_id, user["id"], include_deleted=True) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "note not found")
    return await graph_repo.backlinks(note_id, user["id"])


@router.get("/{note_id}/versions")
async def list_note_versions(
    note_id: str, user: dict = Depends(get_current_user)
) -> list[dict]:
    if await notes_repo.get_owned(note_id, user["id"], include_deleted=True) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "note not found")
    docs = await notes_repo.list_versions(note_id)
    return [notes_repo.version_view(doc) for doc in docs]
