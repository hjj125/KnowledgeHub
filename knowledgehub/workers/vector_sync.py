"""向量索引流水线（阶段 4）：笔记与文档共用一套"分块→嵌入→Milvus"写入逻辑。

- 笔记：content_md 分块（page=0），随 note.updated 事件触发；
- 文档：GridFS 原件 → 解析（带页码）→ 分块 → 状态机 uploaded→parsing→indexing→ready；
- MongoDB chunks 集合是分块文本的权威存储（搜索回填与对账依据），
  Milvus 只存向量 + 过滤标量；
- 向量替换采用"先删 document_id 全部历史版本、再 upsert 当前版本"，天然幂等。
"""

import logging

from app.core import clients
from app.repositories import documents as documents_repo
from app.repositories import milvus as milvus_repo
from app.services import chunking, parsing
from app.services.embedding import get_embedding_client
from app.services.storage import storage

log = logging.getLogger("worker.vector")


async def _replace_chunk_rows(document_id: str, rows: list[dict]) -> None:
    """整文档替换 Mongo 分块行（事务内删旧插新，维持 (document_id, chunk_index) 唯一约束）。"""
    async with clients.mongo_client().start_session() as session:
        async with await session.start_transaction():
            await clients.chunks().delete_many({"document_id": document_id}, session=session)
            if rows:
                await clients.chunks().insert_many(rows, session=session)


async def _upsert_with_ensure(rows: list[dict]) -> None:
    """集合缺失（如 Milvus 晚于 Worker 启动）时自动建表重试一次。"""
    try:
        await milvus_repo.upsert_chunks(rows)
    except Exception:
        await milvus_repo.ensure_collection()
        await milvus_repo.upsert_chunks(rows)


def _build_rows(
    document_id: str,
    owner_id: str,
    kb_id: str,
    source_type: str,
    content_version: int,
    chunks: list[tuple[int, str, int]],
    vectors: list[list[float]],
) -> list[dict]:
    rows = []
    for (index, text, page), vector in zip(chunks, vectors):
        rows.append(
            {
                "pk": milvus_repo.chunk_pk(document_id, content_version, index),
                "dense": vector,
                "owner_id": owner_id,
                "kb_id": kb_id,
                "document_id": document_id,
                "source_type": source_type,
                "content_version": content_version,
                "page": page,
            }
        )
    return rows


def _build_chunk_rows(
    document_id: str,
    owner_id: str,
    kb_id: str,
    source_type: str,
    content_version: int,
    chunks: list[tuple[int, str, int]],
) -> list[dict]:
    return [
        {
            "_id": milvus_repo.chunk_pk(document_id, content_version, index),
            "document_id": document_id,
            "owner_id": owner_id,
            "kb_id": kb_id,
            "source_type": source_type,
            "chunk_index": index,
            "content": text,
            "page": page,
            "content_version": content_version,
        }
        for index, text, page in chunks
    ]


async def sync_note_vectors(note: dict) -> str:
    """笔记向量同步；软删除笔记等价于向量清除。返回动作描述（幂等）。"""
    note_id = note["_id"]
    if note.get("deleted_at") is not None:
        await remove_entity_vectors(note_id)
        return "vectors-removed"

    content = note.get("content_md") or ""
    texts = chunking.chunk_text(content)
    version = note["content_version"]
    await _replace_chunk_rows(
        note_id,
        _build_chunk_rows(
            note_id, note["owner_id"], note["kb_id"], milvus_repo.SOURCE_NOTE, version,
            [(i, t, 0) for i, t in enumerate(texts)],
        ),
    )
    if not texts:
        await milvus_repo.delete_by_document(note_id)
        return "vectors-cleared(empty)"

    vectors = await get_embedding_client().embed_texts(texts)
    chunks = [(i, t, 0) for i, t in enumerate(texts)]
    rows = _build_rows(
        note_id, note["owner_id"], note["kb_id"], milvus_repo.SOURCE_NOTE, version, chunks, vectors
    )
    await milvus_repo.delete_by_document(note_id)  # 清掉旧版本残留
    await _upsert_with_ensure(rows)
    return f"vectors-upserted(n={len(rows)})"


async def remove_entity_vectors(document_id: str) -> None:
    """删除某实体的全部向量与 Mongo 分块行（笔记软删 / 文档删除共用）。"""
    await _replace_chunk_rows(document_id, [])
    try:
        await milvus_repo.delete_by_document(document_id)
    except Exception:
        await milvus_repo.ensure_collection()
        await milvus_repo.delete_by_document(document_id)


async def process_document(document: dict) -> str:
    """文档流水线：解析→分块→嵌入→Milvus，状态机落库；失败标记 failed 并上抛重试。"""
    doc_id = document["_id"]
    version = document["content_version"]
    try:
        await documents_repo.mark_status(doc_id, "parsing")
        content = await storage().get(document["file_path"])
        pages = parsing.parse_content(content, document["file_type"])

        chunks: list[tuple[int, str, int]] = []
        for text, page in pages:
            for piece in chunking.chunk_text(text):
                chunks.append((len(chunks), piece, page))

        await documents_repo.mark_status(doc_id, "indexing")
        await _replace_chunk_rows(
            doc_id,
            _build_chunk_rows(
                doc_id, document["owner_id"], document["kb_id"],
                milvus_repo.SOURCE_DOCUMENT, version, chunks,
            ),
        )

        if not chunks:
            await milvus_repo.delete_by_document(doc_id)
            await documents_repo.mark_status(doc_id, "ready", indexed_version=version)
            return "ready(empty-document)"

        vectors = await get_embedding_client().embed_texts([t for _, t, _ in chunks])
        rows = _build_rows(
            doc_id, document["owner_id"], document["kb_id"],
            milvus_repo.SOURCE_DOCUMENT, version, chunks, vectors,
        )
        await milvus_repo.delete_by_document(doc_id)
        await _upsert_with_ensure(rows)
        await documents_repo.mark_status(doc_id, "ready", indexed_version=version)
        return f"ready(n={len(rows)})"
    except Exception as exc:
        await documents_repo.mark_status(doc_id, "failed", error=str(exc)[:300])
        raise
