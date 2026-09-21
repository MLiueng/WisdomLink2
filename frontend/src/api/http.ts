/**
 * HTTP 与 SSE 封装 + Mock 适配（VITE_USE_MOCK=true 时页面可独立预览，无需后端）。
 */
import axios from 'axios'

let abortController: AbortController | null = null
export function cancelStream() {
  if (abortController) { abortController.abort(); abortController = null }
}
export function setAbortController(ac: AbortController | null) { abortController = ac }

export const USE_MOCK = (import.meta as any).env?.VITE_USE_MOCK !== 'false'

export const http = axios.create({ baseURL: '/api', timeout: 30000 })

http.interceptors.request.use((cfg) => {
  const token = localStorage.getItem('wl2_token')
  if (token) cfg.headers.Authorization = `Bearer ${token}`
  return cfg
})
http.interceptors.response.use(
  (r) => r.data,
  (err) => Promise.reject(new Error(err?.response?.data?.detail?.message || err?.response?.data?.detail || err.message))
)

/**
 * 携带鉴权头下载文件并打开（S-02 管理面整体鉴权后，导出/预览不能再用裸 window.open）。
 * 服务端以 Content-Disposition 给出文件名时优先使用。
 */
export async function authOpen(url: string, fallbackName = 'download') {
  if (USE_MOCK) { window.open(url, '_blank'); return }
  const token = localStorage.getItem('wl2_token')
  const resp = await fetch(url, { headers: token ? { Authorization: `Bearer ${token}` } : {} })
  if (!resp.ok) throw new Error(`下载失败 ${resp.status}`)
  const cd = resp.headers.get('Content-Disposition') || ''
  let name = fallbackName
  const star = /filename\*=UTF-8''([^;]+)/.exec(cd)?.[1]
  const plain = /filename="?([^";]+)"?/.exec(cd)?.[1]
  if (star) name = decodeURIComponent(star)
  else if (plain) name = plain
  const blobUrl = URL.createObjectURL(await resp.blob())
  const a = document.createElement('a')
  a.href = blobUrl
  a.download = name
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(blobUrl), 60_000)
}

/** 携带鉴权头请求并返回文本（Wiki 预览等 GET 文本场景）。 */
export async function authFetchText(url: string): Promise<string> {
  if (USE_MOCK) return ''
  const token = localStorage.getItem('wl2_token')
  const resp = await fetch(url, { headers: token ? { Authorization: `Bearer ${token}` } : {} })
  if (!resp.ok) throw new Error(`请求失败 ${resp.status}`)
  return resp.text()
}

/** SSE 流式请求（chat/stream），onEvent(event, data) 逐事件回调。 */
export async function ssePost(url: string, body: any, onEvent: (ev: string, data: any) => void) {
  if (USE_MOCK) return mockChatStream(body, onEvent)
  abortController = new AbortController()
  const resp = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal: abortController.signal
  })
  if (!resp.ok || !resp.body) throw new Error(`SSE 请求失败 ${resp.status}`)
  const reader = resp.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buf += decoder.decode(value, { stream: true })
    let idx
    while ((idx = buf.indexOf('\n\n')) >= 0) {
      const block = buf.slice(0, idx)
      buf = buf.slice(idx + 2)
      const ev = /event: (.+)/.exec(block)?.[1] || ''
      const dataRaw = /data: (.+)/.exec(block)?.[1] || ''
      if (ev) { try { onEvent(ev, JSON.parse(dataRaw)) } catch { onEvent(ev, dataRaw) } }
    }
  }
}

/* ---------------- Mock 数据层（演示预览用） ---------------- */
const delay = (ms: number) => new Promise((r) => setTimeout(r, ms))

const MOCK_KBS = [
  { id: 1, name: '产品部知识库', description: '产品手册、需求文档、发布说明', status: 'active', is_local_only: false, doc_count: 128, qa_count: 46, created_at: '2026-09-01' },
  { id: 2, name: 'HR 制度库', description: '考勤、薪酬、福利制度', status: 'active', is_local_only: true, doc_count: 64, qa_count: 32, created_at: '2026-08-20' },
  { id: 3, name: 'IT 运维知识库', description: '系统操作手册、故障处理 SOP', status: 'active', is_local_only: false, doc_count: 210, qa_count: 88, created_at: '2026-07-11' }
]

