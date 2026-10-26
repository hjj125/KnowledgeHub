"""Milvus knowledge_chunks 仓储（§6.4 / §9.5）。

- pk = uuid5(f"{document_id}:{content_version}:{chunk_index}")，笔记与文档共用一个
  Collection，靠 source_type 区分——天然幂等，重复 upsert 覆盖旧向量；
- 标量列（owner_id/kb_id/document_id/source_type）建 INVERTED 索引供多租户过滤；
- HNSW(M=16, efConstruction=200) + COSINE 为 §9.5 初始配置；
- EMBEDDING_DIM 冻结（§9.8.3）：建表时即写入维度，任何变更等同重建 Collection。

pymilvus 的 MilvusClient 是同步客户端，统一用 to_thread 包裹，避免阻塞事件循环。
"""

import asyncio
import uuid
from functools import lru_cache
from typing import Any

from pymilvus import DataType, MilvusClient

from app.core.config import settings

HNSW_M = 16
HNSW_EF_CONSTRUCTION = 200
HNSW_EF_SEARCH = 128

SOURCE_NOTE = "note"
SOURCE_DOCUMENT = "document"


def chunk_pk(document_id: str, content_version: int, chunk_index: int) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{document_id}:{content_version}:{chunk_index}"))


@lru_cache(maxsize=1)
def _client() -> MilvusClient:
    return MilvusClient(uri=settings.milvus_uri)


class MilvusError(RuntimeError):
    pass


async def ensure_collection() -> bool:
    """确保 Collection 存在；返回 True 表示本次新建。"""

    def _ensure() -> bool:
        name = settings.milvus_collection
        client = _client()
        if client.has_collection(name):
            return False
        schema = MilvusClient.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field("pk", DataType.VARCHAR, is_primary=True, max_length=64)
        schema.add_field("dense", DataType.FLOAT_VECTOR, dim=settings.embedding_dim)
        schema.add_field("owner_id", DataType.VARCHAR, max_length=64)
        schema.add_field("kb_id", DataType.VARCHAR, max_length=64)
        schema.add_field("document_id", DataType.VARCHAR, max_length=64)
        schema.add_field("source_type", DataType.VARCHAR, max_length=16)
        schema.add_field("content_version", DataType.INT64)
        schema.add_field("page", DataType.INT64)

        index_params = client.prepare_index_params()
        index_params.add_index(
            field_name="dense",
            index_type="HNSW",
            metric_type="COSINE",
            params={"M": HNSW_M, "efConstruction": HNSW_EF_CONSTRUCTION},
        )
        for scalar in ("owner_id", "kb_id", "document_id", "source_type"):
            index_params.add_index(field_name=scalar, index_type="INVERTED")

        client.create_collection(collection_name=name, schema=schema, index_params=index_params)
        return True

    return await asyncio.to_thread(_ensure)


async def upsert_chunks(rows: list[dict[str, Any]]) -> int:
    """整批 upsert（pk 幂等）。rows 需含 pk/dense/owner_id/kb_id/document_id/source_type/content_version/page。"""
    if not rows:
        return 0

    def _upsert() -> int:
        result = _client().upsert(collection_name=settings.milvus_collection, data=rows)
        return int(result.get("upsert_count", len(rows)))

    return await asyncio.to_thread(_upsert)


async def delete_by_document(document_id: str) -> int:
    """删除某来源实体（笔记或文档）的全部向量（跨历史版本）。"""

    def _delete() -> int:
        result = _client().delete(
            collection_name=settings.milvus_collection,
            filter=f'document_id == "{document_id}"',
        )
        return int(result.get("delete_count", 0)) if isinstance(result, dict) else 0

    return await asyncio.to_thread(_delete)


async def search(
    query_vector: list[float],
    owner_id: str,
    kb_id: str | None = None,
    kb_ids: list[str] | None = None,
    source_type: str | None = None,
    top_k: int = 10,
) -> list[dict[str, Any]]:
    """语义 TopK。过滤条件全部走标量索引；返回按相似度降序的命中列表。"""
    conditions = [f'owner_id == "{owner_id}"']
    if kb_ids:
        quoted = ", ".join(f'"{k}"' for k in kb_ids)
        conditions.append(f"kb_id in [{quoted}]")
    elif kb_id:
        conditions.append(f'kb_id == "{kb_id}"')
    if source_type:
        conditions.append(f'source_type == "{source_type}"')
    expr = " and ".join(conditions)

    def _search() -> list[dict[str, Any]]:
        hits = _client().search(
            collection_name=settings.milvus_collection,
            data=[query_vector],
            filter=expr,
            limit=max(1, min(top_k, 50)),
            output_fields=["document_id", "kb_id", "source_type", "content_version", "page"],
            search_params={"params": {"ef": HNSW_EF_SEARCH}},
        )[0]
        results = []
        for hit in hits:
            # pymilvus 3.x 命中为 dict（主键键名跟随 Schema 的字段名 pk）；
            # 兼容旧版对象风格（.id/.distance 属性）。
            if isinstance(hit, dict):
                hit_id = hit.get("pk", hit.get("id"))
                distance = hit["distance"]
                entity = hit.get("entity", {})
            else:
                hit_id = getattr(hit, "pk", None) or hit.id
                distance = hit.distance
                entity = hit.entity if isinstance(hit.entity, dict) else {}
            results.append(
                {
                    "chunk_id": hit_id,
                    "score": float(distance),
                    "document_id": entity.get("document_id"),
                    "kb_id": entity.get("kb_id"),
                    "source_type": entity.get("source_type"),
                    "content_version": entity.get("content_version"),
                    "page": entity.get("page", 0),
                }
            )
        return results

    try:
        return await asyncio.to_thread(_search)
    except Exception as exc:  # 连接失败/集合未加载等统一上抛，由搜索 API 降级（§10.5）
        raise MilvusError(str(exc)) from exc


async def list_by_document(document_id: str) -> list[dict[str, Any]]:
    """查询某实体现存向量（reconciliation 与冒烟校验用）。"""

    def _query() -> list[dict[str, Any]]:
        return _client().query(
            collection_name=settings.milvus_collection,
            filter=f'document_id == "{document_id}"',
            output_fields=["pk", "content_version", "page"],
        )

    return await asyncio.to_thread(_query)
