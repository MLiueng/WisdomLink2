<template>
  <div class="wl-page">
    <div class="page-head">
      <div>
        <h1 class="wl-page-title">QA 对管理</h1>
        <p class="wl-page-desc">高频同类问题命中 QA 对秒回标准答案（不消耗模型 Token）；命中优先级：QA 快速命中 → RAG</p>
      </div>
      <div>
        <el-button :icon="Download" @click="exportQa">导出</el-button>
        <el-button type="primary" :icon="Plus" @click="editOpen">新建 QA 对</el-button>
      </div>
    </div>

    <div class="tools">
      <el-select v-model="currentKb" placeholder="选择知识库" style="width:180px" @change="page = 1; load()">
        <el-option v-for="k in kbs" :key="k.id" :label="k.name" :value="k.id" />
      </el-select>
      <el-input v-model="q" placeholder="搜索标准问题" :prefix-icon="Search" clearable style="width:240px" />
      <el-select v-model="statusFilter" placeholder="状态" clearable style="width:120px">
        <el-option label="启用" value="enabled" /><el-option label="停用" value="disabled" />
      </el-select>
      <span class="spacer" />
      <el-button :icon="Refresh" circle @click="load" />
    </div>
    <el-table :data="items" v-loading="loading">
      <el-table-column prop="question" label="标准问题" min-width="200" show-overflow-tooltip />
      <el-table-column label="变体" width="180">
        <template #default="{ row }">
          <el-tag v-for="v in row.variants.slice(0, 2)" :key="v" size="small" effect="plain" style="margin-right:4px">{{ v }}</el-tag>
          <span v-if="row.variants.length > 2" class="muted">+{{ row.variants.length - 2 }}</span>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="90" align="center">
        <template #default="{ row }"><StatusBadge :status="row.stale ? 'stale' : row.status" /></template>
      </el-table-column>
      <el-table-column label="命中" width="150">
        <template #default="{ row }">
          <div class="hit-bar"><div class="hit-fill" :style="{ width: Math.min(100, row.hit_count / 4) + '%' }" /></div>
          <span class="hit-num">{{ row.hit_count }} 次</span>
        </template>
      </el-table-column>
      <el-table-column prop="last_hit_at" label="最近命中" width="110">
        <template #default="{ row }">{{ (row.last_hit_at || '—').slice(0, 10) }}</template>
      </el-table-column>
      <el-table-column label="" width="150" align="right">
        <template #default="{ row }">
          <el-switch v-model="row.enabled" size="small" @change="toggle(row)" style="margin-right:8px" />
          <el-button text size="small" @click="openEdit(row)">编辑</el-button>
        </template>
      </el-table-column>
      <template #empty><div class="wl-empty"><el-icon><QuestionFilled /></el-icon><p>还没有 QA 对<br /><small>从右侧「待沉淀」把高频问题转成标准答案</small></p></div></template>
    </el-table>
    <div style="margin-left:auto;display:flex;align-items:center;gap:6px"><span style="font-size:12px;color:var(--wl-text-muted)">每页</span><el-select v-model="size" size="small" style="width:76px" @change="page = 1; load()"><el-option v-for="s in SIZES" :key="s" :label="s" :value="s" /></el-select></div><el-pagination class="pager" layout="prev, pager, next, total" :total="total"
                  :page-size="size" v-model:current-page="page" @current-change="load" />

    <div class="mining">
      <div class="sec-title">
        待沉淀（问答日志高频问题 + 点踩问题）
        <el-tooltip content="来自问答日志的高频/未命中问题与点踩问题（F-05），一键转为 QA 对草稿或评估题草稿"><el-icon><InfoFilled /></el-icon></el-tooltip>
      </div>
      <div class="mine-list">
        <div v-for="m in mining" :key="m.question" class="mine-item wl-card is-hover">
          <el-icon color="var(--wl-warning)"><WarningFilled /></el-icon>
          <span class="mine-q">{{ m.question }}</span>
          <el-tag size="small" :type="m.type === 'refusal' ? 'danger' : 'info'" effect="plain">{{ m.type === 'refusal' ? '未命中' : m.type === 'dislike' ? '点踩' : '走 RAG' }}</el-tag>
          <el-tag v-if="m.disliked" size="small" type="danger" effect="dark">点踩</el-tag>
          <span class="mine-count">×{{ m.count }}</span>
          <el-button size="small" type="primary" plain @click="fromMining(m)">转为 QA</el-button>
          <el-button size="small" plain @click="toEvalCase(m)">转评估题</el-button>
        </div>
      </div>
    </div>

    <el-drawer v-model="open" :title="form.id ? '编辑 QA 对' : '新建 QA 对'" size="520px">
      <el-form label-position="top">
        <el-form-item label="标准问题" required><el-input v-model="form.std_question" /></el-form-item>
        <el-form-item label="标准答案（支持 Markdown）" required>
          <el-input v-model="form.std_answer" type="textarea" :rows="6" />
        </el-form-item>
        <el-form-item label="相似问变体（每行一条）">
          <el-input v-model="variantsText" type="textarea" :rows="3" placeholder="怎么申请年假&#10;年假天数" />
        </el-form-item>
        <el-form-item label="权重（同分时优先命中高权重）">
          <el-slider v-model="form.weight" :min="1" :max="200" show-input />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="open = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存并生效</el-button>
      </template>
    </el-drawer>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Plus, Search, Refresh, Download, InfoFilled, WarningFilled, QuestionFilled } from '@element-plus/icons-vue'
