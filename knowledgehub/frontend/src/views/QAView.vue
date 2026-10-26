<template>
  <div class="page">
    <div class="sidebar">
      <el-button type="primary" plain class="new-btn" @click="newSession">＋ 新会话</el-button>
      <div
        v-for="s in sessions"
        :key="s.id"
        class="session-item"
        :class="{ active: s.id === activeId }"
        @click="switchSession(s.id)"
      >
        <el-icon :size="14"><ChatDotRound /></el-icon>
        <span class="session-title">{{ s.title || "新会话" }}</span>
      </div>
    </div>

    <el-card shadow="never" class="chat">
      <div ref="scrollBox" class="messages">
        <div v-if="messages.length === 0" class="welcome">
          <div class="welcome-logo">知</div>
          <h2 class="welcome-title">向你的知识库提问</h2>
          <p class="welcome-sub">回答附带可点击的引用来源，点击角标回溯原文位置</p>
          <div class="suggestions">
            <button v-for="q in suggestions" :key="q" class="suggestion" @click="draft = q; send()">
              {{ q }}
            </button>
          </div>
        </div>

        <div v-for="(m, i) in messages" :key="i">
          <div v-if="m.role === 'user'" class="msg user">
            <div class="bubble user">{{ m.text }}</div>
            <div class="avatar user-avatar">我</div>
          </div>
          <div v-else class="msg assistant">
            <div class="avatar ai-avatar">知</div>
            <div class="bubble assistant" @click="onBubbleClick($event, m)">
              <el-alert
                v-if="m.degraded"
                title="向量服务不可用，本次回答基于关键词降级检索"
                type="warning"
                :closable="false"
                class="flag"
              />
              <el-alert
                v-if="m.llmUnavailable"
                title="AI 生成暂不可用，以下为检索到的资料"
                type="info"
                :closable="false"
                class="flag"
              />
              <div class="kh-md answer" v-html="renderAnswer(m)" />
              <div v-if="m.refused" class="refusal-hint">（未调用大模型：无有效资料支撑作答）</div>
              <div v-if="m.sources && m.sources.length" class="sources">
                <div class="sources-head">引用来源</div>
                <div
                  v-for="s in m.sources"
                  :key="s.n"
                  class="source-card"
                  @click.stop="openSource(m, s.n)"
                >
                  <span class="source-n">{{ s.n }}</span>
                  <el-icon :size="13" class="source-icon">
                    <Notebook v-if="s.source_type === 'note'" /><Document v-else />
                  </el-icon>
                  <span class="source-title">{{ s.title }}</span>
                  <span v-if="s.page" class="source-page">第 {{ s.page }} 页</span>
                  <span class="source-score">相关度 {{ s.score.toFixed(2) }}</span>
                </div>
              </div>
            </div>
          </div>
        </div>

        <div v-if="streaming && lastAssistantEmpty" class="msg assistant">
          <div class="avatar ai-avatar">知</div>
          <div class="bubble assistant typing">
            <span class="dot" /><span class="dot" /><span class="dot" />
          </div>
        </div>
      </div>

      <div class="input-row">
        <el-input
          v-model="draft"
          type="textarea"
          :rows="2"
          resize="none"
          placeholder="输入问题，Enter 发送 / Shift+Enter 换行"
          :disabled="streaming"
          @keydown.enter.exact.prevent="send"
        />
        <el-button v-if="!streaming" type="primary" class="send-btn" @click="send">发送</el-button>
        <el-button v-else type="danger" plain class="send-btn" @click="abort">停止</el-button>
      </div>
    </el-card>

    <el-dialog v-model="dialogVisible" :title="dialogSource ? `来源 [${dialogSource.n}]` : ''" width="580px">
      <p v-if="dialogSource" class="dialog-meta">
        <el-tag size="small" :type="dialogSource.source_type === 'note' ? 'primary' : 'success'">
          {{ dialogSource.source_type === "note" ? "笔记" : "文档" }}
        </el-tag>
        《{{ dialogSource.title }}》
        <span v-if="dialogSource.page">· 第 {{ dialogSource.page }} 页</span>
        <span v-if="dialogSource.score">· 相关度 {{ dialogSource.score.toFixed(3) }}</span>
      </p>
      <div v-if="dialogSource" class="dialog-content">{{ dialogSource.content }}</div>
      <template #footer>
        <el-button @click="dialogVisible = false">关闭</el-button>
        <el-button v-if="dialogSource" type="primary" @click="gotoSource">
          {{ dialogSource.source_type === "note" ? "打开笔记原文" : "前往文档" }}
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import { ChatDotRound, Document, Notebook } from "@element-plus/icons-vue";
import DOMPurify from "dompurify";
import { marked } from "marked";
import { useAuthStore } from "../stores/auth";
import { getToken } from "../api/client";

