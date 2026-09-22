<template>
  <div class="wl-page">
    <div class="page-head">
      <div>
        <h1 class="wl-page-title">知识图谱</h1>
        <p class="wl-page-desc">概念（本体）· 实体 · 关系三层内容；AI 抽取候选 → 人工确认 → 参与关联检索</p>
      </div>
      <div style="display:flex;gap:10px;align-items:center">
        <el-select v-model="kbId" style="width:210px" placeholder="选择知识库">
          <el-option v-for="k in kbs" :key="k.id" :label="k.name" :value="k.id" />
        </el-select>
        <el-button :icon="Download" @click="exportKg">导出三元组</el-button>
      </div>
    </div>

    <el-tabs v-model="tab">
      <el-tab-pane label="图谱视图" name="graph">
        <div class="tools">
          <el-radio-group v-model="graphStatus" size="small" @change="loadGraph">
            <el-radio-button value="confirmed">已确认</el-radio-button>
            <el-radio-button value="all">含候选</el-radio-button>
          </el-radio-group>
          <span class="muted">拖拽节点调整布局 · 滚轮缩放 · 悬停查看关系谓词</span>
          <span class="spacer" />
          <el-button size="small" :icon="Refresh" circle @click="loadGraph" />
        </div>
        <div class="wl-card graph-card">
          <div v-if="!graphData.nodes.length" class="wl-empty">
            <el-icon><Share /></el-icon>
            <p>该知识库暂无图谱数据<br /><small>到「关系」页使用 AI 抽取，或在关联文档入库后构建</small></p>
          </div>
          <div ref="graphEl" class="graph" v-show="graphData.nodes.length" />
        </div>
      </el-tab-pane>

      <el-tab-pane label="实体" name="nodes">
        <div class="tools">
          <el-button type="primary" size="small" :icon="Plus" @click="addNode">新增实体</el-button>
          <el-button size="small" :disabled="!nodeSelected.length" @click="batchNodes('confirm')">批量确认（{{ nodeSelected.length }}）</el-button>
          <el-button size="small" :disabled="!nodeSelected.length" @click="batchNodes('reject')">批量拒绝</el-button>
          <span class="spacer" />
          <el-tag size="small" type="info" effect="plain">共 {{ nodeTotal }} 个</el-tag>
        </div>
        <el-table :data="nodes" size="default" @selection-change="(s: any) => (nodeSelected = s)">
          <el-table-column type="selection" width="44" />
          <el-table-column label="实体" min-width="160">
            <template #default="{ row }"><el-link type="primary" :underline="false" @click="showEntity(row)">{{ row.name }}</el-link></template>
          </el-table-column>
          <el-table-column label="概念类型" width="120">
            <template #default="{ row }">{{ conceptName(row.concept_id) }}</template>
          </el-table-column>
          <el-table-column label="别名" min-width="180">
            <template #default="{ row }"><el-tag v-for="a in row.aliases" :key="a" size="small" effect="plain" style="margin-right:4px">{{ a }}</el-tag></template>
          </el-table-column>
          <el-table-column label="状态" width="100" align="center">
            <template #default="{ row }"><StatusBadge :status="row.status" /></template>
          </el-table-column>
        </el-table>
        <div class="pager-bar">
          <span class="muted">每页</span>
          <el-select v-model="size" size="small" style="width:80px" @change="resetPages">
            <el-option v-for="s in SIZES" :key="s" :label="s" :value="s" />
          </el-select>
          <el-pagination layout="prev, pager, next, total" :total="nodeTotal"
                         :page-size="size" v-model:current-page="nodePage" @current-change="loadNodes" />
        </div>
      </el-tab-pane>

      <el-tab-pane label="关系" name="edges">
        <div class="tools">
          <el-button type="primary" size="small" :icon="MagicStick" :loading="extracting" @click="extract">
            AI 抽取（从已入库文档）
          </el-button>
          <el-radio-group v-model="edgeStatus" size="small" @change="() => { edgePage = 1; loadEdges() }">
            <el-radio-button value="">全部</el-radio-button>
            <el-radio-button value="candidate">候选</el-radio-button>
            <el-radio-button value="confirmed">已确认</el-radio-button>
          </el-radio-group>
          <el-button size="small" type="success" plain :disabled="!edgeSelected.length" @click="batchEdges('confirm')">批量确认（{{ edgeSelected.length }}）</el-button>
          <el-button size="small" :disabled="!edgeSelected.length" @click="batchEdges('reject')">批量拒绝</el-button>
          <el-button size="small" type="warning" plain @click="confirmAll">一键确认全部候选</el-button>
          <span class="muted">抽取产物为「候选」态，确认后才参与关联检索</span>
          <span class="spacer" />
          <el-tag size="small" type="info" effect="plain">共 {{ edgeTotal }} 条</el-tag>
        </div>
        <el-table :data="edges" size="default" @selection-change="(s: any) => (edgeSelected = s)">
          <el-table-column type="selection" width="44" />
          <el-table-column label="三元组" min-width="300">
            <template #default="{ row }">
              <el-tag size="small" effect="plain">{{ row.src }}</el-tag>
              <span class="rel">— {{ row.relation }} →</span>
              <el-tag size="small" effect="plain" type="success">{{ row.dst }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="证据" width="100" align="center">
            <template #default="{ row }"><el-link v-if="row.evidence_chunk_id" type="primary" :underline="false" @click="showEvidence(row.evidence_chunk_id)">证据</el-link><span v-else>—</span></template>
          </el-table-column>
          <el-table-column label="状态" width="110" align="center">
            <template #default="{ row }">
              <template v-if="row.status === 'candidate'">
                <el-button size="small" type="success" plain @click="confirmEdge(row, true)">确认</el-button>
                <el-button size="small" plain @click="confirmEdge(row, false)">拒绝</el-button>
              </template>
              <StatusBadge v-else :status="row.status" />
            </template>
          </el-table-column>
        </el-table>
        <div class="pager-bar">
          <span class="muted">每页</span>
          <el-select v-model="size" size="small" style="width:80px" @change="resetPages">
            <el-option v-for="s in SIZES" :key="s" :label="s" :value="s" />
          </el-select>
          <el-pagination layout="prev, pager, next, total" :total="edgeTotal"
                         :page-size="size" v-model:current-page="edgePage" @current-change="loadEdges" />
        </div>
      </el-tab-pane>

      <el-tab-pane label="概念层级" name="concepts">
        <el-table :data="concepts" size="default" row-key="id">
          <el-table-column prop="name" label="概念" min-width="180" />
          <el-table-column label="同义词" min-width="220">
            <template #default="{ row }"><el-tag v-for="s in row.synonyms" :key="s" size="small" effect="plain" style="margin-right:4px">{{ s }}</el-tag></template>
          </el-table-column>
        </el-table>
      </el-tab-pane>

      <el-tab-pane label="抽取候选" name="candidates">
        <div class="tools">
          <el-radio-group v-model="candStatus" size="small" @change="() => { candPage = 1; loadCandidates() }">
            <el-radio-button value="">全部</el-radio-button>
            <el-radio-button value="pending">待审</el-radio-button>
            <el-radio-button value="confirmed">已入图</el-radio-button>
            <el-radio-button value="rejected">已拒绝</el-radio-button>
          </el-radio-group>
          <span class="muted">Wiki 页面发布后自动抽取入队（M3 变更驱动）；确认后写入图谱候选态，再走关系页最终确认</span>
          <span class="spacer" />
          <el-button size="small" :icon="Refresh" @click="loadCandidates" />
        </div>
        <el-table :data="candidates" size="default">
          <el-table-column label="来源页面" min-width="180">
            <template #default="{ row }"><el-link type="primary" :underline="false" @click="showCandidate(row)">{{ row.doc_title }}</el-link></template>
          </el-table-column>
          <el-table-column label="抽取产物" width="150" align="center">
            <template #default="{ row }">实体 {{ row.entities }} · 关系 {{ row.relations }}</template>
          </el-table-column>
          <el-table-column prop="created_at" label="入队时间" width="160" />
          <el-table-column label="状态" width="200" align="center">
            <template #default="{ row }">
              <template v-if="row.status === 'pending'">
                <el-button size="small" type="success" plain @click="reviewCandidate(row, true)">确认入图</el-button>
                <el-button size="small" plain @click="reviewCandidate(row, false)">拒绝</el-button>
              </template>
              <StatusBadge v-else :status="row.status === 'confirmed' ? 'confirmed' : 'rejected'" />
            </template>
          </el-table-column>
        </el-table>
        <div class="pager-bar">
          <el-pagination layout="prev, pager, next, total" :total="candTotal"
                         :page-size="size" v-model:current-page="candPage" @current-change="loadCandidates" />
        </div>
      </el-tab-pane>

      <el-tab-pane label="体检与洞察" name="health">
        <div class="tools">
          <el-button size="small" :icon="Refresh" @click="loadDiag">重新体检</el-button>
          <span class="muted">系统自动发现：孤立实体 / 悬空证据 / 候选积压 / 未覆盖文档</span>
        </div>
        <el-alert v-for="(i, k) in health.issues || []" :key="k" style="margin-bottom:8px"
                  :type="i.severity === '高' ? 'error' : i.severity === '中' ? 'warning' : 'info'"
                  :closable="false" show-icon :title="i.type + '：' + i.detail" />
        <el-alert v-if="health.healthy" type="success" :closable="false" show-icon title="图谱健康：未发现问题" />
        <div class="sec-title" style="margin-top:18px">枢纽实体（关系最多，知识网络核心）</div>
        <el-tag v-for="h in insights.hubs || []" :key="h.name" style="margin-right:8px" effect="dark">
          {{ h.name }} · {{ h.degree }} 条关系
        </el-tag>
        <div class="sec-title" style="margin-top:16px">知识社区（自动聚类，同社区实体主题相近）</div>
        <div v-for="(c, i) in insights.communities || []" :key="i" class="edge-row">
          社区 {{ i + 1 }}（{{ c.size }} 个）：{{ c.names.join('、') }}
        </div>
        <div v-if="insights.note" class="muted" style="margin-top:10px">{{ insights.note }}</div>
      </el-tab-pane>

      <el-tab-pane label="关联检索预览" name="link">
        <div class="tools">
          <el-input v-model="linkQ" placeholder="输入问题，预览实体链接与多跳扩展（如：报销系统依赖哪些上游）" style="width:420px" />
          <el-button type="primary" @click="doLink">预览</el-button>
        </div>
        <div v-if="linkResult.entities?.length" class="link-box">
          <div class="sec-title">命中实体</div>
          <el-tag v-for="e in linkResult.entities" :key="e.id" style="margin-right:8px">{{ e.name }}</el-tag>
          <div class="sec-title" style="margin-top:16px">扩展子图（{{ linkResult.edges.length }} 条关系边）</div>
          <div v-for="(e, i) in linkResult.edges.slice(0, 10)" :key="i" class="edge-row">
            {{ e.src_name }} —{{ e.relation }}→ {{ e.dst_name }}
          </div>
        </div>
        <div v-else class="wl-empty"><p>输入包含图谱实体的问题后点击预览</p></div>
      </el-tab-pane>
        <el-dialog v-model="nodeFormOpen" title="新增实体（候选态）" width="420px">
      <el-form label-position="top">
        <el-form-item label="实体名称" required><el-input v-model="nodeForm.name" /></el-form-item>
        <el-form-item label="概念类型">
          <el-select v-model="nodeForm.concept_id" style="width:100%">
            <el-option v-for="c in concepts" :key="c.id" :label="c.name" :value="c.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="别名（空格分隔）"><el-input v-model="nodeForm.aliases" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="nodeFormOpen = false">取消</el-button>
        <el-button type="primary" @click="saveNode">保存</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="evidenceOpen" title="关系证据原文" width="560px">
      <div v-if="evidence" class="wl-md">{{ evidence.text }}</div>
      <template #footer>
        <el-button v-if="evidence?.doc_id" @click="() => $router.push(`/kb/${kbId}/doc/${evidence.doc_id}`)">查看所属文档</el-button>
        <el-button type="primary" @click="evidenceOpen = false">关闭</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="entityOpen" :title="entityDetail?.name || '实体详情'" width="560px">
      <el-descriptions :column="1" border size="small" v-if="entityDetail">
        <el-descriptions-item label="概念类型">{{ conceptName(entityDetail.concept_id) }}</el-descriptions-item>
        <el-descriptions-item label="别名">{{ entityDetail.aliases.join('、') || '—' }}</el-descriptions-item>
        <el-descriptions-item label="状态">{{ entityDetail.status === 'confirmed' ? '已确认' : '候选' }}</el-descriptions-item>
      </el-descriptions>
      <div class="sec-title" style="margin-top:14px">关联关系（{{ entityEdges.length }}）</div>
      <div v-for="e in entityEdges" :key="e.id" class="edge-row">
        {{ e.src }} —{{ e.relation }}→ {{ e.dst }}
        <el-link v-if="e.evidence_chunk_id" type="primary" :underline="false" style="margin-left:8px" @click="showEvidence(e.evidence_chunk_id)">证据</el-link>
      </div>
      <template #footer><el-button @click="entityOpen = false">关闭</el-button></template>
    </el-dialog>

    <el-dialog v-model="candOpen" title="抽取候选详情（确认后写入图谱候选态）" width="640px">
      <template v-if="candDetail">
        <div class="sec-title">实体（{{ candDetail.payload.entities?.length || 0 }}）</div>
        <el-tag v-for="(e, i) in (candDetail.payload.entities || [])" :key="'e' + i" style="margin:0 8px 6px 0">
          {{ e.name }}（{{ e.type }}）
        </el-tag>
        <div class="sec-title" style="margin-top:14px">关系（{{ candDetail.payload.relations?.length || 0 }}）</div>
        <div v-for="(r, i) in (candDetail.payload.relations || [])" :key="'r' + i" class="edge-row">
          {{ r.src }} —{{ r.relation }}→ {{ r.dst }}
        </div>
      </template>
      <template #footer>
        <el-button @click="candOpen = false">关闭</el-button>
        <el-button v-if="candDetail?.status === 'pending'" type="primary"
                   @click="reviewCandidate(candDetail, true); candOpen = false">确认入图</el-button>
      </template>
    </el-dialog>
  </el-tabs>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref, watch, nextTick } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import * as echarts from 'echarts'
