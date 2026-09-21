<template>
  <div class="wl-page">
    <div class="page-head">
      <div>
        <h1 class="wl-page-title">Wiki 知识</h1>
        <p class="wl-page-desc">Markdown 编辑 · 版本回溯 · 发布后自动进入 RAG 检索池</p>
      </div>
      <div style="display:flex;gap:10px;align-items:center">
        <el-select v-model="wikiKb" placeholder="发布目标知识库" style="width:210px" @change="onKbChange">
          <el-option v-for="k in kbOptions" :key="k.id" :label="k.name" :value="k.id" />
        </el-select>
        <el-button :icon="FolderAdd" @click="createFolder(null)">新建文件夹</el-button>
        <el-button type="primary" :icon="Plus" @click="startEdit">新建页面</el-button>
      </div>
    </div>

    <div class="wiki-grid">
      <aside class="wl-card page-tree">
        <div class="pt-head">
          <span>页面树</span>
          <el-tooltip content="悬停或右键节点：文件夹（新建子文件夹 / 新建页面 / 删除）· 页面（编辑 / 上下线 / 删除）" placement="top">
            <el-icon class="pt-hint"><QuestionFilled /></el-icon>
          </el-tooltip>
        </div>
        <el-tree :data="treeData" node-key="id" :expand-on-click-node="false" highlight-current
                 :default-expanded-keys="expandedKeys" @node-click="onNodeClick"
                 @node-contextmenu="onNodeContext" class="pt-tree">
          <template #default="{ data }">
            <span class="pt-node">
              <el-icon><Folder v-if="data.type === 'folder'" /><Notebook v-else /></el-icon>
              <span class="pt-label">{{ data.label }}</span>
              <span v-if="data.page?.status === 'offline'" class="pt-off" title="已下线（不参与检索）" />
              <span v-if="data.type === 'folder' && data.id !== 'builtin'" class="pt-actions">
                <el-tooltip content="新建子文件夹" placement="top">
                  <el-icon class="pt-act" @click.stop="createFolder({ id: Number(data.id.slice(1)), name: data.label })"><FolderAdd /></el-icon>
                </el-tooltip>
                <el-tooltip content="在此新建页面" placement="top">
                  <el-icon class="pt-act" @click.stop="startEditIn(Number(data.id.slice(1)))"><Plus /></el-icon>
                </el-tooltip>
              </span>
              <span v-if="data.type === 'page' && data.page?.wikiId" class="pt-actions">
                <el-tooltip content="编辑" placement="top">
                  <el-icon class="pt-act" @click.stop="startEdit(data.page)"><Edit /></el-icon>
                </el-tooltip>
                <el-dropdown trigger="click">
                  <el-icon class="pt-act" title="更多操作" @click.stop><MoreFilled /></el-icon>
                  <template #dropdown>
                    <el-dropdown-menu>
                      <el-dropdown-item @click="toggleOffline(data.page)">
                        {{ data.page.status === 'offline' ? '上线（恢复检索）' : '下线（退出检索）' }}
                      </el-dropdown-item>
                      <el-dropdown-item divided @click="remove(data.page)">删除（回收站）</el-dropdown-item>
                    </el-dropdown-menu>
                  </template>
                </el-dropdown>
              </span>
            </span>
          </template>
        </el-tree>
      </aside>

      <section v-if="!isNarrow" class="wl-card content">
        <template v-if="editing">
          <div class="edit-folder-hint">
            {{ editTarget ? '页面所在文件夹' : '发布到文件夹' }}：<el-tag size="small" effect="plain">{{ targetFolderName }}</el-tag>
          </div>
          <el-input v-model="draft.title" placeholder="页面标题" size="large" class="edit-title" />
          <el-input v-model="draft.body" type="textarea" :rows="18" placeholder="# 标题&#10;&#10;正文，支持 **加粗**、表格、代码块…" class="edit-body" />
          <div class="edit-actions">
            <el-button @click="editing = false">取消</el-button>
            <el-button type="primary" :loading="publishing" @click="publish">发布并入库</el-button>
          </div>
        </template>
        <template v-else-if="current">
          <div class="content-head">
            <h2 class="page-title">{{ current.title }}</h2>
            <div v-if="current.wikiId" class="content-ops">
              <el-button type="primary" :icon="Edit" @click="startEdit(current)">编辑页面</el-button>
              <el-button :type="current.status === 'offline' ? 'success' : 'warning'"
                         @click="toggleOffline(current)">{{ current.status === 'offline' ? '上线' : '下线' }}</el-button>
              <el-button type="danger" plain :icon="Delete" @click="remove(current)">删除</el-button>
            </div>
            <el-tag v-else size="small" type="info" effect="plain">内置示例 · 未发布不参与检索</el-tag>
          </div>
          <div class="page-meta">
            <span>更新于 {{ current.updated }}</span>
            <el-tag v-if="current.wikiId && current.status !== 'offline'" size="small" type="success" effect="light">已入检索索引</el-tag>
            <el-tag v-if="current.status === 'offline'" size="small" type="warning" effect="light">已下线</el-tag>
          </div>
          <div class="wl-md page-body" v-html="render(current.body)"></div>
        </template>
        <div v-else class="wl-empty"><el-icon><Notebook /></el-icon>
          <p>在左侧页面树选择一个页面查看<br /><small>悬停或右键页面节点，可编辑 / 上下线 / 删除</small></p>
        </div>
      </section>
    </div>

    <!-- 窄屏（单栏）：点击页面树弹出 Drawer 展示/编辑 -->
    <el-drawer v-model="drawerOpen" :title="editing ? (editTarget ? '编辑页面' : '新建页面') : drawerPage?.title || ''" size="88%" direction="rtl">
      <template v-if="editing">
        <div class="edit-folder-hint">
          {{ editTarget ? '页面所在文件夹' : '发布到文件夹' }}：<el-tag size="small" effect="plain">{{ targetFolderName }}</el-tag>
        </div>
        <el-input v-model="draft.title" placeholder="页面标题" size="large" class="edit-title" />
        <el-input v-model="draft.body" type="textarea" :rows="14" placeholder="# 标题&#10;&#10;正文，支持 **加粗**、表格、代码块…" />
        <div class="edit-actions">
          <el-button @click="editing = false; drawerOpen = false">取消</el-button>
          <el-button type="primary" :loading="publishing" @click="publish">发布并入库</el-button>
        </div>
      </template>
      <template v-else-if="drawerPage">
        <div class="page-meta"><span>更新于 {{ drawerPage.updated }}</span>
          <el-tag v-if="drawerPage.wikiId && drawerPage.status !== 'offline'" size="small" type="success" effect="light">已入检索索引</el-tag>
          <el-tag v-if="drawerPage.status === 'offline'" size="small" type="warning" effect="light">已下线</el-tag>
        </div>
        <div v-if="drawerPage.wikiId" class="content-ops" style="margin-bottom:16px">
          <el-button type="primary" size="small" :icon="Edit" @click="startEdit(drawerPage)">编辑页面</el-button>
          <el-button size="small" :type="drawerPage.status === 'offline' ? 'success' : 'warning'"
                     @click="toggleOffline(drawerPage)">{{ drawerPage.status === 'offline' ? '上线' : '下线' }}</el-button>
          <el-button size="small" type="danger" :icon="Delete" @click="remove(drawerPage)">删除</el-button>
        </div>
        <div class="wl-md page-body" v-html="render(drawerPage.body)"></div>
      </template>
    </el-drawer>

    <!-- 树节点右键菜单 -->
    <Teleport to="body">
      <div v-if="ctxMenu.visible" class="ctx-mask" @click="ctxClose" @contextmenu.prevent="ctxClose">
        <div class="ctx-menu" :style="{ left: ctxMenu.x + 'px', top: ctxMenu.y + 'px' }" @click.stop>
          <template v-if="ctxMenu.page">
            <div class="ctx-title">{{ ctxMenu.page.title }}</div>
            <div class="ctx-item" @click="ctxRun(startEdit)"><el-icon><Edit /></el-icon>编辑</div>
            <div class="ctx-item" @click="ctxRun(toggleOffline)"><el-icon><CircleCheck v-if="ctxMenu.page.status === 'offline'" /><CircleClose v-else /></el-icon>
              {{ ctxMenu.page.status === 'offline' ? '上线（恢复检索）' : '下线（退出检索）' }}</div>
            <div class="ctx-item ctx-danger" @click="ctxRun(remove)"><el-icon><Delete /></el-icon>删除（回收站）</div>
          </template>
          <template v-else-if="ctxMenu.folder">
            <div class="ctx-title">{{ ctxMenu.folder.name }}</div>
            <div class="ctx-item" @click="ctxRunFolder(createFolder)"><el-icon><FolderAdd /></el-icon>新建子文件夹</div>
            <div class="ctx-item" @click="ctxRunFolder((f) => startEditIn(f.id))"><el-icon><Plus /></el-icon>在此新建页面</div>
            <div class="ctx-item ctx-danger" @click="ctxRunFolder(removeFolder)"><el-icon><Delete /></el-icon>删除文件夹</div>
          </template>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Notebook, Folder, FolderAdd, Edit, Delete, MoreFilled, QuestionFilled, CircleCheck, CircleClose } from '@element-plus/icons-vue'
