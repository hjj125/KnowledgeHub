"""阶段 5 冒烟测试：RAG 问答（R1/R2）全链路。

前置：后端已启动、四库 healthy、SILICONFLOW_API_KEY 有效（R2 真实调用 LLM）。
本脚本自行拉起/终止 publisher 与两个 Worker 子进程。

场景（§12.4 R1/R2 验收标准）：
  1. R1 /qa/retrieve：自然语言问题 → 带来源的资料列表（无 LLM）
  2. R2 /qa SSE：sources → delta* → done；回答含 [n]，每个 n 对应真实来源
     （引用硬约束：sources[] 即送入 LLM 的 chunk）
  3. 拒答分支：知识库无相关资料 → refusal 事件 + 模板话术，不调用 LLM
  4. 多轮改写：追问经 LLM 改写为独立检索问题，会话历史存 Redis（TTL）
  5. 限流：Redis 计数器 10 次/分钟/用户 → 第 11 次 429（拒答路径无 LLM 成本）
  6. qa_logs 问答日志落库；Redis Streams PEL 清空
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
import redis as redis_sync
from pymilvus import MilvusClient
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


def wait_until(desc: str, fn, timeout: float = 60.0, interval: float = 0.5) -> bool:
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


def start_worker(module: str, tag: str) -> subprocess.Popen:
    logs = PROJECT / "tmp"
    logs.mkdir(exist_ok=True)
    log_file = open(logs / f"smoke5_{tag}.log", "w", encoding="utf-8")  # noqa: SIM115
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


def parse_sse(client: httpx.Client, token_headers: dict, body: dict, timeout: float = 180.0) -> list[dict]:
    """消费 SSE 流，返回事件列表。"""
    events: list[dict] = []
    with client.stream(
        "POST", "/qa", headers=token_headers, json=body, timeout=timeout
    ) as response:
        if response.status_code != 200:
            return [{"type": "http-error", "status": response.status_code}]
        for line in response.iter_lines():
            if not line.startswith("data:"):
                continue
            events.append(json.loads(line[5:]))
    return events


def main() -> int:
    base = "http://127.0.0.1:8000"
    api = f"{base}/api/v1"
    http = httpx.Client(base_url=api, timeout=60)
    mongo = MongoClient("mongodb://localhost:27017/?directConnection=true", tz_aware=True)
    db = mongo["knowledgehub"]
    rds = redis_sync.Redis.from_url("redis://localhost:6379/0", decode_responses=True, socket_timeout=5)

    print(f"== 阶段 5 冒烟测试 ({api}) ==")

    suffix = uuid.uuid4().hex[:8]
    password = "smoke-passw0rd"

    def register(tag: str) -> dict:
        email = f"smoke5_{tag}_{suffix}@test.dev"
        http.post("/auth/register", json={"email": email, "password": password})
        token = http.post("/auth/login", json={"email": email, "password": password}).json()["access_token"]
        return {"Authorization": f"Bearer {token}"}

    auth = register("a")
    kb = http.post("/knowledge-bases", headers=auth, json={"name": "操作系统复习"}).json()
    note_a = http.post("/notes", headers=auth, json={
        "kb_id": kb["id"], "title": "进程与线程",
        "content_md": "进程是资源分配的基本单位，拥有独立的地址空间。线程是CPU调度的基本单位，"
                      "同一进程内的线程共享堆与全局变量，切换开销小于进程切换。",
    }).json()
    note_b = http.post("/notes", headers=auth, json={
        "kb_id": kb["id"], "title": "死锁",
        "content_md": "死锁的四个必要条件：互斥、持有并等待、不可剥夺、循环等待。"
                      "破坏任意一个条件即可预防死锁，银行家算法通过安全性检查避免死锁。",
    }).json()

    publisher = start_worker("workers.event_publisher", "publisher")
    graph = start_worker("workers.graph_indexer", "graph")
    vector = start_worker("workers.vector_indexer", "vector")

    try:
        # 就绪判定必须直查 Milvus（Mongo chunks 先写、向量 upsert 后完成，留有窗口）
        mv = MilvusClient(uri="http://localhost:19530")

        def mv_ready(note_id: str) -> bool:
            return len(mv.query(collection_name="knowledge_chunks",
                                filter=f'document_id == "{note_id}"', output_fields=["pk"])) >= 1

        ok = wait_until("向量就位", lambda: mv_ready(note_a["id"]) and mv_ready(note_b["id"]))
        check("准备：两篇笔记完成向量化（Milvus 行就位）", ok)

        # --- 1. R1：检索服务化 ---
        r = http.post("/qa/retrieve", headers=auth,
                      json={"question": "线程和进程有什么区别", "kb_ids": [kb["id"]]})
        check("R1 /qa/retrieve 返回 200", r.status_code == 200)
        results = r.json()["results"] if r.status_code == 200 else []
        check("R1 自然语言问题 → 带来源资料列表",
              len(results) >= 1
              and all(k in results[0] for k in ("source_type", "source_id", "title", "content", "score")),
              f"n={len(results)}")
        check("R1 命中笔记《进程与线程》",
              any(x["title"] == "进程与线程" for x in results))

        # --- 2. R2：SSE 流式问答（真实 LLM）---
        session_id = uuid.uuid4().hex
        events = parse_sse(http, auth, {"question": "线程和进程的区别是什么", "kb_ids": [kb["id"]],
                                        "session_id": session_id})
        types = [e["type"] for e in events]
        check("R2 SSE 事件序列 sources→delta*→done",
              "sources" in types and "delta" in types and types[-1] == "done", str(set(types)))
        sources_event = next((e for e in events if e["type"] == "sources"), {})
        sources = sources_event.get("sources", [])
        check("R2 引用硬约束：sources[] 为真实送入 LLM 的 chunk（含标题/页码/chunk_id）",
              len(sources) >= 1
              and all(k in sources[0] for k in ("n", "source_id", "title", "chunk_id", "content"))
              and sources[0]["chunk_id"],
              f"n={len(sources)}")
        answer = "".join(e["text"] for e in events if e["type"] == "delta")
        cited = {int(n) for n in re.findall(r"\[(\d+)\]", answer)}
        check("R2 回答含 [n] 引用且 n 均可回溯到真实来源",
              len(answer) > 20 and cited.issubset({s["n"] for s in sources}),
              f"len={len(answer)} cited={sorted(cited)}")

        # --- 3. 拒答分支 ---
        events = parse_sse(http, auth, {"question": "量子隧穿效应在超导量子比特中的一致性阈值是多少",
                                        "kb_ids": [kb["id"]]})
        refusal = next((e for e in events if e["type"] == "refusal"), None)
        check("R2 证据不足 → 拒答分支（不调用 LLM）",
              refusal is not None
              and refusal["sources"] == []
              and "缺少足够资料" in refusal["message"]
              and not any(e["type"] == "delta" for e in events))

        # --- 4. 多轮改写 ---
        events = parse_sse(http, auth, {"question": "那死锁呢，怎么预防", "kb_ids": [kb["id"]],
                                        "session_id": session_id})
        sources_event = next((e for e in events if e["type"] == "sources"), {})
        answer2 = "".join(e["text"] for e in events if e["type"] == "delta")
        check("多轮：追问改写为独立检索问题",
              sources_event.get("rewritten_question") not in (None, "那死锁呢，怎么预防"),
              str(sources_event.get("rewritten_question", ""))[:40])
        check("多轮：回答命中死锁资料",
              (any(s["title"] == "死锁" for s in sources_event.get("sources", []))
               or "死锁" in answer2))
        history_len = rds.llen(f"session:{session_id}:qa_history")
        check("多轮：会话历史存 Redis", history_len >= 1, f"n={history_len}")

        # --- 5. qa_logs ---
        check("qa_logs 问答日志落库",
              db["qa_logs"].count_documents({"session_id": session_id}) >= 1,
              str(db["qa_logs"].count_documents({"session_id": session_id})))

        # --- 6. 限流（拒答路径，无 LLM 成本）---
        auth_b = register("b")  # 用户 B 无任何资料 → 全部走拒答分支
        statuses = []
        for _ in range(11):
            r = http.post("/qa", headers=auth_b,
                          json={"question": "完全无关的问题测试限流" + uuid.uuid4().hex[:4]})
            statuses.append(r.status_code)
        check("限流：10 次/分钟后第 11 次 429", statuses[:10] == [200] * 10 and statuses[10] == 429,
              str(statuses[:11]))

        # --- 收尾 ---
        check("Redis Streams PEL 清空", wait_until(
            "pel", lambda: rds.xpending(STREAM, "graph_indexer")["pending"] == 0
            and rds.xpending(STREAM, "vector_indexer")["pending"] == 0, timeout=30))
    finally:
        stop_proc(graph)
        stop_proc(vector)
        stop_proc(publisher)
        http.close()

    print(f"\n结果：{len(PASSED)} 通过 / {len(FAILED)} 失败")
    if FAILED:
        print("失败项：", "、".join(FAILED))
        return 1
    print("阶段 5 冒烟测试全部通过 ✔")
    return 0


if __name__ == "__main__":
    sys.exit(main())
