import { defineStore } from "pinia";

import { apiClient } from "../api/client";

export interface ServiceStatus {
  status: "up" | "down";
  detail?: string | null;
}

export interface HealthSnapshot {
  status: string;
  services: Record<string, ServiceStatus>;
}

interface HealthState {
  snapshot: HealthSnapshot | null;
  loading: boolean;
  error: string | null;
}

export const useHealthStore = defineStore("health", {
  state: (): HealthState => ({
    snapshot: null,
    loading: false,
    error: null,
  }),
  actions: {
    async refresh() {
      this.loading = true;
      this.error = null;
      try {
        const { data } = await apiClient.get<HealthSnapshot>("/system/health");
        this.snapshot = data;
      } catch (err) {
        this.error = err instanceof Error ? err.message : String(err);
      } finally {
        this.loading = false;
      }
    },
  },
});
