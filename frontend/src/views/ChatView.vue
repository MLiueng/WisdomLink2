<template>
  <div class="chat-page">
    <!-- 左：会话列表 -->
    <aside class="chat-sessions">
      <el-button type="primary" class="new-chat" @click="newChat">
        <el-icon><Plus /></el-icon>&nbsp;新对话
      </el-button>
      <div class="session-filter">
        <el-input v-model="kw" placeholder="搜索会话" :prefix-icon="Search" clearable size="small" />
      </div>
      <div class="session-list">
        <div v-for="s in filteredSessions" :key="s.id" class="session-item"
             :class="{ active: s.id === sessionStore.sessionId }" @click="loadSession(s)">
          <el-icon class="s-ico"><ChatDotRound /></el-icon>
          <span class="s-title">{{ s.title }}</span>
          <el-icon class="s-pin" :class="{ active: s.pinned }" @click.stop="pinSession(s)"><Star /></el-icon>
          <el-icon class="s-del" @click.stop="delSession(s)"><Delete /></el-icon>
        </div>
        <div v-if="!filteredSessions.length" class="wl-empty"><p>暂无历史会话</p></div>
        <div v-if="sessHasMore && !kw" class="load-more" @click="loadMoreSessions">
          加载更多（{{ sessTotal - sessions.length }}）
        </div>
      </div>
    </aside>

    <!-- 中：对话区 -->
    <section class="chat-main">
      <header class="chat-toolbar">
        <RangeSelector v-model:kb-ids="scopeKbs" v-model:folder-ids="scopeFolders" />
        <span class="degraded-tag" v-if="degraded.length">
          <el-icon><WarningFilled /></el-icon> 降级模式：{{ degraded.join('、') }} 不可用
        </span>
      </header>

      <div class="chat-flow" ref="flowRef" @scroll="onFlowScroll">
        <div v-if="hasMoreMsgs" class="load-older">
          <el-button size="small" text type="primary" :loading="loadingOlder" @click="loadOlder">
            加载更早的消息
          </el-button>
        </div>
        <div v-if="!messages.length" class="chat-hero">
          <div class="hero-logo">W</div>
          <h2>有什么可以帮你？</h2>
          <p>基于企业知识库回答，每个结论都标注出处，可下钻验证</p>
          <div class="hero-samples">
            <div v-for="s in samples" :key="s" class="sample wl-card is-hover" @click="ask(s)">{{ s }}</div>
          </div>
        </div>

        <template v-for="(m, i) in messages" :key="i">
          <div v-if="m.role === 'user'" class="msg-user">
            <div class="bubble">{{ m.content }}</div>
          </div>

          <div v-else class="msg-ai">
            <!-- QA 标准答案卡 -->
            <div v-if="m.answer_type === 'qa'" class="qa-card">
              <div class="qa-head">
                <span class="qa-badge">标准答案（QA）</span>
                <span class="qa-meta">维护于 {{ m.qa?.updated_at || '—' }}</span>
                <el-tag v-if="m.qa?.stale" size="small" type="warning" effect="light">待复核</el-tag>
              </div>
              <div class="wl-md" v-html="render(m.content)"></div>
              <div class="qa-foot">
                <el-button size="small" text type="primary" @click="deepAnswer(m)">
                  <el-icon><MagicStick /></el-icon>&nbsp;查看 AI 深度回答
                </el-button>
                <el-button size="small" text @click="ElMessage.success('感谢反馈')">有用 👍</el-button>
              </div>
            </div>

            <!-- RAG 回答 -->
            <div v-else class="rag-card wl-card" :class="{ typing: m.typing }" @click="onBodyClick($event, m)">
              <div class="rag-head" v-if="m.answer_type === 'refusal'">
                <el-icon color="var(--wl-text-muted)"><InfoFilled /></el-icon>
              </div>
              <div v-if="m.typing && m.stage" class="stage-line">{{ m.stage }}…</div>
              <div class="wl-md" :class="{ 'wl-cursor': m.typing }" v-html="renderBody(m)"></div>
              <div v-if="m.answer_type === 'refusal' && !m.typing" class="deposit-box">
                <span>没找到想要的？把正确答案沉淀下来，下次直接命中：</span>
                <el-button size="small" type="primary" plain @click.stop="openDeposit(m)">补充为 QA 对</el-button>
                <el-button size="small" plain @click.stop="goWiki(m)">去 Wiki 撰写</el-button>
              </div>
              <div class="rag-meta" v-if="!m.typing && (m.answer_type === 'rag' || m.answer_type === 'cached')">
                <span>引用 {{ (m.citations || []).length }} 条</span>
                <el-divider direction="vertical" />
                <span>token {{ m.token_in || 0 }} / {{ m.token_out || 0 }}</span>
                <el-divider direction="vertical" />
                <span>{{ ((m.latency_ms || 0) / 1000).toFixed(1) }}s</span>
                <el-tag v-if="m.cached" size="small" effect="plain" style="margin-left:8px">缓存命中</el-tag>
                <el-tag v-if="m.web_search" size="small" type="success" effect="light" style="margin-left:8px">联网检索</el-tag>
              </div>
              <div class="rag-actions" v-if="!m.typing">
                <el-button size="small" text @click="ElMessage.success('已复制')">复制</el-button>
                <el-button size="small" text @click="regenerate(m)">重新生成</el-button>
                <el-button size="small" text :disabled="!m.id" :type="m.feedback === 'up' ? 'success' : ''" @click="fb(m, 'up')">👍</el-button>
                <el-button size="small" text :disabled="!m.id" :type="m.feedback === 'down' ? 'danger' : ''" @click="fb(m, 'down')">👎</el-button>
              </div>
              <div v-if="m.feedback === 'down'" class="deposit-box">
                <span>抱歉！把正确答案沉淀下来，下次就能答对：</span>
                <el-button size="small" type="primary" plain @click.stop="openDeposit(m)">补充为 QA 对</el-button>
              </div>
            </div>
            <!-- P-07：建议问题（done 后异步到达，点击直接追问） -->
            <div v-if="m.suggestions?.length" class="sug-row">
              <span class="sug-label">你可以继续问：</span>
              <el-button v-for="(s, si) in m.suggestions" :key="si" size="small" round plain
                         @click="ask(s)">{{ s }}</el-button>
            </div>
          </div>
        </template>
      </div>

      <footer class="chat-input">
        <el-input v-model="input" type="textarea" :rows="1" resize="none"
                  :autosize="{ minRows: 1, maxRows: 6 }"
                  :placeholder="`向 ${sessionStore.scopeName} 提问…（Enter 发送，Shift+Enter 换行）`"
                  @keydown.enter="onEnter" :disabled="busy" />
        <el-button type="primary" :icon="Promotion" :loading="busy" @click="ask()" :disabled="!input.trim()" circle />
      </footer>
    </section>

    <!-- 右：来源面板 -->
    <aside class="chat-sources">
      <div class="src-head">本回答引用</div>
      <div v-if="kgChain.length" class="src-head" style="margin-top:4px">图谱关系链（本次回答由图谱扩展贡献）</div>
      <div v-if="kgChain.length" class="src-entities" style="margin-bottom:10px">
        <el-tag v-for="(c, i) in kgChain" :key="i" size="small" type="warning" effect="plain">{{ c }}</el-tag>
      </div>
      <div v-if="currentCitations.length" class="src-head" style="margin-top:2px">{{ srcTitle }}</div>
      <div v-if="currentCitations.length" class="src-list">
        <div v-for="c in currentCitations" :key="c.seq" class="src-item wl-card is-hover" @click="openCitation(c)">
          <div class="src-row"><span class="src-seq">{{ c.seq }}</span>
            <span class="src-name">{{ c.heading_path || '知识片段' }}</span>
            <el-tag v-if="c.source === 'kg'" size="small" type="warning" effect="light">图谱关联</el-tag>
            <el-tag v-else-if="c.source === 'web'" size="small" type="success" effect="light">联网来源</el-tag></div>
          <div class="src-score"><div class="bar" :style="{ width: (c.score * 100) + '%' }" /></div>
          <div class="src-meta"><template v-if="c.source !== 'web'">第 {{ c.page || 1 }} 页 · </template>相关度 {{ (c.score * 100).toFixed(0) }}%</div>
        </div>
      </div>
      <div v-else class="wl-empty"><el-icon><Collection /></el-icon><p>回答中的引用将展示在这里<br /><small>点击引用角标查看四级溯源</small></p></div>
      <div class="src-head" style="margin-top:20px" v-if="entities.length">相关实体</div>
      <div class="src-entities" v-if="entities.length">
        <el-tag v-for="e in entities" :key="e" size="small" effect="plain" class="ent-tag" @click="ask(e + ' 相关信息')">{{ e }}</el-tag>
      </div>
    </aside>

    <CitationDrawer ref="trace" />

    <el-dialog v-model="depositOpen" title="沉淀为 QA 对（下次同类问题秒回）" width="480px">
      <el-form label-position="top">
        <el-form-item label="标准问题" required><el-input v-model="depositForm.question" /></el-form-item>
        <el-form-item label="标准答案（支持 Markdown）" required>
          <el-input v-model="depositForm.answer" type="textarea" :rows="5" placeholder="把正确答案写在这里" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="depositOpen = false">取消</el-button>
        <el-button type="primary" :loading="depositSaving" @click="saveDeposit">保存并生效</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, ref } from 'vue'
