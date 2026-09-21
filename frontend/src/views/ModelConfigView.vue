<template>
  <div class="wl-page">
    <h1 class="wl-page-title">模型与组件配置</h1>
    <p class="wl-page-desc">本地 / 远程双模式切换（配置生效 ≤5 分钟，不重启）；详细参数在 backend/.env 中维护</p>

    <el-row :gutter="16">
      <el-col v-for="p in providers" :key="p.key" :xs="24" :md="12" :lg="8">
        <div class="prov wl-card is-hover">
          <div class="prov-head">
            <span class="prov-name">{{ p.name }}</span>
            <span class="health" :class="'is-' + (p.up ? 'ok' : 'down')"><span class="dot" />{{ p.up ? '健康' : '未配置' }}</span>
          </div>
          <div class="prov-body">
            <div class="prov-row"><span>当前实现</span><el-tag size="small" :type="p.mode === 'remote' ? 'primary' : 'success'" effect="light">{{ p.mode }}</el-tag></div>
            <div class="prov-row"><span>说明</span><span class="prov-desc">{{ p.desc }}</span></div>
          </div>
        </div>
      </el-col>
    </el-row>

    <div class="wl-card" style="padding:18px 20px;margin-bottom:16px">
      <div style="font-weight:600;margin-bottom:12px">运行时检索参数（即时生效，不用重启）</div>
      <el-form label-position="top" style="max-width:400px">
        <el-form-item label="检索 Top-K（1-20）">
          <el-input-number v-model="rc.top_k" :min="1" :max="20" @change="saveRC" />
        </el-form-item>
        <el-form-item label="重排保留数（1-10）">
          <el-input-number v-model="rc.rerank_top_n" :min="1" :max="10" @change="saveRC" />
        </el-form-item>
        <el-form-item label="相关性下限（0-1）">
          <el-input-number v-model="rc.relevance_floor" :min="0" :max="1" :step="0.05" @change="saveRC" />
        </el-form-item>
      </el-form>
    </div>

    <div class="wl-card env-card">
      <div class="chart-head"><span class="chart-title">配置入口（.env）</span></div>
      <el-alert type="info" :closable="false" show-icon title="所有 Provider 经 .env 配置，修改后重启生效；切换向导与维度校验见设计文档 §10.4/10.5" />
      <pre class="env-pre">WL2_LLM_ACTIVE=remote            # remote | local | mock | auto
WL2_LLM_REMOTE_BASE_URL=https://api.xxx/v1
WL2_LLM_REMOTE_API_KEY=sk-***
WL2_EMB_ACTIVE=remote
WL2_VECTOR_DRIVER=qdrant         # qdrant | faiss
WL2_QDRANT_URL=http://localhost:6333</pre>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { http } from '../api/http'

const rc = ref<any>({ top_k: 5, rerank_top_n: 5, relevance_floor: 0.2 })

async function loadRC() {
  try { rc.value = (await (http as any).get('/admin/runtime-config')) as any } catch { /* */ }
}
async function saveRC() {
  try { await (http as any).put('/admin/runtime-config', rc.value); ElMessage.success('已保存（即时生效）') }
  catch (e: any) { ElMessage.error(e.message) }
}

const providers = ref([
  { key: 'llm', name: 'LLM 大模型', mode: 'remote', up: true, desc: 'OpenAI 兼容协议（本地 Ollama / vLLM 均可）' },
  { key: 'embedding', name: 'Embedding 向量化', mode: 'local', up: true, desc: '默认 BGE-M3；mock 哈希向量仅演示' },
  { key: 'rerank', name: 'Rerank 重排', mode: 'none', up: false, desc: '未配置则跳过精排（降级模式）' },
  { key: 'vector', name: '向量库', mode: 'qdrant', up: true, desc: 'Qdrant / FAISS(dev)，chunk+QA 双集合' },
  { key: 'kg', name: '图存储', mode: 'local', up: true, desc: 'MySQL 三表 + NetworkX 内存图' },
  { key: 'preview', name: '预览转换', mode: 'unavailable', up: false, desc: '配置 WL2_SOFFICE_PATH 后可转换 Office' }
])

onMounted(async () => {
  await loadRC()
  try {
    const h = (await (http as any).get('/admin/health')) as any
    const c = h.components || {}
    // LLM：mock/down 视为未配置真实模型
    if (c.llm) {
      providers.value[0].mode = c.llm
      providers.value[0].up = c.llm !== 'mock' && !c.llm.startsWith('down')
    }
    // Embedding：mock/down 视为未配置真实模型
    if (c.embedding) {
      providers.value[1].mode = c.embedding
      providers.value[1].up = c.embedding !== 'mock' && !c.embedding.startsWith('down')
    }
    // Rerank：none/down 视为未启用
    if (c.rerank) {
      providers.value[2].mode = c.rerank
      providers.value[2].up = c.rerank !== 'none' && !c.rerank.startsWith('down')
    }
    if (c.vector) { providers.value[3].mode = c.vector }
    providers.value[3].up = c.vector !== 'down' && !String(c.vector).startsWith('down')
  } catch { /* 预览模式 */ }
})
</script>

<style scoped>
.prov { padding: 18px 20px; margin-bottom: 16px; }
.prov-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
.prov-name { font-weight: 600; }
.health { display: inline-flex; align-items: center; gap: 6px; font-size: var(--wl-fs-xs); }
.health .dot { width: 8px; height: 8px; border-radius: 50%; }
.health.is-ok .dot { background: var(--wl-success); }
.health.is-down .dot { background: var(--wl-gray-300); }
.prov-row { display: flex; align-items: center; gap: 10px; padding: 5px 0; font-size: var(--wl-fs-md); color: var(--wl-text-secondary); }
.prov-desc { flex: 1; text-align: right; font-size: var(--wl-fs-xs); color: var(--wl-text-muted); }
.env-card { padding: 18px 20px; }
.chart-head { margin-bottom: 10px; }
.chart-title { font-weight: 600; }
.env-pre { background: var(--wl-gray-900); color: #d1fae5; padding: 16px; border-radius: var(--wl-r-md); font-size: var(--wl-fs-sm); overflow: auto; }
</style>
