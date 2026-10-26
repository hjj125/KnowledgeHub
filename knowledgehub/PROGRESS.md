# 开发进度

> 对照《KnowledgeHub.md》V1.2 的分阶段开发计划维护本文件；每次会话结束前更新。

## 总览

| 阶段 | 内容 | 状态 |
|---|---|---|
| 阶段 0 | 脚手架与环境（四库 + 外部 API 连通） | ✅ **完成（2026-10-25）** |
| 阶段 1 | MongoDB 权威数据层 + Redis 缓存 | ✅ **完成（2026-10-25）** |
| 阶段 2 | 异步链路（Outbox + Streams + Worker） | ✅ **完成（2026-10-25）** |
| 阶段 3 | Neo4j 图同步与知识图谱 | ✅ **完成（2026-10-25）** |
| 阶段 4 | 文档上传 + Milvus 语义检索 | ✅ **完成（2026-10-25）** |
| 阶段 5 | RAG 问答（R1/R2/R3）+ 前端整合 | ✅ **完成（2026-10-25）** |
| 阶段 6 | 实验与交付 | ✅ **完成（2026-10-25）** |

## 阶段 0 验收记录（2026-10-25）

- [x] 工程目录结构（backend / frontend / workers / database / scripts / benchmarks / tests / docs/experiments），git init（main）
- [x] docker-compose.yml：MongoDB 单节点副本集（rs0，幂等初始化服务）/ Redis（AOF）/ Neo4j 5 / Milvus v3.0.2（profile=milvus）
- [x] 后端 FastAPI 骨架 + `GET /api/v1/system/health` 四库连通检查
- [x] 前端 Vue3 + TS + Vite + Pinia + Element Plus 骨架 + 系统健康页；`npm run build`（含 vue-tsc 类型检查）通过
- [x] 五容器全部 healthy；`rs.status()` 确认副本集 rs0 且 primary 就绪（事务可用）
- [x] **健康检查实测全绿**：mongodb=up(replica set ping ok) / redis=up(PONG) / neo4j=up(bolt ok) / milvus=up(server 3.0.2)
- [x] scripts/probe_embedding.py 三项探测（文本批量 / dimensions MRL / VL 图片格式），无 Key 时优雅报错（exit 2）
- [x] scripts/pull_image.py 通用镜像拉取工具（网络受限环境的副产品，见决策记录 3）

## 阶段 0 决策与教训记录

1. **Milvus 版本**：`milvusdb/milvus:v3.0.2`（2026-10 最新稳定）+ `pymilvus 3.0.2`，官方模板三件套 etcd v3.5.25（Woodpecker 内置 MQ，无独立 MQ 容器）。
2. **MongoDB**：`--replSet rs0` + 一次性 `mongodb-init` 服务（幂等）；宿主机连接串必须带 `?directConnection=true`（成员注册名是容器名 `mongodb:27017`，宿主机不可解析）。
3. **镜像拉取（网络受限环境）**：Docker Hub 直连被墙；Docker Desktop 配代理后 daemon 链路仍不稳（TLS 抖动/60MiB 截断/minio 403）。最终方案：compose 全部镜像走 `m.daocloud.io/` 前缀直连；minio 在 daocloud 全仓库 403，用自写 `scripts/pull_image.py`（Registry API + 24MiB 分块 Range 续传 + sha256 校验）从其他源拉取。已为此把 Docker Desktop 手动代理配置**回滚还原**（settings-store.json 恢复原状）。
4. **Milvus 存储模式（待办转正）**：因 minio/minio 镜像在各源均不可得，standalone 暂用 `COMMON_STORAGETYPE=local`（合法配置）；`milvus-minio` 服务保留在 `profile: minio-storage`，镜像可得后恢复 remote 三件套。对课程实验（索引/召回）无影响。
5. **Neo4j 环境变量大小写**：`NEO4J_server_memory_heap_max__size` 必须全小写尾部，大写会被 strict validation 拒绝启动。
6. **Redis**：开启 AOF（Streams 消息不因重启丢失）。
7. **Embedding 维度定稿（2026-10-25）**：probe 实测 API Key 有效、原生 4096 维、`dimensions` 参数对 VL 模型生效（1024/2048 精确返回）→ 定稿 `EMBEDDING_DIM=1024`（50 万向量原始数据 ≈2GB，内存友好）。VL 图片请求格式返回 400（与官方 EmbeddingsVLRequest schema 不符，阶段 4 校准，不阻塞维度定稿）。报告：`docs/experiments/embedding_probe_20261025_142104.json`。
8. **前端端口 5273**：5173 让给用户的人机交互作业站点；`vite.config.ts` 固定 5273 并开 `strictPort`，防止被占用时静默换端口打开错站点。

