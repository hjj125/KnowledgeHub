"""文件原件存储抽象（2026-10-22 决策）：P0 GridFS，后期可换 MinIO。

关键约束：`documents.file_path` 只存逻辑 key（`{owner}/{kb}/{sha256}{ext}`），
GridFS 以 sha256 作为 `fs.files._id`（key 与存储标识天然一致，迁移无需映射表；
相同文件天然去重）。大文件读写用线程池包裹同步 GridFS。
"""

import asyncio
import hashlib
from functools import lru_cache
from typing import Any

from gridfs import GridFS
from pymongo import MongoClient

from app.core.config import settings


@lru_cache(maxsize=1)
def _sync_db() -> Any:
    client = MongoClient(settings.mongo_uri, tz_aware=True, appname="knowledgehub-storage")
    return client.get_database(settings.mongo_db)


@lru_cache(maxsize=1)
def storage() -> "GridFSStorage":
    return GridFSStorage()


class StorageError(RuntimeError):
    pass


class GridFSStorage:
    async def put(self, owner_id: str, kb_id: str, content: bytes, ext: str) -> tuple[str, str, bool]:
        """写入文件原件；返回 (sha256, 逻辑 key, 是否命中去重)。"""
        sha256 = hashlib.sha256(content).hexdigest()
        key = f"{owner_id}/{kb_id}/{sha256}{ext}"

        def _write() -> bool:
            fs = GridFS(_sync_db())
            if fs.exists({"_id": sha256}):
                return False  # 同一字节内容已存在（跨用户去重）
            fs.put(content, _id=sha256, filename=key, length=len(content))
            return True

        deduplicated = not await asyncio.to_thread(_write)
        return sha256, key, deduplicated

    async def get(self, key: str) -> bytes:
        sha256 = key.rsplit("/", 1)[-1].rsplit(".", 1)[0]

        def _read() -> bytes:
            fs = GridFS(_sync_db())
            return fs.get(sha256).read()

        return await asyncio.to_thread(_read)

    async def delete(self, key: str) -> None:
        sha256 = key.rsplit("/", 1)[-1].rsplit(".", 1)[0]

        def _delete() -> None:
            fs = GridFS(_sync_db())
            if fs.exists({"_id": sha256}):
                # 引用计数语义：同一 sha 可能被多个 documents 记录引用
                refs = _sync_db().documents.count_documents(
                    {"sha256": sha256, "deleted_at": None}
                )
                if refs == 0:
                    fs.delete(sha256)

        await asyncio.to_thread(_delete)