import { Plus, Search, Delete, ChatDotRound, WarningFilled, InfoFilled, MagicStick, Promotion, Collection, Star } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { http, ssePost } from '../api/http'
import { useRouter } from 'vue-router'
import { onUnmounted } from 'vue'
import { useSessionStore } from '../stores/session'
import { cancelStream } from '../api/http'
import CitationDrawer from '../components/CitationDrawer.vue'
import RangeSelector from '../components/RangeSelector.vue'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import { renderMarkdown } from '../utils/markdown'

const sessionStore = useSessionStore()
const router = useRouter()
const messages = ref<any[]>([])
const input = ref('')
const busy = ref(false)
const kw = ref('')
const degraded = ref<string[]>([])
const entities = ref<string[]>([])
const kgChain = ref<string[]>([])
const srcTitle = ref('本回答引用')
const currentCitations = ref<any[]>([])
const sessions = ref<any[]>([])
const sessPage = ref(1)
const sessTotal = ref(0)
const sessHasMore = ref(false)
const scopeKbs = ref<number[]>([])
const scopeFolders = ref<number[]>([])
const trace = ref<any>({})
const depositOpen = ref(false)
const depositSaving = ref(false)
const depositForm = ref({ question: '', answer: '' })
const flowRef = ref<HTMLElement>()
const samples = ['报销标准是多少？', 'WiFi 密码是多少', '系统 A 依赖哪些上游服务？']