import 'echarts-gl'
import { Plus, Download, MagicStick, Refresh, Share } from '@element-plus/icons-vue'
import { http } from '../api/http'
import StatusBadge from '../components/StatusBadge.vue'

const tab = ref('graph')
const kbs = ref<any[]>([])
const kbId = ref<number | null>(null)
const nodes = ref<any[]>([])
const nodeTotal = ref(0)
const nodePage = ref(1)
const nodeSelected = ref<any[]>([])
const edges = ref<any[]>([])
const edgeTotal = ref(0)
const edgePage = ref(1)
const edgeSelected = ref<any[]>([])
const size = ref(10)
const SIZES = [10, 15, 20, 30, 50]
const concepts = ref<any[]>([])
const linkQ = ref('')
const linkResult = ref<any>({})
const extracting = ref(false)
const edgeStatus = ref<'candidate' | 'confirmed' | ''>('')
const coverage = ref<any>({})
async function loadCoverage() {
  if (!kbId.value) return
  try { coverage.value = (await (http as any).get(`/kg/${kbId.value}/coverage`)) as any } catch { /* */ }
}
const graphStatus = ref('confirmed')
const health = ref<any>({})
const insights = ref<any>({})
async function loadDiag() {
  if (!kbId.value) return
  try {
    health.value = (await (http as any).get(`/kg/${kbId.value}/health`)) as any
    insights.value = (await (http as any).get(`/kg/${kbId.value}/insights`)) as any
  } catch { /* */ }
}
const graphData = ref<{ nodes: any[]; links: any[]; categories: string[] }>({ nodes: [], links: [], categories: [] })
const graphEl = ref<HTMLElement>()
let chart: echarts.ECharts | null = null

