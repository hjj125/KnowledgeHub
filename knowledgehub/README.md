# 知序 KnowledgeHub

基于异构 NoSQL 数据库协同（MongoDB + Redis + Neo4j + Milvus）的个人知识管理与智能检索系统。
《大数据管理》课程设计。设计文档见仓库上级目录 `KnowledgeHub.md`（V1.2）。

## 目录结构

```text
knowledgehub/
├── backend/          # FastAPI 后端（app/ 下分 core / api / services / repositories）
├── frontend/         # Vue 3 + TypeScript + Vite + Element Plus
├── workers/          # 异步 Worker（阶段 2 起实现：publisher / graph / vector / reconciliation）
├── database/         # 数据库初始化脚本（索引 / 约束 / Collection）
├── scripts/          # 探测与运维脚本（probe_embedding.py 等）
├── benchmarks/       # 数据生成与性能实验（阶段 6）
├── tests/            # pytest 测试
├── docs/experiments/ # 实验记录（探测报告、原始数据）
├── docker-compose.yml
└── .env / .env.example
```

## 快速开始

前置：Docker Desktop（WSL2 后端）、Python 3.12+、Node 20+。

```bash
# 1. 配置环境变量（至少填写 SILICONFLOW_API_KEY）
cp .env.example .env

# 2. 启动基础设施
docker compose up -d                 # MongoDB / Redis / Neo4j
docker compose --profile milvus up -d  # 加上 Milvus 三件套（内存紧张时可不启）

# 3. 后端
cd backend
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt   # Linux: .venv/bin/pip
.venv/Scripts/python -m uvicorn app.main:app --reload --port 8000
# 健康检查： http://127.0.0.1:8000/api/v1/system/health

# 4. 前端
cd frontend
npm install
npm run dev    # http://localhost:5273 （/api 已代理到 8000；5173 留给其他项目，strictPort 固定端口）

# 5. Embedding API 探测（阶段 0 Spike，维度定稿依据）
.venv/Scripts/python scripts/probe_embedding.py   # 需在仓库根目录运行

# 6. 阶段 1 冒烟测试与回归测试（需后端已启动）
.venv/Scripts/python scripts/smoke_stage1.py
.venv/Scripts/python -m pytest tests/ -v

# 7. 异步链路（阶段 2）：三个进程分别开终端运行（或后台）
#    Windows 注意 PYTHONPATH 需同时含仓库根与 backend/（见 scripts/smoke_stage2.py）
.venv/Scripts/python -m workers.event_publisher    # Outbox → Redis Streams
.venv/Scripts/python -m workers.graph_indexer      # 图索引消费组（阶段 3 实装）
.venv/Scripts/python -m workers.vector_indexer     # 向量索引消费组（阶段 4 实装）

# 8. 阶段 2 全链路冒烟（自动拉起/终止上述三个进程，含崩溃恢复场景）
.venv/Scripts/python scripts/smoke_stage2.py

# 9. 阶段 3 图谱同步冒烟（建链/删链/改名/级联删除后图谱正确性）
.venv/Scripts/python scripts/smoke_stage3.py

# 10. 阶段 4 文档上传与语义检索冒烟（PDF/TXT 上传→ready→带页码检索→删除清理→Milvus 停机降级）
.venv/Scripts/python scripts/smoke_stage4.py

# 11. 阶段 5 RAG 问答冒烟（R1 检索列表 / R2 SSE 引用回溯 / 拒答 / 多轮改写 / 限流）
.venv/Scripts/python scripts/smoke_stage5.py

# 12. 实验复现（阶段 6；数字与结论见 docs/experiments/report.md）
.venv/Scripts/python ../benchmarks/exp1_mongo_index.py      # MongoDB 索引对比
.venv/Scripts/python ../benchmarks/exp2_redis_cache.py      # Redis 缓存收益
.venv/Scripts/python ../benchmarks/exp3_neo4j_graph.py      # Neo4j 图查询（含 32 万边清理，约 5 分钟）
.venv/Scripts/python ../benchmarks/exp4a_milvus_index.py    # Milvus 三索引对比（含真实嵌入，首次约 5 分钟）
.venv/Scripts/python ../benchmarks/exp4b_retrieval_quality.py  # Dense/BM25/混合/Reranker 质量对比
.venv/Scripts/python ../benchmarks/exp5_consistency.py      # 一致性与故障注入（需后端已启动）
.venv/Scripts/python ../benchmarks/exp6_concurrency.py      # 混合并发压测（需后端已启动）

# 13. 可靠性工具（§16）
.venv/Scripts/python -m workers.reconciliation --dry-run    # 派生索引对账（先预览）
.venv/Scripts/python -m workers.reconciliation              # 实际重新发布失同步任务
curl http://127.0.0.1:8000/api/v1/system/metrics            # 聚合监控指标
```

