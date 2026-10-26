<template>
  <div class="login-page">
    <div class="glow glow-a" />
    <div class="glow glow-b" />

    <el-card class="login-card">
      <div class="head">
        <div class="logo">知</div>
        <h1 class="title">知序 KnowledgeHub</h1>
        <p class="subtitle">个人知识管理与智能检索 · 登录后开始使用</p>
      </div>

      <el-tabs v-model="tab" stretch>
        <el-tab-pane label="登录" name="login">
          <el-form label-position="top" @submit.prevent>
            <el-form-item label="邮箱">
              <el-input v-model="email" placeholder="you@example.com" size="large" />
            </el-form-item>
            <el-form-item label="密码">
              <el-input
                v-model="password"
                type="password"
                show-password
                size="large"
                placeholder="••••••••"
                @keyup.enter="submit"
              />
            </el-form-item>
            <el-button type="primary" size="large" class="submit" :loading="busy" @click="submit">
              登 录
            </el-button>
          </el-form>
        </el-tab-pane>
        <el-tab-pane label="注册" name="register">
          <el-form label-position="top" @submit.prevent>
            <el-form-item label="邮箱">
              <el-input v-model="email" placeholder="you@example.com" size="large" />
            </el-form-item>
            <el-form-item label="密码">
              <el-input v-model="password" type="password" show-password size="large" placeholder="至少 8 位" />
            </el-form-item>
            <el-button type="primary" size="large" class="submit" :loading="busy" @click="submit">
              注册并登录
            </el-button>
          </el-form>
        </el-tab-pane>
      </el-tabs>

      <p class="footnote">MongoDB · Redis · Neo4j · Milvus 异构 NoSQL 协同驱动</p>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ElMessage } from "element-plus";
import { useAuthStore } from "../stores/auth";

const router = useRouter();
const route = useRoute();
const auth = useAuthStore();

const tab = ref<"login" | "register">("login");
const email = ref("");
const password = ref("");
const busy = ref(false);

function errText(e: unknown): string {
  const detail = (e as { response?: { data?: { detail?: unknown } } }).response?.data?.detail;
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object" && "reason" in detail) return String((detail as { reason: unknown }).reason);
  return "请求失败，请重试";
}

async function submit() {
  if (!email.value || !password.value) {
    ElMessage.warning("请填写邮箱与密码");
    return;
  }
  busy.value = true;
  try {
    if (tab.value === "register") {
      await auth.register(email.value, password.value);
      ElMessage.success("注册成功，已自动登录");
    }
    await auth.login(email.value, password.value);
    const redirect = route.query.redirect;
    router.push(typeof redirect === "string" ? redirect : "/qa");
  } catch (e) {
    ElMessage.error(errText(e));
  } finally {
    busy.value = false;
  }
}
</script>

<style scoped>
.login-page {
  min-height: 100vh;
  display: grid;
  place-items: center;
  position: relative;
  overflow: hidden;
  background:
    radial-gradient(1200px 600px at 20% -10%, rgba(82, 107, 234, 0.25), transparent 60%),
    radial-gradient(1000px 500px at 90% 110%, rgba(124, 92, 234, 0.22), transparent 60%),
    var(--kh-bg);
}
.glow {
  position: absolute;
  width: 420px;
  height: 420px;
  border-radius: 50%;
  filter: blur(90px);
  opacity: 0.5;
  pointer-events: none;
}
.glow-a {
  top: -120px;
  left: -80px;
  background: rgba(82, 107, 234, 0.45);
}
.glow-b {
  bottom: -140px;
  right: -60px;
  background: rgba(74, 159, 245, 0.35);
}

.login-card {
  width: 420px;
  position: relative;
  z-index: 1;
  backdrop-filter: blur(6px);
}
.head {
  text-align: center;
  margin-bottom: 14px;
}
.logo {
  width: 52px;
  height: 52px;
  margin: 0 auto 12px;
  display: grid;
  place-items: center;
  border-radius: 14px;
  background: var(--kh-gradient);
  color: #fff;
  font-size: 26px;
  font-weight: 800;
  box-shadow: 0 8px 24px rgba(82, 107, 234, 0.4);
}
.title {
  margin: 0;
  font-size: 22px;
  color: var(--kh-text);
}
.subtitle {
  margin: 6px 0 0;
  font-size: 13px;
  color: var(--kh-text-3);
}
.submit {
  width: 100%;
  margin-top: 6px;
  --el-button-bg-color: var(--kh-brand);
  --el-button-border-color: var(--kh-brand);
}
.footnote {
  margin: 14px 0 0;
  text-align: center;
  font-size: 12px;
  color: var(--kh-text-3);
}
</style>