const conceptName = (id: number) => concepts.value.find((c) => c.id === id)?.name || '—'

async function loadNodes() {
  if (!kbId.value) return
  const r = (await (http as any).get(`/kg/${kbId.value}/nodes`, { params: { page: nodePage.value, size: size.value } })) as any
  nodes.value = r.items
  nodeTotal.value = r.total
}
const edgesAll = ref<any[]>([])
async function loadEdgesAll() {
  if (!kbId.value) return
  try {
    const r = (await (http as any).get(`/kg/${kbId.value}/edges`, { params: { page: 1, size: 500 } })) as any
    edgesAll.value = r.items
  } catch { edgesAll.value = [] }
}
async function loadEdges() {
  if (!kbId.value) return
  const r = (await (http as any).get(`/kg/${kbId.value}/edges`, { params: { page: edgePage.value, size: size.value, status: edgeStatus.value || undefined } })) as any
  edges.value = r.items
  edgeTotal.value = r.total
}
async function loadGraph() {
  if (!kbId.value) return
  graphData.value = (await (http as any).get(`/kg/${kbId.value}/graph`, { params: { status: graphStatus.value } })) as any
  nextTick(renderGraph)
}
function renderGraph() {
  if (!graphEl.value || !graphData.value.nodes.length) return
  chart = chart || echarts.init(graphEl.value)
  chart.setOption({
    tooltip: { formatter: (p: any) => p.dataType === 'edge' ? `${p.data.source} —${p.data.relation}→ ${p.data.target}` : p.data.name },
    legend: [{ data: graphData.value.categories, textStyle: { fontSize: 12, color: '#6b7280' }, type: 'scroll' }],
    series: [{
      type: graphData.value.nodes.length > 300 ? 'graphGL' : 'graph',   // 小图用 Canvas（标签清晰），大图自动切 WebGL
      layout: 'force', roam: true, draggable: graphData.value.nodes.length <= 300,
      data: graphData.value.nodes,
      links: graphData.value.links.map((l: any) => ({ ...l, lineStyle: { width: 1.4, color: 'source', curveness: 0.08 } })),
      categories: graphData.value.categories.map((c: string) => ({ name: c })),
      force: { repulsion: 320, edgeLength: [60, 140], gravity: 0.08 },
      lineStyle: { color: 'source', curveness: 0.08 },
      label: { show: true, fontSize: 11, color: '#1f2937' },
      edgeSymbol: ['none', 'arrow'], edgeSymbolSize: 7,
      emphasis: { focus: 'adjacency', lineStyle: { width: 3 } }
    }]
  })
  chart.resize()
}
onMounted(async () => {
  try {
    kbs.value = (await (http as any).get('/kb')) as any[]
    if (kbs.value.length) kbId.value = kbs.value[0].id
  } catch { /* */ }
})
watch(kbId, async () => {
  nodePage.value = 1
  edgePage.value = 1
  candPage.value = 1
  concepts.value = []
  await Promise.all([loadNodes(), loadEdges(), loadGraph(), loadCandidates(), (async () => {
    try { concepts.value = (await (http as any).get(`/kg/${kbId.value}/concepts`)) as any[] } catch { /* */ }
  })()])
})
window.addEventListener('resize', () => chart?.resize())

