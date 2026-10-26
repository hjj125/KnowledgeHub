<template>
  <div class="page">
    <el-card shadow="never" class="panel">
      <div class="toolbar">
        <el-input
          v-model="query"
          placeholder="输入自然语言问题或关键词…"
          clearable
          size="large"
          class="query-input"
          @keyup.enter="doSearch"
        >
          <template #prefix><el-icon><Search /></el-icon></template>
        </el-input>
        <el-select v-model="kbId" clearable placeholder="全部知识库" class="kb-select">
          <el-option v-for="kb in kbs" :key="kb.id" :label="kb.name" :value="kb.id" />
        </el-select>
        <el-select v-model="sourceType" clearable placeholder="全部来源" class="type-select">
          <el-option label="笔记" value="note" />
          <el-option label="文档" value="document" />
        </el-select>
        <el-radio-group v-model="mode">
          <el-radio-button value="semantic">语义</el-radio-button>
          <el-radio-button value="metadata">关键词</el-radio-button>
        </el-radio-group>
        <el-button type="primary" size="large" :loading="busy" @click="doSearch">检索</el-button>
      </div>

      <el-alert
        v-if="degraded"
        title="向量服务暂不可用，本次结果来自关键词降级检索"
        type="warning"
        show-icon
        :closable="true"
        class="hint"
      />

      <div v-if="searched && results.length === 0" class="kh-empty">
        没有找到相关资料，换个问法试试
      </div>

      <div v-for="(item, i) in results" :key="i" class="result">
        <div class="result-head">
          <span class="source-badge" :class="item.source_type">
            <el-icon :size="12">
              <Notebook v-if="item.source_type === 'note'" /><Document v-else />
            </el-icon>
            {{ item.source_type === "note" ? "笔记" : "文档" }}
          </span>
          <span class="result-title">{{ item.title }}</span>
          <span v-if="item.page" class="page">第 {{ item.page }} 页</span>
          <div v-if="item.score !== null" class="score-bar">
            <div class="score-fill" :style="{ width: `${Math.min(100, item.score * 100)}%` }" />
          </div>
          <span v-if="item.score !== null" class="score-num">{{ item.score.toFixed(3) }}</span>
        </div>
        <div class="snippet" v-html="highlight(item.snippet || '（文档内容，详见原文）')" />
      </div>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from "vue";
import { ElMessage } from "element-plus";
import { Document, Notebook, Search } from "@element-plus/icons-vue";
import { apiClient } from "../api/client";
import { useKbList } from "../composables/useKb";

interface SearchResult {
  source_type: string;
  source_id: string;
  kb_id: string;
  title: string;
  snippet: string;
  page: number;
  score: number | null;
}

const { kbs, kbId, load } = useKbList();
const query = ref("");
const mode = ref<"semantic" | "metadata">("semantic");
const sourceType = ref("");
const results = ref<SearchResult[]>([]);
const degraded = ref(false);
const searched = ref(false);
const busy = ref(false);

/** 关键词高亮：对 snippet 中的查询词（按空格/标点拆分）包 <mark> */
function highlight(snippet: string): string {
  const escaped = snippet.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const terms = query.value.trim().split(/[\s,，。；;]+/).filter((t) => t.length >= 2);
  if (!terms.length) return escaped;
  const rx = new RegExp(`(${terms.map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|")})`, "gi");
  return escaped.replace(rx, "<mark>$1</mark>");
}

async function doSearch() {
  if (!query.value.trim()) return;
  busy.value = true;
  try {
    const { data } = await apiClient.post("/search", {
      query: query.value,
      kb_id: kbId.value || undefined,
      source_type: sourceType.value || undefined,
      mode: mode.value,
      top_k: 10,
    });
    results.value = data.results;
    degraded.value = data.degraded;
    searched.value = true;
  } catch {
    ElMessage.error("检索失败");
  } finally {
    busy.value = false;
  }
}

onMounted(load);
</script>

<style scoped>
.panel {
  padding: 4px;
}
.toolbar {
  display: flex;
  gap: 10px;
  margin-bottom: 14px;
  flex-wrap: wrap;
}
.query-input {
  flex: 1;
  min-width: 240px;
}
.kb-select {
  width: 160px;
}
.type-select {
  width: 120px;
}
.hint {
  margin-bottom: 12px;
}

.result {
  padding: 14px 6px;
  border-bottom: 1px solid var(--kh-border);
}
.result:last-child {
  border-bottom: none;
}
.result-head {
  display: flex;
  align-items: center;
  gap: 10px;
}
.source-badge {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 11.5px;
  font-weight: 700;
  border-radius: 6px;
  padding: 2px 7px;
  flex-shrink: 0;
}
.source-badge.note {
  background: color-mix(in srgb, var(--kh-brand) 13%, transparent);
  color: var(--kh-brand);
}
.source-badge.document {
  background: color-mix(in srgb, var(--kh-success) 15%, transparent);
  color: var(--kh-success);
}
.result-title {
  font-weight: 700;
  color: var(--kh-text);
  font-size: 15px;
}
.page {
  font-size: 12px;
  color: var(--kh-text-3);
  flex-shrink: 0;
}
.score-bar {
  width: 90px;
  height: 5px;
  border-radius: 4px;
  background: var(--kh-bg-soft);
  overflow: hidden;
  flex-shrink: 0;
}
.score-fill {
  height: 100%;
  border-radius: 4px;
  background: var(--kh-gradient);
}
.score-num {
  font-size: 12px;
  color: var(--kh-text-3);
  font-variant-numeric: tabular-nums;
  flex-shrink: 0;
}
.snippet {
  margin-top: 8px;
  color: var(--kh-text-2);
  font-size: 13.5px;
  line-height: 1.7;
  white-space: pre-wrap;
}
.snippet :deep(mark) {
  background: color-mix(in srgb, #ffd54d 55%, transparent);
  color: inherit;
  border-radius: 3px;
  padding: 0 1px;
}
</style>