## 阶段 1 验收记录（2026-10-25）

- [x] 集合与索引：users(email 唯一) / knowledge_bases / notes(§6.3 复合索引 + 多键标签索引) / note_versions；启动时 `ensure_indexes()` 幂等创建，`database/mongodb/init_indexes.js` 为手动核对参考
- [x] 认证：Argon2id 密码哈希 + JWT（PyJWT HS256）；登出将 jti 写入 Redis 黑名单（TTL=剩余有效期）
- [x] 知识库 CRUD：列表含 `$group` 聚合 note_count；**删除为单 MongoDB 事务级联软删**（KB + 全部笔记）
- [x] 笔记 CRUD：`content_version` 乐观锁（不匹配 409 `content_version_conflict`）；创建/更新在事务内写 `note_versions` 快照；软删除；标签筛选 / 标题搜索 / 分页
- [x] Cache-Aside：`cache:note:{user_id}:{note_id}` TTL 300s，读回填写失效，响应头 `X-Cache: MISS/HIT`
- [x] 权限隔离：所有查询绑定 owner_id，跨用户访问返回 404（不泄露存在性）
- [x] **冒烟测试 23/23 通过**（`scripts/smoke_stage1.py`，含登出 token 失效、级联后版本记录保留）
- [x] **pytest 13/13 通过**（`tests/test_stage1_api.py`，2.3s）

### 阶段 1 教训

9. **pymongo 异步 API 细节**：`client.start_session()` 直接返回异步上下文管理器（不加 await），而 `session.start_transaction()` 是协程（`async with await ...`）；`find()` 返回游标对象，`aggregate()` 是协程需先 await。
10. **本机 pip**：PyPI 直连不稳，用阿里云镜像 `-i https://mirrors.aliyun.com/pypi/simple/`。

## 阶段 2 验收记录（2026-10-25）

- [x] `outbox_events` 集合：笔记/知识库的 created/updated/deleted 事件与业务变更**同一 MongoDB 事务**落库（含级联删除逐笔记事件）；乐观锁失败 = 事务中止 = 无事件（有测试）
- [x] `workers/event_publisher.py`：扫描 pending → `XADD stream:knowledge_events` → 标记 published；XADD 失败事件保留重试
- [x] `workers/runner.py` 消费框架：`graph_indexer`/`vector_indexer` 双消费组独立消费；`XREADGROUP`(block 5s) → 处理 → `XACK`；空闲时 `XAUTOCLAIM`(min-idle 10s) 认领崩溃残留的 pending 消息；连续失败 5 次标记 dead 死信
- [x] 幂等与乱序防护：Worker 以 MongoDB 当前状态为准 + `indexing_jobs` 的 `(entity_id, job_type)` 唯一记录 `indexed_version`；重复/过期事件跳过
- [x] `indexing_jobs` 任务跟踪：status(attempts/last_error/indexed_version)，笔记的 `index_status` 由 Worker 翻转 pending→indexed/deleted（答辩可视化点）
- [x] **冒烟 11/11 通过**（`scripts/smoke_stage2.py`）：正常链路 / 杀 Worker + 幽灵消费者模拟读后崩溃 + XAUTOCLAIM 恢复 + PEL 清空 / 重复投递幂等 / 乱序最终一致 / outbox 无积压
- [x] **pytest 17/17 通过**（新增 `tests/test_stage2_outbox.py` 4 项：同事务事件、版本字段、级联逐笔记事件、冲突无事件）

### 阶段 2 教训

11. **redis-py 8.x 默认 socket_timeout=5s**：与 `XREADGROUP block=5s` 竞争导致阻塞读取必然 TimeoutError。Worker 必须显式 `socket_timeout=None`；API 客户端保持短超时。Worker 主循环另加 RedisError 自愈（§10.5 降级语义）。
12. **workers 包跨目录导入**：`workers/` 在仓库根、`app/` 在 `backend/` 下，子进程需 `PYTHONPATH=<repo_root>;<backend>`（见 smoke_stage2.py 的 SUBPROCESS_ENV；容器化时以 WORKDIR+拷贝方式自然解决）。
13. **pymongo `find_one_and_update`**：`return_document` 用 `ReturnDocument.AFTER` 枚举，不能传布尔值。

## 阶段 3 验收记录（2026-10-25）

