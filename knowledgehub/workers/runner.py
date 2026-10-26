"""Redis Streams 消费者框架（§7.3 / §10.3）。

- 每个业务事件 Stream `stream:knowledge_events` 上两个独立消费组：graph_indexer / vector_indexer；
- XREADGROUP 取新消息，处理成功后 XACK；
- 空闲时 XAUTOCLAIM 认领闲置超过 CLAIM_MIN_IDLE_MS 的 pending 消息（Worker 崩溃恢复）；
- 至少一次交付：处理必须幂等 —— 以 MongoDB 当前状态为准 + indexing_jobs 的
  (entity_id, job_type) 唯一记录 indexed_version，旧版本/重复事件直接跳过；
- 连续失败超过 MAX_JOB_ATTEMPTS 的消息标记 dead 并 XACK（死信），避免毒消息堵死队列。
"""

import asyncio
import json
import logging
import os
import socket
from datetime import datetime, timezone
from functools import lru_cache

import redis.asyncio as aioredis
from pymongo import ReturnDocument

from app.core import clients
from app.core.config import settings
from app.repositories import graph as graph_repo
from app.services.storage import storage as storage_backend
from workers import draft_sync, vector_sync

STREAM = "stream:knowledge_events"
MAX_JOB_ATTEMPTS = 5
CLAIM_MIN_IDLE_MS = 10_000
READ_BLOCK_MS = 5_000
READ_COUNT = 10

log = logging.getLogger("worker")


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def ensure_group(redis: aioredis.Redis, group: str) -> None:
    try:
        await redis.xgroup_create(STREAM, group, id="0-0", mkstream=True)
    except aioredis.ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


def consumer_name(group: str) -> str:
    return f"{group}.{socket.gethostname()}.{os.getpid()}"


@lru_cache(maxsize=1)
def _worker_redis() -> aioredis.Redis:
    """Worker 专用连接：XREADGROUP 长轮询必须显式关闭 socket 超时
    （redis-py 8.x 默认 socket_timeout=5s，与 block=5s 竞争导致必然超时）。"""
    return aioredis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_timeout=None,
        health_check_interval=30,
    )


async def _record_job(entity_id: str, job_type: str, **fields) -> None:
    if fields.get("status") == "succeeded":
        # 成功即清零：attempts 语义 = 连续失败次数（死信判定依据），避免历史失败拖累后续重试
        fields["attempts"] = 0
    await clients.indexing_jobs().update_one(
        {"entity_id": entity_id, "job_type": job_type},
        {"$set": {**fields, "updated_at": _now()}, "$setOnInsert": {"created_at": _now()}},
        upsert=True,
    )