interface ChatSource {
  n: number;
  source_type: string;
  source_id: string;
  kb_id: string;
  title: string;
  file_name: string | null;
  page: number;
  chunk_id: string;
  score: number;
  content: string;
}

interface ChatMessage {
  role: "user" | "assistant";
  text: string;
  sources?: ChatSource[];
  refused?: boolean;
  degraded?: boolean;
  llmUnavailable?: boolean;
}

interface StoredSession {
  id: string;
  title: string;
  messages: ChatMessage[];
}

const router = useRouter();
const auth = useAuthStore();

const SESSIONS_KEY = "kh_qa_sessions";
const suggestions = [
  "进程和线程有什么区别？",
  "死锁的四个必要条件是什么？",
  "TCP 三次握手的过程",
  "哈希冲突怎么解决？",
];
const sessions = ref<StoredSession[]>([]);
const activeId = ref("");
const draft = ref("");
const streaming = ref(false);
const scrollBox = ref<HTMLElement | null>(null);
const dialogVisible = ref(false);
const dialogSource = ref<ChatSource | null>(null);
let controller: AbortController | null = null;

const activeSession = computed(
  () => sessions.value.find((s) => s.id === activeId.value) ?? null,
);
const messages = computed(() => activeSession.value?.messages ?? []);
const lastAssistantEmpty = computed(() => {
  const last = messages.value[messages.value.length - 1];
  return !!last && last.role === "assistant" && !last.text;
});

marked.setOptions({ breaks: true, gfm: true });

function renderAnswer(m: ChatMessage): string {
  if (!m.text) return "";
  const html = marked.parse(m.text) as string;
  // 把 [n] 文本替换为可点击角标（n 必须存在于 sources 才渲染为徽标）
  const withBadges = html.replace(/\[(\d+)\]/g, (match, num: string) => {
    const n = Number(num);
    return m.sources?.some((s) => s.n === n)
      ? `<span class="citation" data-n="${n}">${n}</span>`
      : match;
  });
  return DOMPurify.sanitize(withBadges);
}

function onBubbleClick(event: MouseEvent, m: ChatMessage): void {
  const target = (event.target as HTMLElement).closest("[data-n]");
  if (target) openSource(m, Number(target.getAttribute("data-n")));
}

function loadSessions(): void {
  try {
    sessions.value = JSON.parse(localStorage.getItem(SESSIONS_KEY) ?? "[]");
  } catch {
    sessions.value = [];
  }
}

function persist(): void {
  localStorage.setItem(SESSIONS_KEY, JSON.stringify(sessions.value.slice(-30)));
}

function newSession(): void {
  const id = crypto.randomUUID().replace(/-/g, "");
  sessions.value.push({ id, title: "", messages: [] });
  activeId.value = id;
  persist();
}

function switchSession(id: string): void {
  if (streaming.value) return;
  activeId.value = id;
}

function scrollToBottom(): void {
  void nextTick(() => {
    if (scrollBox.value) scrollBox.value.scrollTop = scrollBox.value.scrollHeight;
  });
}

function openSource(m: ChatMessage, n: number | undefined): void {
  if (!m.sources) return;
  const source = m.sources.find((s) => s.n === n);
  if (!source) return;
  dialogSource.value = source;
  dialogVisible.value = true;
}

function gotoSource(): void {
  const source = dialogSource.value;
  if (!source) return;
  dialogVisible.value = false;
  if (source.source_type === "note") {
    router.push({ path: "/notes", query: { kbId: source.kb_id, noteId: source.source_id } });
  } else {
    router.push({ path: "/documents", query: { kbId: source.kb_id } });
  }
}