- [x] `[[标题]]` 解析（`services/links.py`）：create/update 事务内解析为同库稳定 note_id（同名取最近更新、自引用排除、解析不到跳过），存入 `linked_note_ids`
- [x] `repositories/graph.py`：Note/KnowledgeBase 唯一约束；`sync_note` = 软删 DETACH DELETE / 否则单 Neo4j 事务内 MERGE 节点+CONTAINS+**出边整体替换**（§8.2 不能只追加）；目标节点 MERGE 占位（先于目标自身事件建立边，不丢链）；`sync_kb` 节点维护
- [x] runner 派发：graph 任务 → 真实图同步；vector 保持占位（阶段 4）
- [x] 查询 API（数据全部来自 Neo4j）：`GET /notes/{id}/backlinks`、`GET /graph/{kb_id}`（节点+边）、`GET /graph/notes/{id}/neighbors`（1–3 跳，Cython 变长路径深度因 Cypher 不支持参数化而模板渲染 + int 校验）
- [x] 前端：Cytoscape 知识图谱页（KB 选择器 + cose 布局 + 节点/边统计），路由 /graph，`npm run build` 通过
- [x] **冒烟 13/13 通过**（`scripts/smoke_stage3.py`）：入图/建链/反链/图谱/邻居/**删链接清旧边**/改名 MERGE/自引用排除/笔记软删节点级联删/知识库删图清空/PEL 清空
- [x] 全量回归：阶段1 冒烟 23/23、阶段2 冒烟 11/11、pytest 17/17

### 阶段 3 教训

14. **软删除吞噬 bug（重要）**：软删除不改变 `content_version`，Worker 的"版本已最新即跳过"守卫会把 `note.deleted` 事件误判为已处理，导致删除永不同步到图。修复：`indexing_jobs` 增加 `entity_deleted` 状态位，跳过条件 = 版本最新 **且** 删除状态一致。这条对阶段 4 的向量删除同样生效。
15. git-bash curl 发送含中文的 JSON 会以 GBK 编码传输 → FastAPI 400 "error parsing the body"；调试一律用 httpx。

## 阶段 4 验收记录（2026-10-25）

- [x] 服务层四件：`services/storage.py`（GridFS，sha256 为 `fs.files._id` 天然去重，逻辑 key `{owner}/{kb}/{sha256}{ext}`，删除带引用计数）/ `services/parsing.py`（PDF 逐页保留页码 / DOCX / TXT·MD，utf-8→gbk 回退）/ `services/chunking.py`（段落→句子→硬切，目标 450/上限 600/重叠 80）/ `services/embedding.py`（批量 + 429/5xx 指数退避尊重 Retry-After + 维度护栏断言）
- [x] **VL 图片请求格式校准完成**（阶段 0 遗留）：正确格式为 `{"image": "<data-url>"}`（base64 必须带 `data:` 前缀；裸 base64、公网 URL、`image_url` 嵌套对象均 400）；`dimensions=1024` 对图片同样生效，图文统一 1024 维向量空间成立。`embed_images` 就绪，图文入库列入增量
- [x] `repositories/milvus.py`：`knowledge_chunks`（pk=uuid5(`document_id:version:chunk_index`) 幂等主键；owner_id/kb_id/document_id/source_type INVERTED 标量索引；dense HNSW(M=16, efC=200, COSINE)）；upsert/delete_by_document/search/list_by_document，全 to_thread 包裹；`database/milvus/init_collection.py` 独立建表脚本
- [x] `repositories/documents.py`：上传登记（与 `document.created` 事件同事务）/ 状态机 mark_status / reindex（版本+1 触发重建）/ 软删除（`document.deleted` 带 file_path）；KB 级联删除扩展到文档
- [x] `workers/vector_sync.py`：笔记流水线（content_md 分块→Mongo chunks 权威存储→嵌入→Milvus）+ 文档流水线（GridFS→解析→分块→状态机 uploaded→parsing→indexing→ready/failed）；向量替换"先删 document_id 全历史、再 upsert 当前"幂等；runner 增加 document 事件分支（graph 记 not-applicable，vector 实做）；publisher 透传 payload
- [x] API：`POST /knowledge-bases/{kb_id}/documents`（multipart，扩展名白名单 pdf/docx/md/txt + 魔数校验 + 20MB 上限 + 文本控制字符占比二进制检测）/ `GET .../documents`（列表）/ `GET /documents/{id}`（状态）/ `POST /documents/{id}/reindex` / `DELETE /documents/{id}`
- [x] 检索 API `POST /search`：semantic = 查询向量 → Milvus TopK（owner/kb/source_type 标量过滤）→ MongoDB chunks 回填 → 父实体版本/归属校验剔除陈旧命中 → 标题/页码/摘要；**Milvus 或 Embedding 故障自动降级 metadata 模式（degraded=true，§10.5）**；metadata = 正则关键词
- [x] 启动索引：chunks(document_id, chunk_index 唯一) / documents(kb+owner+deleted、owner+status)；main.py 启动 best-effort 建 Milvus 集合（失败不阻塞，worker ensure-on-miss 兜底）
- [x] **pytest 13/13 通过**（`tests/test_stage4_units.py`：分块不变量/重叠精确语义/硬切无损/解析页码/uuid5 主键/魔数校验）
- [x] **冒烟 26/26 通过**（`scripts/smoke_stage4.py`）：笔记语义检索命中（score=0.675）/更新后旧版本剔除/元数据模式/多用户隔离/PDF 第 2 页定位/上传三重校验(422/422/413)/空白文档 ready/GridFS 去重（fs.files 1 份 documents 2 份）/reindex v2/删笔记·删文档向量与分块清空/GridFS 引用计数保留/**Milvus 停机降级 degraded=true + 恢复后正常**/PEL 清空
- [x] **全量回归**：阶段1 冒烟 23/23、阶段2 冒烟 11/11、阶段3 冒烟 13/13、pytest 30/30

