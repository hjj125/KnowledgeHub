"""阶段 3 冒烟测试：Neo4j 图同步与知识图谱全链路。

前置：后端已启动、MongoDB/Redis/Neo4j healthy。
本脚本自行拉起/终止 publisher 与两个 Worker 子进程，并用同步 neo4j 驱动直接查图断言。

场景（§8 / §14 阶段 4 验收标准：建/改/删链接后图谱正确更新）：
  1. [[链接]] 解析：内容含 [[标题]] → 事务内解析为稳定 note_id → Worker 建 LINKS_TO
  2. 反向链接 / 图谱 / 邻居三个查询 API（数据必须来自 Neo4j）
  3. 删链接：出边整体替换（旧边清除，不能只追加）
  4. 改名：标题更新经 MERGE 同步到图
  5. 自引用排除
  6. 笔记软删除 → 图节点级联删除
  7. 知识库删除 → 图全部清空
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
import redis as redis_sync
from neo4j import GraphDatabase
from pymongo import MongoClient

PROJECT = Path(__file__).resolve().parents[1]
BACKEND = PROJECT / "backend"
STREAM = "stream:knowledge_events"
SUBPROCESS_ENV = {**os.environ, "PYTHONPATH": f"{PROJECT}{os.pathsep}{BACKEND}"}

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  √ {name}" + (f"  [{detail}]" if detail else ""))
    else:
        FAILED.append(name)
        print(f"  × {name}  [{detail}]")


def wait_until(desc: str, fn, timeout: float = 40.0, interval: float = 0.5) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if fn():
                return True
        except Exception:  # noqa: BLE001 — 同步期间抖动属预期
            pass
        time.sleep(interval)
    print(f"    (timeout waiting: {desc})")
    return False


def start_worker(module: str) -> subprocess.Popen:
    logs = PROJECT / "tmp"
    logs.mkdir(exist_ok=True)
    log_file = open(logs / f"smoke3_{module.split('.')[-1]}.log", "w", encoding="utf-8")  # noqa: SIM115
    return subprocess.Popen(
        [sys.executable, "-m", module],
        cwd=str(BACKEND),
        env=SUBPROCESS_ENV,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )


def stop_proc(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def main() -> int:
    base = "http://127.0.0.1:8000"
    api = f"{base}/api/v1"
    http = httpx.Client(timeout=15)
    mongo = MongoClient("mongodb://localhost:27017/?directConnection=true", tz_aware=True)
    db = mongo["knowledgehub"]
    rds = redis_sync.Redis.from_url("redis://localhost:6379/0", decode_responses=True, socket_timeout=5)
    driver = GraphDatabase.driver("bolt://localhost:7687", auth=("neo4j", "knowledgehub123"))

    def cypher(query: str, **params) -> list[dict]:
        with driver.session() as session:
            return [record.data() for record in session.run(query, **params)]

    def edge_exists(src: str, dst: str) -> bool:
        return bool(cypher(
            "MATCH (a:Note {note_id:$s})-[:LINKS_TO]->(b:Note {note_id:$d}) RETURN 1 AS e", s=src, d=dst
        ))

    def node_title(note_id: str) -> str | None:
        rows = cypher("MATCH (n:Note {note_id:$id}) RETURN n.title AS t", id=note_id)
        return rows[0]["t"] if rows else None

    print(f"== 阶段 3 冒烟测试 ({api}) ==")

    # --- 准备 ---
    email = f"smoke3_{uuid.uuid4().hex[:8]}@test.dev"
    password = "smoke-passw0rd"
    http.post(f"{api}/auth/register", json={"email": email, "password": password})
    token = http.post(f"{api}/auth/login", json={"email": email, "password": password}).json()["access_token"]
    auth = {"Authorization": f"Bearer {token}"}
    kb = http.post(f"{api}/knowledge-bases", headers=auth, json={"name": "操作系统"}).json()

    def make_note(title: str, content_md: str = "") -> dict:
        r = http.post(f"{api}/notes", headers=auth, json={"kb_id": kb["id"], "title": title, "content_md": content_md})
        return r.json()

    note_a = make_note("进程调度", "进程与线程的区别。")
    note_b = make_note("死锁", "四个必要条件。")
    note_c = make_note("内存管理", "分页与分段。")
    print(f"  准备完成：A={note_a['id'][:8]}… B={note_b['id'][:8]}… C={note_c['id'][:8]}…")

    publisher = start_worker("workers.event_publisher")
    graph = start_worker("workers.graph_indexer")
    vector = start_worker("workers.vector_indexer")

    try:
        ok = wait_until("初始三个节点入图", lambda: len(cypher(
            "MATCH (n:Note {kb_id:$k}) RETURN 1", k=kb["id"])) == 3)
        check("初始同步：3 篇笔记全部入图（KnowledgeBase-CONTAINS）", ok)

        # --- 1. 建链：[[标题]] 解析 → LINKS_TO ---
        r = http.patch(
            f"{api}/notes/{note_a['id']}",
            headers=auth,
            json={"content_md": "前置知识：[[死锁]] 与 [[内存管理]]。", "expected_content_version": 1},
        )
        linked = r.json().get("linked_note_ids", [])
        check("[[标题]] 解析为稳定 note_id（事务内）", sorted(linked) == sorted([note_b["id"], note_c["id"]]), str(len(linked)))

        ok = wait_until("A→B 与 A→C 边建立", lambda: edge_exists(note_a["id"], note_b["id"]) and edge_exists(note_a["id"], note_c["id"]))
        check("Worker 建立两条 LINKS_TO 边", ok)

        # --- 2. 三个查询 API ---
        r = http.get(f"{api}/notes/{note_b['id']}/backlinks", headers=auth)
        bl = r.json()
        check("反向链接 API（Neo4j）：B 的反链 = [A]", r.status_code == 200 and [x["id"] for x in bl] == [note_a["id"]], str(len(bl)))

        r = http.get(f"{api}/graph/{kb['id']}", headers=auth)
        g = r.json()
        check("全库图谱 API：3 节点 2 边", len(g["nodes"]) == 3 and len(g["edges"]) == 2, f"{len(g['nodes'])}n/{len(g['edges'])}e")

        r = http.get(f"{api}/graph/notes/{note_a['id']}/neighbors", headers=auth, params={"depth": 1})
        neighbors = {n["id"] for n in r.json()["nodes"]}
        check("一跳邻居 API：{B, C}", neighbors == {note_b["id"], note_c["id"]}, str(len(neighbors)))

        # --- 3. 删链接：出边整体替换 ---
        r = http.patch(
            f"{api}/notes/{note_a['id']}",
            headers=auth,
            json={"content_md": "只保留 [[死锁]]。", "expected_content_version": 2},
        )
        check("内容移除 [[内存管理]]", note_c["id"] not in r.json().get("linked_note_ids", []))
        ok = wait_until("A→C 边被清除", lambda: not edge_exists(note_a["id"], note_c["id"]))
        check("删链接：旧边清除（非只追加）", ok and edge_exists(note_a["id"], note_b["id"]))

        # --- 4. 改名同步 ---
        http.patch(f"{api}/notes/{note_b['id']}", headers=auth, json={"title": "死锁四条件", "expected_content_version": 1})
        ok = wait_until("图中 B 标题更新", lambda: node_title(note_b["id"]) == "死锁四条件")
        check("改名经 MERGE 同步到图且边保留", ok and edge_exists(note_a["id"], note_b["id"]))

        # --- 5. 自引用排除 ---
        r = http.patch(
            f"{api}/notes/{note_a['id']}",
            headers=auth,
            json={"content_md": "自引用 [[进程调度]] 与 [[死锁四条件]]。", "expected_content_version": 3},
        )
        ok = wait_until("A→B 保留且无自环", lambda: edge_exists(note_a["id"], note_b["id"])
                        and not edge_exists(note_a["id"], note_a["id"]))
        check("自引用排除：无自环边", ok and r.json()["linked_note_ids"] == [note_b["id"]])

        # --- 6. 笔记软删除 → 图节点级联删除 ---
        http.delete(f"{api}/notes/{note_c['id']}", headers=auth)
        ok = wait_until("C 节点从图中移除", lambda: node_title(note_c["id"]) is None)
        check("软删除笔记 → Neo4j 节点 DETACH 删除", ok)

        # --- 7. 知识库删除 → 图清空 ---
        http.delete(f"{api}/knowledge-bases/{kb['id']}", headers=auth)
        ok = wait_until("知识库图数据清空", lambda: not cypher(
            "MATCH (n:Note {kb_id:$k}) RETURN 1", k=kb["id"]))
        check("删除知识库 → 全部笔记节点与边清空", ok)
        check("Redis Streams PEL 清空", wait_until(
            "pel", lambda: rds.xpending(STREAM, "graph_indexer")["pending"] == 0
            and rds.xpending(STREAM, "vector_indexer")["pending"] == 0, timeout=20))
    finally:
        stop_proc(graph)
        stop_proc(vector)
        stop_proc(publisher)
        driver.close()

    print(f"\n结果：{len(PASSED)} 通过 / {len(FAILED)} 失败")
    if FAILED:
        print("失败项：", "、".join(FAILED))
        return 1
    print("阶段 3 冒烟测试全部通过 ✔")
    return 0


if __name__ == "__main__":
    sys.exit(main())