const filteredSessions = computed(() => sessions.value.filter((s) => !kw.value || s.title.includes(kw.value)))

const PAGE_SIZE = 20

/** 会话列表分页加载：reset=true 首页覆盖，否则追加 */
async function loadSessions(reset = false) {
  if (reset) sessPage.value = 1
  try {
    const r = (await (http as any).get('/chat/sessions', { params: { page: sessPage.value, page_size: PAGE_SIZE } })) as any
    const items = Array.isArray(r) ? r : (r.items || [])
    sessions.value = reset ? items : [...sessions.value, ...items.filter((n: any) => !sessions.value.some((o) => o.id === n.id))]
    sessHasMore.value = Array.isArray(r) ? false : !!r.has_more
    sessTotal.value = Array.isArray(r) ? items.length : (r.total || items.length)
  } catch { if (reset) sessions.value = [] }
}

async function loadMoreSessions() {
  sessPage.value++
  await loadSessions(false)
}

async function pinSession(s: any) {
  try {
    await (http as any).post(`/chat/sessions/${s.id}/pin`)
    s.pinned = !s.pinned
    sessions.value.sort((a: any, b: any) => (b.pinned ? 1 : 0) - (a.pinned ? 1 : 0))
  } catch (e: any) { ElMessage.error(`置顶失败：${e.message}`) }
}
const render = (t: string) => renderMarkdown(t)

