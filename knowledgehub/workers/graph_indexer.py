"""图索引 Worker：消费事件同步笔记关系到 Neo4j（§8，阶段 3 实装）。

运行：backend/.venv/Scripts/python -m workers.graph_indexer
"""

import asyncio
import logging

from app.repositories import graph as graph_repo
from workers import runner

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    asyncio.run(graph_repo.ensure_constraints())
    asyncio.run(runner.run_worker(job_type="graph", group="graph_indexer"))
