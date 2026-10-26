from functools import lru_cache
from typing import Any

from neo4j import AsyncGraphDatabase
from pymongo import AsyncMongoClient
from redis.asyncio import Redis

from app.core.config import settings


@lru_cache(maxsize=1)
def mongo_client() -> AsyncMongoClient:
    return AsyncMongoClient(
        settings.mongo_uri,
        serverSelectionTimeoutMS=3000,
        tz_aware=True,
        appname="knowledgehub-api",
    )


@lru_cache(maxsize=1)
def db() -> Any:
    return mongo_client().get_database(settings.mongo_db)


def users() -> Any:
    return db().get_collection("users")


def knowledge_bases() -> Any:
    return db().get_collection("knowledge_bases")


def notes() -> Any:
    return db().get_collection("notes")


def note_versions() -> Any:
    return db().get_collection("note_versions")


def outbox_events() -> Any:
    return db().get_collection("outbox_events")


def indexing_jobs() -> Any:
    return db().get_collection("indexing_jobs")


def documents() -> Any:
    return db().get_collection("documents")


def chunks() -> Any:
    return db().get_collection("chunks")


@lru_cache(maxsize=1)
def redis_client() -> Redis:
    return Redis.from_url(
        settings.redis_url,
        socket_connect_timeout=3,
        socket_timeout=3,
        decode_responses=True,
    )


@lru_cache(maxsize=1)
def neo4j_driver() -> Any:
    return AsyncGraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password),
    )


async def close_all_clients() -> None:
    await mongo_client().close()
    await redis_client().aclose()
    await neo4j_driver().close()
