"""Neo4j 图数据库访问（§8）：图同步（幂等 MERGE + 出边整体替换）与图查询。

所有权原则：所有查询强制 owner_id 过滤；派生数据以 MongoDB 为准，
节点带 indexed_version 供对账（§16.2）。
"""

from typing import Any

from app.core import clients

_NOTE_CONSTRAINT = (
    "CREATE CONSTRAINT note_id_unique IF NOT EXISTS "
    "FOR (n:Note) REQUIRE n.note_id IS UNIQUE"
)
_KB_CONSTRAINT = (
    "CREATE CONSTRAINT kb_id_unique IF NOT EXISTS "
    "FOR (n:KnowledgeBase) REQUIRE n.kb_id IS UNIQUE"
)
_DOC_CONSTRAINT = (
    "CREATE CONSTRAINT document_id_unique IF NOT EXISTS "
    "FOR (n:Document) REQUIRE n.document_id IS UNIQUE"
)

_MERGE_NOTE = """
MERGE (kb:KnowledgeBase {kb_id: $kb_id})
SET kb.owner_id = $owner_id, kb.name = $kb_name
MERGE (n:Note {note_id: $note_id})
SET n.owner_id = $owner_id,
    n.kb_id = $kb_id,
    n.title = $title,
    n.indexed_version = $version,
    n.updated_at = $updated_at
MERGE (kb)-[:CONTAINS]->(n)
"""

# 出边整体替换：先删该笔记全部 LINKS_TO，再按 MongoDB 当前链接集重建；
# 目标节点不存在时 MERGE 占位节点（owner 为空，待目标自身事件补全）。
_REPLACE_LINKS = """
MATCH (n:Note {note_id: $note_id})
OPTIONAL MATCH (n)-[old:LINKS_TO]->()
DELETE old
WITH n
UNWIND $targets AS tid
MERGE (t:Note {note_id: tid})
MERGE (n)-[:LINKS_TO]->(t)
"""

_DELETE_NOTE = "MATCH (n:Note {note_id: $id}) DETACH DELETE n"

# 文档节点：课件/PDF 等原件进入图谱，与由它生成的笔记通过 DERIVED_FROM 连边
_MERGE_DOC = """
MERGE (kb:KnowledgeBase {kb_id: $kb_id})
MERGE (d:Document {document_id: $document_id})
SET kb.owner_id = $owner_id, kb.name = $kb_name,
    d.owner_id = $owner_id, d.kb_id = $kb_id, d.title = $title,
    d.file_name = $file_name, d.indexed_version = $version, d.updated_at = $updated_at
MERGE (kb)-[:HAS_DOCUMENT]->(d)
"""

# 幂等补边：把所有声称"来源于该文档"的笔记连上 DERIVED_FROM（覆盖笔记先于文档节点同步的时序）。
# 来源关系在 MongoDB（notes.source_document_id），这里按 note_id 列表连边
_RELINK_DERIVED = """
UNWIND $note_ids AS nid
MATCH (n:Note {note_id: nid}), (d:Document {document_id: $document_id})
MERGE (n)-[:DERIVED_FROM]->(d)
"""

_MERGE_SINGLE_DERIVED = """
MATCH (n:Note {note_id: $note_id}), (d:Document {document_id: $document_id})
MERGE (n)-[:DERIVED_FROM]->(d)
"""

_DELETE_DOC = "MATCH (d:Document {document_id: $id}) DETACH DELETE d"

_MERGE_KB = """
MERGE (kb:KnowledgeBase {kb_id: $kb_id})
SET kb.owner_id = $owner_id, kb.name = $name
"""

_DELETE_KB = "MATCH (kb:KnowledgeBase {kb_id: $id}) DETACH DELETE kb"

_BACKLINKS = """
MATCH (source:Note)-[:LINKS_TO]->(target:Note {note_id: $note_id})
WHERE source.owner_id = $owner_id
RETURN source.note_id AS id, source.title AS title
ORDER BY source.title
"""

_KB_NODES = """
MATCH (n:Note {kb_id: $kb_id})
WHERE n.owner_id = $owner_id
RETURN n.note_id AS id, n.title AS title, 'note' AS type
"""

_KB_DOC_NODES = """
MATCH (d:Document {kb_id: $kb_id})
WHERE d.owner_id = $owner_id
RETURN d.document_id AS id, d.title AS title, 'document' AS type, d.file_name AS file_name
"""

_KB_EDGES = """
MATCH (a:Note {kb_id: $kb_id})-[r:LINKS_TO]->(b:Note {kb_id: $kb_id})
WHERE a.owner_id = $owner_id AND b.owner_id = $owner_id
RETURN a.note_id AS source, b.note_id AS target, 'LINKS_TO' AS label
"""

_KB_DERIVED_EDGES = """
MATCH (n:Note {kb_id: $kb_id})-[r:DERIVED_FROM]->(d:Document {kb_id: $kb_id})
WHERE n.owner_id = $owner_id AND d.owner_id = $owner_id
RETURN n.note_id AS source, d.document_id AS target, 'DERIVED_FROM' AS label
"""

_NEIGHBORS_TMPL = """
MATCH (n:Note {{note_id: $note_id}})-[:LINKS_TO*1..{depth}]-(r:Note)
WHERE n.owner_id = $owner_id AND r.owner_id = $owner_id
RETURN DISTINCT r.note_id AS id, r.title AS title
LIMIT 100
"""

