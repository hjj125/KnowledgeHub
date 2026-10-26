<template>
  <div class="page">
    <div class="toolbar">
      <el-select v-model="kbId" placeholder="选择知识库" class="kb-select" @change="loadDocs">
        <el-option v-for="kb in kbs" :key="kb.id" :label="kb.name" :value="kb.id" />
      </el-select>
      <KbCreate />
    </div>

    <!-- 还没有任何知识库：引导创建 -->
    <el-card v-if="kbs.length === 0" shadow="never" class="onboard">
      <div class="onboard-icon">📁</div>
      <h3 class="onboard-title">先创建一个知识库，再上传文档</h3>
      <p class="onboard-sub">文档归属于知识库；上传后自动 解析 → 分块 → 嵌入向量 → 支持语义检索与问答引用</p>
      <KbCreate type="primary" size="large" />
    </el-card>

    <template v-else>
      <el-upload
        v-if="kbId"
        class="dropzone"
        drag
        :show-file-list="false"
        :auto-upload="false"
        accept=".pdf,.docx,.pptx,.md,.txt"
        :on-change="onFilePicked"
      >
        <el-icon :size="40" class="dropzone-icon"><UploadFilled /></el-icon>
        <div class="dropzone-text">拖拽文件到此处，或 <em>点击选择</em></div>
        <template #tip>
          <div class="dropzone-tip">支持 PDF / DOCX / PPTX / MD / TXT，≤20MB；上传后自动 解析 → 分块 → 嵌入 → 入库（PPTX 可定位到第 N 张幻灯片）</div>
        </template>
      </el-upload>

      <el-alert
        v-if="kbId && docs.length === 0"
        title="该知识库还没有文档，上传第一个文件开始构建语义索引"
        type="info"
        :closable="false"
        class="hint"
      />
    </template>

    <el-table :data="docs" v-loading="loading" class="doc-table">
      <el-table-column prop="title" label="标题" min-width="160">
        <template #default="{ row }">
          <div class="doc-title">
            <el-icon :size="16" class="doc-icon"><Document /></el-icon>
            <span>{{ row.title }}</span>
          </div>
        </template>
      </el-table-column>
      <el-table-column prop="file_name" label="文件名" min-width="170" show-overflow-tooltip />
      <el-table-column label="大小" width="90">
        <template #default="{ row }">{{ (row.size_bytes / 1024).toFixed(1) }} KB</template>
      </el-table-column>
      <el-table-column label="索引流水线" min-width="220">
        <template #default="{ row }">
          <div class="pipeline">
            <div class="steps">
              <div
                v-for="(step, i) in pipelineSteps"
                :key="step.key"
                class="step"
                :class="stepState(row.status, step.key, i)"
              >
                <span class="step-dot" />
                <span class="step-label">{{ step.label }}</span>
              </div>
            </div>
            <el-tooltip v-if="row.status === 'failed' && row.error" :content="row.error" placement="top">
              <el-tag type="danger" size="small" class="fail-tag">失败</el-tag>
            </el-tooltip>
          </div>
        </template>
      </el-table-column>
      <el-table-column label="版本" width="100">
        <template #default="{ row }">v{{ row.indexed_version }} / v{{ row.content_version }}</template>
      </el-table-column>
      <el-table-column label="操作" width="230" fixed="right">
        <template #default="{ row }">
          <el-button
            v-if="row.draft_status === 'ready'"
            link
            type="primary"
            @click="openDraft(row)"
          >查看草稿</el-button>
          <el-button
            v-else-if="row.draft_status === 'generating'"
            link
            type="info"
            loading
          >生成中…</el-button>
          <el-button
            v-else
            link
            type="primary"
            :disabled="row.status !== 'ready'"
            @click="generateDraft(row)"
          >生成笔记草稿</el-button>
          <el-button link type="primary" @click="reindex(row)">重新索引</el-button>
          <el-button link type="danger" @click="remove(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="draftVisible" title="笔记草稿（LLM 生成）" width="680px" top="6vh">
      <template v-if="draftRow">
        <el-alert
          v-if="draftRow.draft_status === 'failed'"
          :title="`生成失败：${draftRow.draft_error || '请重试'}`"
          type="error"
          :closable="false"
          class="draft-flag"
        />
        <el-form label-position="top" class="draft-form">
          <el-form-item label="笔记标题（可修改）">
            <el-input v-model="draftTitle" maxlength="60" />
          </el-form-item>
        </el-form>
        <div class="draft-meta">
          来自《{{ draftRow.title }}》 · 模型 {{ draftModel || "–" }}
          <span v-if="draftPages"> · 覆盖 {{ draftPages }} 个片段</span>
        </div>
        <div class="draft-preview kh-md" v-html="renderDraft(draftBody)" />
      </template>
      <template #footer>
        <el-button @click="draftVisible = false">关闭</el-button>
        <el-button :loading="regenerating" @click="generateDraft(draftRow!, true)">重新生成</el-button>
        <el-button type="primary" :disabled="!draftBody" :loading="savingDraft" @click="saveDraftAsNote">
          保存为笔记
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from "vue";
import { useRoute } from "vue-router";
import { ElMessage, ElMessageBox } from "element-plus";
import type { UploadFile } from "element-plus";
import { Document, UploadFilled } from "@element-plus/icons-vue";
import DOMPurify from "dompurify";
import { marked } from "marked";
import { apiClient } from "../api/client";
import { useKbList } from "../composables/useKb";
import KbCreate from "../components/KbCreate.vue";

