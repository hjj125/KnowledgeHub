import { defineStore } from "pinia";
import { apiClient, clearToken, getToken, saveEmail, savedEmail, setToken } from "../api/client";

export const useAuthStore = defineStore("auth", {
  state: () => ({
    token: getToken(),
    email: savedEmail(),
  }),
  getters: {
    isLoggedIn: (state) => !!state.token,
  },
  actions: {
    async register(email: string, password: string) {
      await apiClient.post("/auth/register", { email, password });
    },
    async login(email: string, password: string) {
      const { data } = await apiClient.post("/auth/login", { email, password });
      setToken(data.access_token);
      saveEmail(email);
      this.token = data.access_token;
      this.email = email;
    },
    logout() {
      apiClient.post("/auth/logout").catch(() => undefined); // jti 进黑名单；失败不阻断
      clearToken();
      this.token = "";
      this.email = "";
    },
  },
});
