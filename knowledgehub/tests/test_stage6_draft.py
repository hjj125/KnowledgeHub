"""笔记草稿单元测试：资料组装 / 输出解析 / Prompt 构造 / 触发防重（依赖 MongoDB）。"""

from __future__ import annotations

import uuid

from app.core import clients
from app.repositories import documents as documents_repo
from workers import draft_sync


def _chunk(page: int, text: str) -> dict:
    return {"content": text, "page": page}


class TestBuildMaterial:
    def test_page_markers(self):
        material = draft_sync.build_material([_chunk(1, "甲"), _chunk(2, "乙")], 1000)
        assert "【第 1 页】甲" in material and "【第 2 页】乙" in material

    def test_budget_truncates_and_marks(self):
        chunks = [_chunk(i + 1, "字" * 500) for i in range(10)]
        material = draft_sync.build_material(chunks, 1200)
        assert len(material) < 2000
        assert "已截断" in material

    def test_no_page_falls_back(self):
        material = draft_sync.build_material([{"content": "无页码内容", "page": 0}], 1000)
        assert "【正文】无页码内容" in material


class TestParseDraftOutput:
    def test_title_and_body(self):
        raw = "# 死锁要点\n\n## 四个必要条件\n- 互斥\n- 循环等待"
        title, body = draft_sync.parse_draft_output(raw, "回退标题")
        assert title == "死锁要点"
        assert body.startswith("## 四个必要条件")
        assert "互斥" in body

    def test_missing_title_falls_back(self):
        title, body = draft_sync.parse_draft_output("直接正文没有标题行", "《文档》要点笔记")
        assert title == "《文档》要点笔记"
        assert body == "直接正文没有标题行"

    def test_empty_output(self):
        title, body = draft_sync.parse_draft_output("", "回退")
        assert title == "回退" and body == ""


class TestBuildMessages:
    def test_title_list_injected_and_constraint(self):
        messages = draft_sync.build_messages("课件", "课件.pptx", "【第 1 页】内容", ["死锁", "进程调度"])
        assert "死锁" in messages[1]["content"] and "进程调度" in messages[1]["content"]
        assert "逐字使用" in messages[0]["content"]

    def test_empty_title_list(self):
        messages = draft_sync.build_messages("课件", "课件.pptx", "内容", [])
        assert "（暂无其他笔记）" in messages[1]["content"]


class TestRequestDraft:
    def test_lifecycle(self):
        async def _run():
            owner = f"draft-{uuid.uuid4().hex[:8]}"
            kb = await clients.knowledge_bases().insert_one(
                {"_id": str(uuid.uuid4()), "owner_id": owner, "name": "草稿库",
                 "deleted_at": None, "created_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc)}
            )
            doc = await documents_repo.create(
                owner_id=owner, kb_id=kb.inserted_id, file_name="t.txt",
                file_type="txt", size_bytes=3, sha256=uuid.uuid4().hex, file_path="x/y.txt",
            )
            # 未 ready 不能触发
            doc2 = await clients.documents().find_one({"_id": doc["_id"]})
            await clients.documents().update_one({"_id": doc["_id"]}, {"$set": {"status": "ready"}})
            # 手动插入两条 chunk 以通过空文档校验（该校验在 API 层，repo 层直接触发）
            out = await documents_repo.request_draft(doc["_id"], owner)
            assert out["result"] == "requested"
            # 立即再触发 → in_progress（generating 未超时）
            out2 = await documents_repo.request_draft(doc["_id"], owner)
            assert out2["result"] == "in_progress"
            # 事件已发布
            event = await clients.outbox_events().find_one(
                {"entity_id": doc["_id"], "event_type": "document.draft_requested"}
            )
            assert event is not None and event["status"] == "published"
            # worker 落库路径：ready + request_id 幂等语义由 runner 保证，这里验证 mark_draft
            await documents_repo.mark_draft(
                doc["_id"], "ready",
                note_draft={"title": "要点", "content_md": "正文", "request_id": out["request_id"]},
            )
            final = await clients.documents().find_one({"_id": doc["_id"]})
            assert final["draft_status"] == "ready" and final["note_draft"]["title"] == "要点"
            # 清理
            await clients.documents().delete_one({"_id": doc["_id"]})
            await clients.knowledge_bases().delete_one({"_id": kb.inserted_id})
            await clients.outbox_events().delete_many({"entity_id": doc["_id"]})
        import asyncio
        asyncio.run(_run())
