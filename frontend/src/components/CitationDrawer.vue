<template>
  <el-drawer v-model="visible" title="来源溯源" size="520px" :append-to-body="true">
    <template #header>
      <div class="trace-head">
        <span class="trace-title">来源溯源</span>
        <span class="trace-sub">引用 → 知识片段 → 版本与位置 → 原始文件</span>
      </div>
    </template>

    <el-alert v-if="tampered" type="error" :closable="false" show-icon
              title="内容可能已变更（指纹校验未通过），该片段禁止作为依据展示" />

    <div class="trace-list" v-if="citations.length">
      <div v-for="c in citations" :key="c.seq" class="trace-item wl-card is-hover" @click="select(c)">
        <div class="trace-item-head">
          <span class="trace-seq">{{ c.seq }}</span>
          <span class="trace-heading">{{ c.heading_path || '未命名章节' }}</span>
          <el-tag size="small" :type="scoreType(c.score)" effect="light">{{ (c.score * 100 || 0).toFixed(0) }}% 相关</el-tag>
        </div>
        <div class="trace-snippet">{{ c.snippet }}…</div>
        <div class="trace-meta">
          <span v-if="c.page && c.source !== 'web'">第 {{ c.page }} 页</span>
          <el-tag v-if="c.source === 'kg'" size="small" type="warning" effect="plain">图谱关联</el-tag>
          <el-tag v-else-if="c.source === 'web'" size="small" type="success" effect="plain">联网来源</el-tag>
          <el-tag v-else-if="c.source === 'bm25'" size="small" effect="plain">关键词命中</el-tag>
        </div>
      </div>
    </div>
    <div v-else class="wl-empty">
      <el-icon><Document /></el-icon>
      <p>本回答没有外部引用<br /><small>QA 标准答案或闲聊回答不携带检索引用</small></p>
    </div>

    <!-- 二级：片段详情（清洗对照 + 版本 + 原件入口） -->
    <el-dialog v-model="detailOpen" :title="`引用 [${current?.seq}] · 溯源链`" width="640px" append-to-body>
      <template v-if="current">
        <!-- F-04：联网来源没有库内溯源链——不做指纹校验（web: id 必 404 会误报"内容已变更"），
             展示来源信息与原文链接即可 -->
        <template v-if="current.source === 'web'">
          <el-alert type="success" :closable="false" show-icon style="margin-bottom:12px"
                    title="联网检索来源：内容来自公开互联网，请跳转原网页核实" />
          <el-descriptions :column="1" border size="small">
            <el-descriptions-item label="来源标题">{{ current.heading_path || '—' }}</el-descriptions-item>
            <el-descriptions-item label="摘要">{{ current.snippet }}…</el-descriptions-item>
            <el-descriptions-item label="原文链接">
              <a v-if="current.url" :href="current.url" target="_blank" rel="noopener">{{ current.url }}</a>
              <span v-else>—</span>
            </el-descriptions-item>
          </el-descriptions>
          <div class="trace-actions">
            <el-button type="primary" :disabled="!current.url" @click="openWebSource">跳转原网页</el-button>
          </div>
        </template>

        <template v-else>
        <el-steps :active="step" align-center finish-status="success" class="trace-steps">
          <el-step title="知识片段" />
          <el-step title="版本与位置" />
          <el-step title="原始文件" />
        </el-steps>

        <div v-show="step === 0">
          <div class="diff-box">
            <div class="diff-label">清洗前</div>
            <div class="diff-body del">XX公司机密　XX公司机密<br />{{ current.snippet }}</div>
            <div class="diff-label">清洗后</div>
            <div class="diff-body add">{{ current.snippet }}…</div>
          </div>
          <div class="trace-actions">
            <el-button type="primary" @click="step = 1">下一步：版本与位置</el-button>
          </div>
        </div>

        <div v-show="step === 1">
          <el-descriptions :column="1" border size="small">
            <el-descriptions-item label="文档标题">{{ docTitle || '差旅报销制度' }}</el-descriptions-item>
            <el-descriptions-item label="章节路径">{{ current.heading_path || '—' }}</el-descriptions-item>
            <el-descriptions-item label="页码">第 {{ current.page || 1 }} 页</el-descriptions-item>
            <el-descriptions-item label="版本">v3.0（当前版）</el-descriptions-item>
            <el-descriptions-item label="指纹校验">
              <el-tag type="success" size="small">通过</el-tag>
            </el-descriptions-item>
          </el-descriptions>
          <div class="trace-actions">
            <el-button @click="step = 0">上一步</el-button>
            <el-button type="primary" @click="openPreview">查看原始文件</el-button>
          </div>
        </div>

        <div v-show="step === 2">
          <div class="preview-box">
            <div v-if="previewLoading" class="wl-empty">
              <el-icon class="is-loading"><Loading /></el-icon>
              <p>正在加载原件…</p>
            </div>
            <iframe v-else-if="previewKind === 'pdf'" :src="previewUrl" class="preview-frame" frameborder="0" />
            <div v-else-if="previewKind === 'md'" class="preview-md wl-md" v-html="renderMd(previewText)"></div>
            <pre v-else-if="previewKind === 'text'" class="preview-text">{{ previewText }}</pre>
            <div v-else-if="previewKind === 'image'" class="preview-image"><img :src="previewUrl" alt="原件图片" /></div>
            <div v-else-if="USE_MOCK" class="paper">
              <div class="paper-head">
                <span class="paper-title">{{ docTitle || (current.heading_path || '').split(' / ')[0] || '知识文档' }}</span>
                <span class="paper-meta">模拟原件 · 第 {{ current.page || 1 }} 页 · {{ current.heading_path }}</span>
              </div>
              <div class="paper-body">
                <p>……（前文省略）本章节描述相关制度的标准与执行要求。</p>
                <p><mark>{{ current.snippet }}</mark>……具体执行细节以原文为准。</p>
                <p>本制度自发布之日起生效，由归口部门负责解释。（后文省略）</p>
              </div>
              <div class="paper-note">
                预览模式：以上为<b>模拟原件</b>（演示内容）。启动后端并设置 VITE_USE_MOCK=false 后，此处将展示真实文件（PDF/Markdown/图片直接在线渲染，Office 需配置 WL2_SOFFICE_PATH 转换预览）。
              </div>
            </div>
            <div v-else class="wl-empty">
              <el-icon><WarningFilled /></el-icon>
              <p>该类型暂不支持在线渲染（如 Office 未配置转换服务）<br /><small>配置 WL2_SOFFICE_PATH 后可在线预览，当前提供原件下载</small></p>
              <el-button type="primary" plain @click="download">下载原件</el-button>
            </div>
          </div>
          <div class="trace-actions">
            <el-button @click="step = 1">上一步</el-button>
            <el-button @click="download">下载原件</el-button>
          </div>
        </div>
        </template>
      </template>
    </el-dialog>
  </el-drawer>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Document, WarningFilled, Loading } from '@element-plus/icons-vue'
