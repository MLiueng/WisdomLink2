<template>
  <div class="wl-page">
    <div class="page-head">
      <div>
        <h1 class="wl-page-title">检索评测工作台</h1>
        <p class="wl-page-desc">评估基线与路级归因（M1/M6）：四类题型一键评测，度量 Recall@10 / MRR@10 / Precision@10 / Hit@1 / NDCG@10 / 陈旧探针通过率，并归因到检索各路</p>
      </div>
      <div style="display:flex;gap:10px;align-items:center">
        <el-select v-model="kbId" style="width:210px" placeholder="选择知识库" @change="loadAll">
          <el-option v-for="k in kbs" :key="k.id" :label="k.name" :value="k.id" />
        </el-select>
        <el-button type="primary" :loading="running" :disabled="!kbId" @click="runEval">一键评测</el-button>
      </div>
    </div>

    <el-tabs v-model="tab">
      <el-tab-pane label="评估集" name="cases">
        <div class="tools">
          <el-button type="primary" size="small" :icon="Plus" @click="openCaseForm()">新增评估题</el-button>
          <span class="muted">single_hop 单跳事实 · multi_hop 多跳关系 · stale 已删文档探针（期望零召回）· qa 字典快路径</span>
          <span class="spacer" />
          <el-tag size="small" type="info" effect="plain">共 {{ cases.length }} 题</el-tag>
        </div>
        <el-table :data="pagedCases" size="default">
          <el-table-column label="类型" width="110" align="center">
            <template #default="{ row }">
              <el-tag size="small" effect="light" :type="TYPE_TAG[row.case_type] || 'primary'">{{ TYPE_NAME[row.case_type] || row.case_type }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="question" label="评估问题" min-width="240" />
          <el-table-column prop="expect" label="期望（chunk_id 列表 / 关键词；stale 留空）" min-width="220" />
          <el-table-column label="启用" width="80" align="center">
            <template #default="{ row }"><el-switch v-model="row.enabled" @change="patchCase(row)" /></template>
          </el-table-column>
          <el-table-column label="操作" width="140" align="center">
            <template #default="{ row }">
              <el-button size="small" link type="primary" @click="openCaseForm(row)">编辑</el-button>
              <el-button size="small" link type="danger" @click="delCase(row)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>
        <el-pagination v-if="cases.length > casePageSize" layout="prev, pager, next" :total="cases.length"
                       :page-size="casePageSize" v-model:current-page="casePage" class="pager" />
      </el-tab-pane>

      <el-tab-pane label="评测结果" name="result">
        <template v-if="report">
          <el-row :gutter="16">
            <el-col :span="4"><div class="rs"><b>{{ pct(report.metrics.pass_rate) }}</b><span>总通过率</span></div></el-col>
            <el-col :span="4"><div class="rs"><b>{{ pct(report.metrics.recall) }}</b><span>Recall@10</span></div></el-col>
            <el-col :span="4"><div class="rs"><b>{{ report.metrics.mrr }}</b><span>MRR@10</span></div></el-col>
            <el-col :span="4"><div class="rs"><b>{{ pct(report.metrics.multi_hop_recall) }}</b><span>多跳召回（M6）</span></div></el-col>
            <el-col :span="4"><div class="rs"><b>{{ report.metrics.stale_pass_rate == null ? '—' : pct(report.metrics.stale_pass_rate) }}</b><span>陈旧探针通过</span></div></el-col>
            <el-col :span="4"><div class="rs"><b>{{ report.metrics.qa_accuracy == null ? '—' : pct(report.metrics.qa_accuracy) }}</b><span>QA 命中率</span></div></el-col>
          </el-row>
          <el-row :gutter="16" style="margin-top:12px">
            <el-col :span="4"><div class="rs"><b>{{ pct(report.metrics.precision) }}</b><span>Precision@10</span></div></el-col>
            <el-col :span="4"><div class="rs"><b>{{ pct(report.metrics.hit1) }}</b><span>Hit@1</span></div></el-col>
            <el-col :span="4"><div class="rs"><b>{{ pct(report.metrics.ndcg) }}</b><span>NDCG@10</span></div></el-col>
          </el-row>
          <el-alert v-if="(report.metrics.recall || 0) < 0.85" type="warning" :closable="false" show-icon style="margin-top:14px"
                    title="Recall@10 未达 85% 门槛：请调整分片/检索策略后重新评测（宪法红线 2）" />

          <div class="wl-card result-card">
            <div class="chart-head"><span class="chart-title">路级归因（回答由哪路贡献）</span></div>
            <el-row :gutter="16">
              <el-col :span="12">
                <div class="sec-title">被采纳分片来源分布（Top-10 候选）</div>
                <div v-for="(n, src) in (report.attribution.adopted_topk || {})" :key="src" class="attr-row">
                  <el-tag size="small" :type="SRC_TAG[String(src)] || 'info'" effect="plain" style="width:64px;justify-content:center">{{ src }}</el-tag>
                  <el-progress :percentage="attrPct(Number(n))" :stroke-width="10" style="flex:1;margin:0 10px" />
                  <span class="muted">{{ n }} 片</span>
                </div>
                <div v-if="!Object.keys(report.attribution.adopted_topk || {}).length" class="muted">本轮无采纳记录</div>
              </el-col>
              <el-col :span="12">
                <div class="sec-title">路由档位分布（M2）</div>
                <el-tag v-for="(n, tier) in (report.attribution.routing_tiers || {})" :key="tier"
                        style="margin-right:8px" effect="light" :type="String(tier) === 'deep' ? 'warning' : 'success'">
                  {{ tier }} × {{ n }}
                </el-tag>
                <div v-if="!Object.keys(report.attribution.routing_tiers || {}).length" class="muted">无路由记录</div>
              </el-col>
            </el-row>
          </div>

          <div class="chart-head" style="margin-top:16px"><span class="chart-title">逐题明细（{{ report.details.length }}）</span></div>
          <el-table :data="report.details" size="small" max-height="420">
            <el-table-column label="结果" width="70" align="center">
              <template #default="{ row }"><el-tag size="small" :type="row.passed ? 'success' : 'danger'" effect="light">{{ row.passed ? '通过' : '未过' }}</el-tag></template>
            </el-table-column>
            <el-table-column label="类型" width="90">
              <template #default="{ row }">{{ TYPE_NAME[row.type] || row.type }}</template>
            </el-table-column>
            <el-table-column prop="question" label="问题" min-width="220" show-overflow-tooltip />
            <el-table-column prop="recall" label="recall" width="80" align="center" />
            <el-table-column prop="precision" label="precision" width="90" align="center" />
            <el-table-column prop="ndcg" label="NDCG" width="80" align="center" />
            <el-table-column prop="rank" label="首个命中排名" width="100" align="center" />
            <el-table-column prop="tier" label="路由档" width="90" align="center" />
            <el-table-column label="命中来源" min-width="150">
              <template #default="{ row }">
                <el-tag v-for="(s, i) in row.sources.slice(0, 5)" :key="i" size="small" effect="plain"
                        :type="SRC_TAG[s] || 'info'" style="margin-right:4px">{{ s }}</el-tag>
              </template>
            </el-table-column>
          </el-table>
        </template>
        <el-empty v-else description="尚未评测：先维护评估集，再点「一键评测」" />
      </el-tab-pane>

      <el-tab-pane label="评测历史" name="history">
        <el-table :data="runs" size="default">
          <el-table-column prop="id" label="#" width="70" />
          <el-table-column prop="created_at" label="时间" width="180" />
          <el-table-column label="通过率" width="90" align="center">
            <template #default="{ row }">{{ pct(row.metrics.pass_rate) }}</template>
          </el-table-column>
          <el-table-column label="Recall@10" width="100" align="center">
            <template #default="{ row }">{{ pct(row.metrics.recall) }}</template>
          </el-table-column>
          <el-table-column label="MRR@10" width="90" align="center">
            <template #default="{ row }">{{ row.metrics.mrr }}</template>
          </el-table-column>
          <el-table-column label="多跳召回" width="100" align="center">
            <template #default="{ row }">{{ pct(row.metrics.multi_hop_recall) }}</template>
          </el-table-column>
          <el-table-column label="陈旧探针" width="100" align="center">
            <template #default="{ row }">{{ row.metrics.stale_pass_rate == null ? '—' : pct(row.metrics.stale_pass_rate) }}</template>
          </el-table-column>
          <el-table-column label="操作" width="90" align="center">
            <template #default="{ row }"><el-button size="small" link type="primary" @click="viewRun(row)">查看</el-button></template>
          </el-table-column>
        </el-table>
      </el-tab-pane>
    </el-tabs>

    <el-dialog v-model="caseFormOpen" :title="caseForm.id ? '编辑评估题' : '新增评估题'" width="480px">
      <el-form label-position="top">
        <el-form-item label="题型" required>
          <el-select v-model="caseForm.case_type" style="width:100%">
            <el-option v-for="(v, k) in TYPE_NAME" :key="k" :label="v" :value="k" />
          </el-select>
        </el-form-item>
        <el-form-item label="评估问题" required><el-input v-model="caseForm.question" /></el-form-item>
        <el-form-item label="期望命中（chunk_id 逗号列表 / 关键词逗号列表；stale 型留空表示期望零召回）">
          <el-input v-model="caseForm.expect" type="textarea" :rows="2" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="caseFormOpen = false">取消</el-button>
        <el-button type="primary" @click="saveCase">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus } from '@element-plus/icons-vue'
