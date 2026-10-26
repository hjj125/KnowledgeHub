import asyncio
from typing import Any

from pymilvus import MilvusClient

from app.core.clients import mongo_client, neo4j_driver, redis_client
from app.core.config import settings


async def check_mongodb() -> dict[str, Any]:
    try:
        await mongo_client().admin.command("ping")
        return {"status": "up", "detail": "replica set ping ok"}
    except Exception as exc:
        return {"status": "down", "detail": _brief(exc)}


async def check_redis() -> dict[str, Any]:
    try:
        pong = await redis_client().ping()
        return {"status": "up" if pong else "down", "detail": "PONG" if pong else "no response"}
    except Exception as exc:
        return {"status": "down", "detail": _brief(exc)}


async def check_neo4j() -> dict[str, Any]:
    try:
        await neo4j_driver().verify_connectivity()
        return {"status": "up", "detail": "bolt connectivity ok"}
    except Exception as exc:
        return {"status": "down", "detail": _brief(exc)}


async def check_milvus() -> dict[str, Any]:
    def _probe() -> str:
        client = MilvusClient(uri=settings.milvus_uri, timeout=5)
        try:
            return f"server {client.get_server_version()}"
        finally:
            client.close()

    try:
        detail = await asyncio.to_thread(_probe)
        return {"status": "up", "detail": detail}
    except Exception as exc:
        return {"status": "down", "detail": _brief(exc)}


def _brief(exc: Exception) -> str:
    text = str(exc).splitlines()[0].strip() if str(exc) else ""
    return text[:160] or exc.__class__.__name__
