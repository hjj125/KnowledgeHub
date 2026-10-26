// KnowledgeHub MongoDB 初始索引（§6.3）
// 应用启动时会通过 backend/app/main.py ensure_indexes() 幂等创建同样的索引；
// 本脚本用于手动初始化/核对：mongosh mongodb://localhost:27017 database/mongodb/init_indexes.js

use knowledgehub;

// 账号邮箱唯一
db.users.createIndex({ email: 1 }, { unique: true });

// 个人知识库中的最新笔记列表（§6.3）
db.notes.createIndex({ owner_id: 1, kb_id: 1, deleted_at: 1, updated_at: -1 });

// 用户标签筛选（数组字段多键索引）
db.notes.createIndex({ owner_id: 1, tags: 1 });

// 知识库维度筛选与级联软删除
db.notes.createIndex({ kb_id: 1, deleted_at: 1 });

// 知识库列表
db.knowledge_bases.createIndex({ owner_id: 1, deleted_at: 1 });

// 笔记历史版本
db.note_versions.createIndex({ note_id: 1, version: -1 });

// 阶段 2：事件发布器扫描
db.outbox_events.createIndex({ status: 1, created_at: 1 });

// 阶段 2：后台任务跟踪；(entity_id, job_type) 唯一，支撑幂等 upsert
db.indexing_jobs.createIndex({ entity_id: 1, job_type: 1 }, { unique: true });

// 阶段 4：文档列表与状态筛选
db.documents.createIndex({ kb_id: 1, owner_id: 1, deleted_at: 1 });
db.documents.createIndex({ owner_id: 1, status: 1 });

// 阶段 4：分块文本权威存储；(document_id, chunk_index) 唯一，替换式写入
db.chunks.createIndex({ document_id: 1, chunk_index: 1 }, { unique: true });