import { http } from '../api/http'

const TYPE_NAME: Record<string, string> = { single_hop: '单跳', multi_hop: '多跳', stale: '陈旧探针', qa: 'QA' }
const TYPE_TAG: Record<string, string> = { single_hop: 'primary', multi_hop: 'warning', stale: 'danger', qa: 'success' }
const SRC_TAG: Record<string, string> = { dense: 'primary', bm25: 'success', kg: 'warning', qa: 'info', web: 'danger' }

const tab = ref('cases')
const kbs = ref<any[]>([])
const kbId = ref<number | null>(null)
const cases = ref<any[]>([])
const running = ref(false)
const report = ref<any>(null)
const runs = ref<any[]>([])
const casePage = ref(1)
const casePageSize = 20
const pagedCases = computed(() =>
  cases.value.slice((casePage.value - 1) * casePageSize, casePage.value * casePageSize))

const caseFormOpen = ref(false)
const caseForm = ref<any>({ id: null, case_type: 'single_hop', question: '', expect: '', enabled: true })

const pct = (v: number | null | undefined) => v == null ? '—' : `${(v * 100).toFixed(1)}%`
function attrPct(n: number) {
  const total = Number(Object.values(report.value?.attribution?.adopted_topk || {})
    .reduce<number>((a, b) => a + Number(b), 0))
  return total ? Math.round((n / total) * 100) : 0
}