import { useRoute } from 'vue-router'
import { renderMarkdown } from '../utils/markdown'
import { http } from '../api/http'

interface WikiPage {
  id: number | string
  title: string
  updated: string
  body: string
  wikiId?: number
  kbId?: number
  folderId?: number | null
  status?: string
}
interface TreeNode {
  id: string
  label: string
  type: 'folder' | 'page'
  page?: WikiPage
  folderId?: number
  children?: TreeNode[]
}

const BUILTIN: WikiPage[] = [
  { id: 1, title: '产品发布流程 SOP', updated: '2026-09-08', body: '# 产品发布流程\n\n## 1. 发布前检查\n- 需求验收全部通过\n- 回滚方案就绪\n\n## 2. 发布窗口\n每周二、周四 20:00 后。' },
  { id: 2, title: '新人上手指南', updated: '2026-09-02', body: '# 新人上手指南\n\n第一天：开通账号；第一周：读完制度库 FAQ。' }
]

const pages = ref<WikiPage[]>([...BUILTIN])
const current = ref<WikiPage | null>(BUILTIN[0])
const editing = ref(false)
const draft = ref({ title: '', body: '' })
const wikiKb = ref<number | null>(null)
const kbOptions = ref<any[]>([])
const publishing = ref(false)
const folders = ref<any[]>([])
const wikiDocs = ref<any[]>([])
const drawerOpen = ref(false)
const drawerPage = ref<WikiPage | null>(null)
const editTarget = ref<WikiPage | null>(null)   // 编辑已有页时指向该页（发布走其新版本）
const targetFolder = ref<number | null>(null)   // 新建页面归属文件夹（在文件夹节点上发起时预置）
const route = useRoute()   // setup 顶层取 route：异步回调中再调 useRoute() 会拿不到组件上下文
// A 为主：≥900px 一律内联展示（与侧边栏自动收起阈值一致）；仅真窄屏用 Drawer 补充
const NARROW_AT = 900
const isNarrow = ref(window.innerWidth <= NARROW_AT)
const render = (t: string) => renderMarkdown(t)