async def process_event(job_type: str, fields: dict) -> str:
    """幂等处理单条事件；抛出异常表示本次处理失败（消息不 XACK，等待重试）。"""
    event_id = fields["event_id"]
    entity_type = fields["entity_type"]
    entity_id = fields["entity_id"]
    event_version = int(fields.get("entity_version", "0"))
    jobs = clients.indexing_jobs()
    existing = await jobs.find_one({"entity_id": entity_id, "job_type": job_type})

    if entity_type == "note":
        note = await clients.notes().find_one({"_id": entity_id})
        if note is None:
            # 实体已不存在（数据库被清理等）：按成功处理，避免无限重试
            await _record_job(
                entity_id, job_type,
                status="succeeded", indexed_version=event_version,
                attempts=(existing or {}).get("attempts", 0) + 1,
                last_error=f"entity missing (event {event_id})",
            )
            return "processed:entity-missing"

        current_version = note["content_version"]
        note_deleted = note.get("deleted_at") is not None
        # 跳过条件必须同时校验版本与删除状态：软删除不改变 content_version，
        # 若只比版本，删除事件会被误判为"已最新"而吞掉。
        if (
            existing is not None
            and existing.get("status") == "succeeded"
            and existing.get("indexed_version", -1) >= current_version
            and bool(existing.get("entity_deleted", False)) == note_deleted
        ):
            return f"skipped:stale(job_v={existing.get('indexed_version')},current_v={current_version})"

        # —— 派生索引写入点（按任务类型派发）——
        # graph：Neo4j 节点 MERGE + 出边整体替换（§8.2）；vector：分块→嵌入→Milvus（§9.5）
        if job_type not in ("graph", "vector"):
            return f"skipped:job-type-not-applicable({job_type})"
        if job_type == "graph":
            sync_result = await graph_repo.sync_note(entity_id)
        elif job_type == "vector":
            sync_result = await vector_sync.sync_note_vectors(note)
        else:
            sync_result = f"unknown-job-type:{job_type}"

        new_index_status = "deleted" if note_deleted else "indexed"
        await clients.notes().update_one(
            {"_id": entity_id}, {"$set": {"index_status": new_index_status}}
        )
        await _record_job(
            entity_id, job_type,
            status="succeeded", indexed_version=current_version, entity_deleted=note_deleted,
            attempts=(existing or {}).get("attempts", 0) + 1,
            last_error=None,
        )
        return f"processed:v{current_version}({event_type_of(fields)},{sync_result})"

    # document 事件：graph 无对应图实体，直接记完成；vector 走文档流水线（解析→分块→嵌入→Milvus）；
    # draft 只响应用户显式触发的 document.draft_requested（幂等键为 payload 的 request_id）——
    # 其余 document 事件类型（created/updated）必须快速跳过，否则消费组回放历史时会给每个旧文档白烧一次 LLM
    if entity_type == "document":
        if job_type == "draft":
            if fields.get("event_type") != "document.draft_requested":
                return "skipped:not-draft-request"
            document = await clients.documents().find_one({"_id": entity_id})
            if document is None or document.get("deleted_at") is not None:
                await _record_job(
                    entity_id, job_type,
                    status="succeeded", indexed_version=event_version,
                    attempts=(existing or {}).get("attempts", 0) + 1,
                    last_error=None,
                )
                return "processed:draft,deleted-or-missing"
            try:
                payload = json.loads(fields.get("payload") or "{}")
                request_id = str(payload.get("request_id") or "")
            except ValueError:
                request_id = ""
            draft = document.get("note_draft") or {}
            if (
                request_id
                and document.get("draft_status") == "ready"
                and draft.get("request_id") == request_id
            ):
                return "skipped:duplicate-request"
            sync_result = await draft_sync.process_draft(document, request_id)
            await _record_job(
                entity_id, job_type,
                status="succeeded", indexed_version=document["content_version"],
                attempts=(existing or {}).get("attempts", 0) + 1,
                last_error=None,
            )
            return f"processed:draft({sync_result})"

        if job_type == "graph":
            # 文档节点进图谱（HAS_DOCUMENT / DERIVED_FROM），幂等 MERGE
            graph_result = await graph_repo.sync_document(entity_id)
            await _record_job(
                entity_id, job_type,
                status="succeeded", indexed_version=current_version,
                attempts=(existing or {}).get("attempts", 0) + 1,
                last_error=None,
            )
            return f"processed:{fields.get('event_type')},{graph_result}"
        if job_type != "vector":
            if existing is not None and existing.get("status") == "succeeded":
                return "skipped:not-applicable"
            await _record_job(
                entity_id, job_type,
                status="succeeded", indexed_version=event_version,
                attempts=(existing or {}).get("attempts", 0) + 1,
                last_error=None,
            )
            return f"processed:{fields.get('event_type')},not-applicable(graph)"

        document = await clients.documents().find_one({"_id": entity_id})
        if document is None:
            await _record_job(
                entity_id, job_type,
                status="succeeded", indexed_version=event_version,
                attempts=(existing or {}).get("attempts", 0) + 1,
                last_error=f"document missing (event {event_id})",
            )
            return "processed:entity-missing"

        document_deleted = document.get("deleted_at") is not None
        current_version = document["content_version"]
        if (
            existing is not None
            and existing.get("status") == "succeeded"
            and existing.get("indexed_version", -1) >= current_version
            and bool(existing.get("entity_deleted", False)) == document_deleted
        ):
            return f"skipped:stale(job_v={existing.get('indexed_version')},current_v={current_version})"

        if document_deleted:
            file_path = document.get("file_path") or ""
            try:
                payload = json.loads(fields.get("payload") or "{}")
                file_path = payload.get("file_path") or file_path
            except ValueError:
                pass
            await vector_sync.remove_entity_vectors(entity_id)
            if file_path:
                await storage_backend().delete(file_path)
            await _record_job(
                entity_id, job_type,
                status="succeeded", indexed_version=current_version, entity_deleted=True,
                attempts=(existing or {}).get("attempts", 0) + 1,
                last_error=None,
            )
            return f"processed:document.deleted,vectors-removed"

        sync_result = await vector_sync.process_document(document)
        await _record_job(
            entity_id, job_type,
            status="succeeded", indexed_version=current_version, entity_deleted=False,
            attempts=(existing or {}).get("attempts", 0) + 1,
            last_error=None,
        )
        return f"processed:v{current_version}({fields.get('event_type')},{sync_result})"

    # kb 事件：graph → KnowledgeBase 节点 MERGE/删除；vector 无向量实体，直接记完成
    if job_type == "graph":
        kb_result = await graph_repo.sync_kb(entity_id)
        if existing is not None and existing.get("status") == "succeeded" and kb_result == "synced":
            return "skipped:done"
        await _record_job(
            entity_id, job_type,
            status="succeeded", indexed_version=event_version,
            attempts=(existing or {}).get("attempts", 0) + 1,
            last_error=None,
        )
        return f"processed:{fields.get('event_type')},{kb_result}"
    if existing is not None and existing.get("status") == "succeeded":
        return "skipped:done"
    await _record_job(
        entity_id, job_type,
        status="succeeded", indexed_version=event_version,
        attempts=(existing or {}).get("attempts", 0) + 1,
        last_error=None,
    )
    return f"processed:{fields.get('event_type')},vector-stub(stage4)"


