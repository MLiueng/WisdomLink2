<template>
  <span class="wl-badge" :class="cls" :title="title">
    <span class="dot" :class="{ spin: spinning }" v-if="spinning" />
    <span class="dot" v-else />{{ label }}
  </span>
</template>

<script setup lang="ts">
import { computed } from 'vue'

const props = defineProps<{ status: string }>()

/* §7.4 状态徽章色彩映射（全局唯一真值） */
const MAP: Record<string, { label: string; cls: string; spin?: boolean }> = {
  uploaded: { label: '已上传', cls: 'wl-badge--muted' },
  parsing: { label: '解析中', cls: 'wl-badge--info', spin: true },
  cleaning: { label: '清洗中', cls: 'wl-badge--cleaning', spin: true },
  indexing: { label: '索引中', cls: 'wl-badge--indexing', spin: true },
  published: { label: '已发布', cls: 'wl-badge--success' },
  failed: { label: '失败', cls: 'wl-badge--danger' },
  offline: { label: '已下线', cls: 'wl-badge--muted' },
  deleted: { label: '回收站', cls: 'wl-badge--muted' },
  enabled: { label: '启用', cls: 'wl-badge--success' },
  disabled: { label: '停用', cls: 'wl-badge--muted' },
  draft: { label: '草稿', cls: 'wl-badge--muted' },
  stale: { label: '待复核', cls: 'wl-badge--warning' },
  candidate: { label: '候选', cls: 'wl-badge--info' },
  confirmed: { label: '已确认', cls: 'wl-badge--success' },
  rejected: { label: '已拒绝', cls: 'wl-badge--muted' },
  pending: { label: '待审', cls: 'wl-badge--warning' },
  degraded: { label: '降级模式', cls: 'wl-badge--warning' }
}
const conf = computed(() => MAP[props.status] || { label: props.status, cls: 'wl-badge--muted' })
const cls = computed(() => conf.value.cls)
const label = computed(() => conf.value.label)
const spinning = computed(() => !!conf.value.spin)
const title = computed(() => conf.value.spin ? '处理中，完成后自动刷新' : '')
</script>