### 阶段 4 教训

16. **`app.services.storage` 模块名与工厂函数同名**：`from app.services import storage` 后 `storage()` 调用的是模块 → `TypeError: 'module' object is not callable`。跨模块引用工厂必须 `from app.services.storage import storage`。
17. **pymilvus 3.x MilvusClient.search 命中主键键名是 Schema 的 pk 字段名**（`pk`），不是 ORM 时代的 `id`；取错键抛 KeyError 会被故障降级吞掉，表现为"检索无结果"而非报错——降级路径必须记录 reason 并在测试中显式断言 degraded 标志。
18. **分块重叠拼接会把中间块顶过 max_len**（tail 80 + piece 600 > 600）：重叠尾巴须按 `max_len - len(piece)` 截断，单测硬切不变量抓出。
19. **SiliconFlow VL 图片实测 Schema**（与官方文档字面描述有出入，以实测为准）：`{"image": "<data:image/...;base64,...>"}` 200 ✓；裸 base64 / `https://` 公网 URL / `{"type":"image_url",...}` 均 400（code 20015）。混合列表 `[str | {"text"} | {"image"}]` + `dimensions` 同请求可行。
20. **冒烟测试停机降级的查询词要选 metadata 也能命中的**：语义查询词（如"三次握手"）在降级后的正则检索下可能无匹配，需区分"降级生效"与"结果非空"两个断言维度。

## 阶段 5 验收记录（2026-10-25）

- [x] **R1 检索服务化**：`services/retrieval.py` 抽取 /search 与 /qa 共用召回（Milvus TopK + MongoDB 回填校验，新增 kb_ids 多库过滤与 min_score 地板）；`POST /qa/retrieve` 无 LLM 返回带来源资料列表（§12.4 验收：自然语言问题 → 带来源资料列表 ✓）
- [x] **R2 生成与引用**：`services/llm.py`（OpenAI 兼容 chat/completions，非流式 + SSE 流式解析，跳过 reasoning 思考段，429/5xx 退避）；`services/qa.py`（去重/编号装配含字符预算/引用硬约束 sources_view/拒答分支/多轮改写/Redis 会话历史 TTL/固定窗口限流）；`POST /qa` SSE 事件流 `sources → delta* → done`（拒绝发 refusal、LLM 故障发 llm_unavailable 仅返回检索结果，§12.6 降级）
- [x] **引用硬约束（§12.2 决策 1）**：sources[] 只含真正送入 LLM 上下文的 chunk（编号/标题/页码/chunk_id/原文），回答 [n] 全部可回溯（冒烟断言 cited ⊆ sources.n）
- [x] **拒答分支（§12.2 决策 2）**：有效 chunk 为 0 或分数低于 `qa_score_floor` 时不调用 LLM；实测 Qwen3 Embedding COSINE 无关问题 ≤0.38、相关命中 ≥0.5 → 地板 0.45（R4 接 Reranker 后以精排分为准）
- [x] **多轮对话（§12.5）**：会话历史 Redis（TTL 1h，截断保留最近 5 轮），第二轮起 LLM 把追问改写为独立检索问题（改写失败静默回退）；实测"那死锁呢，怎么预防"→"死锁的定义、发生条件及预防方法有哪些？"
- [x] qa_logs 问答日志（问题/改写/回答/来源/延迟/模型，created_at 索引）
- [x] 限流：Redis 固定窗口 10 次/分钟/用户（`rate:qa:{user_id}`），第 11 次 429
- [x] LLM 模型修正：`Qwen/Qwen3-VL` 不是有效 chat 模型 id（20012），改 `Qwen/Qwen3-32B`（文本路由，§12.2 决策 3），`.env`/`.env.example`/config 默认值同步
- [x] **R3 前端整合**（`npm run build` 含 vue-tsc 通过）：auth store + axios 拦截器（token 注入/401 跳登录）+ 路由守卫；登录/注册页；**笔记管理页**（列表/新建/Markdown 编辑/标签/乐观锁 409 处理/删除）；**文档上传页**（上传/状态轮询/错误提示/重新索引/删除）；**检索页**（语义/关键词切换 + 来源定位 + 降级提示）；**问答页**（fetch SSE 流式渲染/引用角标与来源列表点击弹窗回原文/停止按钮/多会话历史 localStorage 持久化）
- [x] **pytest 13/13**（`tests/test_stage5_qa.py`：装配编号/预算/硬约束/去重/提示词/限流窗口/历史截断/改写含失败回退，stub LLM 注入）
- [x] **冒烟 14/14 通过**（`scripts/smoke_stage5.py`）：R1 列表/R2 SSE 序列与引用回溯/拒答/多轮改写+历史/qa_logs/限流 429/PEL 清空
- [x] **全量回归**：pytest 43/43，阶段 1/2/3/4 冒烟 23/11/13/26 全绿