function onResize() { isNarrow.value = window.innerWidth <= NARROW_AT }
onMounted(() => window.addEventListener('resize', onResize))
onUnmounted(() => window.removeEventListener('resize', onResize))

// 页面树 = 文件夹分组（后端 folder_id）+ wiki 文档叶子；空文件夹也展示（刚建的树节点要可见）
const treeData = computed<TreeNode[]>(() => {
  const byParent: Record<number, any[]> = {}
  folders.value.forEach((f) => (byParent[f.parent_id ?? 0] = byParent[f.parent_id ?? 0] || []).push(f))
  const pageNode = (p: WikiPage): TreeNode => ({ id: 'p' + p.id, label: p.title, type: 'page', page: p })
  const pagesOf = (fid: number) => pages.value.filter((p) => p.wikiId && (p.folderId ?? 0) === fid).map(pageNode)
  const build = (fid: number): TreeNode[] =>
    (byParent[fid] || []).map((f: any) => ({
      id: 'f' + f.id, label: f.name, type: 'folder', folderId: f.id, children: [...build(f.id), ...pagesOf(f.id)]
    }))
  const builtin = pages.value.filter((p) => !p.wikiId).map(pageNode)
  return [...build(0), ...pagesOf(0), ...(builtin.length ? [{ id: 'builtin', label: '内置示例', type: 'folder' as const, children: builtin }] : [])]
})
const expandedKeys = computed(() => treeData.value.map((n) => n.id))
// 编辑态提示：新建页归属哪个文件夹（根目录/所选文件夹）；编辑页显示其当前文件夹
const targetFolderName = computed(() =>
  editTarget.value
    ? (folders.value.find((f) => f.id === editTarget.value?.folderId)?.name || '根目录')
    : (folders.value.find((f) => f.id === targetFolder.value)?.name || '根目录'))

