import { createRouter, createWebHistory } from 'vue-router'
import MainLayout from '../layout/MainLayout.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/login', component: () => import('../views/LoginView.vue') },
    {
      path: '/',
      component: MainLayout,
      children: [
        { path: '', redirect: '/chat' },
        { path: 'workbench', component: () => import('../views/WorkbenchView.vue'), meta: { title: '工作台' } },
        { path: 'chat', component: () => import('../views/ChatView.vue'), meta: { title: '智能问答' } },
        { path: 'kb', component: () => import('../views/KbListView.vue'), meta: { title: '知识库', group: 'op' } },
        { path: 'kb/:id', component: () => import('../views/KbDetailView.vue'), meta: { title: '知识库详情', group: 'op' } },
        { path: 'kb/:kbId/doc/:docId', component: () => import('../views/DocDetailView.vue'), meta: { title: '文档详情', group: 'op' } },
        { path: 'qa', component: () => import('../views/QaManageView.vue'), meta: { title: 'QA 对管理', group: 'op' } },
        { path: 'wiki', component: () => import('../views/WikiView.vue'), meta: { title: 'Wiki 知识' } },
        { path: 'graph', component: () => import('../views/GraphView.vue'), meta: { title: '知识图谱', group: 'op' } },
        { path: 'eval', component: () => import('../views/EvalView.vue'), meta: { title: '检索评测', group: 'op' } },
        { path: 'models', component: () => import('../views/ModelConfigView.vue'), meta: { title: '模型与组件', group: 'admin' } },
        { path: 'usage', component: () => import('../views/UsageDashboardView.vue'), meta: { title: 'Token 用量', group: 'admin' } },
        { path: 'clean-rules', component: () => import('../views/CleanRulesView.vue'), meta: { title: '清洗规则', group: 'admin' } },
        { path: 'tasks', component: () => import('../views/TasksAuditView.vue'), meta: { title: '任务与审计', group: 'admin' } },
        { path: 'profile', component: () => import('../views/ProfileView.vue'), meta: { title: '个人中心' } }
      ]
    }
  ]
})

router.beforeEach((to) => {
  document.title = `${to.meta.title || 'WisdomLink2'} · WisdomLink2`
})

export default router
