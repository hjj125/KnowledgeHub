import { ref } from "vue";
import { apiClient } from "../api/client";

export interface Kb {
  id: string;
  name: string;
  category?: string;
  description?: string;
  note_count?: number;
}

// 模块级共享状态：任一视图创建/刷新知识库后，其余视图立即可见
const kbs = ref<Kb[]>([]);
const kbId = ref("");
const loading = ref(false);

export function useKbList() {
  async function load(): Promise<void> {
    loading.value = true;
    try {
      const { data } = await apiClient.get<Kb[]>("/knowledge-bases");
      kbs.value = data;
      // 当前选中项被删除或尚无选中时，自动选第一个
      if (!data.some((k) => k.id === kbId.value)) {
        kbId.value = data[0]?.id ?? "";
      }
    } finally {
      loading.value = false;
    }
  }

  async function createKb(name: string, category = "", description = ""): Promise<Kb> {
    const { data } = await apiClient.post<Kb>("/knowledge-bases", {
      name,
      category,
      description,
    });
    await load();
    kbId.value = data.id;
    return data;
  }

  return { kbs, kbId, loading, load, createKb };
}
