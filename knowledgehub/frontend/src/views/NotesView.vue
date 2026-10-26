<template>
  <div class="page">
    <div class="toolbar">
      <el-select v-model="kbId" placeholder="选择知识库" class="kb-select" @change="loadNotes">
        <el-option v-for="kb in kbs" :key="kb.id" :label="kb.name" :value="kb.id" />
      </el-select>
      <KbCreate />
      <el-input
        v-model="keyword"
        placeholder="按标题搜索…"
        clearable
        class="search-input"
        @keyup.enter="loadNotes"
        @clear="loadNotes"
      >
        <template #prefix><el-icon><Search /></el-icon></template>
      </el-input>
      <el-button type="primary" :disabled="!kbId" @click="startCreate">＋ 新建笔记</el-button>
    </div>

    <!-- 还没有任何知识库：引导创建 -->
    <el-card v-if="kbs.length === 0" shadow="never" class="onboard">
      <div class="onboard-icon">📚</div>
      <h3 class="onboard-title">先创建你的第一个知识库</h3>
      <p class="onboard-sub">知识库用来归组笔记与文档；创建后即可书写笔记、上传文件并获得语义检索与问答</p>
      <KbCreate type="primary" size="large" @created="loadNotes" />
    </el-card>

    <div v-else class="content">
      <div class="note-list">
        <div v-if="notes.length === 0" class="kh-empty">该知识库暂无笔记，点右上角「新建笔记」</div>
        <div
          v-for="note in notes"
          :key="note.id"
          class="note-item"
          :class="{ active: note.id === currentId }"
          @click="openNote(note.id)"
        >
          <div class="note-title">{{ note.title }}</div>
          <div class="note-meta">
            <el-tag v-for="tag in note.tags.slice(0, 2)" :key="tag" size="small" effect="plain" round>
              {{ tag }}
            </el-tag>
            <span class="time">{{ formatTime(note.updated_at) }}</span>
          </div>
        </div>
      </div>

      <el-card class="editor" shadow="never">
        <template v-if="form">
          <el-input v-model="form.title" placeholder="笔记标题" class="title-input" size="large">
            <template #prefix><el-icon><Notebook /></el-icon></template>
          </el-input>
          <el-select
            v-model="form.tags"
            multiple
            filterable
            allow-create
            default-first-option
            placeholder="标签（输入后回车创建）"
            class="tags-input"
          />
          <div class="editor-tabs">
            <el-radio-group v-model="mode" size="small">
              <el-radio-button value="edit">编辑</el-radio-button>
              <el-radio-button value="preview">预览</el-radio-button>
            </el-radio-group>
            <span class="version">v{{ form.content_version }} · {{ form.content_md.length }} 字 · 链接 {{ form.linked_note_ids.length }} 条</span>
          </div>
          <el-input
            v-if="mode === 'edit'"
            v-model="form.content_md"
            type="textarea"
            :rows="16"
            placeholder="Markdown 正文；用 [[标题]] 引用其他笔记"
            class="content-input"
          />
          <div v-else class="preview kh-md" v-html="renderMarkdown(form.content_md)" />
          <div class="editor-footer">
            <el-button @click="form = null">关闭</el-button>
            <el-button type="danger" plain @click="removeNote">删除</el-button>
            <el-button type="primary" :loading="saving" @click="saveNote">保存</el-button>
          </div>
        </template>
        <div v-else class="kh-empty">选择左侧笔记，或点右上角「新建笔记」开始书写</div>
      </el-card>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from "vue";
import { useRoute } from "vue-router";
import { ElMessage, ElMessageBox } from "element-plus";
import { Notebook, Search } from "@element-plus/icons-vue";
import DOMPurify from "dompurify";
import { marked } from "marked";
import { apiClient } from "../api/client";
import { useKbList } from "../composables/useKb";
import KbCreate from "../components/KbCreate.vue";

interface NoteSummary {
  id: string;
  title: string;
  tags: string[];
  updated_at: string;
}

interface NoteDetail extends NoteSummary {
  kb_id: string;
  content_md: string;
  properties: Record<string, unknown>;
  linked_note_ids: string[];
  content_version: number;
}

const { kbs, kbId, load } = useKbList();
const route = useRoute();
const notes = ref<NoteSummary[]>([]);
const keyword = ref("");
const currentId = ref("");
const form = ref<NoteDetail | null>(null);
const saving = ref(false);
const mode = ref<"edit" | "preview">("edit");

marked.setOptions({ breaks: true, gfm: true });

function renderMarkdown(src: string): string {
  // [[标题]] → 双链徽标
  const withLinks = (src || "").replace(/\[\[([^\[\]]+?)\]\]/g, '<span class="kh-wikilink">$1</span>');
  return DOMPurify.sanitize(marked.parse(withLinks) as string);
}

