<template>
  <div class="wl-page narrow">
    <h1 class="wl-page-title">个人中心</h1>
    <p class="wl-page-desc">管理自己的会话与偏好（V2 接入认证后扩展为完整用户中心）</p>

    <div class="wl-card me">
      <el-avatar :size="52">管</el-avatar>
      <div>
        <div class="me-name">管理员</div>
        <div class="me-desc">内置管理员（V1 可信内网边界）</div>
      </div>
      <el-button text type="danger" style="margin-left:auto" @click="$router.push('/login')">退出登录</el-button>
    </div>

    <div class="wl-card prefs">
      <div class="sec-title">偏好</div>
      <div class="pref-row">
        <span>界面密度</span>
        <el-radio-group v-model="density" size="small">
          <el-radio-button value="default">舒适</el-radio-button>
          <el-radio-button value="compact">紧凑</el-radio-button>
        </el-radio-group>
      </div>
    </div>

    <div class="wl-card stats">
      <div class="sec-title">我的使用统计</div>
      <div class="stat-row"><span>本月提问次数</span><b>86</b></div>
      <div class="stat-row"><span>QA 秒回占比</span><b>42%</b></div>
      <div class="stat-row"><span>累计节省 Token</span><b>38.4k</b></div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
const density = ref('default')
</script>

<style scoped>
.narrow { max-width: 720px; }
.me { display: flex; align-items: center; gap: 14px; padding: 20px; margin-bottom: 16px; }
.me-name { font-weight: 600; }
.me-desc { font-size: var(--wl-fs-xs); color: var(--wl-text-muted); }
.prefs, .stats { padding: 18px 20px; margin-bottom: 16px; }
.sec-title { font-weight: 600; margin-bottom: 10px; }
.pref-row, .stat-row { display: flex; justify-content: space-between; align-items: center; padding: 8px 0; font-size: var(--wl-fs-md); color: var(--wl-text-secondary); }
.stat-row b { font-weight: 600; }
</style>