async function send() {
  const question = draft.value.trim();
  if (!question || streaming.value) return;
  if (!activeId.value || !activeSession.value) newSession();
  const session = activeSession.value!;
  session.messages.push({ role: "user", text: question });
  if (!session.title) session.title = question.slice(0, 24);
  const assistant: ChatMessage = { role: "assistant", text: "" };
  session.messages.push(assistant);
  draft.value = "";
  streaming.value = true;
  scrollToBottom();
  persist();

  controller = new AbortController();
  try {
    const response = await fetch("/api/v1/qa", {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${getToken()}` },
      body: JSON.stringify({ question, session_id: session.id }),
      signal: controller.signal,
    });
    if (!response.ok || !response.body) {
      const detail = await response.text();
      assistant.text = `请求失败（HTTP ${response.status}）：${detail.slice(0, 120)}`;
      return;
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let sep: number;
      while ((sep = buffer.indexOf("\n\n")) >= 0) {
        const frame = buffer.slice(0, sep);
        buffer = buffer.slice(sep + 2);
        const line = frame.split("\n").find((l) => l.startsWith("data:"));
        if (!line) continue;
        const event = JSON.parse(line.slice(5));
        if (event.type === "sources") {
          assistant.sources = event.sources as ChatSource[];
          assistant.degraded = !!event.degraded;
        } else if (event.type === "delta") {
          assistant.text += event.text as string;
          scrollToBottom();
        } else if (event.type === "refusal") {
          assistant.text = event.message as string;
          assistant.refused = true;
          assistant.sources = event.sources as ChatSource[];
        } else if (event.type === "llm_unavailable") {
          assistant.llmUnavailable = true;
        } else if (event.type === "done") {
          if (event.degraded) assistant.degraded = true;
        }
      }
    }
  } catch (e) {
    if ((e as Error).name !== "AbortError") {
      assistant.text += "\n[连接中断，请重试]";
    }
  } finally {
    streaming.value = false;
    controller = null;
    persist();
    scrollToBottom();
  }
}

function abort() {
  controller?.abort();
}

onMounted(() => {
  loadSessions();
  if (!auth.isLoggedIn) return;
  if (sessions.value.length === 0) newSession();
  else activeId.value = sessions.value[sessions.value.length - 1].id;
});
</script>

<style scoped>
.page {
  display: flex;
  gap: 16px;
  max-width: 1240px;
  margin: 0 auto;
  height: calc(100vh - 108px);
  min-height: 480px;
}

/* ---------- 会话列表 ---------- */
.sidebar {
  width: 208px;
  flex-shrink: 0;
  overflow-y: auto;
}
.new-btn {
  width: 100%;
  margin-bottom: 10px;
  border-radius: 10px;
}
.session-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 9px 12px;
  border-radius: 10px;
  cursor: pointer;
  font-size: 13px;
  color: var(--kh-text-2);
  margin-bottom: 4px;
  transition: background 0.15s ease;
}
.session-item:hover {
  background: var(--kh-bg-soft);
}
.session-item.active {
  background: color-mix(in srgb, var(--kh-brand) 12%, transparent);
  color: var(--kh-brand);
  font-weight: 600;
}
.session-title {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* ---------- 聊天区 ---------- */
.chat {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
}
.chat :deep(.el-card__body) {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-height: 0;
  padding: 18px;
}
.messages {
  flex: 1;
  overflow-y: auto;
  padding-right: 6px;
}

.welcome {
  text-align: center;
  padding-top: 12vh;
}
.welcome-logo {
  width: 56px;
  height: 56px;
  margin: 0 auto 14px;
  display: grid;
  place-items: center;
  border-radius: 16px;
  background: var(--kh-gradient);
  color: #fff;
  font-size: 28px;
  font-weight: 800;
  box-shadow: 0 8px 24px rgba(82, 107, 234, 0.35);
}
.welcome-title {
  margin: 0;
  font-size: 20px;
  color: var(--kh-text);
}
.welcome-sub {
  margin: 6px 0 18px;
  font-size: 13px;
  color: var(--kh-text-3);
}
.suggestions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  justify-content: center;
}
.suggestion {
  border: 1px solid var(--kh-border);
  background: var(--kh-surface);
  color: var(--kh-text-2);
  border-radius: 999px;
  padding: 7px 14px;
  font-size: 13px;
  cursor: pointer;
  transition: all 0.15s ease;
}
.suggestion:hover {
  border-color: var(--kh-brand);
  color: var(--kh-brand);
  background: color-mix(in srgb, var(--kh-brand) 8%, transparent);
}

.msg {
  display: flex;
  gap: 10px;
  margin-bottom: 16px;
}
.avatar {
  width: 32px;
  height: 32px;
  flex-shrink: 0;
  display: grid;
  place-items: center;
  border-radius: 10px;
  font-size: 13px;
  font-weight: 700;
  color: #fff;
}
.ai-avatar {
  background: var(--kh-gradient);
}
.user-avatar {
  background: linear-gradient(135deg, #34c3a6, #22a06b);
}
.msg.user {
  flex-direction: row-reverse;
}
.bubble {
  max-width: 78%;
  padding: 11px 14px;
  border-radius: 14px;
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-word;
}
.bubble.user {
  background: var(--kh-gradient);
  color: #fff;
  border-bottom-right-radius: 4px;
}
.bubble.assistant {
  background: var(--kh-surface-2);
  border: 1px solid var(--kh-border);
  border-bottom-left-radius: 4px;
}
.answer {
  white-space: normal;
}
.flag {
  margin-bottom: 8px;
}

.citation {
  display: inline-grid;
  place-items: center;
  min-width: 17px;
  height: 17px;
  padding: 0 4px;
  margin: 0 2px;
  border-radius: 6px;
  background: color-mix(in srgb, var(--kh-brand) 16%, transparent);
  color: var(--kh-brand);
  font-size: 11px;
  font-weight: 700;
  cursor: pointer;
  vertical-align: 2px;
  transition: transform 0.12s ease, background 0.12s ease;
}
.citation:hover {
  transform: scale(1.15);
  background: color-mix(in srgb, var(--kh-brand) 28%, transparent);
}

.refusal-hint {
  color: var(--kh-text-3);
  font-size: 12px;
  margin-top: 6px;
}

.sources {
  margin-top: 10px;
  border-top: 1px dashed var(--kh-border);
  padding-top: 8px;
}
.sources-head {
  font-size: 12px;
  color: var(--kh-text-3);
  margin-bottom: 6px;
}
.source-card {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 6px 9px;
  border-radius: 8px;
  cursor: pointer;
  font-size: 12.5px;
  color: var(--kh-text-2);
  transition: background 0.12s ease;
}
.source-card:hover {
  background: color-mix(in srgb, var(--kh-brand) 8%, transparent);
}
.source-n {
  width: 17px;
  height: 17px;
  display: grid;
  place-items: center;
  border-radius: 5px;
  background: color-mix(in srgb, var(--kh-brand) 16%, transparent);
  color: var(--kh-brand);
  font-size: 11px;
  font-weight: 700;
  flex-shrink: 0;
}
.source-icon {
  color: var(--kh-text-3);
}
.source-title {
  font-weight: 600;
  color: var(--kh-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.source-page {
  color: var(--kh-text-3);
  flex-shrink: 0;
}
.source-score {
  margin-left: auto;
  color: var(--kh-text-3);
  font-size: 11.5px;
  flex-shrink: 0;
}

.typing {
  display: inline-flex;
  gap: 4px;
  align-items: center;
  padding: 14px 16px;
}
.dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--kh-text-3);
  animation: bounce 1.2s infinite ease-in-out;
}
.dot:nth-child(2) {
  animation-delay: 0.15s;
}
.dot:nth-child(3) {
  animation-delay: 0.3s;
}
@keyframes bounce {
  0%, 60%, 100% {
    transform: translateY(0);
    opacity: 0.4;
  }
  30% {
    transform: translateY(-4px);
    opacity: 1;
  }
}

/* ---------- 输入区 ---------- */
.input-row {
  display: flex;
  gap: 10px;
  align-items: flex-end;
  padding-top: 12px;
  border-top: 1px solid var(--kh-border);
}
.send-btn {
  height: 54px;
  min-width: 84px;
  border-radius: 10px;
}

/* ---------- 来源弹窗 ---------- */
.dialog-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--kh-text-2);
  margin-top: 0;
}
.dialog-content {
  background: var(--kh-surface-2);
  border: 1px solid var(--kh-border);
  border-radius: 10px;
  padding: 12px;
  max-height: 300px;
  overflow-y: auto;
  white-space: pre-wrap;
  line-height: 1.7;
  font-size: 13px;
}
</style>