### 阶段 5 教训

21. **`.env` 里的 LLM_MODEL 需校验模型 id 有效性**：Embedding 模型（Qwen3-VL-Embedding-8B）与 chat 模型是两个命名空间，`Qwen/Qwen3-VL` 会 20012 Model does not exist；可用 `GET /v1/models` 罗列后选定（本环境取 `Qwen/Qwen3-32B`）。
22. **TopK 召回永远不会"空手而归"**：只要库非空，无关问题也会拿到低分命中——拒答分支必须依赖**分数地板**而非"结果为 0"。Qwen3 COSINE 分数分布偏紧（无关 0.30–0.38、相关 0.5+），阈值要实测标定。
23. **异步 Redis 客户端绑定事件循环**：`redis.asyncio` 客户端缓存在 lru_cache 里跨 `asyncio.run()` 循环复用会 `RuntimeError`；测试需 `cache_clear()` 按用例重建。
24. **冒烟时序窗口（再次出现）**：就绪判定查"权威存储"（Mongo chunks）不代表派生存储（Milvus 向量）就绪——upsert 完成前的整条检索测试链都会静默失败；冒烟等待必须直接断言**最终被消费的那个存储**。
25. **Vue 模板约束**：`<template #slot>` 必须是组件的直接子节点，嵌在 `v-if` 的 `<template>` 里会触发编译器 "Cannot read properties of undefined"；用 `v-if` 下推到内容节点即可。

## 阶段 6 验收记录（2026-10-25）

**总报告：`docs/experiments/report.md`（所有数字来自 `benchmarks/results/` 原始 CSV，可复现）**