const MOCK_DOCS = [
  { id: 1, title: '差旅报销制度', status: 'published', folder_id: 2, version: 'v3.0', sha8: 'a1b2c3d4', source_type: 'file', effective_date: '2026-08-01', created_at: '2026-09-10' },
  { id: 2, title: '员工入职指南', status: 'cleaning', folder_id: 1, version: 'v1.0', sha8: 'e5f6a7b8', source_type: 'file', effective_date: '', created_at: '2026-09-10' },
  { id: 3, title: '系统架构图.png', status: 'failed', folder_id: 3, version: 'v1.0', sha8: 'c9d0e1f2', source_type: 'file', effective_date: '', created_at: '2026-09-09' },
  { id: 4, title: '产品发布流程', status: 'published', folder_id: 1, version: 'v2.0', sha8: '9a8b7c6d', source_type: 'wiki', effective_date: '2026-09-05', created_at: '2026-09-08' }
]

const MOCK_QA = [
  { id: 1, kb_id: 2, question: '公司 WiFi 密码是多少', answer: '办公区 WiFi：**WL2-Office**，密码每月 1 日更新，见 IT 公告。', variants: ['wifi 密码', '无线网密码'], status: 'enabled', enabled: true, weight: 100, hit_count: 320, last_hit_at: '2026-09-11 09:20', review_due: '2026-12-10', stale: false, ref_doc_ids: [] },
  { id: 2, kb_id: 2, question: '年假有几天', answer: '入职满 1 年 5 天，满 5 年 10 天，满 10 年 15 天（详见《考勤制度》第三章）。', variants: ['怎么申请年假', '年假天数'], status: 'enabled', enabled: true, weight: 90, hit_count: 216, last_hit_at: '2026-09-11 08:11', review_due: '2026-06-01', stale: true, ref_doc_ids: [1] }
]

const MOCK_FOLDERS = [
  { id: 1, parent_id: null, name: '制度', path: '/', depth: 0, doc_count: 30 },
  { id: 2, parent_id: 1, name: '财务', path: '/制度/', depth: 1, doc_count: 12 },
  { id: 3, parent_id: 1, name: '考勤', path: '/制度/', depth: 1, doc_count: 8 },
  { id: 4, parent_id: null, name: '手册', path: '/', depth: 0, doc_count: 46 }
]

export async function mockRequest(method: string, url: string, data?: any): Promise<any> {
  await delay(120)
  const get = (p: string) => new URLSearchParams(url.split('?')[1] || '').get(p)
  if (url === '/kb' && method === 'get') return MOCK_KBS
  if (url === '/kb' && method === 'post') return { id: 99 }
  if (url.startsWith('/kb/') && url.includes('/folders')) return MOCK_FOLDERS
  if (url.startsWith('/kb/') && url.includes('/concepts')) return [{ id: 1, name: '系统', parent_id: null, synonyms: [] }]
  if (url.startsWith('/kg/')) return { entities: [], nodes: [], edges: [] }
  if (url.startsWith('/documents') && method === 'get') {
    const fid = get('folder_id')
    return { total: MOCK_DOCS.length, items: fid ? MOCK_DOCS.filter((d) => String(d.folder_id) === fid) : MOCK_DOCS }
  }
  if (url.startsWith('/documents/') && method === 'get') {
    const d = MOCK_DOCS[0]
    return { ...d, kb_id: 1, origin_uri: 'kb/1/x', versions: [{ id: 1, version: 'v3.0', is_current: true, published: true }], chunks: [{ id: 1, seq: 0, role: 'child', page: 1, tokens: 320, hash: 'ab12cd34', text: '报销分为差旅、日常、招待三类……', heading: '第一章 总则' }], clean_logs: [{ rule_type: 'header/footer', position: 'block#0@p1', action: 'remove', before: 'XX公司机密', after: '' }] }
  }
  if (url.startsWith('/qa') && method === 'get') return { total: MOCK_QA.length, items: MOCK_QA }
  if (url.startsWith('/qa/mining')) return [{ question: '报销发票丢失怎么办', count: 12, type: 'refusal' }, { question: '如何申请会议室', count: 9, type: 'rag' }]
  if (url === '/admin/usage/summary') return { today_tokens: 1246800, month_tokens: 14680000, saved_tokens: 384000, daily_limit: 20000000 }
  if (url === '/admin/usage/trend') return Array.from({ length: 14 }, (_, i) => ({ date: `2026-08-2${i + 1}`.slice(0, 10), prompt: 800000 + i * 40000, completion: 300000 + i * 15000 }))
  if (url === '/admin/usage/breakdown') return [{ key: 'deepseek-chat', vendor: 'DeepSeek', tokens: 9200000, calls: 1420 }, { key: 'embedding-3', vendor: '智谱', tokens: 2100000, calls: 980 }]
  if (url === '/admin/overview') return { today_tokens: 1246800, degraded_messages: 0, qa_total_hits: 1120, daily_limit: 20000000 }
  if (url === '/admin/health') return { status: 'up', components: { api: 'up', db: 'up', redis: 'up', vector: 'qdrant' } }
  if (url.startsWith('/admin/audit')) return { total: 2, items: [{ id: 1, actor: 'admin', action: 'document.upload', object_type: 'document', object_id: '1', detail: '{}', created_at: '2026-09-11 09:00' }, { id: 2, actor: 'anonymous', action: 'chat.ask', object_type: 'session', object_id: 's1', detail: '{"type":"qa"}', created_at: '2026-09-11 09:20' }] }
  if (url.startsWith('/admin/clean-rules')) return [{ id: 1, rule_type: 'header', pattern: '', enabled: true, priority: 10 }, { id: 2, rule_type: 'watermark', pattern: '内部资料|机密|仅供参考', enabled: true, priority: 20 }]
  if (url.startsWith('/chat/sessions')) return url.split('/').length > 3 ? [{ id: 1, role: 'user', content: 'WiFi 密码多少' }, { id: 1, role: 'assistant', content: '办公区 WiFi：WL2-Office。', answer_type: 'qa', citations: [] }] : MOCK_KBS.slice(0, 2).map((k, i) => ({ id: `s${i}`, title: 'WiFi 密码 / 报销流程', scope: { kb_ids: [1] }, created_at: '2026-09-11' }))
  return { ok: true }
}

