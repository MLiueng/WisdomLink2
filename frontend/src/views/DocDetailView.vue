<template>
  <div class="wl-page" v-if="doc">
    <div class="page-head">
      <div class="crumb">
        <el-button text :icon="ArrowLeft" @click="$router.push(`/kb/${kbId}`)" />
        <h1 class="wl-page-title">{{ doc.title }}</h1>
        <StatusBadge :status="doc.status" />
        <el-tag size="small" effect="plain">{{ doc.version }}</el-tag>
      </div>
      <div>
        <el-button :icon="Refresh" @click="rebuild">按原文重建索引</el-button>
        <el-button type="primary" :icon="View" @click="previewOriginal">查看原始文件</el-button>
      </div>
    </div>

    <el-tabs v-model="tab" class="doc-tabs">
      <el-tab-pane label="概览" name="overview">
        <div class="ov-grid">
          <div class="wl-card ov-card">
            <div class="sec-title">元数据</div>
            <el-descriptions :column="1" border size="small">
              <el-descriptions-item label="知识库">#{{ doc.kb_id }}</el-descriptions-item>
              <el-descriptions-item label="文件夹">#{{ doc.folder_id || '根目录' }}</el-descriptions-item>
              <el-descriptions-item label="生效日期">{{ doc.effective_date || '—' }}</el-descriptions-item>
              <el-descriptions-item label="内容指纹"><code>{{ doc.sha8 }}</code>（SHA-256 前 8 位）</el-descriptions-item>
              <el-descriptions-item label="来源">{{ doc.source_type === 'wiki' ? 'Wiki 页面' : '文件上传' }}</el-descriptions-item>
            </el-descriptions>
          </div>
          <div class="wl-card ov-card">
            <div class="sec-title">状态时间线</div>
            <el-timeline class="ov-timeline">
              <el-timeline-item v-for="s in timeline" :key="s.k" :type="doc.status === s.k ? 'primary' : undefined"
                                :hollow="doc.status !== s.k" :timestamp="s.label">
                {{ s.label }}<el-tag v-if="doc.status === s.k" size="small" style="margin-left:8px">当前</el-tag>
              </el-timeline-item>
            </el-timeline>
          </div>
        </div>
      </el-tab-pane>

      <el-tab-pane label="清洗对照" name="clean">
        <el-alert v-if="!doc.clean_logs?.length" type="success" :closable="false" show-icon title="本文档没有清洗动作记录（原文即索引内容）" />
        <div v-for="(l, i) in pagedCleanLogs" :key="i" class="diff-item">
          <div class="diff-head">
            <el-tag size="small" :type="l.action === 'remove' ? 'danger' : 'warning'" effect="light">{{ l.rule_type }}</el-tag>
            <span class="diff-pos">{{ l.position }}</span>
            <span class="diff-action">{{ l.action }}</span>
          </div>
          <div class="diff-body del" v-if="l.before"><span class="diff-label">清洗前</span>{{ l.before }}</div>
          <div class="diff-body add" v-if="l.after"><span class="diff-label">清洗后</span>{{ l.after }}</div>
        </div>
        <el-alert v-if="(doc.clean_log_total || 0) > doc.clean_logs?.length" type="warning" :closable="false" show-icon
                  :title="`仅展示前 ${doc.clean_logs.length} 条（共 ${doc.clean_log_total} 条）`" style="margin-bottom:12px" />
        <el-pagination v-if="(doc.clean_logs?.length || 0) > pageSize" layout="prev, pager, next" :total="doc.clean_logs.length"
                       :page-size="pageSize" v-model:current-page="cleanPage" class="pager" />
      </el-tab-pane>

      <el-tab-pane :label="`分片视图（${doc.chunk_total ?? doc.chunks?.length ?? 0}）`" name="chunks">
        <el-alert v-if="(doc.chunk_total || 0) > doc.chunks?.length" type="warning" :closable="false" show-icon
                  :title="`仅展示前 ${doc.chunks.length} 个分片（共 ${doc.chunk_total} 个）`" style="margin-bottom:12px" />
        <div v-for="c in pagedChunks" :key="c.id" class="chunk-item wl-card">
          <div class="chunk-head">
            <el-tag size="small" :type="c.role === 'parent' ? 'warning' : 'info'" effect="light">{{ c.role === 'parent' ? '父块' : '子块' }}</el-tag>
            <span class="chunk-seq">#{{ c.seq }}</span>
            <span v-if="c.page" class="chunk-meta">第 {{ c.page }} 页</span>
            <span class="chunk-meta">{{ c.tokens }} tokens</span>
            <span class="chunk-hash"><el-icon><CircleCheck /></el-icon>{{ c.hash }}</span>
          </div>
          <div class="chunk-text">{{ c.text }}</div>
        </div>
        <el-pagination v-if="(doc.chunks?.length || 0) > pageSize" layout="prev, pager, next" :total="doc.chunks.length"
                       :page-size="pageSize" v-model:current-page="chunkPage" class="pager" />
      </el-tab-pane>

      <el-tab-pane label="版本历史" name="versions">
        <el-table :data="doc.versions" size="default">
          <el-table-column prop="version" label="版本" width="100" />
          <el-table-column label="状态" width="120">
            <template #default="{ row }">
              <el-tag v-if="row.is_current" type="success" size="small">当前版</el-tag>
              <el-tag v-else type="info" size="small" effect="plain">历史</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="索引" width="120">
            <template #default="{ row }"><span>{{ row.published ? '已发布' : '未发布' }}</span></template>
          </el-table-column>
        </el-table>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'
