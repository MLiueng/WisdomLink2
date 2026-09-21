<template>
  <div class="login-page">
    <div class="login-card wl-card">
      <div class="login-brand">
        <div class="brand-mark">W</div>
        <h1>WisdomLink<small>2</small></h1>
        <p>企业级智能知识库平台</p>
      </div>
      <el-form @submit.prevent="submit">
        <el-form-item>
          <el-input v-model="form.username" placeholder="管理员账号" size="large" :prefix-icon="User" />
        </el-form-item>
        <el-form-item>
          <el-input v-model="form.password" type="password" placeholder="密码" size="large" :prefix-icon="Lock" show-password />
        </el-form-item>
        <el-button type="primary" size="large" class="login-btn" :loading="loading" @click="submit">登录管理面</el-button>
      </el-form>
      <p class="login-note">仅管理员需要登录；问答入口在可信内网内免登录使用</p>
    </div>
  </div>
</template>

<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { User, Lock } from '@element-plus/icons-vue'
import { http, USE_MOCK } from '../api/http'

const router = useRouter()
const form = reactive({ username: 'admin', password: '' })
const loading = ref(false)

async function submit() {
  loading.value = true
  try {
    if (USE_MOCK) {
      localStorage.setItem('wl2_token', 'mock')
    } else {
      const r = (await (http as any).post('/admin/login', form)) as any
      localStorage.setItem('wl2_token', r.token)
    }
    ElMessage.success('欢迎回来')
    router.push('/workbench')
  } catch (e: any) {
    ElMessage.error(e.message || '登录失败')
  } finally { loading.value = false }
}
</script>

<style scoped>
.login-page {
  height: 100%; display: flex; align-items: center; justify-content: center;
  background: linear-gradient(160deg, #eff4ff 0%, var(--wl-bg) 45%, #f3e8ff 100%);
}
.login-card { width: 380px; padding: 40px 36px; border-radius: 16px; box-shadow: var(--wl-shadow-md); }
.login-brand { text-align: center; margin-bottom: 28px; }
.brand-mark {
  width: 52px; height: 52px; margin: 0 auto 14px; border-radius: 14px;
  background: linear-gradient(135deg, var(--wl-primary), #7c3aed);
  color: #fff; font-size: 24px; font-weight: 700;
  display: flex; align-items: center; justify-content: center;
  box-shadow: var(--wl-shadow-md);
}
.login-brand h1 { font-size: 22px; margin: 0; color: var(--wl-gray-900); }
.login-brand h1 small { color: var(--wl-primary); }
.login-brand p { font-size: var(--wl-fs-xs); color: var(--wl-text-muted); margin: 4px 0 0; }
.login-btn { width: 100%; margin-top: 4px; }
.login-note { text-align: center; font-size: var(--wl-fs-xs); color: var(--wl-text-muted); margin-top: 18px; }
</style>
