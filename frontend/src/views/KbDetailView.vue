<template>
  <div class="wl-page">
    <div class="page-head">
      <div class="crumb">
        <el-button text :icon="ArrowLeft" @click="$router.push('/kb')" />
        <h1 class="wl-page-title">{{ kbName }}</h1>
        <StatusBadge v-if="kbStatus" :status="kbStatus === 'archived' ? 'offline' : 'published'" />
      </div>
      <div>
        <el-button :icon="Setting" @click="$router.push('/clean-rules')">库设置</el-button>
        <el-button type="primary" :icon="Upload" @click="uploadOpen = true">上传文档</el-button>
      </div>
    </div>

    <div class="body-grid">
      <!-- 文件夹树 -->
      <aside class="folder-pane wl-card">
        <div class="fp-head">
          <span>文件夹</span>
          <el-button size="small" text :icon="Plus" @click="folderOpen = true" />
        </div>
        <el-tree :data="treeData" node-key="id" :expand-on-click-node="false" highlight-current
                 @node-click="onFolderClick" class="fp-tree">
          <template #default="{ data }">
            <div class="fp-node">
              <el-icon><Folder /></el-icon>
              <span class="fp-name">{{ data.name }}</span>
              <span class="fp-count">{{ data.doc_count }}</span>
            </div>
          </template>
        </el-tree>
      </aside>

      <!-- 文档列表 -->
      <section class="doc-pane">
        <div class="doc-toolbar">
          <el-input v-model="q" placeholder="搜索文档标题" :prefix-icon="Search" clearable style="width:240px"
                    @keyup.enter="page = 1; loadDocs()" @clear="page = 1; loadDocs()" />
          <el-select v-model="statusFilter" placeholder="状态" clearable style="width:130px"
                     @change="page = 1; loadDocs()">
            <el-option v-for="(v, k) in STATUSES" :key="k" :label="v" :value="k" />
          </el-select>
          <span class="spacer" />
          <el-button :icon="Refresh" circle @click="loadDocs" />
        <el-button size="small" :disabled="!selectedDocs.length" @click="batchReprocess">批量重处理（{{ selectedDocs.length }}）</el-button>
        </div>

        <el-table :data="docs" v-loading="loading" class="doc-table" @selection-change="(s: any) => (selectedDocs = s)" @row-click="(r: any) => $router.push(`/kb/${kbId}/doc/${r.id}`)">
          <el-table-column type="selection" width="44" />
          <el-table-column prop="title" label="标题" min-width="220" show-overflow-tooltip>
            <template #default="{ row }"><span class="doc-title">{{ row.title }}</span></template>
          </el-table-column>
          <el-table-column label="状态" width="110" align="center">
            <template #default="{ row }"><StatusBadge :status="row.status" /></template>
          </el-table-column>
          <el-table-column prop="version" label="版本" width="80" />
          <el-table-column prop="effective_date" label="生效日期" width="110" />
          <el-table-column prop="created_at" label="上传时间" width="110">
            <template #default="{ row }">{{ (row.created_at || '').slice(0, 10) }}</template>
          </el-table-column>
          <el-table-column label="" width="60" align="right">
            <template #default="{ row }">
              <el-dropdown trigger="click">
                <el-button text :icon="MoreFilled" @click.stop />
                <template #dropdown>
                  <el-dropdown-menu>
                    <el-dropdown-item @click="$router.push(`/kb/${kbId}/doc/${row.id}`)">详情</el-dropdown-item>
                    <el-dropdown-item v-if="row.source_type === 'wiki'" @click="$router.push(`/wiki?title=${encodeURIComponent(row.title)}`)">Wiki 编辑</el-dropdown-item>
                    <el-dropdown-item v-if="row.status === 'failed'" @click="retry(row)">重试入库</el-dropdown-item>
                    <el-dropdown-item divided @click="del(row)">删除（回收站）</el-dropdown-item>
                  </el-dropdown-menu>
                </template>
              </el-dropdown>
            </template>
          </el-table-column>
          <template #empty>
            <div class="wl-empty"><el-icon><Document /></el-icon><p>当前文件夹还没有文档<br /><small>点击右上角「上传文档」开始</small></p></div>
          </template>
        </el-table>
        <el-pagination v-if="total > size" layout="prev, pager, next" :total="total"
                       :page-size="size" v-model:current-page="page" @current-change="loadDocs" class="pager" />
      </section>
    </div>

    <!-- 新建文件夹 -->
    <el-dialog v-model="folderOpen" title="新建文件夹" width="400px">
      <el-input v-model="newFolder" placeholder="文件夹名称（最多 5 级）" />
      <template #footer>
        <el-button @click="folderOpen = false">取消</el-button>
        <el-button type="primary" @click="createFolder">创建</el-button>
      </template>
    </el-dialog>

    <!-- 上传向导（5 步精简为：选择→上传→提交） -->
    <el-drawer v-model="uploadOpen" title="上传文档" size="480px">
      <el-steps :active="step" simple class="up-steps">
        <el-step title="选择位置" /><el-step title="选择文件" /><el-step title="提交入库" />
      </el-steps>
      <div v-show="step === 0">
        <p class="up-hint">目标文件夹</p>
        <el-tree-select v-model="upFolder" :data="treeData" node-key="id" check-strictly
                        :props="{ label: 'name' }" style="width:100%" placeholder="根目录（全部文档）" clearable />
        <div class="up-actions"><el-button type="primary" @click="step = 1">下一步</el-button></div>
      </div>
      <div v-show="step === 1">
        <el-upload drag multiple :auto-upload="false" :on-change="onFiles" :file-list="fileList"
                   accept=".pdf,.docx,.doc,.xlsx,.xls,.pptx,.md,.txt,.html,.png,.jpg">
          <el-icon class="el-icon--upload"><UploadFilled /></el-icon>
          <div class="el-upload__text">拖拽文件到这里，或 <em>点击选择</em></div>
          <template #tip><div class="el-upload__tip">支持 PDF / Office / Markdown / 图片(OCR)，单文件 ≤ 100MB</div></template>
        </el-upload>
        <div class="up-actions">
          <el-button @click="step = 0">上一步</el-button>
          <el-button type="primary" :disabled="!fileList.length" @click="step = 2">下一步</el-button>
        </div>
      </div>
      <div v-show="step === 2">
        <el-alert type="info" :closable="false" show-icon
                  :title="`将上传 ${fileList.length} 个文件到「${upFolderName}」`"
                  description="提交后进入异步流水线：解析 → 清洗（留痕可还原）→ 分片 → 向量化 → 发布。可在文档列表实时跟踪状态。" />
        <div class="up-actions">
          <el-button @click="step = 1">上一步</el-button>
          <el-button type="primary" :loading="uploading" @click="submitUpload">确认上传</el-button>
        </div>
      </div>
    </el-drawer>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ArrowLeft, Setting, Upload, Plus, Folder, Search, Refresh, MoreFilled, Document, UploadFilled } from '@element-plus/icons-vue'
