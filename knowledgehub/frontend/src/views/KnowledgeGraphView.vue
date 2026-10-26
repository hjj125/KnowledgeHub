<template>
  <div class="page graph-page">
    <div class="page-head">
      <div>
        <h2 class="page-title">知识图谱</h2>
        <p class="page-sub">Neo4j 关系图 · 笔记节点 + 文档节点 · [[双链]] 与 来源于文档 两类关系</p>
      </div>
      <div class="controls">
        <el-select v-model="selectedKb" placeholder="选择知识库" class="kb-select" @change="loadGraph">
          <el-option v-for="kb in kbs" :key="kb.id" :label="kb.name" :value="kb.id" />
        </el-select>
        <el-button :loading="loading" :disabled="!selectedKb" @click="loadGraph">
          <el-icon class="btn-icon"><Refresh /></el-icon>刷新
        </el-button>
        <el-button :disabled="!selectedKb" @click="relayout">
          <el-icon class="btn-icon"><Sort /></el-icon>重新布局
        </el-button>
        <el-button :disabled="!selectedKb" @click="fitView">
          <el-icon class="btn-icon"><FullScreen /></el-icon>适应画布
        </el-button>
      </div>
    </div>

    <div class="legend-row">
      <div class="stats-row">
        <div class="stat">
          <span class="stat-num">{{ noteCount }}</span>
          <span class="stat-label">笔记节点</span>
        </div>
        <div class="stat">
          <span class="stat-num">{{ docCount }}</span>
          <span class="stat-label">文档节点</span>
        </div>
        <div class="stat">
          <span class="stat-num">{{ graph?.edges.length ?? 0 }}</span>
          <span class="stat-label">关系边</span>
        </div>
      </div>
      <div class="legend">
        <span class="legend-item"><span class="dot note-dot" />笔记</span>
        <span class="legend-item"><span class="dot doc-dot" />文档</span>
        <span class="legend-item"><span class="line-sample solid" />[[双链]]</span>
        <span class="legend-item"><span class="line-sample dashed" />来源于文档</span>
      </div>
    </div>

    <el-alert v-if="error" :title="error" type="error" :closable="false" class="mb" />

    <div class="canvas-layout">
      <div v-show="selectedKb" class="canvas-wrap">
        <div ref="container" class="canvas" />
        <div v-if="graph && graph.nodes.length === 0" class="canvas-empty kh-empty">
          该知识库暂无笔记与文档——去「笔记」页写一篇，或「文档」页上传课件后生成草稿
        </div>
        <div v-if="hintVisible" class="canvas-hint">点击节点查看详情，双击空白处取消高亮</div>
      </div>

      <transition name="fade">
        <el-card v-if="selectedNode" shadow="never" class="info-panel">
          <template #header>
            <div class="panel-head">
              <el-tag size="small" :type="selectedNode.ntype === 'note' ? 'primary' : 'success'">
                {{ selectedNode.ntype === "note" ? "笔记" : "文档" }}
              </el-tag>
              <el-button link @click="clearSelection"><el-icon><Close /></el-icon></el-button>
            </div>
          </template>
          <h3 class="panel-title">{{ selectedNode.title }}</h3>
          <div class="panel-meta">
            <span v-if="selectedNode.file_name">文件：{{ selectedNode.file_name }}</span>
            <span>关联 {{ selectedDegree }} 条</span>
          </div>
          <div v-if="selectedExcerpt" class="panel-excerpt">{{ selectedExcerpt }}</div>
          <template #footer>
            <el-button type="primary" @click="openNode">
              {{ selectedNode.ntype === "note" ? "打开笔记" : "前往文档" }}
            </el-button>
          </template>
        </el-card>
      </transition>
    </div>
    <el-empty v-if="!selectedKb" description="先创建并选择一个知识库" />
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import cytoscape from "cytoscape";
import { Close, FullScreen, Refresh, Sort } from "@element-plus/icons-vue";
import { useRouter } from "vue-router";

import { apiClient } from "../api/client";
import { useTheme } from "../composables/useTheme";

interface GraphNode {
  id: string;
  title: string;
  type: string;
  file_name?: string | null;
}
interface GraphEdge {
  source: string;
  target: string;
  label: string;
}
interface GraphData {
  nodes: GraphNode[];
  edges: GraphEdge[];
}
interface KbItem {
  id: string;
  name: string;
}

const router = useRouter();
const { isDark } = useTheme();
const kbs = ref<KbItem[]>([]);
const selectedKb = ref("");
const graph = ref<GraphData | null>(null);
const loading = ref(false);
const error = ref<string | null>(null);
const container = ref<HTMLDivElement>();
const selectedNode = ref<{ id: string; title: string; ntype: string; file_name?: string | null } | null>(null);
const selectedExcerpt = ref("");
const hintVisible = ref(false);
let cy: cytoscape.Core | null = null;

