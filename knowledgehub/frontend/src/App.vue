<template>
  <el-container v-if="isLoginRoute" class="auth-layout">
    <router-view />
  </el-container>

  <el-container v-else class="layout">
    <el-aside :width="collapsed ? '64px' : 'var(--kh-sidebar-w)'" class="sidebar">
      <div class="brand" @click="router.push('/qa')">
        <div class="brand-logo">知</div>
        <transition name="fade">
          <div v-if="!collapsed" class="brand-text">
            <div class="brand-name">知序</div>
            <div class="brand-sub">KnowledgeHub</div>
          </div>
        </transition>
      </div>

      <nav class="menu">
        <router-link
          v-for="item in menu"
          :key="item.path"
          :to="item.path"
          class="menu-item"
          :title="item.label"
        >
          <el-icon :size="18"><component :is="item.icon" /></el-icon>
          <transition name="fade">
            <span v-if="!collapsed" class="menu-label">{{ item.label }}</span>
          </transition>
        </router-link>
      </nav>

      <div class="sidebar-footer">
        <div class="menu-item" :title="isDark ? '切换亮色' : '切换暗色'" @click="toggle">
          <el-icon :size="18"><Moon v-if="!isDark" /><Sunny v-else /></el-icon>
          <transition name="fade">
            <span v-if="!collapsed" class="menu-label">{{ isDark ? "亮色模式" : "暗色模式" }}</span>
          </transition>
        </div>
      </div>
    </el-aside>

    <el-container class="main-column">
      <el-header class="topbar">
        <div class="topbar-left">
          <el-button text @click="collapsed = !collapsed">
            <el-icon :size="18"><Expand v-if="collapsed" /><Fold v-else /></el-icon>
          </el-button>
          <span class="topbar-title">{{ currentTitle }}</span>
        </div>
        <div class="topbar-right">
          <el-dropdown v-if="auth.isLoggedIn" @command="onUserCommand">
            <span class="user-chip">
              <span class="avatar">{{ auth.email.slice(0, 1).toUpperCase() }}</span>
              <span class="email">{{ auth.email }}</span>
              <el-icon :size="12"><ArrowDown /></el-icon>
            </span>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="logout">
                  <el-icon><SwitchButton /></el-icon>退出登录
                </el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </el-header>
      <el-main class="content">
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>

<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import {
  ArrowDown, ChatDotRound, Document, Fold, Expand, Monitor, Moon,
  Notebook, Search, Share, Sunny, SwitchButton,
} from "@element-plus/icons-vue";
import { useAuthStore } from "./stores/auth";
import { useTheme } from "./composables/useTheme";

const auth = useAuthStore();
const route = useRoute();
const router = useRouter();
const { isDark, toggle } = useTheme();

const collapsed = ref(false);

const menu = [
  { path: "/qa", label: "智能问答", icon: ChatDotRound },
  { path: "/search", label: "检索", icon: Search },
  { path: "/notes", label: "笔记", icon: Notebook },
  { path: "/documents", label: "文档", icon: Document },
  { path: "/graph", label: "知识图谱", icon: Share },
  { path: "/health", label: "系统状态", icon: Monitor },
];

const isLoginRoute = computed(() => route.name === "login");
const currentTitle = computed(
  () => menu.find((m) => m.path === route.path)?.label ?? "系统状态",
);

watch(() => auth.isLoggedIn, (v) => {
  if (!v && !isLoginRoute.value) router.push("/login");
});

function onUserCommand(cmd: string) {
  if (cmd === "logout") {
    auth.logout();
    router.push("/login");
  }
}
</script>

<style scoped>
.layout {
  min-height: 100vh;
}
.auth-layout {
  min-height: 100vh;
}

/* ---------- 侧边栏 ---------- */
.sidebar {
  display: flex;
  flex-direction: column;
  background: var(--kh-surface);
  border-right: 1px solid var(--kh-border);
  transition: width 0.2s ease;
  overflow: hidden;
}
.brand {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 18px 16px 16px;
  cursor: pointer;
}
.brand-logo {
  width: 36px;
  height: 36px;
  flex-shrink: 0;
  display: grid;
  place-items: center;
  border-radius: 10px;
  background: var(--kh-gradient);
  color: #fff;
  font-size: 18px;
  font-weight: 800;
  box-shadow: 0 4px 12px rgba(82, 107, 234, 0.35);
}
.brand-name {
  font-size: 17px;
  font-weight: 800;
  color: var(--kh-text);
  line-height: 1.1;
}
.brand-sub {
  font-size: 11px;
  color: var(--kh-text-3);
  letter-spacing: 0.4px;
}

.menu {
  flex: 1;
  padding: 8px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.menu-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  border-radius: 10px;
  color: var(--kh-text-2);
  text-decoration: none;
  font-size: 14px;
  cursor: pointer;
  transition: background 0.15s ease, color 0.15s ease;
  white-space: nowrap;
}
.menu-item:hover {
  background: var(--kh-bg-soft);
  color: var(--kh-text);
}
.menu-item.router-link-active {
  background: color-mix(in srgb, var(--kh-brand) 12%, transparent);
  color: var(--kh-brand);
  font-weight: 600;
}
.menu-label {
  overflow: hidden;
  text-overflow: ellipsis;
}
.sidebar-footer {
  padding: 8px;
  border-top: 1px solid var(--kh-border);
}

/* ---------- 顶栏 ---------- */
.main-column {
  min-width: 0;
}
.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  height: 56px;
  background: color-mix(in srgb, var(--kh-surface) 80%, transparent);
  backdrop-filter: blur(8px);
  border-bottom: 1px solid var(--kh-border);
  position: sticky;
  top: 0;
  z-index: 10;
}
.topbar-left {
  display: flex;
  align-items: center;
  gap: 8px;
}
.topbar-title {
  font-size: 15px;
  font-weight: 700;
  color: var(--kh-text);
}
.topbar-right {
  display: flex;
  align-items: center;
}
.user-chip {
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
  padding: 4px 8px;
  border-radius: 999px;
  transition: background 0.15s ease;
  outline: none;
}
.user-chip:hover {
  background: var(--kh-bg-soft);
}
.avatar {
  width: 28px;
  height: 28px;
  display: grid;
  place-items: center;
  border-radius: 50%;
  background: var(--kh-gradient);
  color: #fff;
  font-size: 13px;
  font-weight: 700;
}
.email {
  font-size: 13px;
  color: var(--kh-text-2);
}

.content {
  padding: 20px 24px;
  background: var(--kh-bg);
}
</style>
