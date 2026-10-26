"""阶段 5 QA 服务单元测试：装配/去重/来源视图/限流/会话历史/追问改写（stub LLM）。

前置：Redis 可用（历史与限流用）。不依赖后端进程与 LLM API。
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from app.core import clients
from app.services import qa as qa_service


@pytest.fixture(autouse=True)
def _fresh_redis():
    """每个用例独立的 asyncio.run 循环 → redis 异步客户端必须按循环重建。"""
    clients.redis_client.cache_clear()
    yield
    clients.redis_client.cache_clear()


def _hit(score: float, doc: str = "d1", content: str = "内容", source_type: str = "note",
         title: str = "笔记", page: int = 0, file_name: str | None = None) -> dict:
    return {
        "chunk_id": f"pk-{doc}-{score}",
        "document_id": doc,
        "kb_id": "kb1",
        "source_type": source_type,
        "title": title,
        "file_name": file_name,
        "page": page,
        "score": score,
        "content": content,
        "content_version": 1,
    }


class TestDedupe:
    def test_same_doc_same_content_keeps_best(self):
        hits = [_hit(0.5, content="相同"), _hit(0.9, content="相同"), _hit(0.7, content="不同")]
        out = qa_service.dedupe(hits)
        assert len(out) == 2
        assert out[0]["score"] == 0.9

    def test_different_docs_not_merged(self):
        out = qa_service.dedupe([_hit(0.5, doc="a", content="x"), _hit(0.5, doc="b", content="x")])
        assert len(out) == 2


class TestAssemble:
    def test_numbering_and_hard_constraint(self):
        blocks, used = qa_service.assemble([_hit(0.9), _hit(0.8)])
        assert blocks[0].startswith("[1] ")
        assert blocks[1].startswith("[2] ")
        assert [h["score"] for h in used] == [0.9, 0.8]  # sources[] 只含真正送入的 chunk

    def test_max_chunks_and_budget(self):
        hits = [_hit(1 - i * 0.01, doc=f"d{i}", content="字" * 200) for i in range(20)]
        blocks, used = qa_service.assemble(hits, max_chunks=3, char_budget=1000)
        assert len(blocks) == 3
        assert sum(len(b) for b in blocks) <= 1000 + len(blocks[0])  # 首块不受预算截断

    def test_document_label_has_page(self):
        hit = _hit(0.9, source_type="document", title="课件", file_name="课件.pdf", page=12)
        blocks, _ = qa_service.assemble([hit])
        assert "[1] 文档《课件.pdf》第 12 页" in blocks[0]

    def test_note_label(self):
        blocks, _ = qa_service.assemble([_hit(0.9, title="进程调度")])
        assert "[1] 笔记《进程调度》" in blocks[0]


class TestSourcesView:
    def test_fields_exposed(self):
        sources = qa_service.sources_view([_hit(0.9, doc="doc-1", page=3)])
        assert sources[0]["n"] == 1
        assert sources[0]["source_id"] == "doc-1"
        assert sources[0]["page"] == 3
        assert sources[0]["chunk_id"] and sources[0]["content"]


class TestBuildMessages:
    def test_prompt_contains_sources_and_question(self):
        blocks, _ = qa_service.assemble([_hit(0.9)])
        messages = qa_service.build_messages("什么是进程？", blocks)
        assert messages[0]["role"] == "system"
        assert "禁止编造" in messages[0]["content"]
        assert "[1]" in messages[1]["content"]
        assert "什么是进程？" in messages[1]["content"]


class TestRateLimit:
    def test_window_counts_and_blocks(self):
        async def _run():
            user = f"rl-{uuid.uuid4().hex[:8]}"
            await clients.redis_client().delete(f"rate:qa:{user}")
            counts = [await qa_service.check_rate_limit(user) for _ in range(10)]
            assert counts[-1] == 10
            try:
                await qa_service.check_rate_limit(user)
                raise AssertionError("expected RateLimitError")
            except qa_service.RateLimitError:
                pass
        asyncio.run(_run())


class TestHistory:
    def test_append_and_trim(self):
        async def _run():
            sid = uuid.uuid4().hex
            try:
                for i in range(12):  # 超过 max_turns*2 = 10 条
                    await qa_service.append_history(sid, f"q{i}", f"a{i}")
                history = await qa_service.load_history(sid)
                assert len(history) == 10
                assert history[-1]["question"] == "q11"  # 保留最近对话
            finally:
                await clients.redis_client().delete(f"session:{sid}:qa_history")
        asyncio.run(_run())


class _StubLLM:
    def __init__(self, reply: str = "改写后的问题", fail: bool = False):
        self.reply = reply
        self.fail = fail
        self.calls: list[list[dict]] = []

    async def chat(self, messages):
        self.calls.append(messages)
        if self.fail:
            raise RuntimeError("llm down")
        return self.reply

    def stream(self, messages):  # pragma: no cover — 改写路径不用
        raise NotImplementedError


class TestRewrite:
    def test_first_turn_no_rewrite(self):
        async def _run():
                llm = _StubLLM()
                q, did = await qa_service.rewrite_question(llm, "那死锁呢", [])
                assert q == "那死锁呢" and did is False and llm.calls == []
            
            
        asyncio.run(_run())
    def test_followup_rewritten(self):
        async def _run():
                llm = _StubLLM(reply="死锁的四个必要条件是什么")
                q, did = await qa_service.rewrite_question(
                    llm, "那死锁呢", [{"question": "什么是进程", "answer": "进程是资源分配单位"}]
                )
                assert q == "死锁的四个必要条件是什么" and did is True
                assert "追问" in llm.calls[0][1]["content"]
            
            
        asyncio.run(_run())
    def test_llm_failure_falls_back(self):
        async def _run():
                llm = _StubLLM(fail=True)
                q, did = await qa_service.rewrite_question(llm, "那死锁呢", [{"question": "q", "answer": "a"}])
                assert q == "那死锁呢" and did is False
            
        asyncio.run(_run())