// 正文引用角标：把 [n] 替换为可点击角标（点击打开溯源抽屉并定位）
function renderBody(m: any) {
  let html = marked.parse(m.content || '', { async: false }) as string
  const seqs = new Set((m.citations || []).map((c: any) => Number(c.seq)))
  if (seqs.size > 0) {
    // 按 seq 集合精确匹配（引用对齐后编号可能跳号，如 [2]、[4]，不能用"条数"做上界）；
    // LLM 偶发的逗号列表写法 [1, 2] 展开为多个独立角标，与后端 _cite_numbers 同口径
    html = html.replace(/\[(\d{1,2}(?:\s*,\s*\d{1,2})*)\]/g, (raw, group: string) => {
      const parts = group.split(/\s*,\s*/).map((d) => {
        const i = Number(d)
        return seqs.has(i) ? `<span class="wl-cite" data-seq="${i}">${i}</span>` : String(i)
      })
      // 整组没有一个可转换编号时保留原文（防幻觉角标）
      return parts.some((s) => s.includes('wl-cite')) ? parts.join(', ') : raw
    })
  }
  // 消毒放最后：对含角标（data-seq）的最终 HTML 整体过滤；DOMPurify 默认保留 data-* 属性
  return DOMPurify.sanitize(html)
}

function onBodyClick(e: MouseEvent, m: any) {
  const el = e.target as HTMLElement
  const seq = Number(el?.dataset?.seq)
  if (!seq || !(m.citations || []).length) return
  currentCitations.value = m.citations
  srcTitle.value = `该回答的引用（${(m.q || '').slice(0, 18)}）`
  trace.value.open?.(m.citations, seq)
}

/** F-04：来源面板点击——联网来源直接跳转原网页，库内引用打开溯源抽屉 */
function openCitation(c: any) {
  if (c?.source === 'web') {
    if (c.url) window.open(c.url, '_blank', 'noopener')
    else ElMessage.info('该联网来源未提供原文链接')
    return
  }
  trace.value.open?.(currentCitations.value, c?.seq)
}

const hasMoreMsgs = ref(false)
const oldestMsgId = ref<number | null>(null)
const loadingOlder = ref(false)

async function loadSession(s: any) {
  sessionStore.setSession(s.id)
  try {
    const r = (await (http as any).get(`/chat/sessions/${s.id}/messages`, { params: { limit: PAGE_SIZE } })) as any
    const msgs = Array.isArray(r) ? r : (r.items || [])
    messages.value = msgs
    hasMoreMsgs.value = Array.isArray(r) ? false : !!r.has_more
    oldestMsgId.value = Array.isArray(r) ? null : (r.oldest_id ?? null)
    const lastAi = [...msgs].reverse().find((x) => x.role === 'assistant' && (x.citations || []).length)
    currentCitations.value = lastAi?.citations || []
    srcTitle.value = '本回答引用'
    scrollBottom()
  } catch { messages.value = [] }
}

