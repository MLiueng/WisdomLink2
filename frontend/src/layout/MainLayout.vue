<template>
  <el-container class="wl-layout">
    <el-aside :width="collapsed ? '64px' : '240px'" class="wl-aside">
      <div class="wl-logo" @click="$router.push('/workbench')">
        <span class="wl-logo-mark">W</span>
        <span v-if="!collapsed" class="wl-logo-text">WisdomLink<small>2</small></span>
      </div>
      <el-menu :default-active="$route.path" router :collapse="collapsed" class="wl-menu">
        <div v-if="!collapsed" class="wl-menu-group">使用</div>
        <el-menu-item index="/workbench"><el-icon><Odometer /></el-icon><span>工作台</span></el-menu-item>
        <el-menu-item index="/chat"><el-icon><ChatDotRound /></el-icon><span>智能问答</span></el-menu-item>
        <el-menu-item index="/wiki"><el-icon><Notebook /></el-icon><span>Wiki 知识</span></el-menu-item>
        <div v-if="!collapsed" class="wl-menu-group">知识运营</div>
        <template v-if="collapsed"><el-menu-item index="/kb"><el-icon><FolderOpened /></el-icon></el-menu-item><el-menu-item index="/qa"><el-icon><QuestionFilled /></el-icon></el-menu-item><el-menu-item index="/graph"><el-icon><Share /></el-icon></el-menu-item><el-menu-item index="/eval"><el-icon><DataAnalysis /></el-icon></el-menu-item></template>
        <template v-else>
          <el-menu-item index="/kb"><el-icon><FolderOpened /></el-icon><span>知识库</span></el-menu-item>
          <el-menu-item index="/qa"><el-icon><QuestionFilled /></el-icon><span>QA 对管理</span></el-menu-item>
          <el-menu-item index="/graph"><el-icon><Share /></el-icon><span>知识图谱</span></el-menu-item>
          <el-menu-item index="/eval"><el-icon><DataAnalysis /></el-icon><span>检索评测</span></el-menu-item>
        </template>
        <div v-if="!collapsed" class="wl-menu-group">系统管理</div>
        <template v-if="collapsed"><el-menu-item index="/models"><el-icon><Setting /></el-icon></el-menu-item><el-menu-item index="/usage"><el-icon><Coin /></el-icon></el-menu-item><el-menu-item index="/clean-rules"><el-icon><Brush /></el-icon></el-menu-item><el-menu-item index="/tasks"><el-icon><List /></el-icon></el-menu-item></template>
        <template v-else>
          <el-menu-item index="/models"><el-icon><Setting /></el-icon><span>模型与组件</span></el-menu-item>
          <el-menu-item index="/usage"><el-icon><Coin /></el-icon><span>Token 用量</span></el-menu-item>
          <el-menu-item index="/clean-rules"><el-icon><Brush /></el-icon><span>清洗规则</span></el-menu-item>
          <el-menu-item index="/tasks"><el-icon><List /></el-icon><span>任务与审计</span></el-menu-item>
        </template>
      </el-menu>
      <div class="wl-aside-foot">
        <el-icon v-if="!collapsed" @click="collapsed = true"><Fold /></el-icon>
        <el-icon v-else @click="collapsed = false"><Expand /></el-icon>
      </div>
    </el-aside>

    <el-container class="wl-body">
      <el-header class="wl-topbar" height="56px">
        <el-breadcrumb separator="/">
          <el-breadcrumb-item :to="{ path: '/workbench' }">首页</el-breadcrumb-item>
          <el-breadcrumb-item v-if="$route.meta.title">{{ $route.meta.title }}</el-breadcrumb-item>
        </el-breadcrumb>
        <div class="wl-topbar-right">
          <el-tooltip :content="healthText" placement="bottom">
            <span class="wl-health" :class="'is-' + healthLevel"><span class="dot" /></span>
          </el-tooltip>
          <el-dropdown>
            <span class="wl-user"><el-avatar :size="28">管</el-avatar><span class="wl-user-name">管理员</span></span>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item @click="$router.push('/profile')">个人中心</el-dropdown-item>
                <el-dropdown-item divided @click="logout">退出</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </el-header>
      <el-main class="wl-main"><router-view /></el-main>
    </el-container>
  </el-container>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { http } from '../api/http'