const noteCount = computed(() => graph.value?.nodes.filter((n) => n.type === "note").length ?? 0);
const docCount = computed(() => graph.value?.nodes.filter((n) => n.type === "document").length ?? 0);

const degreeMap = computed(() => {
  const map: Record<string, number> = {};
  for (const e of graph.value?.edges ?? []) {
    map[e.source] = (map[e.source] ?? 0) + 1;
    map[e.target] = (map[e.target] ?? 0) + 1;
  }
  return map;
});

const selectedDegree = computed(() =>
  selectedNode.value ? degreeMap.value[selectedNode.value.id] ?? 0 : 0,
);

async function loadKbs() {
  const { data } = await apiClient.get<KbItem[]>("/knowledge-bases");
  kbs.value = data;
  if (data.length > 0 && !selectedKb.value) {
    selectedKb.value = data[0].id;
    await loadGraph();
  }
}

async function loadGraph() {
  if (!selectedKb.value) return;
  loading.value = true;
  error.value = null;
  selectedNode.value = null;
  try {
    const { data } = await apiClient.get<GraphData>(`/graph/${selectedKb.value}`);
    graph.value = data;
    render(data);
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  } finally {
    loading.value = false;
  }
}

function relayout() {
  if (!cy) return;
  cy.layout({ name: "cose", animate: true, animationDuration: 500, padding: 30 }).run();
}

function fitView() {
  cy?.fit(undefined, 40);
}

async function showNodePanel(node: cytoscape.NodeSingular) {
  const ntype = node.data("ntype") as string;
  const id = node.id() as string;
  selectedNode.value = {
    id,
    title: node.data("label") as string,
    ntype,
    file_name: node.data("fileName") ?? null,
  };
  selectedExcerpt.value = "";
  hintVisible.value = false;
  if (ntype === "note") {
    try {
      const { data } = await apiClient.get(`/notes/${id}`);
      selectedExcerpt.value = (data.content_md || "").slice(0, 260);
    } catch {
      selectedExcerpt.value = "";
    }
  }
}

function clearSelection() {
  selectedNode.value = null;
  selectedExcerpt.value = "";
  cy?.elements().removeClass("dimmed focused");
}

function openNode() {
  const n = selectedNode.value;
  if (!n) return;
  if (n.ntype === "note") {
    router.push({ path: "/notes", query: { kbId: selectedKb.value, noteId: n.id } });
  } else {
    router.push({ path: "/documents", query: { kbId: selectedKb.value } });
  }
}

function render(data: GraphData) {
  if (!container.value) return;
  if (cy) {
    cy.destroy();
    cy = null;
  }
  const labelColor = isDark.value ? "#cdd5e5" : "#4a5872";
  const edgeColor = isDark.value ? "#3a4460" : "#b9c2d8";
  const derivedColor = "#22a06b";

  cy = cytoscape({
    container: container.value,
    elements: [
      ...data.nodes.map((n) => ({
        data: { id: n.id, label: n.title, ntype: n.type, fileName: n.file_name ?? "" },
      })),
      ...data.edges.map((e, i) => ({
        data: { id: `e${i}`, source: e.source, target: e.target, etype: e.label },
      })),
    ],
    style: [
      {
        selector: "node",
        style: {
          label: "data(label)",
          color: labelColor,
          "font-size": 11,
          "text-valign": "bottom",
          "text-margin-y": 6,
          width: 30,
          height: 30,
          "border-width": 2,
          "border-color": "#ffffff55",
          "transition-property": "opacity",
          "transition-duration": 150,
        },
      },
      {
        selector: "node[ntype = 'note']",
        style: { "background-color": "#526BEA", shape: "ellipse" },
      },
      {
        selector: "node[ntype = 'document']",
        style: { "background-color": "#22a06b", shape: "round-rectangle" },
      },
      {
        selector: "edge",
        style: {
          width: 1.5,
          "line-color": edgeColor,
          "target-arrow-color": edgeColor,
          "curve-style": "bezier",
          "target-arrow-shape": "triangle",
          label: "data(etype)",
          "font-size": 9,
          "text-rotation": "autorotate",
          "text-background-color": isDark.value ? "#181e2d" : "#ffffff",
          "text-background-opacity": 0.85,
          "text-background-padding": "2",
          color: isDark.value ? "#8a94aa" : "#8894ad",
          "transition-property": "opacity",
          "transition-duration": 150,
        },
      },
      {
        selector: "edge[etype = 'DERIVED_FROM']",
        style: { "line-color": derivedColor, "target-arrow-color": derivedColor, "line-style": "dashed" },
      },
      {
        selector: ".dimmed",
        style: { opacity: 0.12 },
      },
      {
        selector: "node.focused",
        style: {
          "border-width": 4,
          "border-color": "#526BEA",
          width: 38,
          height: 38,
        },
      },
    ],
    layout: { name: "cose", animate: false, padding: 30 },
  });

  // 按关联度调整节点大小（枢纽节点更醒目）
  const degrees = degreeMap.value;
  cy.nodes().forEach((n) => {
    const deg = degrees[n.id()] ?? 0;
    const size = Math.min(48, 28 + deg * 5);
    n.style({ width: size, height: size });
  });

  // 交互：点击聚焦邻居 + 详情面板；点击空白清除
  const g = cy;
  g.on("tap", "node", (evt) => {
    const node = evt.target;
    g.elements().addClass("dimmed");
    node.removeClass("dimmed");
    node.neighborhood().removeClass("dimmed");
    node.addClass("focused");
    hintVisible.value = false;
    void showNodePanel(node);
  });
  g.on("tap", (evt) => {
    if (evt.target === g) {
      clearSelection();
      hintVisible.value = data.nodes.length > 0;
    }
  });
  g.on("cxttap", "node", (evt) => {
    // 右键节点：以它为中心的局部视图（居中放大）
    g.animate({ center: { eles: evt.target }, zoom: 1.6 }, { duration: 300 });
  });
}