def event_type_of(fields: dict) -> str:
    return fields.get("event_type", "?")


async def _handle_message(redis: aioredis.Redis, group: str, job_type: str, message_id: str, fields: dict) -> None:
    try:
        result = await process_event(job_type, fields)
        await redis.xack(STREAM, group, message_id)
        log.info("[%s] %s %s -> %s", group, message_id, fields.get("event_id", "?"), result)
    except Exception as exc:  # noqa: BLE001 — 失败路径要落入任务记录
        jobs = clients.indexing_jobs()
        doc = await jobs.find_one_and_update(
            {"entity_id": fields.get("entity_id", ""), "job_type": job_type},
            {"$inc": {"attempts": 1}, "$set": {"status": "failed", "last_error": str(exc)[:300], "updated_at": _now()}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        attempts = (doc or {}).get("attempts", 1)
        if attempts >= MAX_JOB_ATTEMPTS:
            await jobs.update_one(
                {"entity_id": fields.get("entity_id", ""), "job_type": job_type},
                {"$set": {"status": "dead", "updated_at": _now()}},
            )
            await redis.xack(STREAM, group, message_id)
            log.error("[%s] %s dead after %s attempts: %s", group, message_id, attempts, exc)
        else:
            log.warning("[%s] %s attempt %s failed: %s", group, message_id, attempts, exc)


async def run_worker(job_type: str, group: str) -> None:
    redis = _worker_redis()
    await ensure_group(redis, group)
    consumer = consumer_name(group)
    log.info("worker started: group=%s consumer=%s", group, consumer)

    while True:
        try:
            resp = await redis.xreadgroup(group, consumer, {STREAM: ">"}, count=READ_COUNT, block=READ_BLOCK_MS)
            messages = resp[0][1] if resp else []
            if not messages:
                # 无新消息：尝试认领闲置的 pending 消息（崩溃恢复路径）
                claimed = await redis.xautoclaim(
                    STREAM, group, consumer, min_idle_time=CLAIM_MIN_IDLE_MS, start_id="0-0", count=READ_COUNT
                )
                messages = claimed[1] if claimed and len(claimed) > 1 else []
                if messages:
                    log.info("[%s] reclaimed %d pending message(s)", group, len(messages))

            for message_id, fields in messages:
                await _handle_message(redis, group, job_type, message_id, fields)
        except aioredis.ResponseError as exc:
            if "NOGROUP" in str(exc):
                await ensure_group(redis, group)
                continue
            log.warning("[%s] redis error, retrying in 2s: %s", group, exc)
            await asyncio.sleep(2)
        except (aioredis.RedisError, OSError) as exc:
            # §10.5：Redis 不可用 → Worker 挂起等待恢复，不退出
            log.warning("[%s] redis unavailable, retrying in 2s: %s", group, exc)
            await asyncio.sleep(2)