interface NoteDraft {
  title: string;
  content_md: string;
  model: string;
  request_id: string;
  source_chunks: number;
}

interface DocRow {
  id: string;
  kb_id: string;
  title: string;
  file_name: string;
  size_bytes: number;
  status: string;
  error: string | null;
  content_version: number;
  indexed_version: number;
  draft_status: string | null;
  note_draft?: NoteDraft | null;
  draft_error?: string | null;
}

const { kbs, kbId, load } = useKbList();
const route = useRoute();
const docs = ref<DocRow[]>([]);
const loading = ref(false);
let pollTimer = 0;

const pipelineSteps = [
  { key: "uploaded", label: "已上传" },
  { key: "parsing", label: "解析" },
  { key: "indexing", label: "索引" },
  { key: "ready", label: "就绪" },
];

function stepState(status: string, stepKey: string, index: number): string {
  const order = ["uploaded", "parsing", "indexing", "ready"];
  if (status === "failed") return stepKey === "indexing" ? "failed" : index < 2 ? "done" : "pending";
  const current = order.indexOf(status);
  const mine = order.indexOf(stepKey);
  if (current > mine) return "done";
  if (current === mine) return status === "ready" ? "done" : "active";
  return "pending";
}

async function loadDocs() {
  if (!kbId.value) {
    docs.value = [];
    return;
  }
  loading.value = true;
  try {
    const { data } = await apiClient.get(`/knowledge-bases/${kbId.value}/documents`);
    docs.value = data.items;
  } finally {
    loading.value = false;
  }
  schedulePoll();
}

function schedulePoll() {
  window.clearTimeout(pollTimer);
  const busy = docs.value.some(
    (d) => ["uploaded", "parsing", "indexing"].includes(d.status) || d.draft_status === "generating",
  );
  if (busy) pollTimer = window.setTimeout(loadDocs, 2000);
}

async function onFilePicked(file: UploadFile) {
  if (!kbId.value || !file.raw) return;
  try {
    const form = new FormData();
    form.append("file", file.raw);
    await apiClient.post(`/knowledge-bases/${kbId.value}/documents`, form, {
      headers: { "Content-Type": "multipart/form-data" },
    });
    ElMessage.success("已上传，后台解析索引中");
    await loadDocs();
  } catch (e) {
    const detail = (e as { response?: { data?: { detail?: unknown } } }).response?.data?.detail;
    ElMessage.error(typeof detail === "string" ? detail : "上传失败（校验未通过或服务异常）");
  }
}

async function reindex(row: DocRow) {
  await apiClient.post(`/documents/${row.id}/reindex`);
  ElMessage.success("已触发重新索引");
  await loadDocs();
}

async function remove(row: DocRow) {
  await ElMessageBox.confirm("删除后向量与分块将同步清理，确认删除？", "删除文档", { type: "warning" });
  await apiClient.delete(`/documents/${row.id}`);
  ElMessage.success("已删除");
  await loadDocs();
}

onMounted(async () => {
  await load();
  const kb = route.query.kbId;
  if (typeof kb === "string" && kbs.value.some((k) => k.id === kb)) kbId.value = kb;
  await loadDocs();
});
onBeforeUnmount(() => window.clearTimeout(pollTimer));

/* ---------- 笔记草稿 ---------- */
marked.setOptions({ breaks: true, gfm: true });

const draftVisible = ref(false);
const draftRow = ref<DocRow | null>(null);
const draftTitle = ref("");
const draftBody = ref("");
const draftModel = ref("");
const draftPages = ref(0);
const regenerating = ref(false);
const savingDraft = ref(false);

function renderDraft(src: string): string {
  return DOMPurify.sanitize(marked.parse(src || "") as string);
}

async function fetchDraft(row: DocRow): Promise<void> {
  const { data } = await apiClient.get(`/documents/${row.id}`);
  row.draft_status = data.draft_status;
  row.note_draft = data.note_draft;
  row.draft_error = data.draft_error;
}

async function generateDraft(row: DocRow, isRegenerate = false) {
  if (!row) return;
  if (isRegenerate) regenerating.value = true;
  try {
    await apiClient.post(`/documents/${row.id}/draft`);
    row.draft_status = "generating";
    if (isRegenerate) {
      draftVisible.value = false;
      ElMessage.success("已开始重新生成，完成后按钮变为「查看草稿」");
    } else {
      ElMessage.success("已开始生成草稿，完成后本行出现「查看草稿」");
    }
    schedulePoll();
  } catch (e) {
    const detail = (e as { response?: { data?: { detail?: unknown } } }).response?.data?.detail;
    if (typeof detail === "object" && detail && "reason" in detail) {
      const reason = (detail as { reason: string }).reason;
      ElMessage.warning(reason === "draft_in_progress" ? "草稿正在生成中" : `无法生成：${reason}`);
    } else {
      ElMessage.error("触发失败，请重试");
    }
  } finally {
    regenerating.value = false;
  }
}