/** 渐进加载更早的历史消息：插入头部并补偿滚动位置，视口不跳动 */
async function loadOlder() {
  if (!hasMoreMsgs.value || loadingOlder.value || !sessionStore.sessionId) return
  loadingOlder.value = true
  const el = flowRef.value
  const prevHeight = el?.scrollHeight || 0
  try {
    const r = (await (http as any).get(`/chat/sessions/${sessionStore.sessionId}/messages`,
      { params: { limit: PAGE_SIZE, before_id: oldestMsgId.value } })) as any
    const msgs = Array.isArray(r) ? [] : (r.items || [])
    if (msgs.length) messages.value = [...msgs, ...messages.value]
    hasMoreMsgs.value = !!r.has_more
    if (r.oldest_id) oldestMsgId.value = r.oldest_id
    nextTick(() => { if (el) el.scrollTop += el.scrollHeight - prevHeight })
  } catch { /* 静默：下次滚动可重试 */ }
  finally { loadingOlder.value = false }
}

/** 滚动接近顶部时自动渐进加载更早历史 */
function onFlowScroll() {
  const el = flowRef.value
  if (el && el.scrollTop < 60 && hasMoreMsgs.value && !loadingOlder.value) loadOlder()
}

function scrollBottom() { nextTick(() => flowRef.value?.scrollTo({ top: flowRef.value.scrollHeight, behavior: 'smooth' })) }

function newChat() {
  messages.value = []
  currentCitations.value = []
  kgChain.value = []
  sessionStore.setSession('')
}

onUnmounted(() => { cancelStream() })
onMounted(async () => {
  try {
    sessionStore.kbs = (await (http as any).get('/kb')) as any[]
    await loadSessions(true)
  } catch { /* mock/离线 */ }
  sessionStore.setScope(scopeKbs.value, scopeFolders.value)
  // F-01：消费工作台带过来的提问（确定性传参，替代无人监听的 wl2:ask 事件）
  const pending = sessionStore.takePendingAsk()
  if (pending) ask(pending)
})

function delSession(s: any) {
  http.delete(`/chat/sessions/${s.id}`)
    .then(() => { sessions.value = sessions.value.filter((x) => x.id !== s.id) })
    .catch((e: any) => ElMessage.error(`删除失败：${e.message}`))   // 401 时 http 层已引导登录，这里兜底提示
}

function onEnter(e: KeyboardEvent) {
  if (e.shiftKey) return
  e.preventDefault()
  ask()
}

// 公共 SSE 流式处理：写目标 aiMsg（ask 新建卡片 / regenerate 原地复用）
function runStream(q: string, aiMsg: any) {
  return ssePost('/api/chat/stream', {
    question: q, session_id: sessionStore.sessionId || undefined,
    kb_ids: scopeKbs.value, folder_ids: scopeFolders.value
  }, (ev: string, data: any) => {
    if (ev === 'meta') sessionStore.setSession(data.session_id)
    else if (ev === 'qa_hit') { Object.assign(aiMsg, { answer_type: 'qa', content: data.answer, qa: data, typing: false }) }
    else if (ev === 'cache_hit') { Object.assign(aiMsg, { answer_type: 'cached', content: data.answer, citations: data.citations || [], cached: true, typing: false }); currentCitations.value = data.citations || []; srcTitle.value = '本回答引用' }
    else if (ev === 'status') { degraded.value = data.degraded || []; entities.value = data.entities || []; aiMsg.stage = data.stage || '' }
    else if (ev === 'delta') { aiMsg.content += data.text }
    else if (ev === 'suggestions') { aiMsg.suggestions = (data || []).slice(0, 3) }   // P-07：建议问题异步后补
    else if (ev === 'citations') {
      aiMsg.citations = data; currentCitations.value = data
      srcTitle.value = '本回答引用'
      if ((data || []).some((c: any) => c.source === 'kg')) {
        http.get(`/kg/${(scopeKbs.value[0]) || 1}/link`, { params: { q } }).then((r: any) => {
          kgChain.value = (r.edges || []).slice(0, 6).map((e: any) => `${e.src_name} —${e.relation}→ ${e.dst_name}`)
        }).catch(() => {})
      } else { kgChain.value = [] }
    }
    else if (ev === 'done') {
      aiMsg.typing = false
      aiMsg.latency_ms = data.latency_ms
      aiMsg.token_in = data.token_in
      aiMsg.token_out = data.token_out
      aiMsg.rerank = data.rerank
      aiMsg.reranked = data.reranked
      aiMsg.web_search = !!data.web_search   // F-03：联网检索标记（合规可辨识）
      aiMsg.cached = !!data.cached   // P-04：语义缓存命中标记（"缓存命中"徽标）
      if (data.message_id) aiMsg.id = data.message_id   // 回填服务端消息 id，点赞/点踩依赖
      if (!aiMsg.answer_type) aiMsg.answer_type = data.answer_type
      if (data.answer_type === 'qa' && !aiMsg.qa) { aiMsg.answer_type = 'qa' }
    }
    scrollBottom()
  })
}

