import { createApp } from "vue";
import { createPinia } from "pinia";
import ElementPlus from "element-plus";
import "element-plus/dist/index.css";
import "element-plus/theme-chalk/dark/css-vars.css";
import "./styles/index.css";

import App from "./App.vue";
import router from "./router";

// 主题偏好：早于首帧应用，避免闪烁
const theme = localStorage.getItem("kh_theme");
if (theme === "dark") document.documentElement.classList.add("dark");

const app = createApp(App);
app.use(createPinia());
app.use(router);
app.use(ElementPlus);
app.mount("#app");