async function batchEdges(action: string) {
  const ids = edgeSelected.value.map((e) => e.id)
  const r = (await (http as any).post(`/kg/${kbId.value}/edges/batch`, { ids, action })) as any
  ElMessage.success(`已${action === 'confirm' ? '确认' : '拒绝'} ${r.affected} 条关系（两端候选实体已级联确认）`)
  await Promise.all([loadEdges(), loadNodes(), loadGraph()])
}
async function batchNodes(action: string) {
  const ids = nodeSelected.value.map((n) => n.id)
  const r = (await (http as any).post(`/kg/${kbId.value}/nodes/batch`, { ids, action })) as any
  ElMessage.success(`已${action === 'confirm' ? '确认' : '拒绝'} ${r.affected} 个实体`)
  await loadNodes()
}
async function confirmAll() {
  await ElMessageBox.confirm('将确认全部候选关系与实体（关系会级联确认两端实体），确认执行？', '一键确认全部候选', { type: 'warning' })
  const r = (await (http as any).post(`/kg/${kbId.value}/candidates/confirm-all`)) as any
  ElMessage.success(`已确认：关系 ${r.edges_confirmed} 条、实体 ${r.nodes_confirmed} 个`)
  await Promise.all([loadEdges(), loadNodes(), loadGraph()])
}
function resetPages() {
  nodePage.value = 1
  edgePage.value = 1
  loadNodes()
  loadEdges()
}
const nodeFormOpen = ref(false)
const nodeForm = ref({ name: '', concept_id: null as number | null, aliases: '' })
function addNode() {
  nodeForm.value = { name: '', concept_id: concepts.value[0]?.id || null, aliases: '' }
  nodeFormOpen.value = true
}
async function saveNode() {
  if (!nodeForm.value.name.trim()) return ElMessage.warning('请填写实体名称')
  const body = { name: nodeForm.value.name, concept_id: nodeForm.value.concept_id,
                aliases: nodeForm.value.aliases.split(' ').filter(Boolean), status: 'candidate' }
  await (http as any).post(`/kg/${kbId.value}/nodes`, body)
  ElMessage.success('实体已创建（候选态，可在关系页 AI 抽取后一并确认）')
  nodeFormOpen.value = false
  await loadNodes()
}
async function confirmEdge(row: any, accept: boolean) {
  await (http as any).post(`/kg/edges/${row.id}/confirm?accept=${accept}`)
  row.status = accept ? 'confirmed' : 'rejected'
  ElMessage.success(accept ? '已确认（参与关联检索）' : '已拒绝')
  loadGraph()
}
async function doLink() {
  linkResult.value = (await (http as any).get(`/kg/${kbId.value}/link`, { params: { q: linkQ.value } })) as any
}
async function extract() {
  extracting.value = true
  try {
    const r = (await (http as any).post(`/kg/${kbId.value}/extract`, { doc_ids: [], max_chunks: 30 }, { timeout: 120000 })) as any
    if (r.error) ElMessage.error('抽取失败：' + r.error)
    else ElMessage.success(`抽取完成：新增实体 ${r.entities}、候选关系 ${r.relations}（基于 ${r.chunks} 个片段）`)
    tab.value = 'edges'            // 体验改造 4：抽取后直达候选确认
    edgeStatus.value = 'candidate'
    await Promise.all([loadNodes(), loadEdges(), loadGraph(), loadCoverage()])
  } catch (e: any) { ElMessage.error(e.message) } finally { extracting.value = false }
}
const evidenceOpen = ref(false)
const evidence = ref<any>(null)
async function showEvidence(cid: number) {
  evidence.value = (await (http as any).get(`/kg/evidence/${cid}`)) as any
  evidenceOpen.value = true
}
const entityOpen = ref(false)
const entityDetail = ref<any>(null)
const entityEdges = ref<any[]>([])
let edgesLoadedKb: number | null = null   // 边数据按库缓存，换库后重新加载
async function showEntity(row: any) {
  entityDetail.value = row
  if (!edgesAll.value.length || edgesLoadedKb !== kbId.value) {
    await loadEdgesAll()
    edgesLoadedKb = kbId.value
  }
  entityEdges.value = edgesAll.value.filter((e: any) => e.src === row.name || e.dst === row.name)
  entityOpen.value = true
}
function exportKg() {
  if (!kbId.value) return
  window.open(`/api/kg/${kbId.value}/export`, '_blank')
}