async function ask(preset?: string) {
  const q = (preset ?? input.value).trim()
  if (!q || busy.value) return
  input.value = ''
  busy.value = true
  degraded.value = []
  messages.value.push({ role: 'user', content: q })
  const aiMsg: any = { role: 'assistant', content: '', typing: true, answer_type: '', citations: [], q }
  messages.value.push(aiMsg)
  scrollBottom()

  try {
    await runStream(q, aiMsg)
  } catch (e: any) {
    aiMsg.content = aiMsg.content || `请求失败：${e.message}`
    degraded.value = ['服务']
  } finally {
    aiMsg.typing = false
    busy.value = false
    await loadSessions(true)
  }
}

function openDeposit(m: any) {
  depositForm.value = { question: m.q || '', answer: '' }
  depositOpen.value = true
}
async function saveDeposit() {
  if (!depositForm.value.answer.trim()) return ElMessage.warning('请填写标准答案')
  depositSaving.value = true
  try {
    const kb = scopeKbs.value[0] || (sessionStore.kbs[0] && sessionStore.kbs[0].id) || 1
    await (http as any).post('/qa', { kb_id: kb, std_question: depositForm.value.question,
      std_answer: depositForm.value.answer, variants: [], ref_doc_ids: [], status: 'enabled' })
    ElMessage.success('已沉淀生效：下次同类问题将直接秒回')
    depositOpen.value = false
  } catch (e: any) {
    ElMessage.error(String(e.message || e).includes('409') || String(e).includes('重复') ? '该问题已存在 QA 对' : e.message)
  } finally { depositSaving.value = false }
}
function goWiki(m: any) {
  sessionStorage.setItem('wl2_wiki_prefill', JSON.stringify({ title: m.q || '', body: '' }))
  router.push('/wiki')
}
async function fb(m: any, value: string) {
  m.feedback = value
  await (http as any).post('/chat/feedback', { message_id: m.id, value })
  if (value === 'down') ElMessage.info('差评已记录：可补充正确答案帮系统改进')
}
function deepAnswer(m: any) {
  ask(m.qa?.question || m.content.slice(0, 30))
}
async function regenerate(m: any) {
  if (busy.value) return
  const idx = messages.value.indexOf(m)
  const user = [...messages.value.slice(0, idx)].reverse().find((x) => x.role === 'user')
  if (!user || !user.content) return
  // 备份旧回答，失败时恢复（借鉴 Java 版验收第 6 条）
  const backup = { content: m.content, citations: m.citations, answer_type: m.answer_type,
                   token_in: m.token_in, token_out: m.token_out, qa: m.qa, q: m.q,
                   latency_ms: m.latency_ms, rerank: m.rerank, reranked: m.reranked }
  busy.value = true
  degraded.value = []
  // 原地重置卡片为生成中状态（不重复追加用户消息、不残留空卡片）
  Object.assign(m, { content: '', typing: true, answer_type: '', citations: [], stage: '',
                     q: user.content, qa: undefined, feedback: undefined,
                     latency_ms: undefined, token_in: undefined, token_out: undefined,
                     rerank: undefined, reranked: undefined, cached: false })
  scrollBottom()
  try {
    await runStream(user.content, m)
  } catch (e: any) {
    // 失败恢复旧回答
    Object.assign(m, backup, { typing: false })
    ElMessage.error(`重新生成失败：${e.message}`)
  } finally {
    m.typing = false
    busy.value = false
  }
}
</script>

