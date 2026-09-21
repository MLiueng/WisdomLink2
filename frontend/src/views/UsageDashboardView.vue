<template>
  <div class="wl-page">
    <div class="page-head">
      <div>
        <h1 class="wl-page-title">Token 用量看板</h1>
        <p class="wl-page-desc">按 厂商 × 模型 × 知识库 × 用途 × 日 统计；QA/缓存命中计为节省量</p>
      </div>
      <el-button :icon="Download" @click="exportCsv">导出 CSV</el-button>
    </div>

    <el-row :gutter="16" class="metrics">
      <el-col :xs="12" :lg="6"><MetricCard label="今日消耗" :value="summary.today_tokens" unit="tok" /></el-col>
      <el-col :xs="12" :lg="6"><MetricCard label="本月消耗" :value="summary.month_tokens" unit="tok" /></el-col>
      <el-col :xs="12" :lg="6"><MetricCard label="节省（QA+缓存）" :value="summary.saved_tokens" unit="tok"
        tip="QA 命中与语义缓存命中替代的生成 Token" /></el-col>
      <el-col :xs="12" :lg="6"><MetricCard label="日耗告警" :value="summary.today_tokens >= summary.daily_limit ? '已超限' : '正常'"
        :foot="`上限 ${((summary.daily_limit ?? 0) / 10000)} 万 tok/日`" /></el-col>
    </el-row>

    <el-row :gutter="16" class="metrics">
      <el-col :xs="12" :lg="6"><MetricCard label="本月 Prompt 缓存命中" :value="ops.month_cached_tokens || 0" unit="tok"
        tip="M5：支持前缀缓存的模型命中的输入 Token（厂商口径，计入输入之内）" /></el-col>
      <el-col :xs="12" :lg="6"><MetricCard label="QA 字典累计命中" :value="ops.qa_total_hits || 0" unit="次" /></el-col>
      <el-col :xs="12" :lg="6"><MetricCard label="抽取候选积压" :value="ops.candidate_pending || 0" unit="条"
        tip="M3：Wiki 发布自动抽取的待审候选（确认后才入图）" /></el-col>
      <el-col :xs="12" :lg="6"><MetricCard label="语义缓存命中率"
        :value="((ops.semantic_cache?.hit_rate ?? 0) * 100).toFixed(1)" unit="%"
        :tip="`P-04：${ops.semantic_cache?.hits || 0} / ${ops.semantic_cache?.lookups || 0} 次查询命中（零 Token 应答）`" /></el-col>
      <el-col :xs="12" :lg="6"><MetricCard label="降级回答累计" :value="ops.degraded_messages || 0" unit="条" /></el-col>
    </el-row>

    <div class="wl-card chart-card">
      <div class="chart-head">
        <span class="chart-title">消耗趋势</span>
        <el-radio-group v-model="days" size="small" @change="loadTrend">
          <el-radio-button :value="7">近 7 天</el-radio-button>
          <el-radio-button :value="14">近 14 天</el-radio-button>
          <el-radio-button :value="30">近 30 天</el-radio-button>
        </el-radio-group>
      </div>
      <div ref="chartEl" class="chart" />
    </div>

    <el-row :gutter="16">
      <el-col :xs="24" :lg="12">
        <div class="wl-card rank-card">
          <div class="chart-head"><span class="chart-title">Top 模型（近 30 天）</span></div>
          <div v-for="m in breakdown" :key="m.key" class="rank-row">
            <span class="rank-name">
              <el-tag size="small" effect="plain" class="vendor-tag">{{ m.vendor || '未知厂商' }}</el-tag>
              {{ m.key }}
            </span>
            <div class="rank-bar"><div :style="{ width: pct(m.tokens) + '%' }" /></div>
            <span class="rank-val">{{ fmt(m.tokens) }} tok · {{ m.calls }} 次</span>
          </div>
          <div v-if="!breakdown.length" class="wl-empty"><p>暂无调用数据</p></div>
        </div>
      </el-col>
      <el-col :xs="24" :lg="12">
        <div class="wl-card rank-card">
          <div class="chart-head"><span class="chart-title">明细（聚合行）</span></div>
          <el-table :data="breakdown" size="small">
            <el-table-column label="厂商" width="110">
              <template #default="{ row }"><el-tag size="small" effect="plain">{{ row.vendor || '—' }}</el-tag></template>
            </el-table-column>
            <el-table-column prop="key" label="模型" min-width="140" show-overflow-tooltip />
            <el-table-column label="Token" width="100">
              <template #default="{ row }">{{ fmt(row.tokens) }}</template>
            </el-table-column>
            <el-table-column prop="calls" label="调用" width="80" />
          </el-table>
        </div>
      </el-col>
    </el-row>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import * as echarts from 'echarts'
