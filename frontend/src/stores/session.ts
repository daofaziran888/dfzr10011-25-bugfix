import { defineStore } from 'pinia'

// 团队身份决定看板可见范围与改动权限：归属团队可读写，跨团队只读共享摘要。
export const useSessionStore = defineStore('session', {
  state: () => ({
    operator: '值班管理员',
    team: '采集A组',
    teamOptions: ['采集A组', '采集B组', '采集团队C'],
    shiftLabel: '白班 08:00-20:00',
    scope: '分布式采集调度引擎',
  }),
  getters: {
    canOperate: (state) => state.operator.length > 0,
  },
  actions: {
    setShift(label: string) {
      this.shiftLabel = label
    },
    setTeam(team: string) {
      this.team = team
    },
  },
})
