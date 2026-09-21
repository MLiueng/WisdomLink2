<template>
  <div class="rs-bar">
    <el-select v-model="kbs" multiple collapse-tags collapse-tags-tooltip placeholder="全部知识库"
               clearable style="width: 280px" size="default">
      <el-option v-for="k in kbsList" :key="k.id" :label="k.name" :value="k.id">
        <span style="display:flex;justify-content:space-between">
          <span>{{ k.name }}</span><span style="color:var(--wl-text-muted);font-size:12px">{{ k.doc_count }} 文档</span>
        </span>
      </el-option>
    </el-select>
    <el-select v-model="folders" multiple collapse-tags placeholder="全部文件夹（可选）" clearable
               style="width: 220px" size="default" :disabled="!kbs.length">
      <el-option v-for="f in folderList" :key="f.id" :label="f.name" :value="f.id" />
    </el-select>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { http } from '../api/http'

const props = defineProps<{ kbIds: number[]; folderIds: number[] }>()
const emit = defineEmits(['update:kbIds', 'update:folderIds'])
const kbs = computed({ get: () => props.kbIds, set: (v) => emit('update:kbIds', v) })
const folders = computed({ get: () => props.folderIds, set: (v) => emit('update:folderIds', v) })
const kbsList = ref<any[]>([])
const folderList = ref<any[]>([])

watch(kbs, async (ids) => {
  emit('update:folderIds', [])
  folderList.value = []
  if (!ids?.length) return
  try {
    const all = await Promise.all(ids.map((id) => (http as any).get(`/kb/${id}/folders`)))
    folderList.value = all.flat()
  } catch { /* mock */ }
}, { immediate: true })

if (!(http as any).constructor || true) { /* mock 数据在 http 层 */ }
</script>

<style scoped>
.rs-bar { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.rs-bar .el-select { max-width: 100%; }
</style>
