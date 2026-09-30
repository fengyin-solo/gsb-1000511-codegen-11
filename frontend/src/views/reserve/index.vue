<template>
  <section class="page" data-module="reserve">
    <header class="page-head">
      <div>
        <h2>储量估算管理</h2>
        <p class="page-desc">计算批次轨道：选择矿体块段 → 套用公式 → 复核签发；台账、估算表、储量图与报告共享同一批次。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">新建计算批次</button>
      </div>
    </header>

    <div class="tab-bar">
      <button
        v-for="tab in tabs"
        :key="tab.key"
        class="tab-btn"
        :class="{ active: activeTab === tab.key }"
        type="button"
        @click="switchTab(tab.key)"
      >
        {{ tab.label }}
      </button>
    </div>

    <!-- 首屏：计算批次轨道 -->
    <div v-if="activeTab === 'track'">
      <div class="stat-row">
        <article class="stat-card">
          <span class="stat-label">批次总数</span>
          <strong class="stat-value">{{ track.summary?.批次总数 ?? 0 }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">已确认</span>
          <strong class="stat-value">{{ track.summary?.已确认 ?? 0 }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">进行中</span>
          <strong class="stat-value">{{ track.summary?.进行中 ?? 0 }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">已回滚待续算</span>
          <strong class="stat-value">{{ track.summary?.已回滚待续算 ?? 0 }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">当前模型</span>
          <strong class="stat-value formula-current">{{ track.summary?.当前公式 ?? '—' }}</strong>
        </article>
      </div>

      <h3 class="block-title">每个块段的公式版本与受影响报告</h3>
      <table class="data-table">
        <thead>
          <tr>
            <th>块段编号</th>
            <th>矿体名称</th>
            <th>项目属性</th>
            <th>状态</th>
            <th>公式版本</th>
            <th>当前批次 / 代次</th>
            <th>矿石量</th>
            <th>金属量</th>
            <th>受影响报告</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="s in track.segments" :key="s.segment_id">
            <td>{{ s.块段编号 }}</td>
            <td>{{ s.矿体名称 }}</td>
            <td><span class="tag" :class="s.项目属性 === '历史项目' ? 'tag-hist' : 'tag-new'">{{ s.项目属性 }}</span></td>
            <td>{{ s.状态 }}</td>
            <td>{{ s.公式版本 }}</td>
            <td>
              <button v-if="s.签发批次" class="link" type="button" @click="openBatchByNo(s.签发批次)">
                {{ s.签发批次 }} / 第{{ s.签发代数 ?? 1 }}代
              </button>
              <span v-else class="muted">未签发</span>
            </td>
            <td>{{ s.矿石量 ?? '—' }}</td>
            <td>{{ s.金属量 ?? '—' }}</td>
            <td>
              <div v-for="rp in s.受影响报告" :key="rp.报告编号" class="report-line">
                <button class="link" type="button" @click="reportBatchNo = rp.批次号; activeTab = 'reports'">
                  {{ rp.报告编号 }}
                </button>
                <span class="muted">· {{ rp.受影响状态 }}</span>
              </div>
            </td>
          </tr>
        </tbody>
      </table>

      <h3 class="block-title">计算批次轨道</h3>
      <table class="data-table">
        <thead>
          <tr>
            <th>批次号</th>
            <th>项目属性</th>
            <th>公式版本</th>
            <th>代次</th>
            <th>状态</th>
            <th>分步链路</th>
            <th>块段</th>
            <th>确认时间</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="b in track.batches" :key="b.id" :class="{ 'row-rolled': b.status === '已回滚' }">
            <td><button class="link" type="button" @click="selectBatch(b.id)">{{ b.batch_no }}</button></td>
            <td>{{ b.项目属性 }}</td>
            <td>{{ b.formula_id }}</td>
            <td>第{{ b.generation }}代</td>
            <td><span class="tag" :class="statusTagClass(b.status)">{{ b.status }}</span></td>
            <td>
              <span class="mini-track">
                <i v-for="(name, i) in flowSteps" :key="name" class="mini-step" :class="miniState(b, i)">
                  {{ i + 1 }}.{{ name }}
                </i>
              </span>
            </td>
            <td>{{ b.块段数 }} 个</td>
            <td>{{ b.confirmed_at ?? '—' }}</td>
            <td class="row-actions">
              <button class="link" type="button" @click="selectBatch(b.id)">链路</button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 批次详情：分步链路 / 重算 / 续算 -->
    <div v-else-if="activeTab === 'detail' && currentBatch">
      <button class="btn ghost" type="button" @click="activeTab = 'track'">← 返回批次轨道</button>
      <div class="detail-head">
        <div>
          <h3 class="block-title">{{ currentBatch.batch_no }}（第{{ currentBatch.generation }}代）</h3>
          <span class="tag" :class="statusTagClass(currentBatch.status)">{{ currentBatch.status }}</span>
          <span class="muted"> · {{ currentBatch.formula_name }} · 创建 {{ currentBatch.created_at }}</span>
        </div>
        <div class="page-actions">
          <template v-if="currentBatch.status !== '已确认'">
            <button class="btn primary" type="button" :disabled="busy" @click="advance(currentBatch.id)">
              {{ advanceLabel(currentBatch) }}
            </button>
          </template>
          <template v-else>
            <select v-model="recomputeFormula" class="mini-select">
              <option v-for="f in formulas" :key="f.id" :value="f.id">{{ f.id }} {{ f.name }}</option>
            </select>
            <button class="btn primary" type="button" :disabled="busy" @click="recompute(currentBatch.id, null)">重算</button>
            <select v-model.number="drillStage" class="mini-select" title="故障注入：演示回滚与检查点续算">
              <option :value="null">正常执行</option>
              <option :value="1">故障：迁移快照阶段</option>
              <option :value="2">故障：公式重算阶段</option>
              <option :value="3">故障：整批回写阶段</option>
            </select>
            <button class="btn" type="button" :disabled="busy" @click="recompute(currentBatch.id, drillStage)">模拟故障重算</button>
          </template>
          <template v-if="currentBatch.status === '已回滚'">
            <button class="btn" type="button" :disabled="busy" @click="resetBatch(currentBatch.id)">复位</button>
            <button class="btn primary" type="button" :disabled="busy" @click="resumeBatch(currentBatch.id)">从检查点继续</button>
          </template>
        </div>
      </div>

      <p v-if="currentBatch.rollback_reason" class="rollback-note">
        回滚原因：{{ currentBatch.rollback_reason }}（{{ currentBatch.rolled_back_at }}）
      </p>
      <p v-if="currentBatch.idempotent" class="muted">本次为幂等回放：同一公式版本与输入指纹只落库一次，未产生新代次。</p>

      <h4 class="block-title">分步链路检查点</h4>
      <ol class="step-track">
        <li v-for="s in currentBatch.steps" :key="s.index" class="step-item" :class="`step-${s.state}`">
          <span class="step-index">{{ s.index + 1 }}</span>
          <span class="step-name">{{ s.name }}</span>
          <span class="step-state">{{ stateLabel(s.state) }}</span>
        </li>
      </ol>

      <h4 class="block-title">本代块段结果（快照 → 公式 → 结果）</h4>
      <table class="data-table">
        <thead>
          <tr>
            <th>块段编号</th><th>项目属性</th><th>公式</th>
            <th>面积快照</th><th>厚度</th><th>品位</th><th>体重</th>
            <th>校正系数</th><th>矿石量</th><th>金属量</th><th>阶段</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="s in currentBatch.segments" :key="s.segment_id">
            <td>{{ s.块段编号 }}</td>
            <td>{{ s.项目属性 }}</td>
            <td>{{ s.formula_id }}</td>
            <td>{{ s.snapshot?.面积 ?? '—' }}</td>
            <td>{{ s.snapshot?.厚度 ?? '—' }}</td>
            <td>{{ s.snapshot?.品位 ?? '—' }}</td>
            <td>{{ s.snapshot?.矿石体重 ?? '—' }}</td>
            <td>{{ s.result?.校正系数 ?? '—' }}</td>
            <td>{{ s.result?.矿石量 ?? '—' }}</td>
            <td>{{ s.result?.金属量 ?? '—' }}</td>
            <td>{{ stateLabel(s.step_status) }}</td>
          </tr>
        </tbody>
      </table>

      <div class="detail-grid">
        <div>
          <h4 class="block-title">估算表（同批次回写）</h4>
          <table class="data-table compact">
            <thead><tr><th>块段</th><th>版本</th><th>矿石量</th><th>金属量</th><th>最新</th></tr></thead>
            <tbody>
              <tr v-for="e in currentBatch.estimates" :key="e.id">
                <td>{{ e.块段编号 }}</td><td>{{ e.formula_id }}</td>
                <td>{{ e.矿石量 }}</td><td>{{ e.金属量 }}</td>
                <td>{{ e.is_latest ? '是' : '否' }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <div>
          <h4 class="block-title">储量图清单（同批次回写）</h4>
          <table class="data-table compact">
            <thead><tr><th>图号</th><th>版本</th><th>矿石量</th><th>最新</th></tr></thead>
            <tbody>
              <tr v-for="m in currentBatch.maps" :key="m.id">
                <td>{{ m.图号 }}</td><td>{{ m.formula_id }}</td>
                <td>{{ m.矿石量 }}</td>
                <td>{{ m.is_latest ? '是' : '否' }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <h4 class="block-title">受影响报告（引用同一批次号）</h4>
      <table class="data-table compact">
        <thead><tr><th>报告编号</th><th>报告名称</th><th>状态</th><th>批次号</th></tr></thead>
        <tbody>
          <tr v-for="rp in currentBatch.affected_reports" :key="rp.id">
            <td>{{ rp.报告编号 }}</td><td>{{ rp.报告名称 }}</td>
            <td>{{ rp.受影响状态 }}</td><td>{{ rp.批次号 }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 台账 -->
    <div v-else-if="activeTab === 'ledger'">
      <table class="data-table">
        <thead>
          <tr><th v-for="c in ledgerColumns" :key="c">{{ c }}</th></tr>
        </thead>
        <tbody>
          <tr v-for="r in ledgerRows" :key="r.id">
            <td v-for="c in ledgerColumns" :key="c">{{ r[c] ?? '—' }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 估算表 -->
    <div v-else-if="activeTab === 'estimates'">
      <BatchBatchFilter :model-value="estimateBatchNo" @update:model-value="estimateBatchNo = $event" :batches="track.batches" />
      <table class="data-table">
        <thead><tr><th>批次号</th><th>代次</th><th>块段</th><th>版本</th><th>矿石量</th><th>金属量</th><th>最新</th></tr></thead>
        <tbody>
          <tr v-for="e in estimateRows" :key="e.id">
            <td><button class="link" type="button" @click="openBatchByNo(e.batch_no)">{{ e.batch_no }}</button></td>
            <td>第{{ e.generation }}代</td><td>{{ e.块段编号 }}</td><td>{{ e.formula_id }}</td>
            <td>{{ e.矿石量 }}</td><td>{{ e.金属量 }}</td><td>{{ e.is_latest ? '是' : '否' }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 储量图清单 -->
    <div v-else-if="activeTab === 'maps'">
      <BatchBatchFilter :model-value="mapBatchNo" @update:model-value="mapBatchNo = $event" :batches="track.batches" />
      <table class="data-table">
        <thead><tr><th>批次号</th><th>代次</th><th>图号</th><th>图名</th><th>版本</th><th>矿石量</th><th>金属量</th><th>最新</th></tr></thead>
        <tbody>
          <tr v-for="m in mapRows" :key="m.id">
            <td><button class="link" type="button" @click="openBatchByNo(m.batch_no)">{{ m.batch_no }}</button></td>
            <td>第{{ m.generation }}代</td><td>{{ m.图号 }}</td><td>{{ m.图名 }}</td>
            <td>{{ m.formula_id }}</td><td>{{ m.矿石量 }}</td><td>{{ m.金属量 }}</td>
            <td>{{ m.is_latest ? '是' : '否' }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 受影响报告 -->
    <div v-else-if="activeTab === 'reports'">
      <BatchBatchFilter :model-value="reportBatchNo" @update:model-value="reportBatchNo = $event" :batches="track.batches" />
      <table class="data-table">
        <thead><tr><th>报告编号</th><th>报告名称</th><th>矿体</th><th>受影响状态</th><th>引用批次</th></tr></thead>
        <tbody>
          <tr v-for="rp in reportRows" :key="rp.id">
            <td>{{ rp.报告编号 }}</td><td>{{ rp.报告名称 }}</td><td>{{ rp.矿体名称 }}</td>
            <td>{{ rp.受影响状态 }}</td>
            <td>
              <button v-if="rp.批次号" class="link" type="button" @click="openBatchByNo(rp.批次号)">{{ rp.批次号 }}</button>
              <span v-else>—</span>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- 幂等任务锁 -->
    <div v-else-if="activeTab === 'locks'">
      <p class="muted">幂等任务锁：同一批次 + 同一公式版本 + 同一输入指纹，只允许确认落库一次。</p>
      <table class="data-table">
        <thead><tr><th>批次号</th><th>块段</th><th>公式版本</th><th>输入指纹</th><th>代次</th><th>状态</th><th>确认时间</th></tr></thead>
        <tbody>
          <tr v-for="lk in lockRows" :key="lk.id">
            <td>{{ lk.batch_no }}</td><td>{{ lk.segment_id }}</td><td>{{ lk.formula_id }}</td>
            <td><code class="fingerprint">{{ lk.fingerprint.slice(0, 28) }}…</code></td>
            <td>第{{ lk.generation }}代</td><td>{{ lk.status }}</td><td>{{ lk.confirmed_at }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <footer class="page-foot">
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
      <span v-else class="muted">历史项目按当时公式版本保留，新项目以当前模型为准。</span>
    </footer>

    <!-- 新建批次弹窗 -->
    <div v-if="creating" class="modal-mask" @click.self="creating = false">
      <div class="modal">
        <h3>新建计算批次 · 选择矿体块段</h3>
        <table class="data-table compact">
          <thead><tr><th>选</th><th>块段</th><th>矿体</th><th>项目属性</th><th>当前版本</th></tr></thead>
          <tbody>
            <tr v-for="s in track.segments" :key="s.segment_id">
              <td><input v-model="chosenSegments" type="checkbox" :value="s.segment_id" /></td>
              <td>{{ s.块段编号 }}</td><td>{{ s.矿体名称 }}</td>
              <td>{{ s.项目属性 }}</td><td>{{ s.公式版本 }}</td>
            </tr>
          </tbody>
        </table>
        <label class="filter-item">
          <span>套用公式（历史块段仍固定 FV-2018）</span>
          <select v-model="createFormula" class="mini-select">
            <option :value="null">按项目属性自动选择</option>
            <option v-for="f in formulas" :key="f.id" :value="f.id">{{ f.id }} {{ f.name }}</option>
          </select>
        </label>
        <div class="modal-actions">
          <button class="btn ghost" type="button" @click="creating = false">取消</button>
          <button class="btn primary" type="button" :disabled="busy || !chosenSegments.length" @click="submitCreate">
            选定并建批次
          </button>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { defineComponent, h, onMounted, ref } from 'vue'

import { request } from '@/api/client'

/* eslint-disable @typescript-eslint/no-explicit-any */
type AnyRow = any
const ENDPOINT = '/api/reserve-batches'

const tabs = [
  { key: 'track', label: '计算批次轨道' },
  { key: 'ledger', label: '矿体块段台账' },
  { key: 'estimates', label: '估算表' },
  { key: 'maps', label: '储量图清单' },
  { key: 'reports', label: '受影响报告' },
  { key: 'locks', label: '幂等任务锁' },
] as const

const flowSteps = ['选择块段', '套用公式', '复核签发']
const ledgerColumns = ['块段编号', '矿体名称', '项目属性', '面积', '厚度', '品位', '矿石体重', '资源类别', '块段状态', '公式版本', '签发批次', '签发代数', '矿石量', '金属量']

const activeTab = ref<string>('track')
const busy = ref(false)
const errorMessage = ref('')
const formulas = ref<AnyRow[]>([])
const track = ref<{ summary: AnyRow | null; segments: AnyRow[]; batches: AnyRow[] }>({
  summary: null, segments: [], batches: [],
})
const currentBatch = ref<AnyRow | null>(null)
const ledgerRows = ref<AnyRow[]>([])
const estimateRows = ref<AnyRow[]>([])
const mapRows = ref<AnyRow[]>([])
const reportRows = ref<AnyRow[]>([])
const lockRows = ref<AnyRow[]>([])
const estimateBatchNo = ref('')
const mapBatchNo = ref('')
const reportBatchNo = ref('')

const creating = ref(false)
const chosenSegments = ref<number[]>([])
const createFormula = ref<string | null>(null)
const recomputeFormula = ref('FV-2024')
const drillStage = ref<number | null>(null)

// 估算表 / 图清单 / 报告共享的批次号过滤组件
const BatchBatchFilter = defineComponent({
  name: 'BatchBatchFilter',
  props: {
    modelValue: { type: String, default: '' },
    batches: { type: Array as () => AnyRow[], default: () => [] },
  },
  emits: ['update:modelValue'],
  setup(props, { emit }) {
    return () =>
      h('label', { class: 'filter-item batch-filter' }, [
        h('span', '按批次号过滤（多个入口引用同一批次）'),
        h(
          'select',
          {
            class: 'mini-select',
            value: props.modelValue,
            onChange: (e: Event) => emit('update:modelValue', (e.target as HTMLSelectElement).value),
          },
          [
            h('option', { value: '' }, '全部批次'),
            ...props.batches.map((b: AnyRow) => h('option', { value: String(b.batch_no) }, String(b.batch_no))),
          ],
        ),
      ])
  },
})

function statusTagClass(status: string) {
  if (status === '已确认') return 'tag-ok'
  if (status === '已回滚') return 'tag-rollback'
  return 'tag-running'
}

function stateLabel(state: string) {
  return { done: '已完成', active: '进行中', todo: '待执行', failed: '已中断' }[state] ?? state
}

function miniState(b: AnyRow, i: number) {
  const cp = Number(b.checkpoint)
  if (cp >= i) return 'done'
  if (b.status === '已回滚' && cp + 1 === i) return 'failed'
  if (b.status === '进行中' && cp + 1 === i) return 'active'
  return 'todo'
}

function advanceLabel(b: AnyRow) {
  const cp = Number(b.checkpoint ?? -1)
  return ['确认选择块段', '套用公式并试算', '复核签发并整批回写'][cp + 1] ?? '链路已走完'
}

async function api(path: string, init?: RequestInit) {
  const response = await request(path, init)
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) {
    throw new Error(typeof payload.detail === 'string' ? payload.detail : '操作未生效，请稍后重试')
  }
  return payload
}

async function loadTrack() {
  track.value = await api(`${ENDPOINT}/track`)
}

async function selectBatch(id: number) {
  currentBatch.value = await api(`${ENDPOINT}/${id}`)
  activeTab.value = 'detail'
  recomputeFormula.value = String(currentBatch.value.formula_id ?? 'FV-2024')
}

async function openBatchByNo(batchNo: string) {
  const found = track.value.batches.find((b) => b.batch_no === batchNo)
  if (found) await selectBatch(Number(found.id))
}

async function loadLedger() {
  const payload = await api('/api/reserve?size=200')
  ledgerRows.value = payload.items ?? []
}

async function loadEstimates() {
  estimateRows.value = (await api(`${ENDPOINT}/estimates${estimateBatchNo.value ? `?batch_no=${estimateBatchNo.value}` : ''}`)).items ?? []
}
async function loadMaps() {
  mapRows.value = (await api(`${ENDPOINT}/maps${mapBatchNo.value ? `?batch_no=${mapBatchNo.value}` : ''}`)).items ?? []
}
async function loadReports() {
  reportRows.value = (await api(`${ENDPOINT}/reports${reportBatchNo.value ? `?batch_no=${reportBatchNo.value}` : ''}`)).items ?? []
}
async function loadLocks() {
  lockRows.value = (await api(`${ENDPOINT}/locks`)).items ?? []
}

async function switchTab(key: string) {
  activeTab.value = key
  await reloadTab(key)
}

async function reloadTab(key = activeTab.value) {
  errorMessage.value = ''
  try {
    if (key === 'track') await loadTrack()
    else if (key === 'ledger') await loadLedger()
    else if (key === 'estimates') await loadEstimates()
    else if (key === 'maps') await loadMaps()
    else if (key === 'reports') await loadReports()
    else if (key === 'locks') await loadLocks()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '数据读取失败'
  }
}

function openCreate() {
  chosenSegments.value = []
  createFormula.value = null
  creating.value = true
}

async function submitCreate() {
  busy.value = true
  errorMessage.value = ''
  try {
    const payload = await api(ENDPOINT, {
      method: 'POST',
      body: JSON.stringify({ segment_ids: chosenSegments.value, formula_id: createFormula.value, operator: '当班估算员' }),
    })
    creating.value = false
    await loadTrack()
    currentBatch.value = payload.batch
    activeTab.value = 'detail'
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '创建批次失败'
  } finally {
    busy.value = false
  }
}

async function afterMutation(id: number, keepDetail = true) {
  await loadTrack()
  if (keepDetail) {
    currentBatch.value = await api(`${ENDPOINT}/${id}`)
    if (activeTab.value === 'estimates') await loadEstimates()
    if (activeTab.value === 'maps') await loadMaps()
    if (activeTab.value === 'reports') await loadReports()
  }
}

async function advance(id: number) {
  busy.value = true
  errorMessage.value = ''
  try {
    const payload = await api(`${ENDPOINT}/${id}/advance`, {
      method: 'POST',
      body: JSON.stringify({ operator: '当班估算员' }),
    })
    await afterMutation(id)
    currentBatch.value = payload.batch
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '链路推进失败'
    await afterMutation(id)
  } finally {
    busy.value = false
  }
}

async function recompute(id: number, stage: number | null) {
  busy.value = true
  errorMessage.value = ''
  try {
    const payload = await api(`${ENDPOINT}/${id}/recompute`, {
      method: 'POST',
      body: JSON.stringify({ formula_id: recomputeFormula.value, operator: '当班估算员', drill_stage: stage }),
    })
    currentBatch.value = payload.batch
    await afterMutation(id)
    currentBatch.value = payload.batch
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '重算未通过'
    await afterMutation(id)
  } finally {
    busy.value = false
  }
}

async function resumeBatch(id: number) {
  busy.value = true
  errorMessage.value = ''
  try {
    const payload = await api(`${ENDPOINT}/${id}/resume`, {
      method: 'POST',
      body: JSON.stringify({ operator: '当班估算员' }),
    })
    currentBatch.value = payload.batch
    await afterMutation(id)
    currentBatch.value = payload.batch
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '续算失败'
    await afterMutation(id)
  } finally {
    busy.value = false
  }
}

async function resetBatch(id: number) {
  busy.value = true
  errorMessage.value = ''
  try {
    const payload = await api(`${ENDPOINT}/${id}/reset`, { method: 'POST' })
    currentBatch.value = payload.batch
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '复位失败'
  } finally {
    busy.value = false
  }
}

onMounted(async () => {
  formulas.value = (await api(`${ENDPOINT}/formulas`)).items ?? []
  await loadTrack()
})
</script>

<style scoped>
.tab-bar { display: flex; gap: 4px; border-bottom: 1px solid var(--border); margin: 8px 0 14px; }
.tab-btn { border: none; background: none; padding: 8px 14px; cursor: pointer; font-size: 13px; color: var(--muted); border-bottom: 2px solid transparent; }
.tab-btn.active { color: var(--brand); border-bottom-color: var(--brand); font-weight: 600; }
.block-title { font-size: 14px; margin: 18px 0 8px; }
.muted { color: var(--muted); font-size: 12px; }
.tag { display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 12px; }
.tag-hist { background: #fef3c7; color: #92400e; }
.tag-new { background: #dbeafe; color: #1e40af; }
.tag-ok { background: #dcfce7; color: #166534; }
.tag-rollback { background: #fee2e2; color: #991b1b; }
.tag-running { background: #e0e7ff; color: #3730a3; }
.formula-current { font-size: 15px; }
.report-line { font-size: 12px; line-height: 1.7; }
.mini-track { display: flex; gap: 6px; flex-wrap: wrap; }
.mini-step { font-style: normal; font-size: 12px; color: var(--muted); }
.mini-step.done { color: #166534; font-weight: 600; }
.mini-step.active { color: var(--brand); font-weight: 600; }
.mini-step.failed { color: #b42318; font-weight: 600; }
.row-rolled { background: #fff7f7; }
.detail-head { display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px; margin: 12px 0; }
.mini-select { padding: 5px 8px; border: 1px solid var(--border); border-radius: 6px; font-size: 12px; }
.rollback-note { color: #991b1b; background: #fef2f2; border: 1px solid #fecaca; padding: 8px 10px; border-radius: 6px; font-size: 13px; }
.step-track { list-style: none; display: flex; gap: 0; padding: 0; margin: 0 0 8px; }
.step-item { flex: 1; display: flex; align-items: center; gap: 8px; padding: 10px 12px; border: 1px solid var(--border); background: #f8fafc; position: relative; }
.step-item:not(:last-child) { margin-right: 18px; }
.step-item:not(:last-child)::after { content: '→'; position: absolute; right: -16px; color: var(--muted); }
.step-index { width: 22px; height: 22px; border-radius: 50%; display: inline-flex; align-items: center; justify-content: center; font-size: 12px; background: #e2e8f0; color: #475569; }
.step-done { background: #f0fdf4; border-color: #86efac; }
.step-done .step-index { background: #16a34a; color: #fff; }
.step-active { background: #eff6ff; border-color: #93c5fd; }
.step-active .step-index { background: var(--brand); color: #fff; }
.step-failed { background: #fef2f2; border-color: #fca5a5; }
.step-failed .step-index { background: #dc2626; color: #fff; }
.step-state { margin-left: auto; font-size: 12px; color: var(--muted); }
.detail-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
.data-table.compact th, .data-table.compact td { padding: 5px 8px; font-size: 12px; }
.batch-filter { margin: 8px 0; }
.fingerprint { font-size: 11px; color: var(--muted); }
.modal-mask { position: fixed; inset: 0; background: rgba(15, 23, 42, 0.45); display: flex; align-items: center; justify-content: center; z-index: 20; }
.modal { background: #fff; border-radius: 10px; padding: 18px 20px; width: 640px; max-height: 80vh; overflow: auto; }
.modal-actions { display: flex; justify-content: flex-end; gap: 10px; margin-top: 14px; }
</style>