http.interceptors.request.use(async (cfg) => {
  if (USE_MOCK) {
    const data = await mockRequest((cfg.method || 'get').toLowerCase(), cfg.url || '', cfg.data)
    ;(cfg as any).mockData = data
  }
  return cfg
})

// mock 模式下把 adapter 换成直接返回
if (USE_MOCK) {
  http.defaults.adapter = async (cfg: any) => ({
    data: cfg.mockData,
    status: 200,
    statusText: 'OK',
    headers: {},
    config: cfg
  })
}

async function mockChatStream(body: any, onEvent: (ev: string, data: any) => void) {
  onEvent('meta', { session_id: 'mock-session' })
  await delay(300)
  const q = (body.question || '').toLowerCase()
  if (q.includes('wifi') || q.includes('年假')) {
    onEvent('qa_hit', { qa_id: 1, mode: 'exact', score: 0.96, kb_id: 2, question: '公司 WiFi 密码是多少', answer: '办公区 WiFi：**WL2-Office**，密码每月 1 日更新，详见 IT 公告栏。', ref_doc_ids: [], updated_at: '2026-09-01', review_due_at: '2026-12-10' })
  } else {
    onEvent('status', { entities: ['报销系统'], degraded: [] })
    const text = '根据《差旅报销制度》第三章，报销流程分为三步：\n\n1. **提交申请** — 在 OA 系统填写报销单并上传发票照片 [1]\n2. **主管审批** — 直属主管 3 个工作日内审批 [1]\n3. **财务打款** — 每月 15 日、30 日统一打款 [2]\n\n> 注意：单笔超过 5000 元需额外财务总监审批 [2]'
    for (let i = 0; i < text.length; i += 8) {
      onEvent('delta', { text: text.slice(i, i + 8) })
      await delay(28)
    }
  }
  onEvent('citations', [
    { seq: 1, chunk_id: 'c1', doc_version_id: 1, page: 12, heading_path: '差旅报销制度 / 第三章 流程', snippet: '员工应在差旅结束后 10 个工作日内提交报销申请……', score: 0.93, source: 'dense' },
    { seq: 2, chunk_id: 'c2', doc_version_id: 1, page: 13, heading_path: '差旅报销制度 / 第四章 审批', snippet: '单笔超过 5000 元的报销需财务总监审批……', score: 0.88, source: 'bm25' }
  ])
  onEvent('done', { latency_ms: 1420, answer_type: 'rag', degraded: [], token_in: 1234, token_out: 567, tokens_saved: 0 })
}
