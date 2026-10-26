"""阶段 1 API 集成测试（对运行中的后端执行）。

前置：后端已启动且 MongoDB/Redis healthy。
运行：backend/.venv/Scripts/python -m pytest tests/ -v
"""

from __future__ import annotations

import os
import uuid

import httpx
import pytest

BASE_URL = os.environ.get("KH_TEST_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
API = f"{BASE_URL}/api/v1"


def _unique_email() -> str:
    return f"pytest_{uuid.uuid4().hex[:10]}@test.dev"


@pytest.fixture(scope="session")
def client() -> httpx.Client:
    try:
        httpx.get(f"{BASE_URL}/", timeout=3)
    except httpx.HTTPError:
        pytest.skip(f"backend not reachable at {BASE_URL}")
    with httpx.Client(base_url=API, timeout=15) as c:
        yield c


@pytest.fixture()
def auth_headers(client: httpx.Client) -> dict:
    email = _unique_email()
    password = "pytest-passw0rd"
    r = client.post("/auth/register", json={"email": email, "password": password})
    assert r.status_code == 201
    r = client.post("/auth/login", json={"email": email, "password": password})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture()
def kb_id(client: httpx.Client, auth_headers: dict) -> str:
    r = client.post(
        "/knowledge-bases", headers=auth_headers, json={"name": "测试知识库", "category": "测试"}
    )
    return r.json()["id"]


def _create_note(client: httpx.Client, headers: dict, kb: str, title: str = "测试笔记") -> dict:
    r = client.post(
        "/notes",
        headers=headers,
        json={"kb_id": kb, "title": title, "content_md": "# 内容", "tags": ["t1"]},
    )
    assert r.status_code == 201
    return r.json()


class TestAuth:
    def test_duplicate_register_conflict(self, client: httpx.Client):
        email = _unique_email()
        assert client.post("/auth/register", json={"email": email, "password": "pytest-passw0rd"}).status_code == 201
        assert client.post("/auth/register", json={"email": email, "password": "pytest-passw0rd"}).status_code == 409

    def test_wrong_password(self, client: httpx.Client):
        email = _unique_email()
        client.post("/auth/register", json={"email": email, "password": "pytest-passw0rd"})
        r = client.post("/auth/login", json={"email": email, "password": "wrong-password"})
        assert r.status_code == 401

    def test_logout_revokes_token(self, client: httpx.Client):
        email = _unique_email()
        client.post("/auth/register", json={"email": email, "password": "pytest-passw0rd"})
        token = client.post("/auth/login", json={"email": email, "password": "pytest-passw0rd"}).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        assert client.get("/notes", headers=headers).status_code == 200
        assert client.post("/auth/logout", headers=headers).status_code == 204
        assert client.get("/notes", headers=headers).status_code == 401

    def test_invalid_token_rejected(self, client: httpx.Client):
        r = client.get("/notes", headers={"Authorization": "Bearer not-a-jwt"})
        assert r.status_code == 401


class TestKnowledgeBases:
    def test_create_and_get_with_count(self, client: httpx.Client, auth_headers: dict, kb_id: str):
        _create_note(client, auth_headers, kb_id)
        r = client.get(f"/knowledge-bases/{kb_id}", headers=auth_headers)
        assert r.status_code == 200
        assert r.json()["note_count"] == 1

    def test_ownership_isolation(self, client: httpx.Client, kb_id: str, auth_headers: dict):
        other_email = _unique_email()
        client.post("/auth/register", json={"email": other_email, "password": "pytest-passw0rd"})
        other_token = client.post(
            "/auth/login", json={"email": other_email, "password": "pytest-passw0rd"}
        ).json()["access_token"]
        r = client.get(f"/knowledge-bases/{kb_id}", headers={"Authorization": f"Bearer {other_token}"})
        assert r.status_code == 404  # 不泄露他人资源存在性

    def test_delete_cascades_notes(self, client: httpx.Client, auth_headers: dict, kb_id: str):
        note = _create_note(client, auth_headers, kb_id)
        assert client.delete(f"/knowledge-bases/{kb_id}", headers=auth_headers).status_code == 204
        assert client.get(f"/knowledge-bases/{kb_id}", headers=auth_headers).status_code == 404
        assert client.get(f"/notes/{note['id']}", headers=auth_headers).status_code == 404


class TestNotes:
    def test_create_and_cache_aside(self, client: httpx.Client, auth_headers: dict, kb_id: str):
        note = _create_note(client, auth_headers, kb_id)
        r1 = client.get(f"/notes/{note['id']}", headers=auth_headers)
        r2 = client.get(f"/notes/{note['id']}", headers=auth_headers)
        assert r1.headers.get("x-cache") == "MISS"
        assert r2.headers.get("x-cache") == "HIT"
        assert r2.json()["content_version"] == 1

    def test_optimistic_lock_conflict(self, client: httpx.Client, auth_headers: dict, kb_id: str):
        note = _create_note(client, auth_headers, kb_id)
        r = client.patch(
            f"/notes/{note['id']}",
            headers=auth_headers,
            json={"title": "不该成功", "expected_content_version": note["content_version"] + 5},
        )
        assert r.status_code == 409
        assert r.json()["detail"]["reason"] == "content_version_conflict"

    def test_update_bumps_version_and_history(self, client: httpx.Client, auth_headers: dict, kb_id: str):
        note = _create_note(client, auth_headers, kb_id)
        r = client.patch(
            f"/notes/{note['id']}",
            headers=auth_headers,
            json={"title": "修订标题", "expected_content_version": 1},
        )
        assert r.status_code == 200 and r.json()["content_version"] == 2
        versions = client.get(f"/notes/{note['id']}/versions", headers=auth_headers).json()
        assert [v["version"] for v in versions] == [2, 1]

    def test_cache_invalidated_on_update(self, client: httpx.Client, auth_headers: dict, kb_id: str):
        note = _create_note(client, auth_headers, kb_id)
        client.get(f"/notes/{note['id']}", headers=auth_headers)  # 预热缓存
        client.patch(
            f"/notes/{note['id']}",
            headers=auth_headers,
            json={"title": "缓存应失效", "expected_content_version": 1},
        )
        r = client.get(f"/notes/{note['id']}", headers=auth_headers)
        assert r.headers.get("x-cache") == "MISS"  # 更新后缓存已失效
        assert r.json()["title"] == "缓存应失效"

    def test_list_filters(self, client: httpx.Client, auth_headers: dict, kb_id: str):
        _create_note(client, auth_headers, kb_id, title="进程调度")
        _create_note(client, auth_headers, kb_id, title="死锁")
        assert client.get("/notes", headers=auth_headers, params={"q": "进程"}).json()["total"] == 1
        assert client.get("/notes", headers=auth_headers, params={"tag": "t1"}).json()["total"] == 2

    def test_soft_delete_then_404(self, client: httpx.Client, auth_headers: dict, kb_id: str):
        note = _create_note(client, auth_headers, kb_id)
        assert client.delete(f"/notes/{note['id']}", headers=auth_headers).status_code == 204
        assert client.get(f"/notes/{note['id']}", headers=auth_headers).status_code == 404
