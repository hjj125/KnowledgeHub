"""向量索引 Worker：消费事件执行分块/Embedding/Milvus Upsert（阶段 4 实装）。

运行：backend/.venv/Scripts/python -m workers.vector_indexer
"""

import asyncio
import logging

from app.repositories import milvus as milvus_repo
from workers import runner

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    async def main() -> None:
        try:  # 集合缺失先建；Milvus 未就绪不阻塞 Worker（upsert 时 ensure-on-miss 兜底）
            created = await milvus_repo.ensure_collection()
            if created:
                logging.getLogger("worker.vector").info("created collection %s", runner.settings.milvus_collection)
        except Exception as exc:  # noqa: BLE001
            logging.getLogger("worker.vector").warning("milvus ensure_collection deferred: %s", exc)
        await runner.run_worker(job_type="vector", group="vector_indexer")

    asyncio.run(main())
