"""文档上传/状态/重索引/删除（§5.3）。

上传即入账：原件先落 GridFS（sha256 去重），元数据与 document.created 事件同事务写 Mongo；
流水线由 vector worker 异步消费（uploaded→parsing→indexing→ready/failed）。
"""

from fastapi import APIRouter, Depends, HTTPException, UploadFile, status

from app.api.deps import get_current_user
from app.core import clients
from app.core.config import settings
from app.repositories import documents as documents_repo
from app.repositories import knowledge_bases as kb_repo
from app.services.storage import storage

ALLOWED_TYPES = {"pdf", "docx", "pptx", "md", "txt"}

router = APIRouter(tags=["documents"])


def _sniff(content: bytes, file_type: str) -> bool:
    """魔数粗校验：拒绝伪装扩展名的二进制（§5.3 上传安全）。"""
    if file_type == "pdf":
        return content[:5] == b"%PDF-"
    if file_type in ("docx", "pptx"):  # 同为 OOXML zip 容器
        return content[:4] == b"PK\x03\x04"
    try:  # md/txt：可解码且控制字符占比低才视为文本
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = content.decode("gbk")
        except UnicodeDecodeError:
            return False
    if not text:
        return True
    controls = sum(1 for ch in text if ord(ch) < 32 and ch not in "\t\n\r\f\v")
    return controls / len(text) < 0.1


@router.post("/knowledge-bases/{kb_id}/documents", status_code=status.HTTP_201_CREATED)
async def upload_document(
    kb_id: str, file: UploadFile, user: dict = Depends(get_current_user)
) -> dict:
    if await kb_repo.get_owned(kb_id, user["id"]) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "knowledge base not found")

    name = file.filename or ""
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in ALLOWED_TYPES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            {"reason": "unsupported_file_type", "allowed": sorted(ALLOWED_TYPES)},
        )

    content = await file.read()
    max_bytes = settings.upload_max_mb * 1024 * 1024
    if len(content) == 0:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "empty file")
    if len(content) > max_bytes:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            {"reason": "file_too_large", "max_mb": settings.upload_max_mb},
        )
    if not _sniff(content, ext):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "file content does not match extension")

    sha256, key, _deduplicated = await storage().put(user["id"], kb_id, content, f".{ext}")
    try:
        doc = await documents_repo.create(
            owner_id=user["id"],
            kb_id=kb_id,
            file_name=name,
            file_type=ext,
            size_bytes=len(content),
            sha256=sha256,
            file_path=key,
        )
    except Exception:
        await storage().delete(key)  # 元数据入账失败，回收本次写入的原件
        raise
    return documents_repo.public_view(doc)


@router.get("/knowledge-bases/{kb_id}/documents")
async def list_documents(kb_id: str, user: dict = Depends(get_current_user)) -> dict:
    if await kb_repo.get_owned(kb_id, user["id"]) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "knowledge base not found")
    docs = await documents_repo.list_for_kb(kb_id, user["id"])
    return {"total": len(docs), "items": [documents_repo.public_view(d) for d in docs]}


@router.get("/documents/{document_id}")
async def document_status(document_id: str, user: dict = Depends(get_current_user)) -> dict:
    doc = await documents_repo.get_owned(document_id, user["id"], include_deleted=True)
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")
    return documents_repo.public_view(doc, include_draft=True)


@router.post("/documents/{document_id}/draft", status_code=status.HTTP_202_ACCEPTED)
async def generate_draft(document_id: str, user: dict = Depends(get_current_user)) -> dict:
    """触发笔记草稿生成（异步）：校验后发布事件，draft worker 消费执行。"""
    doc = await documents_repo.get_owned(document_id, user["id"])
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")
    if doc["status"] != "ready":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"reason": "document_not_ready", "status": doc["status"]},
        )
    chunk_count = await clients.chunks().count_documents({"document_id": document_id})
    if chunk_count == 0:
        raise HTTPException(status.HTTP_409_CONFLICT, {"reason": "no_extractable_text"})

    outcome = await documents_repo.request_draft(document_id, user["id"])
    if outcome is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")
    if outcome["result"] == "in_progress":
        raise HTTPException(status.HTTP_409_CONFLICT, {"reason": "draft_in_progress"})
    return {"result": "requested", "request_id": outcome["request_id"]}


@router.post("/documents/{document_id}/reindex")
async def reindex_document(document_id: str, user: dict = Depends(get_current_user)) -> dict:
    doc = await documents_repo.reindex(document_id, user["id"])
    if doc is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")
    return documents_repo.public_view(doc)


@router.delete("/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(document_id: str, user: dict = Depends(get_current_user)) -> None:
    deleted = await documents_repo.soft_delete(document_id, user["id"])
    if deleted is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "document not found")
