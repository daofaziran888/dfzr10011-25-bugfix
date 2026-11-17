<template>
  <section class="page" data-module="storage">
    <header class="page-head">
      <div>
        <h2>采集存储分层管理</h2>
        <p class="page-desc">维护采集存储的分层配置与导出快照；存储分层按看板刷新结果生成快照并保留完整数据范围，归档待处理清单与看板面板归档同步。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记存储策略</button>
        <button class="btn" type="button" @click="exportRows">导出采集存储分层清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无采集存储分层数据，可先登记存储策略</td>
        </tr>
      </tbody>
    </table>

    <h3 class="section-title">看板导出快照（按刷新结果生成，保留完整数据范围）</h3>
    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in snapshotColumns" :key="column">{{ column }}</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="snapshot in snapshots" :key="String(snapshot.id)">
          <td v-for="column in snapshotColumns" :key="column">{{ snapshot[column] ?? '—' }}</td>
        </tr>
        <tr v-if="!snapshots.length">
          <td :colspan="snapshotColumns.length" class="empty-state">暂无看板导出快照，成功刷新后自动同步</td>
        </tr>
      </tbody>
    </table>

    <h3 class="section-title">修复看板刷新归档待处理清单</h3>
    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in archiveColumns" :key="column">{{ column }}</th>
          <th>处置</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="task in archiveTasks" :key="String(task.id)">
          <td v-for="column in archiveColumns" :key="column">{{ task[column] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-if="task.任务状态 === '待处理'"
              class="link"
              type="button"
              @click="completeArchiveTask(task)"
            >
              标记完成
            </button>
            <span v-else class="muted">已处置</span>
          </td>
        </tr>
        <tr v-if="!archiveTasks.length">
          <td :colspan="archiveColumns.length + 1" class="empty-state">暂无归档待办，看板面板归档后自动挂账</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条采集存储分层记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null | Record<string, unknown>>

const ENDPOINT = '/api/storage'
const columns = ['策略编号', '存储层级', '文件格式', '压缩算法', '保留天数', '迁移规则', '清理方式', '策略状态']
const snapshotColumns = ['快照编号', '面板编号', '批次编号', '快照版本', '数据范围', '来源状态', '保留状态', '存储层级', '文件格式', '生成时间']
const archiveColumns = ['任务编号', '面板编号', '归属团队', '任务状态', '数据范围', '保留快照版本', '最后刷新批次', '备注', '创建时间']
const actions = ['启用策略', '暂停策略', '关闭策略']
const stats = [
  { label: '存储策略总数', value: 0 },
  { label: '导出快照数', value: 0 },
  { label: '归档待办', value: 0 },
]

const rows = ref<Row[]>([])
const snapshots = ref<Row[]>([])
const archiveTasks = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '存储策略登记入口尚未接入审批流'
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values: { action } }),
    })
    if (!response.ok) {
      throw new Error('采集存储分层动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集存储分层操作失败'
  }
}

async function completeArchiveTask(task: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/archive-tasks/${task.id}/complete`, {
      method: 'POST',
      body: JSON.stringify({}),
    })
    const payload = await response.json()
    if (!response.ok) {
      throw new Error(payload.message || '归档待办处置失败')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '归档待办处置失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const [policyResp, snapshotResp, archiveResp] = await Promise.all([
      request(`${ENDPOINT}?${query}`),
      request(`${ENDPOINT}/snapshots?page=1&size=10000`),
      request(`${ENDPOINT}/archive-tasks?page=1&size=10000`),
    ])
    if (!policyResp.ok) {
      throw new Error('存储策略列表读取失败')
    }
    const payload = await policyResp.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    snapshots.value = (await snapshotResp.json()).items ?? []
    archiveTasks.value = (await archiveResp.json()).items ?? []
    stats[0].value = total.value
    stats[1].value = snapshots.value.length
    stats[2].value = archiveTasks.value.filter((task) => task.任务状态 === '待处理').length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集存储分层列表读取失败'
  }
}

onMounted(reload)
</script>