- [x] **公共设施**：`benchmarks/common.py`（分位统计/CSV 追加/中文 matplotlib/环境元数据）+ `data_gen.py`（固定种子合成笔记，含语义级"场景句"注入与图边生成）
- [x] **实验一 MongoDB 索引**（1万/5万/10万 × 4 索引配置 × 3 查询 + 执行计划 + 写开销）：10 万规模复合索引列表页 P95 **105.8→4.4ms（24 倍）**，顺序不当索引在时间范围查询劣化 2.3 倍——ESR 顺序需按查询模式定制
- [x] **实验二 Redis 缓存**（全读 Mongo vs Cache-Aside × 均匀/热点 × 并发 1-64，均异步驱动公平对比）：热点命中率 77%、命中 P50 快 1.5 倍；**诚实负结果**——本地 Mongo 点查太便宜，c=64 时缓存 P95 反超直读，缓存收益条件 = 热点集中或读路径昂贵
- [x] **实验三 Neo4j 图查询**（1k/10k/50k 节点 32 万边）：一跳/两跳/最短路径 vs Mongo 对照——**Neo4j 优势在表达力**（变长遍历与 shortestPath 单 Cypher，Mongo 需多轮应用侧 BFS），浅层数组读 Mongo 更快；Neo4j 规模扩展平稳
- [x] **实验四 A Milvus 多索引**（7686 条真实嵌入向量）：HNSW/IVF_FLAT 较 FLAT **提速 4-4.7 倍**、ANN Recall@10 无损失；方法论教训——跨集合对比必须显式主键（auto_id 不可对齐，假 0 召回）
- [x] **实验四 B 检索质量**（Dense/BM25/RRF/Reranker）：合成模板语料得**负结果并归档**（模板重复稀释嵌入 + 纯数字 ID 对 dense 不可分 cos 0.869，实证 §15.7 警告）；改用**真实文档语料**（设计文档/README/PROGRESS 章节 chunk，103 条标题→章节查询）——BM25 MRR 0.127 > dense 0.033，**Reranker 精排 4.8 倍提升 dense 至 0.158**（R4 价值实证）；查询嵌入 API 520ms 占链路延迟 93%
- [x] **实验五 一致性与故障注入**（§15.5 全部 6 情景）：端到端同步 P95 1.4s（100% 成功）、发布器中断恢复 4.4s、Worker 恢复 0.04-3.5s、重复投递**零重复**、6 连改 0.8s 收敛无旧版覆盖、软删后 0.6s 向量清除**无陈旧命中**
- [x] **实验六 并发压测**（混合负载 c=1→64）：峰值 138 req/s @c=4；高并发错误率上升主因是**乐观锁 409**（正确语义）；压测后零积压
- [x] **对账工具（§16.2）**：`python -m workers.reconciliation [--kb-id] [--dry-run]`，版本/删除状态比对 + 重新发布；实测闭环两轮（41 条历史失同步→重发布→0；容器时代 4096 维坏数据 2 条→修复→0）
- [x] **监控指标（§16.1）**：`GET /system/metrics` 聚合各库计数/Outbox 积压/任务状态/Streams PEL/图规模/Milvus entity/24h 问答量（仅全局聚合，注释注明部署时应置于网关鉴权后）
- [x] **容器化（§13.2）**：backend/frontend Dockerfile（API 与 Worker 共用镜像、nginx SSE 反代）+ compose `profile: app` 五服务；实测整栈启动 health 全绿、**容器 worker 真实完成笔记→嵌入→Milvus 闭环**；回归时需停 app 容器（消费组共享会抢事件）
- [x] **全量回归**：pytest 43/43，smoke1-5 = 23/11/13/26/14 全绿（app 容器停用后本地标准形态）

### 阶段 6 教训

26. **容器 worker 必须显式透传模型参数**：镜像内无 `.env`（.dockerignore 正确排除），`EMBEDDING_DIM` 落回默认 0 → 不带 dimensions 参数 → API 返回原生 4096 维 → 与 1024 维 Schema 冲突（code=1100）。compose 需显式列出全部模型配置项。
27. **冒烟/实验与常驻 Worker 不能共享消费组**：容器化 Worker 或对账遗留进程会把事件抢走，导致"幽灵消费者积压""等待就绪超时"等假失败。回归前置检查：`Get-CimInstance Win32_Process | ? CommandLine -match 'workers\.'` 清场。
28. **`docker compose` 跨 profile depends_on 非法**：app 服务不能依赖 profile:milvus 的 milvus-standalone；应用代码本就有 ensure-on-miss 兜底，去掉该依赖即可。
29. **Dockerfile 不支持行尾注释**（compose YAML 支持），`FROM xx # yy` 会导致构建失败；注释必须整行。
30. **合成模板语料不能用于检索质量评测**（§15.7 的实证版）：模板句重复 → chunk 嵌入稀释 → 精确余弦都找不到目标；纯数字 ID 差异对语义嵌入不可分（cos 0.87）。质量实验必须用真实文本 + 作者级标注（本报告改用仓库文档章节标题）。
31. **Neo4j 批量删除撞事务内存上限**（32 万边单事务 DETACH DELETE OOM）——必须 `WITH n LIMIT 5000` 分批；实验脚本的清理路径也要遵循。
32. **Worker 内 neo4j 异步驱动偶发 `'NoneType' object has no attribute 'send'`**（Windows 长驻事件循环相关，fresh 进程不复现）；重试 + XAUTOCLAIM 可自愈（实测 dead 记录自行收敛），如复发考虑换同步驱动。

## 笔记草稿：文档 → LLM 抽取要点 → 半自动建图（2026-10-25，交付后增强）

