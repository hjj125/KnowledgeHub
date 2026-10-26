from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/core/config.py -> parents[3] = knowledgehub/（仓库根）
BASE_DIR = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "KnowledgeHub API"

    # 四库连接（宿主机开发态；容器化部署时由环境变量覆盖）
    mongo_uri: str = "mongodb://localhost:27017/?directConnection=true"
    mongo_db: str = "knowledgehub"
    redis_url: str = "redis://localhost:6379/0"
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "knowledgehub123"
    milvus_uri: str = "http://localhost:19530"

    # 认证（阶段 1）
    jwt_secret: str = "dev-insecure-change-me"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 1440

    # Cache-Aside（阶段 1，§7.1 初期策略）
    note_cache_ttl_seconds: int = 300

    # 硅基流动模型服务（§9.8 / §12）
    siliconflow_api_base: str = "https://api.siliconflow.cn/v1"
    siliconflow_api_key: str = ""
    embedding_model: str = "Qwen/Qwen3-VL-Embedding-8B"
    # 0 = 未定稿；定稿后写死，任何变更等同全量重建 Collection（§9.8.3 硬约束）
    embedding_dim: int = 0
    embedding_batch_size: int = 16
    reranker_model: str = "Qwen/Qwen3-Reranker-8B"
    llm_model: str = "Qwen/Qwen3-32B"

    # 向量库与文件上传（阶段 4）
    milvus_collection: str = "knowledge_chunks"
    upload_max_mb: int = 20

    # RAG 问答（§12，阶段 5）
    llm_temperature: float = 0.3
    llm_max_tokens: int = 1024
    # LLM 独立端点（§12.2 决策 3 的模型路由）：缺省回落到硅基流动；
    # 切换官方 DeepSeek API 时填 LLM_API_BASE=https://api.deepseek.com + LLM_API_KEY + LLM_MODEL=deepseek-chat
    llm_api_base: str = ""
    llm_api_key: str = ""
    # 追加到 LLM 请求体的厂商私有参数（JSON 字符串），
    # 如关闭思考模式：{"thinking": {"type": "disabled"}}（DeepSeek 官方 API 实测有效）
    llm_extra_body: str = ""
    qa_top_k: int = 20
    qa_context_chunks: int = 8
    qa_context_char_budget: int = 8000
    # COSINE 相似度拒答地板（§12.2 决策 2）：实测无关问题 ≤0.38、相关命中 ≥0.5，
    # 取 0.45 起步；R4 接入 Reranker 后以精排分数为准
    qa_score_floor: float = 0.45
    qa_rate_limit_per_min: int = 10
    qa_session_ttl_seconds: int = 3600
    qa_session_max_turns: int = 5

    # 笔记草稿（文档 → LLM 抽取要点 → 半自动建图，交付后增强）
    # map-reduce 两段式：单请求过大（实测 >3k 字符）会被硅基流动 429 拒绝，
    # 故按批提取要点（map）再汇总成笔记（reduce）
    draft_map_char_budget: int = 3000
    draft_max_batches: int = 12
    # 思考型模型（如 deepseek-flash）的 reasoning_content 与 content 共享输出上限，
    # map 阶段 max_tokens 过小会被思考段耗尽导致正文为空
    draft_map_max_tokens: int = 2000
    draft_max_tokens: int = 2048
    draft_stale_seconds: int = 600
    draft_max_note_titles: int = 100


settings = Settings()
