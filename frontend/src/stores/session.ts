import { defineStore } from 'pinia'

export const useSessionStore = defineStore('session', {
  state: () => ({
    kbs: [] as any[],
    scopeKbIds: [] as number[],
    scopeFolderIds: [] as number[],
    sessionId: localStorage.getItem('wl2_session') || '',
    // F-01：工作台提问经 store 确定性传参（替代无人监听的 wl2:ask 事件）
    pendingAsk: ''
  }),
  getters: {
    scopeName(state): string {
      if (!state.scopeKbIds.length) return '全部知识库'
      const names = state.kbs.filter((k) => state.scopeKbIds.includes(k.id)).map((k) => k.name)
      return names.length ? names.join('、') : '全部知识库'
    }
  },
  actions: {
    setScope(kbIds: number[], folderIds: number[] = []) {
      this.scopeKbIds = kbIds
      this.scopeFolderIds = folderIds
    },
    setSession(id: string) {
      this.sessionId = id
      localStorage.setItem('wl2_session', id)
    },
    /** F-01：取出并清空待提问文本（工作台→对话页确定性传参） */
    takePendingAsk(): string {
      const t = this.pendingAsk
      this.pendingAsk = ''
      return t
    }
  }
})