async function loadFolders() {
  if (!wikiKb.value) { folders.value = []; return }
  try { folders.value = (await (http as any).get(`/kb/${wikiKb.value}/folders`)) as any[] } catch { folders.value = [] }
}
async function loadWikiDocs() {
  if (!wikiKb.value) { wikiDocs.value = []; return }
  try {
    // 分页拉全（后端 size 上限内逐页累积），避免 Wiki 页面超过单页上限时被静默截断
    const acc: any[] = []
    let page = 1
    let total = 0
    do {
      const r = (await (http as any).get(`/documents?kb_id=${wikiKb.value}&page=${page}&size=200`)) as any
      total = r.total
      const items = r.items || []
      acc.push(...items)
      if (!items.length) break   // 空页保护，防止 total 与实际不一致导致死循环
      page += 1
    } while (acc.length < total)
    wikiDocs.value = acc.filter((d: any) => d.source_type === 'wiki' && d.status !== 'deleted')
  } catch { wikiDocs.value = [] }
  pages.value = [...BUILTIN, ...wikiDocs.value.map((d: any) => ({
    id: 'w' + d.id, title: d.title, updated: d.created_at || '', body: '',
    wikiId: d.id, kbId: d.kb_id, folderId: d.folder_id ?? null, status: d.status
  }))]
}
async function onKbChange() {
  await Promise.all([loadFolders(), loadWikiDocs()])
  current.value = pages.value[0] || null
}

async function ensureBody(p: WikiPage) {   // 懒加载正文（条目多时不批量拉 preview）
  if (p.wikiId && !p.body) {
    try { p.body = await (await fetch(`/api/documents/${p.wikiId}/preview`)).text() } catch { p.body = '*正文加载失败*' }
  }
}

async function onNodeClick(data: TreeNode) {
  if (data.type !== 'page' || !data.page) return
  const p = data.page
  await ensureBody(p)
  current.value = p
  if (isNarrow.value) { drawerPage.value = p; drawerOpen.value = true }
}

/** 树节点右键菜单：页面（编辑/上下线/删除）与文件夹（新建子文件夹/新建页面/删除） */
const ctxMenu = ref({ visible: false, x: 0, y: 0, page: null as WikiPage | null, folder: null as { id: number; name: string } | null })
function onNodeContext(ev: MouseEvent, data: TreeNode) {
  ev.preventDefault()
  if (data.type === 'page' && data.page?.wikiId) {
    ctxMenu.value = { visible: true, x: ev.clientX, y: ev.clientY, page: data.page, folder: null }
  } else if (data.type === 'folder' && data.id !== 'builtin' && data.folderId) {
    ctxMenu.value = { visible: true, x: ev.clientX, y: ev.clientY, page: null, folder: { id: data.folderId, name: data.label } }
  }
}
function ctxClose() { ctxMenu.value.visible = false }
function ctxRun(fn: (p: WikiPage) => void) {
  const p = ctxMenu.value.page
  ctxMenu.value.visible = false
  if (p) fn(p)
}
function ctxRunFolder(fn: (f: { id: number; name: string }) => void) {
  const f = ctxMenu.value.folder
  ctxMenu.value.visible = false
  if (f) fn(f)
}

/** 新建文件夹：parent 为 null 建根级；后端限制 ≤5 级（B-30） */
async function createFolder(parent: { id: number; name: string } | null) {
  if (!wikiKb.value) return ElMessage.warning('请先选择目标知识库')
  let name = ''
  try {
    const res = await ElMessageBox.prompt(
      parent ? `在「${parent.name}」下新建子文件夹（最多 5 级）` : '在根目录新建文件夹（最多 5 级）',
      '新建文件夹', { confirmButtonText: '创建', cancelButtonText: '取消', inputPlaceholder: '文件夹名称' })
    name = (typeof res === 'string' ? res : (res as any)?.value || '').trim()
    if (!name) return
  } catch { return }   // 仅弹窗取消静默退出
  try {   // API 失败（401/层级超限等）单独提示，不得与用户取消混在一个 catch 里吞掉
    await (http as any).post(`/kb/${wikiKb.value}/folders`, { name, parent_id: parent?.id ?? null })
    ElMessage.success(`已创建文件夹「${name}」`)
    await loadFolders()
  } catch (e: any) {
    ElMessage.error(e.message || '创建文件夹失败')
  }
}

