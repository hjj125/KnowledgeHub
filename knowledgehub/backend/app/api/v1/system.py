import asyncio
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from app.core import clients
from app.core.config import settings
from app.services import health

router = APIRouter(prefix="/system", tags=["system"])

_CHECK_TIMEOUT_SECONDS = 10.0

STREAM = "stream:knowledge_events"


class ServiceStatus(BaseModel):
    status: str
    detail: str | None = None


class HealthResponse(BaseModel):
    status: str
    services: dict[str, ServiceStatus]


@router.get("/health", response_model=HealthResponse)
async def system_health() -> HealthResponse:
    checks = [
        health.check_mongodb(),
        health.check_redis(),
        health.check_neo4j(),
        health.check_milvus(),
    ]
    results = await asyncio.gather(
        *(asyncio.wait_for(c, timeout=_CHECK_TIMEOUT_SECONDS) for c in checks),
        return_exceptions=True,
    )

    services: dict[str, ServiceStatus] = {}
    for name, result in zip(("mongodb", "redis", "neo4j", "milvus"), results):
        if isinstance(result, BaseException):
            services[name] = ServiceStatus(status="down", detail=_brief(result))
        else:
            services[name] = ServiceStatus(**result)

    overall = "ok" if all(s.status == "up" for s in services.values()) else "degraded"
    return HealthResponse(status=overall, services=services)


def _brief(exc: BaseException) -> str:
    text = str(exc).splitlines()[0].strip() if str(exc) else ""
    return text[:160] or exc.__class__.__name__


async def _safe(value: Any, default: Any) -> Any:
    try:
        return await value if asyncio.iscoroutine(value) else value
    except Exception:  # noqa: BLE001 — 指标端点不因单库故障而 500
        return default


@router.get("/metrics")
async def system_metrics() -> dict:
    """聚合监控指标（§16.1）。仅返回全局聚合值，不含用户内容；
    课程项目无管理员体系，部署时应置于内网/网关鉴权之后。"""
    db = clients.db()
    counts = {}
    for name in ("users", "knowledge_bases", "notes", "documents", "chunks", "note_versions"):
        counts[name] = await _safe(db.get_collection(name).count_documents({}), None)
    outbox_pending = await _safe(db.get_collection("outbox_events").count_documents(
        {"status": "pending"}), None)
    job_status = {}
    try:
        cursor = await db.get_collection("indexing_jobs").aggregate(
            [{"$group": {"_id": "$status", "count": {"$sum": 1}}}]
        )
        job_status = {doc["_id"] or "unknown": doc["count"] async for doc in cursor}
    except Exception:  # noqa: BLE001
        job_status = {}
    qa_24h = await _safe(db.get_collection("qa_logs").count_documents({
        "created_at": {"$gte": datetime.now(timezone.utc).fromtimestamp(
            datetime.now(timezone.utc).timestamp() - 86400, tz=timezone.utc)}}), None)

    stream_len = await _safe(clients.redis_client().xlen(STREAM), None)
    pel = {}
    for group in ("graph_indexer", "vector_indexer"):
        info = await _safe(clients.redis_client().xpending(STREAM, group), None)
        pel[group] = info.get("pending") if isinstance(info, dict) else None

    graph_counts = {"notes": None, "links": None}
    try:
        from app.core.clients import neo4j_driver

        async with neo4j_driver().session() as session:
            result = await session.run("MATCH (n:Note) RETURN count(n) AS c")
            rec = await result.single()
            graph_counts["notes"] = rec["c"] if rec else None
            result = await session.run("MATCH ()-[r:LINKS_TO]->() RETURN count(r) AS c")
            rec = await result.single()
            graph_counts["links"] = rec["c"] if rec else None
    except Exception:  # noqa: BLE001
        pass

    milvus_entities = None
    try:
        from pymilvus import MilvusClient

        stats_doc = MilvusClient(uri=settings.milvus_uri).get_collection_stats(
            collection_name=settings.milvus_collection)
        milvus_entities = stats_doc.get("row_count")
    except Exception:  # noqa: BLE001
        pass

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mongodb": {"counts": counts, "outbox_pending": outbox_pending, "job_status": job_status},
        "redis": {"stream_length": stream_len, "pending_messages": pel},
        "neo4j": graph_counts,
        "milvus": {"collection": settings.milvus_collection, "entities": milvus_entities},
        "qa": {"qa_logs_24h": qa_24h},
    }