onMounted(async () => {
  try {
    await loadKbs();
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  }
});
onBeforeUnmount(() => {
  if (cy) {
    cy.destroy();
    cy = null;
  }
});
</script>

<style scoped>
.graph-page {
  max-width: 1240px;
}
.controls {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
}
.kb-select {
  width: 200px;
}
.btn-icon {
  margin-right: 4px;
}
.mb {
  margin-bottom: 12px;
}

.legend-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 14px;
  flex-wrap: wrap;
}
.stats-row {
  display: flex;
  gap: 12px;
}
.stat {
  background: var(--kh-surface);
  border: 1px solid var(--kh-border);
  border-radius: var(--kh-radius-sm);
  padding: 8px 16px;
  box-shadow: var(--kh-shadow);
  display: flex;
  align-items: baseline;
  gap: 8px;
}
.stat-num {
  font-size: 20px;
  font-weight: 800;
  color: var(--kh-brand);
  font-variant-numeric: tabular-nums;
}
.stat-label {
  font-size: 12px;
  color: var(--kh-text-3);
}
.legend {
  display: flex;
  align-items: center;
  gap: 16px;
  font-size: 12px;
  color: var(--kh-text-2);
}
.legend-item {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  display: inline-block;
}
.note-dot {
  background: #526bea;
}
.doc-dot {
  background: #22a06b;
  border-radius: 3px;
}
.line-sample {
  width: 26px;
  height: 0;
  display: inline-block;
  border-top: 2px solid #b9c2d8;
}
.line-sample.dashed {
  border-top-style: dashed;
  border-top-color: #22a06b;
}

.canvas-layout {
  display: flex;
  gap: 14px;
  align-items: stretch;
}
.canvas-wrap {
  position: relative;
  flex: 1;
  min-width: 0;
}
.canvas {
  height: 560px;
  border: 1px solid var(--kh-border);
  border-radius: var(--kh-radius);
  background: var(--kh-surface);
  box-shadow: var(--kh-shadow);
}
.canvas-empty {
  position: absolute;
  inset: 0;
  display: grid;
  place-items: center;
  pointer-events: none;
}
.canvas-hint {
  position: absolute;
  bottom: 12px;
  left: 50%;
  transform: translateX(-50%);
  background: color-mix(in srgb, var(--kh-surface) 88%, transparent);
  border: 1px solid var(--kh-border);
  border-radius: 999px;
  padding: 4px 14px;
  font-size: 12px;
  color: var(--kh-text-3);
  pointer-events: none;
}

.info-panel {
  width: 280px;
  flex-shrink: 0;
  align-self: flex-start;
}
.panel-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.panel-title {
  margin: 0 0 8px;
  font-size: 15px;
  color: var(--kh-text);
  line-height: 1.4;
}
.panel-meta {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 12px;
  color: var(--kh-text-3);
  margin-bottom: 10px;
}
.panel-excerpt {
  background: var(--kh-surface-2);
  border: 1px solid var(--kh-border);
  border-radius: 8px;
  padding: 10px;
  font-size: 12.5px;
  line-height: 1.7;
  color: var(--kh-text-2);
  max-height: 220px;
  overflow-y: auto;
  white-space: pre-wrap;
}
</style>