import { http, USE_MOCK } from '../api/http'
import StatusBadge from '../components/StatusBadge.vue'

const items = ref<any[]>([])
const mining = ref<any[]>([])
const loading = ref(false)
const kbs = ref<any[]>([])
const currentKb = ref<number>(0)
const q = ref('')
const size = ref(10)
const SIZES = [10, 15, 20, 30, 50]
const statusFilter = ref('')
const open = ref(false)
const saving = ref(false)
const variantsText = ref('')
const form = ref<any>({ id: null, kb_id: 1, std_question: '', std_answer: '', weight: 100 })
const variants = computed(() => variantsText.value.split('\n').map((s) => s.trim()).filter(Boolean))

const page = ref(1)
const total = ref(0)
async function load() {
  loading.value = true
  try {
    // F-06：列表与待沉淀榜单统一按当前知识库传参（/qa/mining 的 kb_id 为必填，缺省真实模式必 422）
    const r = (await (http as any).get('/qa', { params: { kb_id: currentKb.value || undefined, q: q.value || undefined, status: statusFilter.value || undefined, page: page.value, size: size.value } })) as any
    items.value = r.items
    total.value = r.total
    mining.value = (await (http as any).get('/qa/mining', { params: { kb_id: currentKb.value } })) as any[]
  } finally { loading.value = false }
}
onMounted(async () => {
  try {
    kbs.value = (await (http as any).get('/kb')) as any[]
    if (kbs.value.length) currentKb.value = kbs.value[0].id
  } catch { /* 预览模式 */ }
  await load()
})

function editOpen() {
  form.value = { id: null, kb_id: currentKb.value || 1, std_question: '', std_answer: '', weight: 100 }
  variantsText.value = ''
  open.value = true
}
function openEdit(row: any) {
  form.value = { id: row.id, kb_id: row.kb_id, std_question: row.question, std_answer: row.answer, weight: row.weight }
  variantsText.value = (row.variants || []).join('\n')
  open.value = true
}
function fromMining(m: any) {
  form.value = { id: null, kb_id: currentKb.value || 1, std_question: m.question, std_answer: '', weight: 100 }
  variantsText.value = ''
  open.value = true
}
async function save() {
  saving.value = true
  try {
    const body = { ...form.value, variants: variants.value, ref_doc_ids: [], status: 'enabled' }
    if (form.value.id) await (http as any).patch(`/qa/${form.value.id}`, body)
    else await (http as any).post('/qa', body)
    ElMessage.success('已保存，命中字典 ≤1 分钟生效')
    open.value = false
    await load()
  } catch (e: any) { ElMessage.error(e.message) } finally { saving.value = false }
}
async function toggle(row: any) {
  await (http as any).post(`/qa/${row.id}/toggle`)
  ElMessage.success(row.enabled ? '已启用' : '已停用（即时退出命中）')
}
async function toEvalCase(m: any) {
  // F-05：差评/高频问题一键转评估题草稿（默认停用，补期望后再启用）
  try {
    await (http as any).post('/admin/eval/cases', {
      kb_id: currentKb.value, case_type: 'single_hop', question: m.question, expect: '', enabled: false
    })
    ElMessage.success('已转为评估题草稿（检索评测工作台 → 评估集，补充期望命中后启用）')
  } catch (e: any) { ElMessage.error(e.message || '转评估题失败') }
}
function exportQa() {
  if (USE_MOCK) return ElMessage.info('预览模式')
  window.open(`/api/qa/export?kb_id=${currentKb.value || 1}`, '_blank')
}
</script>

<style scoped>
.page-head { display: flex; justify-content: space-between; align-items: flex-end; margin-bottom: 16px; }
.tools { display: flex; gap: 10px; margin-bottom: 12px; }
.spacer { flex: 1; }
.hit-bar { height: 4px; background: var(--wl-gray-100); border-radius: 2px; width: 100px; }
.hit-fill { height: 100%; background: var(--wl-primary); border-radius: 2px; }
.hit-num { font-size: var(--wl-fs-xs); color: var(--wl-text-muted); }
.mining { margin-top: 28px; }
.sec-title { display: flex; align-items: center; gap: 6px; font-weight: 600; margin-bottom: 10px; font-size: var(--wl-fs-md); }
.mine-list { display: flex; flex-direction: column; gap: 8px; }
.mine-item { display: flex; align-items: center; gap: 10px; padding: 10px 14px; flex-wrap: wrap; }
.mine-q { flex: 1; font-size: var(--wl-fs-md); }
.mine-count { font-size: var(--wl-fs-xs); color: var(--wl-text-muted); }
.muted { font-size: var(--wl-fs-xs); color: var(--wl-text-muted); }
.pager { margin-top: 12px; justify-content: flex-end; display: flex; }
</style>