import { ArrowLeft, Refresh, View, CircleCheck } from '@element-plus/icons-vue'
import { http, USE_MOCK } from '../api/http'
import StatusBadge from '../components/StatusBadge.vue'

const route = useRoute()
const kbId = Number(route.params.kbId)
const docId = Number(route.params.docId)
const doc = ref<any>(null)
const tab = ref('overview')
const chunkPage = ref(1)
const cleanPage = ref(1)
const pageSize = 20
const pagedChunks = computed(() => {
  const list = doc.value?.chunks || []
  return list.slice((chunkPage.value - 1) * pageSize, chunkPage.value * pageSize)
})
const pagedCleanLogs = computed(() => {
  const list = doc.value?.clean_logs || []
  return list.slice((cleanPage.value - 1) * pageSize, cleanPage.value * pageSize)
})
const timeline = [
  { k: 'uploaded', label: '已上传' }, { k: 'parsing', label: '解析中' },
  { k: 'cleaning', label: '清洗中' }, { k: 'indexing', label: '索引中' }, { k: 'published', label: '已发布' }
]

onMounted(async () => {
  try { doc.value = (await (http as any).get(`/documents/${docId}`)) as any } catch { /* */ }
})

async function rebuild() {
  await (http as any).post(`/documents/${docId}/rebuild`)
  ElMessage.success('已提交重建（异步执行，状态将刷新）')
}
function previewOriginal() {
  if (USE_MOCK) return ElMessage.info('预览模式：后端运行后可查看原件')
  window.open(`/api/documents/${docId}/preview`, '_blank')
}
</script>

<style scoped>
.page-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
.crumb { display: flex; align-items: center; gap: 10px; }
.doc-tabs { margin-top: 8px; }
.ov-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.ov-card { padding: 18px 20px; }
.sec-title { font-weight: 600; margin-bottom: 12px; }
.ov-timeline { padding-left: 4px; }
.diff-item { margin-bottom: 14px; border: 1px solid var(--wl-border); border-radius: var(--wl-r-md); overflow: hidden; }
.diff-head { display: flex; align-items: center; gap: 10px; padding: 8px 12px; background: var(--wl-gray-50); border-bottom: 1px solid var(--wl-border); font-size: var(--wl-fs-xs); }
.diff-pos { color: var(--wl-text-muted); }
.diff-action { margin-left: auto; color: var(--wl-text-muted); }
.diff-body { padding: 10px 14px; font-size: var(--wl-fs-md); line-height: 1.7; white-space: pre-wrap; }
.diff-body.del { background: #fef2f2; color: #991b1b; }
.diff-body.add { background: #f0fdf4; color: #166534; }
.diff-label { display: inline-block; font-size: var(--wl-fs-xs); opacity: 0.7; margin-right: 10px; }
.chunk-item { padding: 12px 16px; margin-bottom: 10px; }
.chunk-head { display: flex; align-items: center; gap: 10px; font-size: var(--wl-fs-xs); color: var(--wl-text-muted); }
.chunk-seq { font-weight: 600; color: var(--wl-text-secondary); }
.chunk-hash { margin-left: auto; display: inline-flex; align-items: center; gap: 4px; color: var(--wl-success); font-family: Consolas, monospace; }
.chunk-text { font-size: var(--wl-fs-md); color: var(--wl-text-secondary); line-height: 1.7; margin-top: 8px; }
.pager { margin-top: 16px; justify-content: flex-end; display: flex; }
</style>