async function openDraft(row: DocRow) {
  await fetchDraft(row);
  if (row.draft_status !== "ready" || !row.note_draft) {
    ElMessage.warning("草稿尚未就绪");
    return;
  }
  draftRow.value = row;
  draftTitle.value = row.note_draft.title;
  draftBody.value = row.note_draft.content_md;
  draftModel.value = row.note_draft.model;
  draftPages.value = row.note_draft.source_chunks;
  draftVisible.value = true;
}

async function saveDraftAsNote() {
  const row = draftRow.value;
  if (!row || !draftBody.value.trim()) return;
  if (!draftTitle.value.trim()) {
    ElMessage.warning("请填写笔记标题");
    return;
  }
  savingDraft.value = true;
  try {
    await apiClient.post("/notes", {
      kb_id: row.kb_id,
      title: draftTitle.value.trim(),
      content_md: draftBody.value,
      tags: ["文档要点"],
      source_document_id: row.id, // 图谱中建立 笔记-[来源于]->文档 边
    });
    ElMessage.success("已保存为笔记；[[链接]] 与来源文档将自动接入知识图谱");
    draftVisible.value = false;
  } catch {
    ElMessage.error("保存失败，请重试");
  } finally {
    savingDraft.value = false;
  }
}
</script>

<style scoped>
.toolbar {
  display: flex;
  gap: 12px;
  margin-bottom: 14px;
}
.kb-select {
  width: 220px;
}

.onboard {
  text-align: center;
  padding: 48px 0;
}
.onboard-icon {
  font-size: 44px;
}
.onboard-title {
  margin: 12px 0 6px;
  color: var(--kh-text);
}
.onboard-sub {
  margin: 0 auto 20px;
  max-width: 480px;
  color: var(--kh-text-3);
  font-size: 13px;
  line-height: 1.7;
}

.dropzone {
  width: 100%;
  margin-bottom: 16px;
}
.dropzone :deep(.el-upload-dragger) {
  border-radius: var(--kh-radius);
  border: 1.5px dashed var(--kh-border);
  background: var(--kh-surface);
  padding: 26px 0;
  transition: border-color 0.15s ease, background 0.15s ease;
}
.dropzone :deep(.el-upload-dragger:hover) {
  border-color: var(--kh-brand);
  background: color-mix(in srgb, var(--kh-brand) 5%, var(--kh-surface));
}
.dropzone-icon {
  color: var(--kh-brand);
}
.dropzone-text {
  color: var(--kh-text-2);
  font-size: 14px;
  margin-top: 6px;
}
.dropzone-text em {
  color: var(--kh-brand);
  font-style: normal;
  font-weight: 600;
}
.dropzone-tip {
  color: var(--kh-text-3);
  font-size: 12px;
  margin-top: 4px;
}

.hint {
  margin-bottom: 12px;
}
.doc-table {
  border-radius: var(--kh-radius);
  overflow: hidden;
}
.doc-title {
  display: flex;
  align-items: center;
  gap: 7px;
  font-weight: 600;
}
.doc-icon {
  color: var(--kh-brand);
}

.pipeline {
  display: flex;
  align-items: center;
  gap: 10px;
}
.steps {
  display: flex;
  align-items: center;
  gap: 12px;
}
.step {
  display: flex;
  align-items: center;
  gap: 5px;
  font-size: 12px;
  color: var(--kh-text-3);
}
.step-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--kh-border);
  transition: background 0.2s ease, box-shadow 0.2s ease;
}
.step.done {
  color: var(--kh-success);
}
.step.done .step-dot {
  background: var(--kh-success);
}
.step.active {
  color: var(--kh-brand);
  font-weight: 600;
}
.step.active .step-dot {
  background: var(--kh-brand);
  box-shadow: 0 0 0 4px color-mix(in srgb, var(--kh-brand) 18%, transparent);
  animation: pulse 1.4s infinite;
}
.step.failed {
  color: var(--kh-danger);
  font-weight: 600;
}
.step.failed .step-dot {
  background: var(--kh-danger);
}
@keyframes pulse {
  0%, 100% {
    box-shadow: 0 0 0 3px color-mix(in srgb, var(--kh-brand) 14%, transparent);
  }
  50% {
    box-shadow: 0 0 0 6px color-mix(in srgb, var(--kh-brand) 8%, transparent);
  }
}
.fail-tag {
  margin-left: 4px;
}

/* ---------- 笔记草稿弹窗 ---------- */
.draft-flag {
  margin-bottom: 12px;
}
.draft-form {
  margin-bottom: 6px;
}
.draft-meta {
  color: var(--kh-text-3);
  font-size: 12px;
  margin-bottom: 8px;
}
.draft-preview {
  background: var(--kh-surface-2);
  border: 1px solid var(--kh-border);
  border-radius: 10px;
  padding: 14px 16px;
  max-height: 46vh;
  overflow-y: auto;
}
</style>
