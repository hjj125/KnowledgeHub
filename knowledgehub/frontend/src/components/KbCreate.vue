<template>
  <el-button v-bind="$attrs" @click="visible = true">
    <slot>＋ 新建知识库</slot>
  </el-button>

  <el-dialog v-model="visible" title="新建知识库" width="440px">
    <el-form label-position="top" @submit.prevent>
      <el-form-item label="名称" required>
        <el-input v-model="name" maxlength="100" placeholder="例如：操作系统复习" />
      </el-form-item>
      <el-form-item label="分类">
        <el-input v-model="category" maxlength="50" placeholder="例如：课程 / 工作 / 兴趣（可选）" />
      </el-form-item>
      <el-form-item label="描述">
        <el-input v-model="description" type="textarea" :rows="2" maxlength="500" placeholder="这个知识库用来记录什么？（可选）" />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="visible = false">取消</el-button>
      <el-button type="primary" :loading="busy" @click="submit">创建</el-button>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { ref } from "vue";
import { ElMessage } from "element-plus";
import { useKbList } from "../composables/useKb";

const emit = defineEmits<{ created: [name: string] }>();

const { createKb } = useKbList();
const visible = ref(false);
const name = ref("");
const category = ref("");
const description = ref("");
const busy = ref(false);

async function submit() {
  if (!name.value.trim()) {
    ElMessage.warning("请填写知识库名称");
    return;
  }
  busy.value = true;
  try {
    const kb = await createKb(name.value.trim(), category.value.trim(), description.value.trim());
    ElMessage.success(`知识库「${kb.name}」已创建`);
    visible.value = false;
    name.value = "";
    category.value = "";
    description.value = "";
    emit("created", kb.name);
  } catch {
    ElMessage.error("创建失败，请重试");
  } finally {
    busy.value = false;
  }
}
</script>