import { USE_MOCK, http } from '../api/http'
import { renderMarkdown } from '../utils/markdown'

const visible = ref(false)
const detailOpen = ref(false)
const step = ref(0)
const citations = ref<any[]>([])
const current = ref<any>(null)
const docTitle = ref('')
const tampered = ref(false)
const previewUrl = ref('')
const previewKind = ref<'pdf' | 'md' | 'text' | 'image' | 'download' | ''>('')
const previewText = ref('')
const previewLoading = ref(false)
let objectUrl = ''

function revoke() {
  if (objectUrl) { URL.revokeObjectURL(objectUrl); objectUrl = '' }
}

function renderMd(t: string) {
  return renderMarkdown(t)
}

async function openPreview() {
  step.value = 2
  if (USE_MOCK) return   // 模拟原件直接展示
  previewLoading.value = true
  revoke()
  previewKind.value = ''
  try {
    const url = `/api/documents/${current.value?.doc_id || current.value?.doc_version_id || 1}/preview`
    const resp = await fetch(url)
    const ctype = (resp.headers.get('Content-Type') || '').split(';')[0]
    const blob = await resp.blob()
    if (ctype === 'application/pdf') {
      previewKind.value = 'pdf'
      objectUrl = URL.createObjectURL(blob)
      previewUrl.value = objectUrl + `#page=${current.value?.page || 1}`
    } else if (ctype.startsWith('image/')) {
      previewKind.value = 'image'
      objectUrl = URL.createObjectURL(blob)
      previewUrl.value = objectUrl
    } else if (ctype.startsWith('text/') || ctype === 'application/json') {
      // 在线渲染：Markdown 走排版视图，纯文本等宽展示
      previewKind.value = ctype === 'text/markdown' ? 'md' : 'text'
      previewText.value = await blob.text()
    } else {
      previewKind.value = 'download'   // Office 未配置转换服务等：保底下载
    }
  } catch {
    previewKind.value = 'download'
  } finally {
    previewLoading.value = false
  }
}

