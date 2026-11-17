<template>
  <section class="page" data-module="dashboard">
    <header class="page-head">
      <div>
        <h2>全局采集看板管理</h2>
        <p class="page-desc">维护看板面板的指标配置、刷新批次与归档联动；团队内只呈现面板声明数据与最近成功快照，跨团队共享面板只读摘要。</p>
      </div>
      <div class="page-actions">
        <label class="filter-item">
          <span>当前团队</span>
          <select :value="store.team" @change="switchTeam(($event.target as HTMLSelectElement).value)">
            <option v-for="team in store.teamOptions" :key="team" :value="team">{{ team }}</option>
          </select>
        </label>
        <button class="btn" type="button" @click="exportRows">导出全局采集看板清单</button>
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
          <th>最近成功快照</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ formatCell(row, column) }}</td>
          <td>
            <template v-if="row.摘要访问">
              <span class="muted">共享摘要，明细不可见</span>
            </template>
            <template v-else>
              <span v-if="row.最近快照版本">{{ row.最近快照版本 }} · {{ row.最近刷新时间 || '—' }}</span>
              <span v-else class="muted">尚无成功快照</span>
            </template>
          </td>
          <td class="row-actions">
            <template v-if="row.摘要访问">
              <span class="muted">跨团队只读</span>
            </template>
            <template v-else>
              <button class="link" type="button" @click="openFields(row)">保存字段</button>
              <button class="link" type="button" @click="openBatches(row)">刷新批次</button>
              <button
                v-for="action in actions"
                :key="action"
                class="link"
                type="button"
                @click="runAction(action, row)"
              >
                {{ action }}
              </button>
            </template>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 2" class="empty-state">当前团队范围暂无看板面板</td>
        </tr>
      </tbody>
    </table>

    <!-- 面板字段保存：只改面板声明，历史刷新批次与导出快照保持原范围 -->
    <div v-if="fieldPanel" class="modal-mask" @click.self="fieldPanel = null">
      <div class="modal-card">
        <h3>保存面板字段 · {{ fieldPanel.面板编号 }}</h3>
        <p class="page-desc">字段保存后只影响后续刷新，历史刷新结果仍绑定申请时的面板声明与数据范围。</p>
        <label v-for="field in editableFields" :key="field" class="filter-item modal-field">
          <span>{{ field }}</span>
          <input v-model="fieldForm[field]" :placeholder="field" />
        </label>
        <label class="filter-item modal-field">
          <span>共享范围</span>
          <select v-model="fieldForm.共享范围">
            <option value="团队内">团队内</option>
            <option value="跨团队共享">跨团队共享</option>
          </select>
        </label>
        <div class="modal-actions">
          <button class="btn ghost" type="button" @click="fieldPanel = null">取消</button>
          <button class="btn primary" type="button" @click="saveFields">保存字段</button>
        </div>
      </div>
    </div>

    <!-- 刷新批次：申请刷新、回执成功/失败，查看绑定范围与快照版本 -->
    <div v-if="batchPanel" class="modal-mask" @click.self="batchPanel = null">
      <div class="modal-card modal-wide">
        <h3>刷新批次 · {{ batchPanel.面板编号 }}</h3>
        <p class="page-desc">
          面板声明数据范围「{{ batchPanel.数据范围 }}」；并发刷新同一面板只允许一个批次落库，失败刷新不覆盖旧快照。
        </p>
        <div class="modal-actions">
          <button class="btn primary" type="button" @click="startRefresh(batchPanel)">申请刷新</button>
          <button class="btn ghost" type="button" @click="batchPanel = null">关闭</button>
        </div>
        <table class="data-table">
          <thead>
            <tr>
              <th v-for="column in batchColumns" :key="column">{{ column }}</th>
              <th>回执</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="batch in batchRows" :key="String(batch.id)">
              <td v-for="column in batchColumns" :key="column">{{ batch[column] ?? '—' }}</td>
              <td class="row-actions">
                <template v-if="batch.批次状态 === '刷新中'">
                  <button class="link" type="button" @click="completeBatch(batch, true)">成功回执</button>
                  <button class="link" type="button" @click="completeBatch(batch, false)">失败回执</button>
                </template>
                <span v-else class="muted">已结束</span>
              </td>
            </tr>
            <tr v-if="!batchRows.length">
              <td :colspan="batchColumns.length + 1" class="empty-state">暂无刷新批次</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条看板面板记录（团队：{{ store.team }}）</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'