> 冒烟/实验前置检查：若 `docker compose --profile app up` 的常驻容器 Worker 在运行，
> 请先 `docker compose --profile app stop`——它们与脚本自拉起的 Worker 共享消费组会抢事件。

前端页面（`npm run dev` → http://localhost:5273）：`/qa` 智能问答（SSE 流式 + 引用角标点击回原文 + 会话历史）；`/search` 检索（语义/关键词 + 降级提示）；`/notes` 笔记管理（Markdown 编辑 + `[[链接]]`）；`/documents` 文档上传与索引状态；`/graph` 知识图谱；`/health` 四库状态。

## 容器化部署（阶段 6 交付）

```bash
docker compose --profile milvus --profile app up -d --build
# backend      → http://127.0.0.1:8000（API 与三个 Worker 共用镜像，command 区分）
# frontend     → http://127.0.0.1:8088（nginx 托管 + /api 反代，SSE 已配置禁缓冲）
# event-publisher / worker-graph / worker-vector 常驻容器
```

容器网络内直连服务名（`mongodb`/`redis`/`neo4j`/`milvus-standalone`），模型配置（EMBEDDING_DIM/LLM_MODEL 等）由 `.env` 经 compose environment 显式透传——镜像不含 `.env`（.dockerignore 排除）。

## 实验与报告（阶段 6）

六组核心实验（MongoDB 索引 / Redis 缓存 / Neo4j 图查询 / Milvus 索引与检索质量 / 跨库一致性故障注入 / 整体并发）全部可复现：
脚本在 `benchmarks/`（固定随机种子），原始 CSV 与统计图在 `benchmarks/results/`，
**结论与数字汇总见 [docs/experiments/report.md](docs/experiments/report.md)**（含限制声明与 4B 评测集修订记录）。

## 已实现 API（阶段 1–6）

统一前缀 `/api/v1`；除认证接口外均需 `Authorization: Bearer <JWT>`。

| 模块 | 方法与路径 | 说明 |
|---|---|---|
| 认证 | POST `/auth/register` / `/auth/login` / `/auth/logout` | Argon2id + JWT；登出写入 Redis 黑名单 |
| 知识库 | GET/POST `/knowledge-bases`，GET/PATCH/DELETE `/{kb_id}` | 列表含 `$group` 聚合的 note_count；删除为**单事务级联软删**全部笔记与文档 |
| 笔记 | GET/POST `/notes`，GET/PATCH/DELETE `/{note_id}` | 详情走 Cache-Aside（响应头 `X-Cache: MISS/HIT`，TTL 300s） |
| 笔记 | GET `/{note_id}/versions` | 每次更新在事务内写版本快照 |
| 笔记 | PATCH 携带 `expected_content_version` | 乐观锁：不匹配返回 409 `content_version_conflict` |
| 笔记 | `?kb_id=&tag=&q=&page=&page_size=` | 标签筛选（多键索引）、标题正则搜索、分页 |
| 笔记 | GET `/{note_id}/backlinks` | 反向链接（数据来自 Neo4j） |
| 图谱 | GET `/graph/{kb_id}`、GET `/graph/notes/{id}/neighbors?depth=` | 全库图谱（节点+边）与 1–3 跳邻居（Neo4j 查询） |
| 笔记 | 内容中写 `[[标题]]` | 保存时在事务内解析为同库稳定 note_id（同名取最近更新、自引用排除） |
| 文档 | POST `/knowledge-bases/{kb_id}/documents` | multipart 上传（pdf/docx/pptx/md/txt 白名单 + 魔数校验 + 20MB 上限；PPTX 按幻灯片编号定位页码）；原件落 GridFS（sha256 去重） |
| 文档 | GET `/knowledge-bases/{kb_id}/documents`、GET `/documents/{id}` | 列表 / 流水线状态（uploaded→parsing→indexing→ready/failed） |
| 文档 | POST `/documents/{id}/reindex`、DELETE `/documents/{id}` | 版本+1 触发重新索引；删除级联清理向量/分块/GridFS（引用计数） |
| 文档 | POST `/documents/{id}/draft` | **笔记草稿（半自动建图）**：LLM 抽取文档要点生成单篇结构化笔记（异步，事件驱动 worker）；草稿注入知识库已有笔记标题清单，保存为笔记后 `[[链接]]` 自动接入知识图谱 |
| 检索 | POST `/search` | `{query, kb_id?, source_type?, top_k?, mode?}`；semantic：Milvus TopK + MongoDB 回填校验（标题/页码/摘要）；**向量服务故障自动降级 metadata 并置 `degraded: true`** |
| 问答 | POST `/qa/retrieve` | R1：召回 + 回填后的带来源资料列表（无 LLM） |
| 问答 | POST `/qa` | R2：SSE 流式（`sources → delta* → done`）；回答带 [n] 引用，`sources[]` 即送入 LLM 的 chunk（引用硬约束）；**证据不足走拒答分支（分数地板，不调 LLM）**；多轮追问自动改写；限流 10 次/分钟/用户（429）；LLM 故障仅返回检索结果 |
| 运维 | GET `/system/metrics` | 聚合监控指标（§16.1）：各库计数、Outbox 积压、任务状态分布、Streams/Pending、图规模、Milvus entity 数、24h 问答量；仅全局聚合值，部署时应置于网关鉴权之后 |

