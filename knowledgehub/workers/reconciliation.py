"""对账与恢复工具（§16.2）：比对权威数据与派生索引的版本差异，重新发布缺失任务。

    backend/.venv/Scripts/python -m workers.reconciliation [--kb-id <id>] [--dry-run] [--batch 200]

检查逻辑（按批次扫描，控制速率）：
- 笔记：MongoDB 当前 content_version / deleted_at vs indexing_jobs(graph/vector) 的
  indexed_version / entity_deleted —— 缺记录、版本落后、删除状态不一致均视为失同步；
- 文档：同上（仅 vector 任务有实际工作，graph 记 not-applicable）；
- 失同步实体重新发布事件：写 Outbox（留痕）并立即 XADD（与发布器同语义），
  Worker 端幂等保证重复发布无副作用。

运行说明：可与后端/Worker 并行运行；建议在 Worker 崩溃恢复或长时间告警后执行。
"""

import argparse
import asyncio
import json
import logging
from datetime import datetime, timezone

from app.core import clients
from app.core.config import settings
from workers.runner import STREAM, ensure_group

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("reconciliation")


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _republish(event_type: str, entity_type: str, entity_id: str, version: int,
                     payload: dict | None = None, dry_run: bool = False) -> None:
    if dry_run:
        log.info("[dry-run] would republish %s %s v%s", entity_type, entity_id, version)
        return
    async with clients.mongo_client().start_session() as session:
        async with await session.start_transaction():
            doc = {
                "_id": __import__("uuid").uuid4().hex,
                "event_type": event_type,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "entity_version": version,
                "payload": payload or {},
                "status": "pending",
                "attempts": 0,
                "last_error": None,
                "created_at": _now(),
                "published_at": None,
            }
            await clients.outbox_events().insert_one(doc, session=session)
        # 事务外直接投递（与 event_publisher 同语义）；重复消费由 Worker 幂等吸收
        fields = {"event_id": doc["_id"], "event_type": event_type, "entity_type": entity_type,
                  "entity_id": entity_id, "entity_version": str(version)}
        if payload:
            fields["payload"] = json.dumps(payload, ensure_ascii=False)
        await clients.redis_client().xadd(STREAM, fields)
        await clients.outbox_events().update_one(
            {"_id": doc["_id"]}, {"$set": {"status": "published", "published_at": _now()}}
        )


async def reconcile_notes(kb_id: str | None, batch: int, dry_run: bool) -> tuple[int, int]:
    query: dict = {"kb_id": {"$ne": None}} if kb_id is None else {"kb_id": kb_id}
    cursor = clients.notes().find(query, {"kb_id": 1, "content_version": 1, "deleted_at": 1})
    total = desynced = 0
    jobs = clients.indexing_jobs()
    notes: list[dict] = []
    async for note in cursor:
        notes.append(note)
        if len(notes) >= batch:
            d = await _check_note_batch(notes, jobs, dry_run)
            total += len(notes)
            desynced += d
            notes = []
            await asyncio.sleep(0.05)  # 限速，避免冲击在线业务
    if notes:
        d = await _check_note_batch(notes, jobs, dry_run)
        total += len(notes)
        desynced += d
    return total, desynced


async def _check_note_batch(notes: list[dict], jobs, dry_run: bool) -> int:
    ids = [n["_id"] for n in notes]
    records = {r["entity_id"]: r async for r in
               jobs.find({"entity_id": {"$in": ids}, "job_type": {"$in": ["graph", "vector"]}})}
    desynced = 0
    for note in notes:
        version = note.get("content_version", 0)
        deleted = note.get("deleted_at") is not None
        for job_type in ("graph", "vector"):
            record = records.get(note["_id"])
            ok = (
                record is not None
                and record.get("status") == "succeeded"
                and record.get("indexed_version", -1) >= version
                and bool(record.get("entity_deleted", False)) == deleted
            )
            if not ok:
                desynced += 1
                event = "note.deleted" if deleted else "note.updated"
                await _republish(event, "note", note["_id"], version, dry_run=dry_run)
                break  # 一个实体一条事件即可让两个消费组各自对齐
    return desynced


async def reconcile_documents(kb_id: str | None, batch: int, dry_run: bool) -> tuple[int, int]:
    query: dict = {} if kb_id is None else {"kb_id": kb_id}
    cursor = clients.documents().find(query, {"content_version": 1, "deleted_at": 1})
    total = desynced = 0
    jobs = clients.indexing_jobs()
    docs: list[dict] = []
    async for doc in cursor:
        docs.append(doc)
        if len(docs) >= batch:
            d = await _check_doc_batch(docs, jobs, dry_run)
            total += len(docs)
            desynced += d
            docs = []
            await asyncio.sleep(0.05)
    if docs:
        d = await _check_doc_batch(docs, jobs, dry_run)
        total += len(docs)
        desynced += d
    return total, desynced


async def _check_doc_batch(docs: list[dict], jobs, dry_run: bool) -> int:
    ids = [d["_id"] for d in docs]
    records = {r["entity_id"]: r async for r in
               jobs.find({"entity_id": {"$in": ids}, "job_type": "vector"})}
    desynced = 0
    for doc in docs:
        version = doc.get("content_version", 0)
        deleted = doc.get("deleted_at") is not None
        record = records.get(doc["_id"])
        ok = (
            record is not None
            and record.get("status") == "succeeded"
            and record.get("indexed_version", -1) >= version
            and bool(record.get("entity_deleted", False)) == deleted
        )
        if not ok:
            desynced += 1
            event = "document.deleted" if deleted else "document.updated"
            await _republish(event, "document", doc["_id"], version, dry_run=dry_run)
    return desynced


async def main() -> None:
    parser = argparse.ArgumentParser(description="KnowledgeHub 派生索引对账（§16.2）")
    parser.add_argument("--kb-id", default=None, help="限定知识库；缺省扫描全部")
    parser.add_argument("--dry-run", action="store_true", help="只报告不重新发布")
    parser.add_argument("--batch", type=int, default=200, help="批次大小（限速）")
    args = parser.parse_args()

    await ensure_group(clients.redis_client(), "graph_indexer")
    await ensure_group(clients.redis_client(), "vector_indexer")

    log.info("reconciliation start (kb=%s dry_run=%s)", args.kb_id or "<all>", args.dry_run)
    n_total, n_desync = await reconcile_notes(args.kb_id, args.batch, args.dry_run)
    log.info("notes: %d checked, %d desynced", n_total, n_desync)
    d_total, d_desync = await reconcile_documents(args.kb_id, args.batch, args.dry_run)
    log.info("documents: %d checked, %d desynced", d_total, d_desync)
    log.info("reconciliation done")
    await clients.close_all_clients()


if __name__ == "__main__":
    asyncio.run(main())