_LOCAL_EDGES = """
MATCH (a:Note)-[r:LINKS_TO]->(b:Note)
WHERE a.owner_id = $owner_id AND b.owner_id = $owner_id
  AND (a.note_id = $note_id OR b.note_id = $note_id)
RETURN a.note_id AS source, b.note_id AS target
"""


def _driver() -> Any:
    return clients.neo4j_driver()


async def ensure_constraints() -> None:
    async with _driver().session() as session:
        await session.run(_NOTE_CONSTRAINT)
        await session.run(_KB_CONSTRAINT)
        await session.run(_DOC_CONSTRAINT)


async def sync_note(note_id: str) -> str:
    """幂等同步单篇笔记：软删除 → 节点级联删除；否则 MERGE + 出边替换。

    返回动作描述，用于任务日志与冒烟断言。
    """
    note = await clients.notes().find_one({"_id": note_id})
    if note is None:
        return "missing"
    if note.get("deleted_at") is not None:
        async with _driver().session() as session:
            await session.run(_DELETE_NOTE, id=note_id)
        return "deleted"

    kb = await clients.knowledge_bases().find_one({"_id": note["kb_id"]}, {"name": 1})
    kb_name = (kb or {}).get("name", "")
    async with _driver().session() as session:
        async with await session.begin_transaction() as tx:
            await tx.run(
                _MERGE_NOTE,
                note_id=note_id,
                owner_id=note["owner_id"],
                kb_id=note["kb_id"],
                kb_name=kb_name,
                title=note["title"],
                version=note["content_version"],
                updated_at=note["updated_at"].isoformat(),
            )
            await tx.run(_REPLACE_LINKS, note_id=note_id, targets=note.get("linked_note_ids", []))
            if note.get("source_document_id"):
                await tx.run(
                    _MERGE_SINGLE_DERIVED,
                    note_id=note_id,
                    document_id=note["source_document_id"],
                )
    return "synced"


async def sync_document(document_id: str) -> str:
    """幂等同步文档节点：软删除 → 节点删除；否则 MERGE 并补齐 DERIVED_FROM 边。"""
    doc = await clients.documents().find_one({"_id": document_id})
    if doc is None:
        return "missing"
    if doc.get("deleted_at") is not None:
        async with _driver().session() as session:
            await session.run(_DELETE_DOC, id=document_id)
        return "deleted"
    kb = await clients.knowledge_bases().find_one({"_id": doc["kb_id"]}, {"name": 1})
    kb_name = (kb or {}).get("name", "")
    derived_note_ids = [
        n["_id"]
        async for n in clients.notes().find(
            {"source_document_id": document_id, "deleted_at": None}, {"_id": 1}
        )
    ]
    async with _driver().session() as session:
        await session.run(
            _MERGE_DOC,
            document_id=document_id,
            owner_id=doc["owner_id"],
            kb_id=doc["kb_id"],
            kb_name=kb_name,
            title=doc["title"],
            file_name=doc.get("file_name", ""),
            version=doc["content_version"],
            updated_at=doc["updated_at"].isoformat(),
        )
        if derived_note_ids:
            await session.run(_RELINK_DERIVED, document_id=document_id, note_ids=derived_note_ids)
    return "synced"


async def sync_kb(kb_id: str) -> str:
    kb = await clients.knowledge_bases().find_one({"_id": kb_id})
    if kb is None:
        return "missing"
    if kb.get("deleted_at") is not None:
        async with _driver().session() as session:
            await session.run(_DELETE_KB, id=kb_id)
        return "deleted"
    async with _driver().session() as session:
        await session.run(_MERGE_KB, kb_id=kb_id, owner_id=kb["owner_id"], name=kb["name"])
    return "synced"


async def backlinks(note_id: str, owner_id: str) -> list[dict]:
    async with _driver().session() as session:
        result = await session.run(_BACKLINKS, note_id=note_id, owner_id=owner_id)
        return await result.data()


async def kb_graph(kb_id: str, owner_id: str) -> dict:
    """全库图谱：笔记 + 文档两类节点，LINKS_TO（双链）与 DERIVED_FROM（来源于文档）两类边。"""
    async with _driver().session() as session:
        nodes = await (await session.run(_KB_NODES, kb_id=kb_id, owner_id=owner_id)).data()
        doc_nodes = await (await session.run(_KB_DOC_NODES, kb_id=kb_id, owner_id=owner_id)).data()
        edges = await (await session.run(_KB_EDGES, kb_id=kb_id, owner_id=owner_id)).data()
        derived = await (await session.run(_KB_DERIVED_EDGES, kb_id=kb_id, owner_id=owner_id)).data()
    return {"nodes": nodes + doc_nodes, "edges": edges + derived}


async def neighbors(note_id: str, owner_id: str, depth: int = 1) -> dict:
    if depth < 1:
        depth = 1
    async with _driver().session() as session:
        nodes = await (
            await session.run(_NEIGHBORS_TMPL.format(depth=int(depth)), note_id=note_id, owner_id=owner_id)
        ).data()
        edges = await (
            await session.run(_LOCAL_EDGES, note_id=note_id, owner_id=owner_id)
        ).data()
    return {"note_id": note_id, "depth": depth, "nodes": nodes, "edges": edges}