/* ---------- M3 变更驱动抽取候选 ---------- */
const candidates = ref<any[]>([])
const candTotal = ref(0)
const candPage = ref(1)
const candStatus = ref<'' | 'pending' | 'confirmed' | 'rejected'>('')
const candOpen = ref(false)
const candDetail = ref<any>(null)
async function loadCandidates() {
  if (!kbId.value) return
  try {
    const r = (await (http as any).get(`/kg/${kbId.value}/extract-candidates`,
      { params: { page: candPage.value, size: size.value, status: candStatus.value || undefined } })) as any
    candidates.value = r.items
    candTotal.value = r.total
  } catch { candidates.value = [] }
}
async function showCandidate(row: any) {
  candDetail.value = (await (http as any).get(`/kg/extract-candidates/${row.id}`)) as any
  candOpen.value = true
}
async function reviewCandidate(row: any, accept: boolean) {
  try {
    if (accept) {
      const r = (await (http as any).post(`/kg/extract-candidates/${row.id}/confirm`)) as any
      ElMessage.success(`已入图：实体 ${r.entities}、关系 ${r.relations}（候选态，请到「关系」页最终确认）`)
      tab.value = 'edges'
      edgeStatus.value = 'candidate'
      await Promise.all([loadNodes(), loadEdges(), loadGraph()])
    } else {
      await (http as any).post(`/kg/extract-candidates/${row.id}/reject`)
      ElMessage.success('已拒绝（不入图，留档审计）')
    }
    await loadCandidates()
  } catch (e: any) { ElMessage.error(e.message) }
}
</script>

<style scoped>
.page-head { display: flex; justify-content: space-between; align-items: flex-end; margin-bottom: 12px; }
.tools { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; flex-wrap: wrap; }
.spacer { flex: 1; }
.muted { font-size: var(--wl-fs-xs); color: var(--wl-text-muted); }
.rel { color: var(--wl-text-muted); margin: 0 8px; font-size: var(--wl-fs-sm); }
.sec-title { font-weight: 600; margin: 8px 0; }
.edge-row { font-size: var(--wl-fs-md); color: var(--wl-text-secondary); padding: 6px 0; border-bottom: 1px dashed var(--wl-border); }
.graph-card { height: 560px; }
.graph { width: 100%; height: 520px; }
.pager-bar { margin-top: 12px; display: flex; justify-content: flex-end; align-items: center; gap: 8px; }
</style>