import { http, USE_MOCK } from '../api/http'
import StatusBadge from '../components/StatusBadge.vue'

const STATUSES: Record<string, string> = { parsing: '解析中', cleaning: '清洗中', indexing: '索引中', published: '已发布', failed: '失败', offline: '已下线' }
const route = useRoute()
const kbId = Number(route.params.id)
const kbName = ref(`知识库 #${kbId}`)
const kbStatus = ref('')
const folders = ref<any[]>([])
const docs = ref<any[]>([])
const total = ref(0)
const page = ref(1)
const size = ref(10)
const SIZES = [10, 15, 20, 30, 50]
const q = ref('')
const statusFilter = ref('')
const loading = ref(false)
const currentFolder = ref<number | null>(null)
const folderOpen = ref(false)
const newFolder = ref('')
const uploadOpen = ref(false)
const step = ref(0)
const upFolder = ref<number | null>(null)
const fileList = ref<any[]>([])
const uploading = ref(false)
const selectedDocs = ref<any[]>([])

async function batchReprocess() {
  const ids = selectedDocs.value.map((d: any) => d.id)
  await (http as any).post('/documents/reprocess-batch', { doc_ids: ids })
  ElMessage.success(`已触发 ${ids.length} 篇文档重处理`)
  loadDocs()
}

const treeData = computed(() => buildTree(folders.value))
const upFolderName = computed(() => folders.value.find((f) => f.id === upFolder.value)?.name || '根目录')