function open(cites: any[], seq?: number) {
  citations.value = cites || []
  visible.value = true
  const target = seq ? (cites || []).find((x) => x.seq == seq) : null
  if (target) select(target)
}
function select(c: any) {
  current.value = c
  step.value = 0
  detailOpen.value = true
  tampered.value = false
  // F-04：联网来源（chunk_id 形如 web:1）没有库内分片，指纹校验必然 404，
  // 跳过校验改为跳转原网页，避免误报"内容已变更"
  if (c?.source === 'web' || USE_MOCK) return
  http.get(`/documents/trace/verify/${c.chunk_id}`)
    .then((r: any) => { tampered.value = !(r as any).verified })
    .catch(() => { tampered.value = true })   // 校验失败按可疑处理（BR-010）
}
function openWebSource() {
  const url = current.value?.url
  if (url) window.open(url, '_blank', 'noopener')
  else ElMessage.info('该联网来源未提供原文链接')
}
function download() {
  if (USE_MOCK) { ElMessage.info('预览模式：启动后端（VITE_USE_MOCK=false）后可下载真实原件'); return }
  window.open(`/api/documents/${current.value?.doc_id || current.value?.doc_version_id || 1}/preview`, '_blank')
}
function scoreType(score: number): 'success' | 'warning' | 'info' {
  return score >= 0.8 ? 'success' : score >= 0.5 ? 'warning' : 'info'
}
defineExpose({ open })
</script>

<style scoped>
.trace-head { display: flex; flex-direction: column; }
.trace-title { font-weight: 600; font-size: var(--wl-fs-lg); }
.trace-sub { font-size: var(--wl-fs-xs); color: var(--wl-text-muted); margin-top: 2px; }
.trace-list { display: flex; flex-direction: column; gap: 12px; }
.trace-item { padding: 14px 16px; cursor: pointer; }
.trace-item-head { display: flex; align-items: center; gap: 10px; }
.trace-seq {
  width: 22px; height: 22px; border-radius: 6px; flex-shrink: 0;
  background: var(--wl-primary-light); color: var(--wl-primary);
  font-size: var(--wl-fs-xs); font-weight: 600;
  display: inline-flex; align-items: center; justify-content: center;
}
.trace-heading { flex: 1; font-size: var(--wl-fs-md); font-weight: 600; color: var(--wl-text-primary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.trace-snippet { font-size: var(--wl-fs-md); color: var(--wl-text-secondary); line-height: 1.6; margin: 8px 0; }
.trace-meta { display: flex; gap: 8px; font-size: var(--wl-fs-xs); color: var(--wl-text-muted); }
.trace-steps { margin-bottom: 20px; }
.diff-box { border: 1px solid var(--wl-border); border-radius: var(--wl-r-md); overflow: hidden; }
.diff-label { font-size: var(--wl-fs-xs); color: var(--wl-text-muted); padding: 6px 12px; background: var(--wl-gray-50); border-bottom: 1px solid var(--wl-border); }
.diff-body { padding: 12px; font-size: var(--wl-fs-sm); line-height: 1.7; white-space: pre-wrap; font-family: 'JetBrains Mono', Consolas, monospace; }
.diff-body.del { background: #fef2f2; color: #991b1b; }
.diff-body.add { background: #f0fdf4; color: #166534; }
.trace-actions { margin-top: 16px; display: flex; justify-content: flex-end; gap: 8px; }
.preview-box { border: 1px solid var(--wl-border); border-radius: var(--wl-r-md); min-height: 280px; background: #fff; overflow: hidden; }
.preview-frame { width: 100%; height: 460px; border: none; display: block; }
.preview-md { padding: 20px 24px; max-height: 460px; overflow: auto; }
.preview-text { padding: 16px 20px; margin: 0; font-family: 'JetBrains Mono', Consolas, monospace; font-size: 13px; line-height: 1.7; max-height: 460px; overflow: auto; white-space: pre-wrap; color: var(--wl-text-secondary); }
.preview-image { display: flex; justify-content: center; padding: 16px; background: var(--wl-gray-50); }
.preview-image img { max-width: 100%; max-height: 460px; border-radius: var(--wl-r-sm); }
/* 模拟原件（预览模式）：纸张质感视图 */
.paper { background: #fff; }
.paper-head { display: flex; justify-content: space-between; align-items: center; padding: 14px 18px; border-bottom: 1px solid var(--wl-border); background: var(--wl-gray-50); border-radius: var(--wl-r-md) var(--wl-r-md) 0 0; }
.paper-title { font-weight: 600; font-size: var(--wl-fs-md); }
.paper-meta { font-size: var(--wl-fs-xs); color: var(--wl-text-muted); }
.paper-body { padding: 20px 24px; line-height: 1.9; font-size: var(--wl-fs-lg); color: var(--wl-text-secondary); }
.paper-body h4 { margin: 0 0 8px; color: var(--wl-text-primary); }
.paper-body p { margin: 0 0 10px; }
.paper-body mark { background: #fef08a; padding: 1px 3px; border-radius: 3px; color: var(--wl-text-primary); }
.paper-note { padding: 10px 18px; font-size: var(--wl-fs-xs); color: var(--wl-warning); background: #fdf3e7; border-top: 1px dashed var(--wl-border); }
</style>
