<template>
  <div class="wl-page">
    <section class="hero">
      <h1>早上好，今天想了解什么？</h1>
      <div class="hero-search">
        <el-input v-model="q" size="large" placeholder="搜索知识、提问、查找文档…（Enter 直接提问）"
                  :prefix-icon="Search" @keydown.enter="goAsk" clearable />
      </div>
    </section>

    <section class="quick-cards">
      <div class="quick wl-card is-hover" @click="$router.push('/chat')">
        <el-icon class="q-ico" color="var(--wl-primary)"><ChatDotRound /></el-icon>
        <div><div class="q-title">智能问答</div><div class="q-desc">QA 秒回 · 引用溯源</div></div>
      </div>
      <div class="quick wl-card is-hover" @click="$router.push('/kb')">
        <el-icon class="q-ico" color="var(--wl-cyan)"><FolderOpened /></el-icon>
        <div><div class="q-title">知识库</div><div class="q-desc">{{ kbs.length }} 个知识库在运营</div></div>
      </div>
      <div class="quick wl-card is-hover" @click="$router.push('/wiki')">
        <el-icon class="q-ico" color="var(--wl-purple)"><Notebook /></el-icon>
        <div><div class="q-title">Wiki 知识</div><div class="q-desc">结构化经验沉淀</div></div>
      </div>
      <div class="quick wl-card is-hover" @click="$router.push('/graph')">
        <el-icon class="q-ico" color="var(--wl-warning)"><Share /></el-icon>
        <div><div class="q-title">知识图谱</div><div class="q-desc">多跳关联查找</div></div>
      </div>
    </section>

    <section class="cols">
      <div class="col">
        <div class="sec-title">最近会话</div>
        <div v-if="sessions.length" class="wl-card list">
          <div v-for="s in sessions" :key="s.id" class="row" @click="$router.push('/chat')">
            <el-icon color="var(--wl-text-muted)"><ChatDotRound /></el-icon>
            <span class="row-main">{{ s.title }}</span>
            <span class="row-side">{{ (s.created_at || '').slice(0, 10) }}</span>
          </div>
        </div>
        <div v-else class="wl-card"><div class="wl-empty"><el-icon><ChatDotRound /></el-icon><p>还没有会话<br /><small>去「智能问答」发起第一次提问</small></p></div></div>
      </div>
      <div class="col">
        <div class="sec-title">热门问题</div>
        <div class="wl-card list">
          <div v-for="(q, i) in hot" :key="i" class="row" @click="goAsk(q)">
            <span class="rank" :class="{ top: i < 3 }">{{ i + 1 }}</span>
            <span class="row-main">{{ q }}</span>
            <el-tag v-if="i < 2" size="small" type="success" effect="light">QA</el-tag>
          </div>
        </div>
      </div>
      <div class="col" v-if="overview">
        <div class="sec-title">系统状态（管理员）</div>
        <div class="wl-card status-card">
          <div class="st-row"><span>今日 Token</span><b>{{ fmt(overview.today_tokens) }}</b></div>
          <div class="st-row"><span>QA 累计命中</span><b>{{ fmt(overview.qa_total_hits) }}</b></div>
          <div class="st-row"><span>降级回答</span><b :style="{ color: overview.degraded_messages ? 'var(--wl-warning)' : 'var(--wl-success)' }">{{ overview.degraded_messages }}</b></div>
        </div>
      </div>
    </section>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ChatDotRound, FolderOpened, Notebook, Share, Search } from '@element-plus/icons-vue'
import { http } from '../api/http'
import { useSessionStore } from '../stores/session'

const router = useRouter()
const store = useSessionStore()
const q = ref('')
const kbs = ref<any[]>([])
const sessions = ref<any[]>([])
const overview = ref<any>(null)
const hot = ['公司 WiFi 密码是多少', '年假有几天', '差旅报销流程', '如何申请会议室', '打印机故障怎么办']

const fmt = (v: number) => (v >= 1e6 ? (v / 1e6).toFixed(1) + 'M' : v >= 1e3 ? (v / 1e3).toFixed(1) + 'k' : v)

onMounted(async () => {
  try {
    kbs.value = (await (http as any).get('/kb')) as any[]
    sessions.value = (await (http as any).get('/chat/sessions')) as any[]
    overview.value = await (http as any).get('/admin/overview')
  } catch { /* 预览模式 */ }
  store.kbs = kbs.value
})

function goAsk(preset?: string) {
  // F-01：经 store 确定性传参（原 wl2:ask 事件无监听者，问题文本丢失）
  store.pendingAsk = preset || q.value
  router.push('/chat')
}
</script>

<style scoped>
.hero { text-align: center; padding: 40px 0 8px; }
.hero h1 { font-size: 26px; margin: 0 0 20px; }
.hero-search { max-width: 640px; margin: 0 auto; }
.hero-search :deep(.el-input__wrapper) { border-radius: 12px; padding: 6px 16px; box-shadow: var(--wl-shadow-md); }
.quick-cards { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin: 32px 0; }
.quick { display: flex; align-items: center; gap: 14px; padding: 18px 20px; cursor: pointer; }
.q-ico { font-size: 28px; }
.q-title { font-weight: 600; }
.q-desc { font-size: var(--wl-fs-xs); color: var(--wl-text-muted); margin-top: 2px; }
.cols { display: grid; grid-template-columns: 1fr 1fr 0.8fr; gap: 16px; }
.sec-title { font-size: var(--wl-fs-md); font-weight: 600; margin-bottom: 10px; }
.list { padding: 6px 0; }
.row { display: flex; align-items: center; gap: 10px; padding: 10px 16px; cursor: pointer; transition: background var(--wl-t-fast); }
.row:hover { background: var(--wl-gray-50); }
.row-main { flex: 1; font-size: var(--wl-fs-md); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.row-side { font-size: var(--wl-fs-xs); color: var(--wl-text-muted); }
.rank { width: 20px; height: 20px; border-radius: 4px; background: var(--wl-gray-100); color: var(--wl-text-muted); font-size: 11px; display: inline-flex; align-items: center; justify-content: center; flex-shrink: 0; }
.rank.top { background: var(--wl-primary-light); color: var(--wl-primary); font-weight: 700; }
.status-card { padding: 6px 0; }
.st-row { display: flex; justify-content: space-between; padding: 11px 16px; font-size: var(--wl-fs-md); color: var(--wl-text-secondary); }
.st-row b { font-weight: 600; }
@media (max-width: 1100px) { .quick-cards { grid-template-columns: repeat(2, 1fr); } .cols { grid-template-columns: 1fr; } }
</style>