依赖安装提示：本机网络下 pip 直连 PyPI 不稳，可用阿里云镜像：
`pip install -i https://mirrors.aliyun.com/pypi/simple/ -r backend/requirements.txt`

## 说明

- **MongoDB**：单节点副本集 `rs0`（由 `mongodb-init` 一次性服务初始化），启用多文档事务以支持 Transactional Outbox。宿主机连接串带 `directConnection=true`（副本集成员注册名是容器名，宿主机不可解析）。
- **Milvus**：官方 v3.0.2 Standalone 模板（milvus + etcd），放在 `profile: milvus` 下按需启动。当前以 `COMMON_STORAGETYPE=local`（本地文件系统对象存储）运行——因镜像源对 `minio/minio` 全仓库 403（见 compose 内注释）；内部 minio 服务保留在 `profile: minio-storage`，镜像可拉取后恢复 remote 模式。`knowledge_chunks` 集合由 API/Worker 启动时自动创建（或手动 `backend/.venv/Scripts/python database/milvus/init_collection.py`）。
- **向量维度**：已定稿 `EMBEDDING_DIM=1024`（2026-10-25 probe 实测：原生 4096，`dimensions` 参数对 VL 模型生效，图片与文本同请求均可用）。定稿后任何模型/维度变更等于全量重建 Milvus Collection（设计文档 §9.8.3 硬约束）。
- **文件存储**：P0 用 MongoDB GridFS（sha256 作 `fs.files._id`，同字节文件跨库去重，删除带引用计数）；`file_path` 只存逻辑 key，后期可平滑迁移 MinIO。
- **检索链路**：笔记/文档统一进入 `knowledge_chunks`（pk=uuid5 幂等，Milvus HNSW+COSINE；MongoDB `chunks` 为分块文本权威存储）；搜索经 Milvus 标量过滤（owner/kb/source_type）→ MongoDB 回填版本/归属校验，剔除陈旧命中。
- **RAG 问答**：模型三件套独立可配（`EMBEDDING_MODEL` / `LLM_MODEL` / `RERANKER_MODEL`）；chat 模型须用有效 id（当前 `Qwen/Qwen3-32B`）；拒答地板 `qa_score_floor=0.45`（实测无关问题 ≤0.38、相关 ≥0.5，R4 Reranker 后以精排分为准）；会话历史在 Redis（TTL 1h），qa_logs 落 MongoDB。
- **安全**：`.env` 含密钥，已被 `.gitignore` 排除；提交前确认不含真实密钥。

## 网络受限环境的镜像拉取

compose 内镜像全部使用 `m.daocloud.io/` 加速前缀（上游原始引用见行尾注释）。若某镜像在 daocloud 不可得，可用自带脚本从其他源拉取后 `docker load`：

```bash
.venv/Scripts/python scripts/pull_image.py --image <上游引用> \
    [--mirror https://docker.1ms.run] [--proxy http://127.0.0.1:7897] [--tag <compose 内引用>]
docker load -i tmp/pull/<slug>.tar
```

脚本走 Registry HTTP API（动态解析认证域），带 24MiB 分块 Range 断点续传与 sha256 校验，可对抗不稳定链路。
