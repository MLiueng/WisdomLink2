<template>
  <div class="wl-page">
    <div class="page-head">
      <div>
        <h1 class="wl-page-title">知识库</h1>
        <p class="wl-page-desc">知识库是唯一的权限与运营单元；库内使用多级文件夹分类组织文档</p>
      </div>
      <el-button type="primary" :icon="Plus" @click="createOpen = true">新建知识库</el-button>
    </div>

    <el-row v-if="kbs.length" :gutter="16">
      <el-col v-for="k in kbs" :key="k.id" :xs="24" :sm="12" :lg="8">
        <div class="kb-card wl-card is-hover" @click="$router.push(`/kb/${k.id}`)">
          <div class="kb-head">
            <el-icon class="kb-ico" :color="k.is_local_only ? 'var(--wl-warning)' : 'var(--wl-primary)'"><FolderOpened /></el-icon>
            <div class="kb-name-row">
              <span class="kb-name">{{ k.name }}</span>
              <el-tag v-if="k.is_local_only" size="small" type="warning" effect="light">仅本地</el-tag>
            </div>
          </div>
          <p class="kb-desc">{{ k.description || '暂无描述' }}</p>
          <div class="kb-stats">
            <span><b>{{ k.doc_count }}</b> 文档</span>
            <el-divider direction="vertical" />
            <span><b>{{ k.qa_count }}</b> QA 对</span>
            <el-tag v-if="k.status === 'archived'" size="small" type="info" effect="plain" style="margin-left:auto">已归档</el-tag>
          </div>
        </div>
      </el-col>
    </el-row>
    <div v-else class="wl-card"><div class="wl-empty"><el-icon><FolderOpened /></el-icon><p>还没有知识库<br /><small>创建第一个知识库，开始沉淀团队知识</small></p>
      <el-button type="primary" @click="createOpen = true">创建知识库</el-button></div></div>

    <el-dialog v-model="createOpen" title="新建知识库" width="480px">
      <el-form label-position="top">
        <el-form-item label="名称" required><el-input v-model="form.name" placeholder="如：产品部知识库" /></el-form-item>
        <el-form-item label="描述"><el-input v-model="form.description" type="textarea" :rows="2" /></el-form-item>
        <el-form-item>
          <el-checkbox v-model="form.is_local_only">仅本地模式（禁止该库内容发送到远程模型）</el-checkbox>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createOpen = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="create">创建</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { FolderOpened, Plus } from '@element-plus/icons-vue'
import { http } from '../api/http'

const kbs = ref<any[]>([])
const createOpen = ref(false)
const saving = ref(false)
const form = ref({ name: '', description: '', is_local_only: false })

async function load() {
  try { kbs.value = (await (http as any).get('/kb')) as any[] } catch { /* */ }
}
onMounted(load)

async function create() {
  if (!form.value.name.trim()) return ElMessage.warning('请填写名称')
  saving.value = true
  try {
    await (http as any).post('/kb', form.value)
    ElMessage.success('已创建')
    createOpen.value = false
    form.value = { name: '', description: '', is_local_only: false }
    await load()
  } catch (e: any) { ElMessage.error(e.message) } finally { saving.value = false }
}
</script>

<style scoped>
.page-head { display: flex; justify-content: space-between; align-items: flex-end; margin-bottom: 20px; }
.kb-card { padding: 20px; cursor: pointer; height: 100%; margin-bottom: 16px; }
.kb-head { display: flex; align-items: center; gap: 12px; }
.kb-ico { font-size: 26px; }
.kb-name-row { display: flex; align-items: center; gap: 8px; min-width: 0; }
.kb-name { font-weight: 600; font-size: var(--wl-fs-lg); }
.kb-desc { color: var(--wl-text-secondary); font-size: var(--wl-fs-md); margin: 12px 0 16px; min-height: 20px;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.kb-stats { display: flex; align-items: center; gap: 10px; font-size: var(--wl-fs-xs); color: var(--wl-text-muted); }
.kb-stats b { color: var(--wl-text-primary); font-weight: 600; }
</style>
