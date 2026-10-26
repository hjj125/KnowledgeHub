from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import get_current_user
from app.repositories import knowledge_bases as kb_repo
from app.schemas.kb import KnowledgeBaseCreate, KnowledgeBaseUpdate

router = APIRouter(prefix="/knowledge-bases", tags=["knowledge-bases"])


@router.get("")
async def list_knowledge_bases(user: dict = Depends(get_current_user)) -> list[dict]:
    kbs = await kb_repo.list_for_owner(user["id"])
    counts = await kb_repo.note_counts(user["id"])
    return [kb_repo.public_view(kb, counts.get(kb["_id"], 0)) for kb in kbs]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_knowledge_base(
    payload: KnowledgeBaseCreate, user: dict = Depends(get_current_user)
) -> dict:
    kb = await kb_repo.create(user["id"], payload.name, payload.category, payload.description)
    return kb_repo.public_view(kb)


@router.get("/{kb_id}")
async def get_knowledge_base(kb_id: str, user: dict = Depends(get_current_user)) -> dict:
    kb = await kb_repo.get_owned(kb_id, user["id"])
    if kb is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "knowledge base not found")
    counts = await kb_repo.note_counts(user["id"])
    return kb_repo.public_view(kb, counts.get(kb_id, 0))


@router.patch("/{kb_id}")
async def update_knowledge_base(
    kb_id: str, payload: KnowledgeBaseUpdate, user: dict = Depends(get_current_user)
) -> dict:
    kb = await kb_repo.get_owned(kb_id, user["id"])
    if kb is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "knowledge base not found")
    fields = payload.model_dump(exclude_none=True)
    if fields:
        await kb_repo.update(kb_id, user["id"], fields)
    updated = await kb_repo.get_owned(kb_id, user["id"])
    counts = await kb_repo.note_counts(user["id"])
    return kb_repo.public_view(updated, counts.get(kb_id, 0))


@router.delete("/{kb_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_knowledge_base(kb_id: str, user: dict = Depends(get_current_user)) -> None:
    try:
        await kb_repo.soft_delete_cascade(kb_id, user["id"])
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "knowledge base not found") from exc
