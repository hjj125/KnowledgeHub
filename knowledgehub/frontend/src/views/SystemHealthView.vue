<template>
  <div class="page health-page">
    <div class="page-head">
      <div>
        <h2 class="page-title">系统状态</h2>
        <p class="page-sub">四库连通性与聚合指标（MongoDB / Redis / Neo4j / Milvus）</p>
      </div>
      <el-button :loading="health.loading" @click="refreshAll">
        <el-icon class="btn-icon"><Refresh /></el-icon>刷新
      </el-button>
    </div>

    <div v-if="services.length" class="cards">
      <div v-for="s in services" :key="s.name" class="svc-card" :class="s.status">
        <div class="svc-icon">
          <el-icon :size="22"><component :is="s.icon" /></el-icon>
        </div>
        <div class="svc-body">
          <div class="svc-name">{{ s.label }}</div>
          <div class="svc-detail">{{ s.detail }}</div>
        </div>
        <span class="svc-status" :class="s.status">
          <span class="pulse-dot" />{{ s.status === "up" ? "在线" : "离线" }}
        </span>
      </div>
    </div>

    <el-alert
      v-if="health.error"
      :title="`无法连接后端 API：${health.error}`"
      type="error"
      show-icon
      :closable="false"
      class="hint"
    />

    <el-card v-if="metrics" shadow="never" class="metrics-card">
      <template #header><span class="metrics-head">聚合指标 · /system/metrics</span></template>
      <div class="metric-grid">
        <div class="metric">
          <div class="metric-value">{{ metrics.mongodb?.counts?.notes ?? "–" }}</div>
          <div class="metric-label">笔记总数</div>
        </div>
        <div class="metric">
          <div class="metric-value">{{ metrics.mongodb?.counts?.documents ?? "–" }}</div>
          <div class="metric-label">文档总数</div>
        </div>
        <div class="metric">
          <div class="metric-value">{{ metrics.milvus?.entities ?? "–" }}</div>
          <div class="metric-label">Milvus 向量</div>
        </div>
        <div class="metric">
          <div class="metric-value">{{ metrics.neo4j?.notes ?? "–" }}</div>
          <div class="metric-label">图笔记节点</div>
        </div>
        <div class="metric">
          <div class="metric-value">{{ metrics.neo4j?.links ?? "–" }}</div>
          <div class="metric-label">图链接边</div>
        </div>
        <div class="metric">
          <div class="metric-value" :class="{ warn: (metrics.mongodb?.outbox_pending ?? 0) > 0 }">
            {{ metrics.mongodb?.outbox_pending ?? "–" }}
          </div>
          <div class="metric-label">Outbox 积压</div>
        </div>
        <div class="metric">
          <div class="metric-value">{{ metrics.redis?.stream_length ?? "–" }}</div>
          <div class="metric-label">Stream 长度</div>
        </div>
        <div class="metric">
          <div class="metric-value">{{ metrics.qa?.qa_logs_24h ?? "–" }}</div>
          <div class="metric-label">24h 问答量</div>
        </div>
      </div>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { Coin, Cpu, DataLine, MagicStick, Refresh } from "@element-plus/icons-vue";
import { useHealthStore } from "../stores/health";
import { apiClient } from "../api/client";

const health = useHealthStore();
const metrics = ref<any>(null);

const ICONS: Record<string, any> = {
  mongodb: Coin,
  redis: MagicStick,
  neo4j: Cpu,
  milvus: DataLine,
};
const LABELS: Record<string, string> = {
  mongodb: "MongoDB · 权威数据源",
  redis: "Redis · 缓存与事件流",
  neo4j: "Neo4j · 知识图谱",
  milvus: "Milvus · 向量检索",
};

const services = computed(() => {
  const map = health.snapshot?.services ?? {};
  return Object.entries(map).map(([name, s]) => ({
    name,
    label: LABELS[name] ?? name,
    icon: ICONS[name] ?? Cpu,
    status: s.status,
    detail: s.detail ?? "",
  }));
});

async function refreshAll() {
  await health.refresh();
  try {
    metrics.value = (await apiClient.get("/system/metrics")).data;
  } catch {
    metrics.value = null;
  }
}

onMounted(refreshAll);
</script>

<style scoped>
.health-page {
  max-width: 960px;
}
.btn-icon {
  margin-right: 4px;
}

.cards {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 14px;
  margin-bottom: 16px;
}
.svc-card {
  display: flex;
  align-items: center;
  gap: 12px;
  background: var(--kh-surface);
  border: 1px solid var(--kh-border);
  border-radius: var(--kh-radius);
  padding: 16px;
  box-shadow: var(--kh-shadow);
}
.svc-icon {
  width: 44px;
  height: 44px;
  display: grid;
  place-items: center;
  border-radius: 12px;
  background: color-mix(in srgb, var(--kh-brand) 10%, transparent);
  color: var(--kh-brand);
  flex-shrink: 0;
}
.svc-card.down .svc-icon {
  background: color-mix(in srgb, var(--kh-danger) 12%, transparent);
  color: var(--kh-danger);
}
.svc-body {
  flex: 1;
  min-width: 0;
}
.svc-name {
  font-weight: 700;
  font-size: 14px;
  color: var(--kh-text);
}
.svc-detail {
  font-size: 12px;
  color: var(--kh-text-3);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.svc-status {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  font-weight: 600;
  flex-shrink: 0;
}
.svc-status.up {
  color: var(--kh-success);
}
.svc-status.down {
  color: var(--kh-danger);
}
.pulse-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: currentColor;
}
.svc-status.up .pulse-dot {
  animation: pulse-dot 1.6s infinite;
}
@keyframes pulse-dot {
  0%, 100% {
    box-shadow: 0 0 0 0 color-mix(in srgb, var(--kh-success) 45%, transparent);
  }
  60% {
    box-shadow: 0 0 0 6px transparent;
  }
}

.hint {
  margin-bottom: 12px;
}
.metrics-card {
  border-radius: var(--kh-radius);
}
.metrics-head {
  font-weight: 700;
  font-size: 14px;
}
.metric-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
  gap: 12px;
}
.metric {
  background: var(--kh-surface-2);
  border: 1px solid var(--kh-border);
  border-radius: 12px;
  padding: 14px;
  text-align: center;
}
.metric-value {
  font-size: 24px;
  font-weight: 800;
  color: var(--kh-brand);
  font-variant-numeric: tabular-nums;
}
.metric-value.warn {
  color: var(--kh-warning);
}
.metric-label {
  margin-top: 4px;
  font-size: 12px;
  color: var(--kh-text-3);
}
</style>