import { Odometer, ChatDotRound, Notebook, FolderOpened, QuestionFilled, Share, DataAnalysis, Setting, Coin, Brush, List, Fold, Expand } from '@element-plus/icons-vue'

const collapsed = ref(window.innerWidth < 900)
const route = useRoute()
const router = useRouter()
const health = ref<any>({ status: 'up', components: {} })

const healthLevel = computed(() => (health.value?.status === 'up' ? 'ok' : 'warn'))
const healthText = computed(() => {
  const c = health.value?.components || {}
  const text = Object.entries(c).map(([k, v]) => `${k}: ${v}`).join('　')
  return health.value?.status === 'up' ? `组件健康：${text}` : `降级模式：${text}`
})

onMounted(async () => {
  if (route.path === '/login') return
  try { health.value = await (http as any).get('/admin/health') } catch { /* 预览模式忽略 */ }
})

function logout() {
  localStorage.removeItem('wl2_token')
  router.push('/login')
}
</script>

<style scoped>
.wl-layout { height: 100%; }
/* 内层容器（顶栏+主区）：min-height:0 允许 flex 收缩，主区内容再多也不会撑破整页布局 */
.wl-body { min-height: 0; overflow: hidden; }
.wl-aside {
  display: flex; flex-direction: column;
  background: var(--wl-card); border-right: 1px solid var(--wl-border);
  transition: width var(--wl-t-slow); overflow: hidden;
}
.wl-logo {
  display: flex; align-items: center; gap: 10px;
  height: 56px; padding: 0 16px; cursor: pointer; flex-shrink: 0;
}
.wl-logo-mark {
  width: 32px; height: 32px; border-radius: var(--wl-r-md);
  background: linear-gradient(135deg, var(--wl-primary), #7c3aed);
  color: #fff; font-weight: 700; display: flex; align-items: center; justify-content: center;
  font-size: 16px; flex-shrink: 0;
}
.wl-logo-text { font-size: 17px; font-weight: 700; color: var(--wl-gray-900); }
.wl-logo-text small { color: var(--wl-primary); }
.wl-menu { border-right: none; flex: 1; overflow-y: auto; }
.wl-menu-group {
  font-size: var(--wl-fs-xs); color: var(--wl-text-muted);
  padding: 16px 20px 6px; letter-spacing: 1px;
}
.wl-menu :deep(.el-menu-item) {
  height: 40px; margin: 2px 8px; border-radius: var(--wl-r-sm); color: var(--wl-text-secondary);
}
.wl-menu :deep(.el-menu-item:hover) { background: var(--wl-gray-100); }
.wl-menu :deep(.el-menu-item.is-active) {
  background: var(--wl-primary-light); color: var(--wl-primary); font-weight: 600;
}
.wl-aside-foot { padding: 12px 16px; border-top: 1px solid var(--wl-border); color: var(--wl-text-muted); }
.wl-aside-foot .el-icon { cursor: pointer; font-size: 16px; box-sizing: content-box; padding: 4px; border-radius: var(--wl-r-sm); }
.wl-aside-foot .el-icon:hover { color: var(--wl-primary); background: var(--el-fill-color-light); }
.wl-topbar {
  display: flex; align-items: center; justify-content: space-between;
  background: var(--wl-card); border-bottom: 1px solid var(--wl-border);
  padding: 0 var(--wl-sp-6);
}
.wl-topbar-right { display: flex; align-items: center; gap: 20px; }
.wl-health { display: inline-flex; cursor: default; }
.wl-health .dot { width: 10px; height: 10px; border-radius: 50%; }
.wl-health.is-ok .dot { background: var(--wl-success); box-shadow: 0 0 0 3px rgba(22, 163, 74, 0.15); }
.wl-health.is-warn .dot { background: var(--wl-warning); box-shadow: 0 0 0 3px rgba(217, 119, 6, 0.15); }
.wl-user { display: inline-flex; align-items: center; gap: 8px; cursor: pointer; }
.wl-user-name { font-size: var(--wl-fs-md); color: var(--wl-text-secondary); }
.wl-main { padding: 0; overflow: auto; background: var(--wl-bg); min-height: 0; }
@media (max-width: 900px) { .wl-topbar { padding: 0 12px; } }
</style>
