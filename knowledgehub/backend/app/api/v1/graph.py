"""知识图谱查询 API（§5.5 / §11）：全部由 Neo4j 提供关系数据。"""

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.deps import get_current_user
from app.repositories import graph as graph_repo
from app.repositories import knowledge_bases as kb_repo
from app.repositories import notes as notes_repo

router = APIRouter(prefix="/graph", tags=["graph"])


@router.get("/{kb_id}")
async def knowledge_base_graph(kb_id: str, user: dict = Depends(get_current_user)) -> dict:
    if await kb_repo.get_owned(kb_id, user["id"]) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "knowledge base not found")
    return await graph_repo.kb_graph(kb_id, user["id"])


@router.get("/notes/{note_id}/neighbors")
async def note_neighbors(
    note_id: str,
    depth: int = Query(default=1, ge=1, le=3),
    user: dict = Depends(get_current_user),
) -> dict:
    if await notes_repo.get_owned(note_id, user["id"]) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "note not found")
    return await graph_repo.neighbors(note_id, user["id"], depth)
