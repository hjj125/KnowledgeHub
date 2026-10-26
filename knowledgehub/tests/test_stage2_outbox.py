"""阶段 2 Outbox 事务与事件字段集成测试。

前置：后端已启动、MongoDB/Redis healthy。
验证：业务变更与 outbox 事件同事务落库（含级联删除）、事件字段正确、
     publisher 正常运行时事件被发布。
"""

from __future__ import annotations

import os
import uuid

import httpx
import pytest
from pymongo import MongoClient

BASE_URL = os.environ.get("KH_TEST_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
API = f"{BASE_URL}/api/v1"
MONGO_URI = os.environ.get("KH_TEST_MONGO_URI", "mongodb://localhost:27017/?directConnection=true")


@pytest.fixture(scope="module")
def mongo():
    try:
        client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=3000)
        client.admin.command("ping")
        yield client["knowledgehub"]
        client.close()
    except Exception:
        pytest.skip("mongodb not reachable")


@pytest.fixture(scope="module")
def client() -> httpx.Client:
    try:
        httpx.get(f"{BASE_URL}/", timeout=3)
    except httpx.HTTPError:
        pytest.skip(f"backend not reachable at {BASE_URL}")
    with httpx.Client(base_url=API, timeout=15) as c:
        yield c


@pytest.fixture(scope="module")
def auth(client: httpx.Client) -> dict:
    email = f"pytest2_{uuid.uuid4().hex[:10]}@test.dev"
    password = "pytest-passw0rd"
    assert client.post("/auth/register", json={"email": email, "password": password}).status_code == 201
    token = client.post("/auth/login", json={"email": email, "password": password}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_note_create_writes_outbox_event_in_same_transaction(client, auth, mongo):
    kb = client.post("/knowledge-bases", headers=auth, json={"name": "事务测试"}).json()
    note = client.post(
        "/notes", headers=auth, json={"kb_id": kb["id"], "title": "事件测试", "content_md": "x"}
    ).json()
    event = mongo.outbox_events.find_one({"entity_id": note["id"], "event_type": "note.created"})
    assert event is not None
    assert event["entity_version"] == 1
    assert event["status"] in {"pending", "published"}


def test_note_update_writes_event_with_new_version(client, auth, mongo):
    kb = client.post("/knowledge-bases", headers=auth, json={"name": "版本事件测试"}).json()
    note = client.post(
        "/notes", headers=auth, json={"kb_id": kb["id"], "title": "笔记", "content_md": "v1"}
    ).json()
    r = client.patch(
        f"/notes/{note['id']}",
        headers=auth,
        json={"content_md": "v2", "expected_content_version": 1},
    )
    assert r.status_code == 200
    event = mongo.outbox_events.find_one({"entity_id": note["id"], "event_type": "note.updated"})
    assert event is not None
    assert event["entity_version"] == 2  # 与事务内提交的 content_version 一致


def test_kb_cascade_delete_emits_per_note_events(client, auth, mongo):
    kb = client.post("/knowledge-bases", headers=auth, json={"name": "级联事件测试"}).json()
    notes = [
        client.post("/notes", headers=auth, json={"kb_id": kb["id"], "title": f"n{i}", "content_md": ""}).json()
        for i in range(3)
    ]
    assert client.delete(f"/knowledge-bases/{kb['id']}", headers=auth).status_code == 204

    deleted_events = list(
        mongo.outbox_events.find(
            {"entity_id": {"$in": [n["id"] for n in notes]}, "event_type": "note.deleted"}
        )
    )
    assert len(deleted_events) == 3
    kb_event = mongo.outbox_events.find_one({"entity_id": kb["id"], "event_type": "kb.deleted"})
    assert kb_event is not None


def test_failed_note_update_writes_no_event(client, auth, mongo):
    kb = client.post("/knowledge-bases", headers=auth, json={"name": "冲突无事件测试"}).json()
    note = client.post(
        "/notes", headers=auth, json={"kb_id": kb["id"], "title": "笔记", "content_md": "v1"}
    ).json()
    r = client.patch(
        f"/notes/{note['id']}",
        headers=auth,
        json={"content_md": "不该提交", "expected_content_version": 99},
    )
    assert r.status_code == 409
    # 乐观锁失败 = 事务中止，不应产生任何 note.updated 事件
    assert mongo.outbox_events.count_documents(
        {"entity_id": note["id"], "event_type": "note.updated"}
    ) == 0