function formatTime(iso: string): string {
  return new Date(iso).toLocaleString("zh-CN", { hour12: false, month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

async function loadNotes() {
  if (!kbId.value) {
    notes.value = [];
    return;
  }
  const { data } = await apiClient.get("/notes", {
    params: { kb_id: kbId.value, q: keyword.value || undefined },
  });
  notes.value = data.items;
}

function blankNote(): NoteDetail {
  return {
    id: "",
    title: "",
    tags: [],
    updated_at: "",
    kb_id: kbId.value,
    content_md: "",
    properties: {},
    linked_note_ids: [],
    content_version: 0,
  };
}

function startCreate() {
  currentId.value = "";
  mode.value = "edit";
  form.value = blankNote();
}

async function openNote(id: string) {
  const { data } = await apiClient.get(`/notes/${id}`);
  form.value = data;
  currentId.value = id;
  mode.value = "edit";
}

async function saveNote() {
  if (!form.value) return;
  if (!form.value.title.trim()) {
    ElMessage.warning("请填写标题");
    return;
  }
  saving.value = true;
  try {
    if (currentId.value) {
      await apiClient.patch(`/notes/${currentId.value}`, {
        title: form.value.title,
        content_md: form.value.content_md,
        tags: form.value.tags,
        expected_content_version: form.value.content_version,
      });
      ElMessage.success("已保存（索引更新中）");
    } else {
      const { data } = await apiClient.post("/notes", {
        kb_id: kbId.value,
        title: form.value.title,
        content_md: form.value.content_md,
        tags: form.value.tags,
      });
      currentId.value = data.id;
      ElMessage.success("已创建");
    }
    await loadNotes();
    if (currentId.value) await openNote(currentId.value);
  } catch (e) {
    const status = (e as { response?: { status?: number } }).response?.status;
    if (status === 409) {
      ElMessage.error("版本冲突：内容已被其他会话修改，已刷新最新版本");
      if (currentId.value) await openNote(currentId.value);
    } else {
      ElMessage.error("保存失败");
    }
  } finally {
    saving.value = false;
  }
}

async function removeNote() {
  if (!currentId.value) return;
  await ElMessageBox.confirm("删除后图谱与向量将同步清理，确认删除？", "删除笔记", { type: "warning" });
  await apiClient.delete(`/notes/${currentId.value}`);
  ElMessage.success("已删除");
  form.value = null;
  currentId.value = "";
  await loadNotes();
}

onMounted(async () => {
  await load();
  await loadNotes();
  const noteId = route.query.noteId;
  const kb = route.query.kbId;
  if (typeof kb === "string" && kbs.value.some((k) => k.id === kb)) kbId.value = kb;
  await loadNotes();
  if (typeof noteId === "string") await openNote(noteId);
});
</script>

<style scoped>
.toolbar {
  display: flex;
  gap: 12px;
  margin-bottom: 16px;
}
.kb-select {
  width: 220px;
}
.search-input {
  width: 260px;
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

.content {
  display: flex;
  gap: 16px;
  align-items: flex-start;
}
.note-list {
  width: 300px;
  flex-shrink: 0;
  background: var(--kh-surface);
  border: 1px solid var(--kh-border);
  border-radius: var(--kh-radius);
  padding: 8px;
  box-shadow: var(--kh-shadow);
  max-height: calc(100vh - 200px);
  overflow-y: auto;
}
.note-item {
  padding: 11px 12px;
  border-radius: 10px;
  cursor: pointer;
  transition: background 0.15s ease;
}
.note-item:hover {
  background: var(--kh-bg-soft);
}
.note-item.active {
  background: color-mix(in srgb, var(--kh-brand) 12%, transparent);
}
.note-title {
  font-weight: 600;
  color: var(--kh-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.note-meta {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 5px;
}
.time {
  font-size: 12px;
  color: var(--kh-text-3);
  margin-left: auto;
}

.editor {
  flex: 1;
  min-width: 0;
}
.title-input {
  margin-bottom: 10px;
}
.title-input :deep(.el-input__wrapper) {
  font-weight: 600;
}
.tags-input {
  width: 100%;
  margin-bottom: 10px;
}
.editor-tabs {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 10px;
}
.version {
  color: var(--kh-text-3);
  font-size: 12px;
}
.content-input :deep(textarea) {
  font-family: Consolas, "Courier New", monospace;
  font-size: 13.5px;
  line-height: 1.7;
}
.preview {
  min-height: 380px;
  max-height: calc(100vh - 340px);
  overflow-y: auto;
  background: var(--kh-surface-2);
  border: 1px solid var(--kh-border);
  border-radius: 10px;
  padding: 16px 18px;
}
.editor-footer {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  margin-top: 14px;
}
</style>
