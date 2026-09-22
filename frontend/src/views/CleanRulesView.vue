<template>
  <div class="wl-page">
    <div class="page-head">
      <div>
        <h1 class="wl-page-title">清洗规则库</h1>
        <p class="wl-page-desc">解析后、分片前的去噪规则；只允许删噪/规范化（BR-009），全程留痕可还原</p>
      </div>
      <el-button type="primary" :icon="Plus" @click="add">新增规则</el-button>
    </div>

    <el-table :data="rules" v-loading="loading">
      <el-table-column prop="priority" label="优先级" width="80" sortable />
      <el-table-column label="类型" width="120">
        <template #default="{ row }"><el-tag size="small" effect="plain">{{ typeLabel(row.rule_type) }}</el-tag></template>
      </el-table-column>
      <el-table-column prop="pattern" label="匹配模式" min-width="220">
        <template #default="{ row }"><code v-if="row.pattern">{{ row.pattern }}</code><span v-else class="muted">（自动识别）</span></template>
      </el-table-column>
      <el-table-column label="启用" width="90" align="center">
        <template #default="{ row }"><el-switch v-model="row.enabled" size="small" @change="patch(row)" /></template>
      </el-table-column>
      <el-table-column label="" width="80" align="right">
        <template #default="{ row }"><el-button text type="danger" size="small" @click="del(row)">删除</el-button></template>
      </el-table-column>
    </el-table>

    <el-alert class="hint" type="warning" :closable="false" show-icon
              title="规则变更后建议对受影响文档执行「按原文重建索引」（文档详情页），重跑期间新旧片段不混检" />

    <el-dialog v-model="open" title="新增清洗规则" width="460px">
      <el-form label-position="top">
        <el-form-item label="类型">
          <el-select v-model="form.rule_type" style="width:100%">
            <el-option v-for="t in TYPES" :key="t.v" :label="t.l" :value="t.v" />
          </el-select>
        </el-form-item>
        <el-form-item label="匹配模式（正则；页眉页脚/去重/规范化可留空=自动识别）">
          <el-input v-model="form.pattern" placeholder="如：内部资料|机密|仅供参考" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="open = false">取消</el-button>
        <el-button type="primary" @click="save">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Plus } from '@element-plus/icons-vue'
import { http } from '../api/http'

// 库上下文：从路由 query 取（KbDetailView「库设置」入口传入），缺省 1
const route = useRoute()
const kbId = Number(route.query.kb_id) || 1

const TYPES = [
  { v: 'header', l: '页眉' }, { v: 'footer', l: '页脚' }, { v: 'watermark', l: '水印' },
  { v: 'dedup', l: '重复内容' }, { v: 'normalize', l: '格式规范化' }, { v: 'ocr_noise', l: 'OCR 噪声' },
  { v: 'desensitize', l: '敏感信息脱敏' }, { v: 'custom', l: '自定义正则' }
]
const typeLabel = (t: string) => TYPES.find((x) => x.v === t)?.l || t

const rules = ref<any[]>([])
const loading = ref(false)
const open = ref(false)
const form = ref<any>({ kb_id: kbId, rule_type: 'watermark', pattern: '', enabled: true, priority: 50 })

async function load() {
  loading.value = true
  try { rules.value = (await (http as any).get('/admin/clean-rules', { params: { kb_id: kbId } })) as any[] } finally { loading.value = false }
}
onMounted(load)

function add() { form.value = { kb_id: kbId, rule_type: 'watermark', pattern: '', enabled: true, priority: 50 }; open.value = true }
async function save() {
  await (http as any).post('/admin/clean-rules', form.value)
  ElMessage.success('已保存（审计已记录）')
  open.value = false
  load()
}
async function patch(row: any) {
  await (http as any).patch(`/admin/clean-rules/${row.id}`, { ...row, kb_id: kbId })
  ElMessage.success('已更新')
}
async function del(row: any) {
  await (http as any).delete(`/admin/clean-rules/${row.id}`)
  load()
}
</script>

<style scoped>
.page-head { display: flex; justify-content: space-between; align-items: flex-end; margin-bottom: 16px; }
.hint { margin-top: 16px; }
.muted { color: var(--wl-text-muted); font-size: var(--wl-fs-xs); }
code { background: var(--wl-gray-100); padding: 2px 8px; border-radius: 4px; font-size: var(--wl-fs-sm); }
</style>
