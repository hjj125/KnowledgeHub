import { createRouter, createWebHistory } from "vue-router";
import { useAuthStore } from "../stores/auth";

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/", redirect: "/qa" },
    {
      path: "/login",
      name: "login",
      component: () => import("../views/LoginView.vue"),
      meta: { public: true },
    },
    {
      path: "/health",
      name: "system-health",
      component: () => import("../views/SystemHealthView.vue"),
      meta: { public: true },
    },
    {
      path: "/qa",
      name: "qa",
      component: () => import("../views/QAView.vue"),
    },
    {
      path: "/notes",
      name: "notes",
      component: () => import("../views/NotesView.vue"),
    },
    {
      path: "/documents",
      name: "documents",
      component: () => import("../views/DocumentsView.vue"),
    },
    {
      path: "/search",
      name: "search",
      component: () => import("../views/SearchView.vue"),
    },
    {
      path: "/graph",
      name: "knowledge-graph",
      component: () => import("../views/KnowledgeGraphView.vue"),
    },
  ],
});

router.beforeEach((to) => {
  const auth = useAuthStore();
  if (!to.meta.public && !auth.isLoggedIn) {
    return { name: "login", query: { redirect: to.fullPath } };
  }
  if (to.name === "login" && auth.isLoggedIn) {
    return { path: "/qa" };
  }
});

export default router;
