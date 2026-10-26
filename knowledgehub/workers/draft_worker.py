"""笔记草稿 Worker：消费 document.draft_requested 事件，LLM 抽取要点生成草稿。

运行：backend/.venv/Scripts/python -m workers.draft_worker
"""

import asyncio
import logging

from workers import runner

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    asyncio.run(runner.run_worker(job_type="draft", group="draft_generator"))
