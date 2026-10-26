"""Milvus knowledge_chunks 建表脚本（§9.5 / §9.8.3）。

与 backend/app/repositories/milvus.py 的 ensure_collection 同一套定义；
API 与 Worker 启动时会自动建表，本脚本用于手动初始化/核对：

    backend/.venv/Scripts/python database/milvus/init_collection.py

EMBEDDING_DIM 冻结硬约束：维度写死在建表 Schema 中，任何变更 = 全量重建 Collection。
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.core.config import settings  # noqa: E402
from app.repositories import milvus as milvus_repo  # noqa: E402


async def main() -> None:
    created = await milvus_repo.ensure_collection()
    name = settings.milvus_collection
    if created:
        print(f"created collection '{name}' (dim={settings.embedding_dim}, HNSW/COSINE)")
    else:
        print(f"collection '{name}' already exists")
    rows = await milvus_repo.list_by_document("__probe__")
    print(f"query ok, {len(rows)} row(s) for probe document")


if __name__ == "__main__":
    asyncio.run(main())