/** 删除文件夹：内部页面（含子文件夹）移回根目录 */
async function removeFolder(f: { id: number; name: string }) {
  try {
    await ElMessageBox.confirm(`删除文件夹「${f.name}」？其下页面（含子文件夹内）将移回根目录。`, '删除确认', { type: 'warning' })
  } catch { return }   // 用户取消
  try {
    await (http as any).delete(`/kb/folders/${f.id}`)
    ElMessage.success('已删除，页面已移回根目录')
    await Promise.all([loadFolders(), loadWikiDocs()])
    if (current.value && !pages.value.find((p) => p.id === current.value?.id)) current.value = null
  } catch (e: any) {
    ElMessage.error(e.message || '删除文件夹失败')
  }
}

/** 在指定文件夹内新建页面 */
function startEditIn(folderId: number) {
  startEdit(null, folderId)
}

async function startEdit(page?: WikiPage | null, folderId?: number) {
  if (page?.wikiId) {   // 编辑已有页：回填正文，发布走该文档的新版本
    await ensureBody(page)
    draft.value = { title: page.title, body: page.body || '' }
    editTarget.value = page
    targetFolder.value = page.folderId ?? null
  } else {   // 新建：顶部按钮=根目录；文件夹入口（悬停 +/右键）经 folderId 归组
    draft.value = { title: '', body: '' }
    editTarget.value = null
    targetFolder.value = folderId ?? null
  }
  editing.value = true
  if (isNarrow.value) drawerOpen.value = true
}

async function toggleOffline(p: WikiPage) {
  const toOffline = p.status !== 'offline'
  await ElMessageBox.confirm(
    toOffline ? `下线「${p.title}」后将立即退出检索召回，可随时恢复上线。` : `恢复上线「${p.title}」并重新参与检索。`,
    toOffline ? '下线确认' : '上线确认', { type: 'warning' })
  await (http as any).patch(`/documents/${p.wikiId}`, { status: toOffline ? 'offline' : 'published' })
  p.status = toOffline ? 'offline' : 'published'
  ElMessage.success(toOffline ? '已下线，退出检索' : '已上线，恢复检索')
}

async function remove(p: WikiPage) {
  await ElMessageBox.confirm(`删除「${p.title}」？将进回收站保留 30 天，并立即退出检索召回。`, '删除确认', { type: 'warning' })
  await (http as any).delete(`/documents/${p.wikiId}`)
  ElMessage.success('已删除（回收站保留 30 天）')
  drawerOpen.value = false
  await loadWikiDocs()
  current.value = current.value?.wikiId === p.wikiId ? null : current.value
}

onMounted(async () => {
  try { kbOptions.value = (await (http as any).get('/kb')) as any[] } catch { /* 预览模式 */ }
  if (kbOptions.value.length) wikiKb.value = kbOptions.value[0].id
  await Promise.all([loadFolders(), loadWikiDocs()])
  const qTitle = (route.query.title || '') as string
  if (qTitle) {
    const d = wikiDocs.value.find((x) => x.title === qTitle)
    if (d) await onNodeClick({ id: 'p' + ('w' + d.id), label: d.title, type: 'page', page: pages.value.find((p) => p.wikiId === d.id) })
  }
  const pre = sessionStorage.getItem('wl2_wiki_prefill')
  if (pre) {
    try {
      const pf = JSON.parse(pre)
      draft.value = { title: pf.title || '', body: pf.body || '' }
      startEdit()
    } finally { sessionStorage.removeItem('wl2_wiki_prefill') }
  }
})

async function publish() {
  if (!draft.value.title.trim()) return ElMessage.warning('请填写标题')
  if (!wikiKb.value) return ElMessage.warning('请先选择目标知识库（Wiki 页面发布后归入该库并参与检索）')
  publishing.value = true
  try {
    const file = new File([draft.value.body], draft.value.title + '.md', { type: 'text/markdown' })
    const fd = new FormData()
    fd.append('kb_id', String(wikiKb.value))
    fd.append('source_type', 'wiki')
    if (editTarget.value?.wikiId) {   // 编辑已有页：原地新版本（旧版分片/向量级联清理）
      fd.append('new_version', 'true')
      fd.append('doc_id', String(editTarget.value.wikiId))
    } else {
      if (wikiDocs.value.find((x) => x.title === draft.value.title)) fd.append('new_version', 'true')
      if (targetFolder.value) fd.append('folder_id', String(targetFolder.value))   // 新建页面归入所选文件夹
    }
    fd.append('file', file)
    await (http as any).post('/documents/upload', fd, { headers: { 'Content-Type': 'multipart/form-data' } })
    ElMessage.success(editTarget.value ? '已更新：新版本进入 解析→清洗→分片→索引 流水线' : '已发布：Wiki 页面进入 解析→清洗→分片→索引 流水线（到对应知识库可查看）')
    const targetId = editTarget.value?.wikiId
    await loadWikiDocs()
    const np = targetId ? pages.value.find((p) => p.wikiId === targetId) : pages.value.find((p) => p.title === draft.value.title)
    if (np) { np.body = draft.value.body; np.status = 'published'; current.value = np; drawerPage.value = np }
    editing.value = false
    drawerOpen.value = false
    editTarget.value = null
    targetFolder.value = null
  } catch (e: any) {
    ElMessage.error(e.message || '发布失败')
  } finally {
    publishing.value = false
  }
}
</script>