import { Download } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { http, USE_MOCK, authOpen } from '../api/http'
import MetricCard from '../components/MetricCard.vue'

const summary = ref<any>({})
const ops = ref<any>({})
const trend = ref<any[]>([])
const breakdown = ref<any[]>([])
const days = ref(14)
const chartEl = ref<HTMLElement>()
let chart: echarts.ECharts | null = null

const fmt = (v: number) => (v >= 1e6 ? (v / 1e6).toFixed(1) + 'M' : (v / 1e3).toFixed(1) + 'k')
const maxTokens = () => Math.max(...breakdown.value.map((b) => b.tokens), 1)
const pct = (t: number) => (t / maxTokens()) * 100

async function loadTrend() {
  trend.value = (await (http as any).get('/admin/usage/trend', { params: { days: days.value } })) as any[]
  render()
}
function render() {
  if (!chartEl.value) return
  chart = chart || echarts.init(chartEl.value)
  chart.setOption({
    tooltip: { trigger: 'axis' },
    legend: { data: ['输入 Token', '输出 Token'], textStyle: { fontSize: 12, color: '#6b7280' } },
    grid: { left: 50, right: 30, top: 40, bottom: 28 },
    xAxis: { type: 'category', data: trend.value.map((t) => t.date.slice(5)) },
    yAxis: { type: 'value', axisLabel: { formatter: (v: number) => (v >= 1e6 ? v / 1e6 + 'M' : v / 1e3 + 'k') } },
    series: [
      { name: '输入 Token', type: 'bar', stack: 't', data: trend.value.map((t) => t.prompt), itemStyle: { color: '#2563eb', borderRadius: [3, 0, 0, 3] } },
      { name: '输出 Token', type: 'bar', stack: 't', data: trend.value.map((t) => t.completion), itemStyle: { color: 'rgba(37,99,235,0.45)', borderRadius: [0, 3, 3, 0] } }
    ]
  })
}
onMounted(async () => {
  summary.value = (await (http as any).get('/admin/usage/summary')) as any
  breakdown.value = (await (http as any).get('/admin/usage/breakdown')) as any[]
  try { ops.value = (await (http as any).get('/admin/ops/metrics')) as any } catch { /* 无评测数据时忽略 */ }
  await loadTrend()
  window.addEventListener('resize', () => chart?.resize())
})
async function exportCsv() {
  if (USE_MOCK) return ElMessage.info('预览模式')
  try { await authOpen('/api/admin/usage/export', 'usage.csv') }
  catch (e: any) { ElMessage.error(e.message || '导出失败') }
}
</script>

<style scoped>
.page-head { display: flex; justify-content: space-between; align-items: flex-end; margin-bottom: 16px; }
.metrics { margin-bottom: 16px; }
.chart-card { padding: 18px 20px; margin-bottom: 16px; }
.chart-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
.chart-title { font-weight: 600; font-size: var(--wl-fs-md); }
.chart { height: 300px; }
.rank-card { padding: 18px 20px; }
.rank-row { display: flex; align-items: center; gap: 12px; padding: 8px 0; }
.rank-name { flex: 0 1 220px; min-width: 0; font-size: var(--wl-fs-md); display: flex; align-items: center; gap: 8px; overflow: hidden; white-space: nowrap; }
.vendor-tag { flex-shrink: 0; max-width: 88px; overflow: hidden; text-overflow: ellipsis; }
.rank-bar { flex: 1; height: 10px; background: var(--wl-gray-100); border-radius: 5px; overflow: hidden; }
.rank-bar div { height: 100%; background: linear-gradient(90deg, var(--wl-primary), var(--wl-primary-60)); border-radius: 5px; }
.rank-val { font-size: var(--wl-fs-xs); color: var(--wl-text-muted); flex-shrink: 0; text-align: right; }
</style>