- [x] **触发与执行**：文档页「生成笔记草稿」按钮（仅 ready 且 chunks>0）→ `POST /documents/{id}/draft` 校验归属/防重（generating 未超时 409，超时视为卡死可重触发）→ 发布 `document.draft_requested` 事件 → **新消费组 draft_generator**（worker-draft 容器）消费执行 LLM 生成
- [x] **`workers/draft_sync.py`**：资料取自 MongoDB chunks（带页码标注 `【第N页】`，`draft_char_budget=12000` 截断）；Prompt 注入该知识库**已有笔记标题清单**，硬约束 `[[标题]]` 只能逐字引用清单内标题；输出首行 `# 标题`（缺失回退"《文档名》要点笔记"），正文 2-4 节要点 + 疑问与待办
- [x] **幂等**：事件 payload 携带 request_id，同 request_id 已生成则跳过；失败随事件重试（≤5 次死信）；reindex 重置草稿（内容已变）
- [x] **前端**：操作列「生成笔记草稿/生成中…/查看草稿」状态流转（纳入轮询）；草稿弹窗 = 可编辑标题 + Markdown 预览 + 来源片段数 + **保存为笔记**（走现有 POST /notes，[[链接]] 自动解析成边）+ 重新生成
- [x] **端到端（真实 LLM）**：上传《操作系统讲义》→ 触发 → 草稿 ready（含 `[[死锁]]` 引用与三个章节要点）→ 保存为笔记 → **Neo4j 出现新节点且自动连边到《死锁》**——半自动建图闭环打通
- [x] 单测 +9（资料组装/截断/页码标注、输出解析/回退、清单注入、触发防重生命周期），pytest 共 54/54；smoke1-5 回归 23/11/13/29/14 全绿

### 补充教训

35. **新消费组回放全历史 + 事件类型不分流 = 隐性成本泄漏**：draft 分支最初对所有 document 事件都触发生成，而新消费组从 0-0 建组回放整条流——每个历史文档白烧一次 LLM 调用（回放期间还阻塞了真实请求数分钟）。修复：draft 任务只响应 `document.draft_requested`，其余 document 事件快速 skip；note 分支对未知 job_type 一律提前 skip 不落任务记录。新增消费组时必须逐事件类型审读派发语义。

36. **硅基流动按"请求大小"限流（code 50609 System is too busy）**：实测同一时刻 3k 字符请求通过、6k/12k 一律 429（小探针正常，排除全局限流）——长文档摘要不能用单次大请求。解法：**map-reduce 两段式**（`draft_map_char_budget=3000` 按批提取要点 → 汇总成笔记），每次调用都是小请求；配套加强 llm.py 429 退避（5s 起指数到 60s、重试 5 次）并把 attempts 语义改为**连续失败次数**（成功清零、用户重触发清零），否则死信记录会让后续重试一次就死。

### 用户实测修复记录（进程控制与管理.pptx）

- 症状：87 分块的课件生成草稿全部 429 → 7 次尝试后死信，draft_status=failed
- 修复链：429 退避加强 → attempts 语义修正 → map-reduce（87 分块 → 4 批 map + 1 次 reduce）
- 结果：draft ready（"进程控制与管理要点笔记"，809 字、4 小节）；[[链接]] 为空是正确行为——该库暂无其他笔记，清单为空时 Prompt 禁止写 [[]]

## LLM 端点解耦：支持官方 DeepSeek API（2026-10-25，用户需求）

- [x] 动机：Embedding 保留硅基流动（§9.8 定稿），LLM 切换官方 DeepSeek API（§12.2 决策 3 的模型路由落地）
- [x] `config` 新增 `llm_api_base/llm_api_key`（**必须成对配置**：单独设置其一会在工厂处抛错，防止"DeepSeek 端点 + 硅基流动 Key"式混搭 401；同时留空回落硅基流动）；LLM 客户端更名 `OpenAICompatLLM`（任意 OpenAI 兼容服务）
- [x] compose 五服务透传 `LLM_API_BASE/LLM_API_KEY`；`.env` 注释给出三行切换步骤（BASE/KEY/MODEL=deepseek-chat 一起改）
- [x] 当前运行态：**官方 DeepSeek API + deepseek-flash**（用户已自行填 Key 并重启）；QA 链路穿透 8088 验证（SSE + 引用正常）
- [x] 重负载验证：87 分块课件重新生成草稿，全程零 429、18-70 秒完成（硅基流动时代同场景 429×7 死信）

### 补充教训

37. **思考型模型的 reasoning_content 与 content 共享 max_tokens 输出上限**：`deepseek-flash` 在 map 阶段 `max_tokens=700` 时随机出现"思考段耗尽配额 → 正文为空"（finish_reason=length），这是草稿前 3 批 0 字符的根因。修复：map 输出上限提至 2000 + 空输出加倍重试一次 + 仍为空用原文摘录兜底。
38. **关闭思考模式的参数形态各家不一，必须实测**：DeepSeek 官方 API 实测 `"thinking": {"type": "disabled"}` 与 `reasoning_effort=none` 有效（reasoning=0），`enable_thinking=false`（硅基流动风格）与 `chat_template_kwargs` 被忽略。落地为通用 `LLM_EXTRA_BODY`（JSON 合并进请求体），任何厂商私有参数都可配置；实测关思考后 map 全部饱满（0/0/0/594 → 778/2574/2487/1581）、速度快约 30%、且 LLM 正确写出了指向已有笔记的 `[[处理器调度的三个层次与常见算法]]`。