function buildTree(list: any[]): any[] {
  const byParent: Record<number, any[]> = {}
  list.forEach((f) => (byParent[f.parent_id ?? 0] = byParent[f.parent_id ?? 0] || []).push(f))
  const attach = (nodes: any[]): any[] => nodes.map((n) => ({ ...n, children: byParent[n.id] ? attach(byParent[n.id]) : [] }))
  return attach(byParent[0] || [])
}

async function loadAll() {
  try {
    const kbs = (await (http as any).get('/kb')) as any[]
    const kb = kbs.find((k) => k.id === kbId)
    if (kb) { kbName.value = kb.name; kbStatus.value = kb.status }
    folders.value = (await (http as any).get(`/kb/${kbId}/folders`)) as any[]
  } catch { /* */ }
  await loadDocs()
}
async function loadDocs() {
  loading.value = true
  try {
    const params: any = { kb_id: kbId, page: page.value, size: size.value }
    if (currentFolder.value) params.folder_id = currentFolder.value
    if (q.value) params.q = q.value
    if (statusFilter.value) params.status = statusFilter.value
    const r = (await (http as any).get('/documents', { params })) as any
    docs.value = r.items
    total.value = r.total
  } finally { loading.value = false }
}
function onFolderClick(node: any) { currentFolder.value = node.id; page.value = 1; loadDocs() }
async function createFolder() {
  if (!newFolder.value.trim()) return
  await (http as any).post(`/kb/${kbId}/folders`, { name: newFolder.value })
  ElMessage.success('已创建')
  folderOpen.value = false
  newFolder.value = ''
  await loadAll()
}
function onFiles(_: any, list: any[]) { fileList.value = list }
async function submitUpload() {
  uploading.value = true
  try {
    if (USE_MOCK) { await new Promise((r) => setTimeout(r, 500)) } else {
      for (const f of fileList.value) {
        const fd = new FormData()
        fd.append('kb_id', String(kbId))
        if (upFolder.value) fd.append('folder_id', String(upFolder.value))
        fd.append('file', f.raw)
        await (http as any).post('/documents/upload', fd, { headers: { 'Content-Type': 'multipart/form-data' } })
      }
    }
    ElMessage.success('已受理，文档将异步入库（可在列表跟踪状态）')
    uploadOpen.value = false
    fileList.value = []
    step.value = 0
    await loadDocs()
  } catch (e: any) { ElMessage.error(e.message) } finally { uploading.value = false }
}
async function retry(row: any) {
  await (http as any).post(`/documents/${row.id}/rebuild`)
  ElMessage.success('已重新提交入库')
  loadDocs()
}
async function del(row: any) {
  await ElMessageBox.confirm(`确认删除「${row.title}」？将进入回收站保留 30 天。`, '删除确认', { type: 'warning' })
  await (http as any).delete(`/documents/${row.id}`)
  ElMessage.success('已移入回收站')
  loadDocs()
}
onMounted(loadAll)
</script>

<style scoped>
.page-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
.crumb { display: flex; align-items: center; gap: 8px; }
.body-grid { display: grid; grid-template-columns: 260px 1fr; gap: 16px; }
@media (max-width: 1100px) { .body-grid { grid-template-columns: 1fr; } }
.folder-pane { padding: 12px; height: fit-content; position: sticky; top: 16px; max-height: calc(100vh - 32px); overflow-y: auto; }
.fp-head { display: flex; justify-content: space-between; align-items: center; padding: 4px 8px 10px; font-weight: 600; font-size: var(--wl-fs-md); }
.fp-tree :deep(.el-tree-node__content) { height: 34px; border-radius: var(--wl-r-sm); }
.fp-node { display: flex; align-items: center; gap: 6px; width: 100%; font-size: var(--wl-fs-md); }
.fp-node .el-icon { font-size: 14px; flex-shrink: 0; }
.fp-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.fp-count { font-size: var(--wl-fs-xs); color: var(--wl-text-muted); }
.doc-pane { min-width: 0; }
.doc-toolbar { display: flex; gap: 10px; align-items: center; margin-bottom: 12px; }
.spacer { flex: 1; }
.doc-table { cursor: pointer; }
.doc-title { font-weight: 500; }
.pager { margin-top: 16px; justify-content: flex-end; display: flex; }
.up-steps { margin-bottom: 20px; }
.up-hint { font-size: var(--wl-fs-sm); color: var(--wl-text-secondary); margin: 0 0 8px; }
.up-actions { margin-top: 20px; display: flex; justify-content: flex-end; gap: 8px; }
</style>