import { useSessionStore } from '@/stores/session'

type Row = Record<string, string | number | boolean | null | Record<string, unknown>>

const store = useSessionStore()
const ENDPOINT = '/api/dashboard'
const columns = ['面板编号', '面板名称', '显示指标', '刷新间隔', '告警配置', '数据范围', '归属团队', '共享范围', '面板状态']
const editableFields = ['面板名称', '显示指标', '刷新间隔', '告警配置', '数据范围']
const batchColumns = ['批次编号', '批次状态', '数据范围', '快照版本', '归档后拒绝', '失败原因', '申请时间', '完成时间']
const actions = ['启用面板', '暂停面板', '归档面板']
const stats = [
  { label: '看板面板总数', value: 0 },
  { label: '本团队面板', value: 0 },
  { label: '共享摘要', value: 0 },
]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)

const fieldPanel = ref<Row | null>(null)
const fieldForm = ref<Record<string, string>>({})
const batchPanel = ref<Row | null>(null)
const batchRows = ref<Row[]>([])

function teamQuery() {
  return `team=${encodeURIComponent(store.team)}`
}

function formatCell(row: Row, column: string) {
  if (row.摘要访问 && !['面板编号', '面板名称', '显示指标', '归属团队', '共享范围'].includes(column)) {
    return '—'
  }
  return row[column] ?? '—'
}

function resetFilters() {
  filters.value = {}
  void reload()
}

function switchTeam(team: string) {
  store.setTeam(team)
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export?${teamQuery()}`, '_blank')
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${teamQuery()}&${query}`)
    if (!response.ok) {
      throw new Error('看板面板列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    stats[0].value = total.value
    stats[1].value = rows.value.filter((row) => !row.摘要访问).length
    stats[2].value = rows.value.filter((row) => row.摘要访问).length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '全局采集看板列表读取失败'
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values: { action, 团队: store.team } }),
    })
    const payload = await response.json()
    if (!response.ok) {
      throw new Error(payload.message || '全局采集看板动作未生效')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '全局采集看板操作失败'
  }
}

function openFields(row: Row) {
  fieldPanel.value = row
  fieldForm.value = {
    面板名称: String(row.面板名称 ?? ''),
    显示指标: String(row.显示指标 ?? ''),
    刷新间隔: String(row.刷新间隔 ?? ''),
    告警配置: String(row.告警配置 ?? ''),
    数据范围: String(row.数据范围 ?? ''),
    共享范围: String(row.共享范围 ?? '团队内'),
  }
}

async function saveFields() {
  if (!fieldPanel.value) return
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${fieldPanel.value.id}/fields`, {
      method: 'PUT',
      body: JSON.stringify({ values: { ...fieldForm.value, 团队: store.team } }),
    })
    const payload = await response.json()
    if (!response.ok) {
      throw new Error(payload.message || '字段保存失败')
    }
    fieldPanel.value = null
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '字段保存失败'
  }
}

async function openBatches(row: Row) {
  batchPanel.value = row
  await loadBatches(row)
}

async function loadBatches(row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/refresh-batches?${teamQuery()}`)
    const payload = await response.json()
    if (!response.ok) {
      throw new Error(payload.message || '刷新批次读取失败')
    }
    batchRows.value = payload.items ?? []
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '刷新批次读取失败'
  }
}

async function startRefresh(row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/refresh-batches`, {
      method: 'POST',
      body: JSON.stringify({ values: { 团队: store.team, 操作人: store.operator } }),
    })
    const payload = await response.json()
    if (!response.ok) {
      throw new Error(payload.message || '刷新申请失败')
    }
    await loadBatches(row)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '刷新申请失败'
  }
}

async function completeBatch(batch: Row, success: boolean) {
  if (!batchPanel.value) return
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/refresh-batches/${batch.id}/complete`, {
      method: 'POST',
      body: JSON.stringify({
        values: {
          团队: store.team,
          操作人: store.operator,
          成功: success,
          失败原因: success ? '' : '采集源返回超时',
        },
      }),
    })
    const payload = await response.json()
    if (!response.ok) {
      throw new Error(payload.message || '刷新回执失败')
    }
    await loadBatches(batchPanel.value)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '刷新回执失败'
  }
}

onMounted(reload)
</script>