<style scoped>
.chat-page {
  display: grid; grid-template-columns: 264px 1fr 320px;
  /* 关键：约束行高为视口高度，否则隐式行随内容撑开导致整页滚动 */
  grid-template-rows: minmax(0, 1fr);
  height: 100%; min-height: 0; overflow: hidden;
  background: var(--wl-bg);
}
/* 左栏 */
.chat-sessions { display: flex; flex-direction: column; min-height: 0; overflow: hidden; background: var(--wl-card); border-right: 1px solid var(--wl-border); padding: 16px 12px; }
.new-chat { width: 100%; border-radius: var(--wl-r-sm); }
.session-filter { margin: 12px 0 8px; }
.session-list { flex: 1; overflow-y: auto; }
.session-item {
  display: flex; align-items: center; gap: 8px; padding: 9px 10px; margin-bottom: 2px;
  border-radius: var(--wl-r-sm); cursor: pointer; color: var(--wl-text-secondary);
  transition: background var(--wl-t-fast);
}
.session-item:hover { background: var(--wl-gray-100); }
.session-item.active { background: var(--wl-primary-light); color: var(--wl-primary); font-weight: 600; }
.s-ico { flex-shrink: 0; }
.s-title { flex: 1; font-size: var(--wl-fs-md); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.s-del, .s-pin { opacity: 0; color: var(--wl-text-muted); font-size: 16px; box-sizing: content-box; padding: 2px; cursor: pointer; border-radius: var(--wl-r-sm); }
.s-del:hover { color: var(--el-color-danger); }
.s-pin.active { opacity: 1; color: var(--wl-warning); }
.session-item:hover .s-del { opacity: 1; }
.load-more {
  text-align: center; padding: 8px 0; font-size: var(--wl-fs-xs); color: var(--wl-primary);
  cursor: pointer; border-radius: var(--wl-r-sm);
}
.load-more:hover { background: var(--wl-gray-100); }
/* 中栏 */
.chat-main { display: flex; flex-direction: column; min-width: 0; min-height: 0; }
.chat-toolbar {
  display: flex; align-items: center; gap: 12px; padding: 10px 24px; flex-shrink: 0; flex-wrap: wrap;
  background: var(--wl-card); border-bottom: 1px solid var(--wl-border);
}
.degraded-tag { color: var(--wl-warning); font-size: var(--wl-fs-sm); display: inline-flex; align-items: center; gap: 4px; }
.chat-flow { flex: 1; overflow-y: auto; padding: 24px 48px; }
.chat-hero { text-align: center; padding-top: 10vh; }
.load-older { text-align: center; margin-bottom: 12px; }
.hero-logo {
  width: 56px; height: 56px; margin: 0 auto 16px; border-radius: 14px;
  background: linear-gradient(135deg, var(--wl-primary), #7c3aed);
  color: #fff; font-size: 26px; font-weight: 700;
  display: flex; align-items: center; justify-content: center;
  box-shadow: var(--wl-shadow-md);
}
.chat-hero h2 { font-size: 22px; margin: 0 0 6px; }
.chat-hero p { color: var(--wl-text-muted); font-size: var(--wl-fs-sm); margin: 0 0 28px; }
.hero-samples { display: flex; gap: 12px; justify-content: center; flex-wrap: wrap; }
.sample { padding: 12px 20px; cursor: pointer; font-size: var(--wl-fs-md); color: var(--wl-text-secondary); }
.msg-user { display: flex; justify-content: flex-end; margin-bottom: 20px; }
.msg-user .bubble {
  max-width: 70%; padding: 10px 16px; border-radius: 12px 12px 2px 12px;
  background: var(--wl-primary); color: #fff; font-size: var(--wl-fs-md); line-height: 1.6;
}
.msg-ai { margin-bottom: 24px; max-width: 860px; }
/* QA 卡 */
.qa-card { border-left: 3px solid var(--wl-success); background: var(--wl-card); border-radius: var(--wl-r-md); padding: 14px 18px; box-shadow: var(--wl-shadow-sm); }
.qa-head { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
.qa-badge { font-size: var(--wl-fs-xs); font-weight: 600; color: var(--wl-success); background: #e9f9ef; padding: 3px 10px; border-radius: var(--wl-r-full); }
.qa-meta { font-size: var(--wl-fs-xs); color: var(--wl-text-muted); }
.qa-foot { margin-top: 8px; }
.deposit-box { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; font-size: var(--wl-fs-xs); color: var(--wl-text-muted); background: var(--wl-gray-50); border-radius: var(--wl-r-sm); padding: 8px 12px; margin-top: 10px; }
/* RAG 卡 */
.rag-card { padding: 16px 20px; }
.stage-line { font-size: var(--wl-fs-xs); color: var(--wl-text-muted); margin-bottom: 6px; }
.rag-meta { display: flex; align-items: center; font-size: var(--wl-fs-xs); color: var(--wl-text-muted); margin-top: 12px; }
.rag-actions { margin-top: 6px; display: flex; gap: 4px; }
/* P-07 建议问题行 */
.sug-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-top: 10px; font-size: var(--wl-fs-xs); color: var(--wl-text-muted); }
.sug-label { white-space: nowrap; }
/* 输入区 */
.chat-input {
  display: flex; align-items: flex-end; gap: 12px; padding: 14px 48px 20px; flex-shrink: 0;
  background: linear-gradient(transparent, var(--wl-bg) 30%);
}
/* el-input 默认 width:100% 会独占整行把发送按钮挤出视口；改为弹性占位 */
.chat-input .el-textarea { flex: 1; min-width: 0; }
.chat-input :deep(.el-textarea__inner) {
  border-radius: 12px; padding: 11px 16px; box-shadow: var(--wl-shadow-sm);
  background: var(--wl-card);
}
.chat-input :deep(.el-textarea__inner:focus) { box-shadow: 0 0 0 2px var(--wl-primary-30); }
/* 右栏 */
.chat-sources { background: var(--wl-card); border-left: 1px solid var(--wl-border); padding: 16px; overflow-y: auto; min-height: 0; }
.src-head { font-size: var(--wl-fs-xs); font-weight: 600; color: var(--wl-text-muted); letter-spacing: 1px; margin-bottom: 12px; }
.src-list { display: flex; flex-direction: column; gap: 10px; }
.src-item { padding: 12px; cursor: pointer; }
.src-row { display: flex; align-items: center; gap: 8px; }
.src-seq { width: 18px; height: 18px; border-radius: 4px; background: var(--wl-primary-light); color: var(--wl-primary); font-size: 11px; display: inline-flex; align-items: center; justify-content: center; font-weight: 600; flex-shrink: 0; }
.src-name { font-size: var(--wl-fs-xs); color: var(--wl-text-primary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.src-score { height: 3px; background: var(--wl-gray-100); border-radius: 2px; margin: 8px 0 6px; }
.src-score .bar { height: 100%; background: var(--wl-primary); border-radius: 2px; }
.src-meta { font-size: var(--wl-fs-xs); color: var(--wl-text-muted); }
.src-entities { display: flex; flex-wrap: wrap; gap: 8px; }
.ent-tag { cursor: pointer; }
.src-entities .el-tag { max-width: 100%; }
@media (max-width: 1280px) { .chat-page { grid-template-columns: 220px 1fr; } .chat-sources { display: none; } }
@media (max-width: 900px) { .chat-page { grid-template-columns: 1fr; } .chat-sessions { display: none; } }
</style>
