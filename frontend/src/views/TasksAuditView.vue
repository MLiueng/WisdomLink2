<template>
  <div class="wl-page">
    <h1 class="wl-page-title">任务中心与审计</h1>
    <p class="wl-page-desc">失败任务处置 + 全量操作留痕（只增，保留 ≥180 天）</p>

    <el-tabs v-model="tab">
      <el-tab-pane label="任务中心" name="tasks">
        <el-alert v-if="failed.length" type="warning" :closable="false" show-icon
                  :title="`有 ${failed.length} 条失败/降级回答需要关注`" style="margin-bottom:12px" />
        <el-table :data="messages" size="default">
          <el-table-column prop="id" label="#" width="70" />
          <el-table-column prop="question" label="触发问题" min-width="220" show-overflow-tooltip />
          <el-table-column label="类型" width="90" align="center">
            <template #default="{ row }">
              <el-tag size="small" :type="typeTag(row.answer_type)" effect="light">{{ row.answer_type }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="降级" width="80" align="center">
            <template #default="{ row }"><el-tag v-if="row.degraded" size="small" type="warning">是</el-tag><span v-else>—</span></template>
          </el-table-column>
          <el-table-column prop="latency_ms" label="耗时" width="90">
            <template #default="{ row }">{{ row.latency_ms }}ms</template>
          </el-table-column>
          <el-table-column prop="created_at" label="时间" width="170" />
        </el-table>
      </el-tab-pane>

      <el-tab-pane label="审计日志" name="audit">
        <div class="tools">
          <el-input v-model="action" placeholder="按动作筛选（如 document/qa/chat）" clearable style="width:260px" @change="loadAudit" />
          <span class="spacer" />
          <el-button :icon="Refresh" circle @click="loadAudit" />
        </div>
        <el-table :data="audits" v-loading="loading" size="default">
          <el-table-column prop="created_at" label="时间" width="170" />
          <el-table-column prop="actor" label="操作者" width="110" />
          <el-table-column prop="action" label="动作" width="170">
            <template #default="{ row }"><el-tag size="small" effect="plain">{{ row.action }}</el-tag></template>
          </el-table-column>
          <el-table-column label="对象" min-width="140">
            <template #default="{ row }">{{ row.object_type }} #{{ row.object_id }}</template>
          </el-table-column>
          <el-table-column prop="detail" label="详情" min-width="200" show-overflow-tooltip />
        </el-table>
        <div style="margin-left:auto;display:flex;align-items:center;gap:6px"><span style="font-size:12px;color:var(--wl-text-muted)">每页</span><el-select v-model="size" size="small" style="width:76px" @change="loadAudit()"><el-option v-for="s in SIZES" :key="s" :label="s" :value="s" /></el-select></div><el-pagination class="pager" layout="prev, pager, next, total" :total="total"
                      :page-size="size" v-model:current-page="page" @current-change="loadAudit" />
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import { http } from '../api/http'

const tab = ref('tasks')
const messages = ref<any[]>([])
const audits = ref<any[]>([])
const loading = ref(false)
const action = ref('')
const failed = computed(() => messages.value.filter((m) => m.degraded))
const typeTag = (t: string) => (t === 'qa' ? 'success' : t === 'refusal' ? 'info' : 'primary')

async function loadTasks() {
  try { messages.value = (await (http as any).get('/chat/sessions/mock-session/messages')) as any[] } catch { /* */ }
}
const page = ref(1)
const size = ref(10)
const SIZES = [10, 15, 20, 30, 50]
const total = ref(0)
async function loadAudit() {
  loading.value = true
  try { const r = (await (http as any).get('/admin/audit', { params: { action: action.value || undefined, page: page.value, size: size.value } })) as any; audits.value = r.items; total.value = r.total } finally { loading.value = false }
}
onMounted(() => { loadTasks(); loadAudit() })
</script>

<style scoped>
.tools { display: flex; gap: 10px; margin-bottom: 12px; }
.pager { margin-top: 12px; justify-content: flex-end; display: flex; }
.spacer { flex: 1; }
</style>
