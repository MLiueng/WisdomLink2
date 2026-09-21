<template>
  <div class="metric wl-card is-hover">
    <div class="metric-label">
      {{ label }}
      <el-tooltip v-if="tip" :content="tip" placement="top"><el-icon class="metric-tip"><InfoFilled /></el-icon></el-tooltip>
    </div>
    <div class="metric-value">{{ prefix }}{{ display }}<span v-if="unit" class="metric-unit">{{ unit }}</span></div>
    <div class="metric-foot" v-if="foot">{{ foot }}</div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { InfoFilled } from '@element-plus/icons-vue'

const props = defineProps<{ label: string; value: number | string; unit?: string; prefix?: string; tip?: string; foot?: string }>()
const display = computed(() => {
  if (typeof props.value === 'string') return props.value
  const v = props.value
  if (v >= 1_000_000) return (v / 1_000_000).toFixed(1) + 'M'
  if (v >= 1000) return (v / 1000).toFixed(1) + 'k'
  return String(v)
})
</script>

<style scoped>
.metric { padding: 18px 20px; }
.metric-label { font-size: var(--wl-fs-sm); color: var(--wl-text-muted); display: flex; align-items: center; gap: 4px; }
.metric-tip { color: var(--wl-gray-400); font-size: 14px; cursor: help; }
.metric-value { font-size: var(--wl-fs-num); font-weight: 600; margin-top: 8px; color: var(--wl-gray-900); }
.metric-unit { font-size: var(--wl-fs-sm); color: var(--wl-text-muted); margin-left: 4px; font-weight: 400; }
.metric-foot { font-size: var(--wl-fs-xs); color: var(--wl-text-muted); margin-top: 6px; }
</style>