39. **nginx 默认 1MB 请求体限制静默拦截正常上传**：容器化前端（8088→nginx→backend）上传 1MB~20MB 的文件会被 nginx 以默认 `client_max_body_size 1m` 拒绝，返回 **HTML 格式的 413**（不是应用层的 JSON 413），前端报错信息也变得不可读；应用层的 20MB 校验根本没机会执行。修复：nginx.conf 加 `client_max_body_size 25m`（略高于应用限制，让超限由 FastAPI 返回结构化错误）。反代的每一层都有隐式限额，上传链路要端到端验证真实文件大小。

## PPTX 支持（2026-10-25，交付后追加）

- [x] `parse_pptx`：逐张幻灯片提取文本框/组合形状/表格 + 演讲者备注（"备注："前缀），**幻灯片编号作页码**——检索结果可定位到"第 N 张"
- [x] 上传白名单加 `pptx`（与 docx 同为 OOXML，魔数 `PK\x03\x04`）；requirements.txt 加 `python-pptx>=1.0`；前端拖拽区 accept 与提示同步
- [x] 单测 +2（页码保留 / 魔数），`test_stage4_units` 15/15；smoke_stage4 +3 用例（上传/ready/检索命中第 2 张幻灯片）29/29
- [x] 端到端（穿透 8088）：课件 pptx → ready → "栈溢出" 查询命中第 2 张幻灯片（page=2）

### 补充教训

33. **compose 多服务共用同一 Dockerfile ≠ 共用镜像**：backend / event-publisher / worker-graph / worker-vector 各有 `build:` → 各自镜像 `knowledgehub-<service>`，改代码后只 `build backend` 会让 worker 继续跑旧解析器（pptx 被当文本解析出二进制乱码 chunk）。改共享代码必须 `docker compose --profile app build` 全量重建。
34. **Windows 下 127.0.0.1:8000 与 0.0.0.0:8000 可共存**：残留的本地 uvicorn 与 Docker 端口代理同时监听，`127.0.0.1` 回环优先命中旧进程——冒烟结果"看似新 bug 实为旧代码"。回归前 `netstat -ano | grep :8000` 确认唯一监听者。

## 前端界面升级（2026-10-25，阶段 6 后追加）

- [x] 设计系统：`styles/index.css` 设计令牌（亮/暗双主题 CSS 变量）+ Element Plus 全局覆盖 + Markdown 排版；暗色模式（Element dark css-vars + 持久化偏好，`useTheme`）
- [x] 应用外壳重做：侧边栏导航（可折叠 + 图标）+ 毛玻璃顶栏 + 用户下拉菜单 + 主题切换
- [x] 登录页：渐变光斑背景 + 品牌卡
- [x] 问答页：回答 **Markdown 渲染**（marked + DOMPurify 消毒）与 [n] 引用角标混排点击回原文、来源卡片、欢迎页建议问题、打字动画、会话列表精修
- [x] 笔记页：编辑/预览切换 + [[双链]] 徽标渲染、卡片化列表、字数/链接统计
- [x] 文档页：拖拽上传区 + **流水线步骤条**（已上传→解析→索引→就绪，脉冲动画）
- [x] 检索页：查询词 `<mark>` 高亮 + 相关度渐变条 + 来源徽标
- [x] 系统状态页：四库状态卡（呼吸灯）+ `/system/metrics` 八项聚合指标卡
- [x] 图谱页：统计卡（节点/边/平均链接度）+ 主题自适应画布配色
- [x] 新增依赖：marked、dompurify（`npm install --registry npmmirror`）；`npm run build`（vue-tsc）通过
- [x] **交付链路端到端验证**（穿透前端 nginx :8088）：注册→建库→[[双链]] 事务解析→容器 worker 向量化→语义检索命中→**/qa SSE 经 nginx 反代流式回答**（sources 2 条 + 176 字回答）

## 遗留事项 / 交付增强（可选）

- [ ] 图文混排（R4）：页面截图走 `embed_images` 入库 source_type=image（格式已校准）
- [ ] 文档来源跳转的页内定位（PDF 阅读器视图）
- [ ] 大规模档实验（10 万+ 向量）在 32GB 机器上补测
- [ ] 4B 人工盲标评测集扩充（当前 103 条标题构造）
- [ ] minio 镜像可得后恢复 Milvus remote 三件套
- [ ] CI（GitHub Actions：pytest + smoke + 前端 build）