async function loadAll() {
  if (!kbId.value) return
  const [c, h, r] = await Promise.all([
    (http as any).get('/admin/eval/cases', { params: { kb_id: kbId.value } }),
    (http as any).get('/admin/eval/runs', { params: { kb_id: kbId.value, limit: 20 } }),
    report.value ? Promise.resolve(null) : (http as any).get('/admin/eval/runs', { params: { kb_id: kbId.value, limit: 1 } })
  ]) as any
  cases.value = c
  runs.value = h
  // 校验集刷新后页码越界保护（如删除后总数变小）
  const maxPage = Math.max(1, Math.ceil(cases.value.length / casePageSize))
  if (casePage.value > maxPage) casePage.value = maxPage
  if (r && !report.value && r.length) report.value = { metrics: r[0].metrics, attribution: r[0].attribution, details: r[0].details || [] }
}

function openCaseForm(row?: any) {
  caseForm.value = row
    ? { id: row.id, case_type: row.case_type, question: row.question, expect: row.expect, enabled: row.enabled, kb_id: row.kb_id }
    : { id: null, case_type: 'single_hop', question: '', expect: '', enabled: true }
  caseFormOpen.value = true
}
async function saveCase() {
  if (!caseForm.value.question.trim()) return ElMessage.warning('请填写评估问题')
  const body = { kb_id: kbId.value, case_type: caseForm.value.case_type,
                 question: caseForm.value.question, expect: caseForm.value.expect, enabled: caseForm.value.enabled }
  if (caseForm.value.id) await (http as any).patch(`/admin/eval/cases/${caseForm.value.id}`, body)
  else await (http as any).post('/admin/eval/cases', body)
  ElMessage.success('已保存')
  caseFormOpen.value = false
  await loadAll()
}
async function patchCase(row: any) {
  await (http as any).patch(`/admin/eval/cases/${row.id}`,
    { kb_id: row.kb_id, case_type: row.case_type, question: row.question, expect: row.expect, enabled: row.enabled })
}
async function delCase(row: any) {
  await ElMessageBox.confirm(`删除评估题「${row.question}」？`, '删除确认', { type: 'warning' })
  await (http as any).delete(`/admin/eval/cases/${row.id}`)
  ElMessage.success('已删除')
  await loadAll()
}

async function runEval() {
  if (!kbId.value) return
  running.value = true
  try {
    report.value = (await (http as any).post('/admin/eval/auto-run', null,
      { params: { kb_id: kbId.value }, timeout: 300000 })) as any
    tab.value = 'result'
    ElMessage.success('评测完成（结果已落库，可在「评测历史」回看）')
    await loadAll()
  } catch (e: any) {
    ElMessage.error(e.message || '评测失败')
  } finally { running.value = false }
}

async function viewRun(row: any) {
  report.value = { metrics: row.metrics, attribution: row.attribution, details: row.details || [] }
  tab.value = 'result'
}

onMounted(async () => {
  try {
    kbs.value = (await (http as any).get('/kb')) as any[]
    if (kbs.value.length) kbId.value = kbs.value[0].id
    await loadAll()
  } catch { /* */ }
})
</script>

<style scoped>
.page-head { display: flex; justify-content: space-between; align-items: flex-end; margin-bottom: 16px; }
.tools { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; flex-wrap: wrap; }
.spacer { flex: 1; }
.muted { font-size: var(--wl-fs-xs); color: var(--wl-text-muted); }
.result-card { margin-top: 16px; padding: 18px 20px; }
.chart-head { margin-bottom: 12px; }
.chart-title { font-weight: 600; }
.sec-title { font-weight: 600; margin: 4px 0 10px; }
.rs { text-align: center; padding: 12px 0; background: var(--wl-gray-50); border-radius: var(--wl-r-md); }
.rs b { display: block; font-size: 22px; }
.rs span { font-size: var(--wl-fs-xs); color: var(--wl-text-muted); }
.attr-row { display: flex; align-items: center; margin-bottom: 8px; }
.pager { margin-top: 16px; justify-content: flex-end; display: flex; }
</style>
