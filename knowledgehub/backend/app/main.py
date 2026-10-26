from contextlib import asynccontextmanager

import logging

from fastapi import FastAPI
from pymongo import ASCENDING, DESCENDING

from app.api.v1.router import api_router
from app.core.clients import close_all_clients, db
from app.core.config import settings


async def ensure_indexes() -> None:
    """启动时幂等创建 §6.3 初始索引（唯一约束与查询覆盖）。"""
    database = db()
    await database.get_collection("users").create_index("email", unique=True)
    notes = database.get_collection("notes")
    await notes.create_index(
        [("owner_id", ASCENDING), ("kb_id", ASCENDING), ("deleted_at", ASCENDING), ("updated_at", DESCENDING)]
    )
    await notes.create_index([("owner_id", ASCENDING), ("tags", ASCENDING)])
    await notes.create_index([("kb_id", ASCENDING), ("deleted_at", ASCENDING)])
    await database.get_collection("knowledge_bases").create_index(
        [("owner_id", ASCENDING), ("deleted_at", ASCENDING)]
    )
    await database.get_collection("note_versions").create_index(
        [("note_id", ASCENDING), ("version", DESCENDING)]
    )
    await database.get_collection("outbox_events").create_index(
        [("status", ASCENDING), ("created_at", ASCENDING)]
    )
    await database.get_collection("indexing_jobs").create_index(
        [("entity_id", ASCENDING), ("job_type", ASCENDING)], unique=True
    )
    documents = database.get_collection("documents")
    await documents.create_index([("kb_id", ASCENDING), ("owner_id", ASCENDING), ("deleted_at", ASCENDING)])
    await documents.create_index([("owner_id", ASCENDING), ("status", ASCENDING)])
    await database.get_collection("chunks").create_index(
        [("document_id", ASCENDING), ("chunk_index", ASCENDING)], unique=True
    )
    await database.get_collection("qa_logs").create_index([("created_at", DESCENDING)])
    await database.get_collection("qa_logs").create_index(
        [("user_id", ASCENDING), ("created_at", DESCENDING)]
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    await ensure_indexes()
    try:  # Milvus 未就绪不阻塞 API 启动；worker 侧有 ensure-on-miss 兜底
        from app.repositories import milvus as milvus_repo

        await milvus_repo.ensure_collection()
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("startup").warning("milvus ensure_collection deferred: %s", exc)
    yield
    await close_all_clients()


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
app.include_router(api_router)


@app.get("/")
async def root():
    return {"name": settings.app_name, "docs": "/docs", "health": "/api/v1/system/health"}