<style scoped>
.page-head { display: flex; justify-content: space-between; align-items: flex-end; margin-bottom: 16px; }
.wiki-grid { display: grid; grid-template-columns: 260px 1fr; gap: 16px; }
@media (max-width: 900px) { .wiki-grid { grid-template-columns: 1fr; } }
.page-tree { padding: 12px; max-height: min(560px, calc(100vh - 220px)); display: flex; flex-direction: column; }
.pt-head { font-weight: 600; font-size: var(--wl-fs-md); padding: 4px 8px 10px; flex-shrink: 0; display: flex; justify-content: space-between; align-items: center; }
.pt-hint { color: var(--wl-text-muted); cursor: help; font-size: 14px; }
.pt-tree { overflow: auto; min-height: 0; }
.pt-node { display: flex; align-items: center; gap: 6px; font-size: var(--wl-fs-md); min-width: 0; width: 100%; }
.pt-node > .el-icon { font-size: 14px; flex-shrink: 0; }
.pt-label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; }
.pt-off { width: 6px; height: 6px; border-radius: 50%; background: var(--el-color-warning); flex-shrink: 0; }
.pt-actions { display: none; align-items: center; gap: 4px; margin-left: auto; flex-shrink: 0; }
.pt-node:hover .pt-actions { display: inline-flex; }
/* 注意：.el-icon 自带 width/height:1em，全局 border-box 会让 padding 吃掉图标本体，
   故交互图标统一 content-box，padding 作为外扩点击区 */
.pt-act { font-size: 16px; box-sizing: content-box; color: var(--wl-text-muted); cursor: pointer; padding: 4px; border-radius: var(--wl-r-sm); }
.pt-act:hover { color: var(--el-color-primary); background: var(--el-fill-color-light); }
/* 右键菜单 */
.ctx-mask { position: fixed; inset: 0; z-index: 3000; }
.ctx-menu { position: fixed; min-width: 180px; background: var(--el-bg-color-overlay); border: 1px solid var(--wl-border); border-radius: var(--wl-r-md); box-shadow: var(--el-box-shadow-light); padding: 6px; }
.ctx-title { font-size: var(--wl-fs-xs); color: var(--wl-text-muted); padding: 4px 8px 6px; max-width: 260px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; border-bottom: 1px solid var(--wl-border); margin-bottom: 4px; }
.ctx-item { display: flex; align-items: center; gap: 8px; padding: 7px 10px; border-radius: var(--wl-r-sm); font-size: var(--wl-fs-md); cursor: pointer; }
.ctx-item .el-icon { font-size: 14px; }   /* 菜单图标对齐 EP 下拉默认 14px */
.ctx-item:hover { background: var(--el-fill-color-light); color: var(--el-color-primary); }
.ctx-danger:hover { background: var(--el-color-danger-light-9); color: var(--el-color-danger); }
.content { padding: 28px 32px; min-height: 500px; }
.content-head { display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; }
.content-ops { display: flex; gap: 8px; flex-wrap: wrap; }
.page-title { margin: 0 0 6px; font-size: 22px; }
.page-meta { display: flex; gap: 12px; align-items: center; font-size: var(--wl-fs-xs); color: var(--wl-text-muted); margin-bottom: 24px; padding-bottom: 16px; border-bottom: 1px solid var(--wl-border); }
.page-body { max-width: 820px; font-size: var(--wl-fs-lg); }   /* 阅读区用 16px 阅读字号，大于问答区 14px */
.page-body :deep(h1) { font-size: 1.6em; }   /* 正文 h1 多为文档标题，2em 在 16px 下过大 */
.edit-title { margin-bottom: 12px; }
.edit-folder-hint { display: flex; align-items: center; gap: 6px; font-size: var(--wl-fs-xs); color: var(--wl-text-muted); margin-bottom: 8px; }
.edit-actions { margin-top: 14px; display: flex; justify-content: flex-end; gap: 8px; }
</style>
