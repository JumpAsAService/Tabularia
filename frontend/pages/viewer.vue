<script setup lang="ts">
// Viewer: a READ-ONLY mini BI tool on a Tabularia Datasource. It builds filters +
// computed fields + pivot on the fly and runs them with the chosen engine through the
// PREVIEW (which reads the datasource's snapshot, it does NOT change it). The table and
// the charts (ChartPanel, aggregation over the whole dataset) run on the same
// transformed data. Nothing is saved.
import { ref, computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { useFormat } from '~/composables/useFormat'
import { usePopover } from '~/composables/usePopover'
import {
  PieChart, Table2, Filter, Sigma, Plus, X, Play, Cpu, Rows3, Download, FileSpreadsheet, Bookmark, Save,
} from 'lucide-vue-next'
import { useApi, errMessage, type Operation, type ColumnInfo } from '~/composables/useApi'
import { usePreviewSlots, isSuperseded } from '~/composables/usePreviewSlots'
import { useDatasources, type DatasourceInfo } from '~/composables/useDatasources'
import ContractBadge from '~/components/ui/ContractBadge.vue'
import { useSavedViews, type ChartSettings, type SavedView, type SavedViewSpec } from '~/composables/useSavedViews'
import { useProjects, type Project } from '~/composables/useProjects'
import { useFolderPaths } from '~/composables/useFolderPaths'

const api = useApi()
// Table and chart on two slots: an Apply (or a touch to the chart) knocks down the
// previous one instead of queueing behind it — see usePreviewSlots.
const previewSlots = usePreviewSlots('vw')
onUnmounted(() => previewSlots.cancelAll())
let applySeq = 0 // only the last Apply turns the spinner off
const dsApi = useDatasources()
const viewsApi = useSavedViews()
const projectsApi = useProjects()
const toast = useToast()
const { t } = useI18n()
const { pathOf } = useFolderPaths() // datasource and view names repeat across folders
const fmtx = useFormat()

// ── source + engine ─────────────────────────────────────────────────────
const datasources = ref<DatasourceInfo[]>([])
const dsId = ref<number | null>(null)
const { preferredEngine, defaultEngine } = usePreferredEngine()
const engines = ref<{ id: string; label: string; available: boolean }[]>([
  { id: 'polars', label: 'Polars', available: true },
])
// default = the user's favourite engine (Polars as fallback until the catalog is loaded;
// then onMounted checks the availability)
const engine = ref(preferredEngine.value)

const selectedDs = computed(() => datasources.value.find((d) => d.id === dsId.value) || null)
const dsOptions = computed(() =>
  datasources.value.filter((d) => d.key).map((d) => ({ value: d.id, label: d.name, hint: pathOf(d.project_id) })),
)
const engineOptions = computed(() =>
  engines.value.filter((e) => e.available).map((e) => ({ value: e.id, label: e.label })),
)

// ── on-the-fly transformations ────────────────────────────────────────────────
const FILTER_OPS = computed(() => [
  { value: 'eq', label: '=' }, { value: 'ne', label: '≠' },
  { value: 'gt', label: '>' }, { value: 'ge', label: '≥' },
  { value: 'lt', label: '<' }, { value: 'le', label: '≤' },
  { value: 'between', label: t('viewer.opBetween') },
  { value: 'contains', label: t('viewer.opContains') }, { value: 'starts_with', label: t('viewer.opStartsWith') },
  { value: 'in', label: t('viewer.opIn') }, { value: 'not_in', label: t('viewer.opNotIn') },
  { value: 'is_null', label: t('viewer.opIsNull') }, { value: 'is_not_null', label: t('viewer.opIsNotNull') },
])
const NO_VALUE = new Set(['is_null', 'is_not_null'])

// time column → native calendar input (as in the flow editor)
function temporalType(col: string): 'date' | 'datetime-local' | 'time' | 'text' {
  const dt = baseCols.value.find((c) => c.name === col)?.dtype || ''
  if (dt.startsWith('Datetime')) return 'datetime-local'
  if (dt === 'Date') return 'date'
  if (dt === 'Time') return 'time'
  return 'text'
}
const AGG_FUNCS = computed(() => [
  { value: 'sum', label: t('viewer.aggSum') }, { value: 'mean', label: t('viewer.aggMean') },
  { value: 'min', label: t('viewer.aggMin') }, { value: 'max', label: t('viewer.aggMax') },
  { value: 'count', label: t('viewer.aggCount') },
])

interface FilterRow { column: string; operator: string; value: string; value2: string }
interface CompRow { name: string; expr: string }

const filters = ref<FilterRow[]>([])
const computedFields = ref<CompRow[]>([])

// sidebar width (resizable)
const sidebarW = ref(300)
function startResize(e: MouseEvent) {
  const startX = e.clientX
  const startW = sidebarW.value
  const onMove = (ev: MouseEvent) => { sidebarW.value = Math.min(600, Math.max(220, startW + ev.clientX - startX)) }
  const onUp = () => {
    document.removeEventListener('mousemove', onMove)
    document.removeEventListener('mouseup', onUp)
    document.body.style.userSelect = ''
  }
  document.body.style.userSelect = 'none'
  document.addEventListener('mousemove', onMove)
  document.addEventListener('mouseup', onUp)
}
const pivotOn = ref(false)
const outline = ref(false) // do not repeat the values of the row dimensions (compact view)
const pivot = ref<{ index: string[]; on: string[]; values: string; func: string }>({
  index: [''], on: [''], values: '', func: 'sum',
})

const addFilter = () => filters.value.push({ column: '', operator: 'eq', value: '', value2: '' })
const addComputed = () => computedFields.value.push({ name: '', expr: '' })

// ── current schema (for the pickers) + table result ────────────────────
const baseCols = ref<ColumnInfo[]>([]) // columns after filters+fields (NO pivot) → chart & pivot config
const tableCols = ref<ColumnInfo[]>([]) // columns shown in the table (with any pivot)
const rows = ref<Record<string, any>[]>([])
// more rows than ROW_LIMIT: the table shows the first ones, and says so
const truncated = ref(false)
const view = ref<'table' | 'chart'>('table')
// the chart's settings, kept in a saved view
const chart = ref<ChartSettings | null>(null)
const loading = ref(false)
const error = ref('')
const ROW_LIMIT = 500

const colNames = computed(() => baseCols.value.map((c) => c.name))
// the chart's columns: the computed fields too, before the query that adds them answers —
// a saved view's chart on a computed field kept it, instead of resetting it
const chartCols = computed<ColumnInfo[]>(() => {
  const have = new Set(baseCols.value.map((c) => c.name))
  return [...baseCols.value, ...computedFields.value.filter((f) => f.name && !have.has(f.name)).map((f) => ({ name: f.name, dtype: '' }))]
})
const isNum = (name: string) => /Int|Float|Decimal/i.test(baseCols.value.find((c) => c.name === name)?.dtype || '')

function parseVal(v: string, col: string): any {
  if (v === '' || v == null) return v
  if (v.includes(',')) return v.split(',').map((s) => s.trim()) // lists for in/not_in
  if (isNum(col)) { const n = Number(v); return Number.isNaN(n) ? v : n }
  return v
}

// base ops = filters + computed fields (shared by table and chart)
const baseOps = computed<Operation[]>(() => {
  const ops: Operation[] = []
  for (const f of filters.value) {
    if (!f.column || !f.operator) continue
    const params: any = { column: f.column, operator: f.operator }
    if (f.operator === 'between') {
      if (f.value === '' || f.value2 === '') continue // both bounds are needed
      params.value = [parseVal(f.value, f.column), parseVal(f.value2, f.column)]
    } else if (!NO_VALUE.has(f.operator)) {
      params.value = parseVal(f.value, f.column)
    }
    ops.push({ type: 'filter', params })
  }
  const comp = computedFields.value.filter((c) => c.name.trim() && c.expr.trim())
  if (comp.length) ops.push({ type: 'compute', params: { columns: comp.map((c) => ({ name: c.name.trim(), expr: c.expr.trim() })) } })
  return ops
})

// query for ChartPanel: ALWAYS prepends the base ops (the chart aggregates the
// filtered+computed data)
async function chartQuery(ops: Operation[], limit?: number) {
  const ds = selectedDs.value
  if (!ds) return null
  return await previewSlots.preview({ bucket: ds.bucket, input_key: ds.key, operations: [...baseOps.value, ...ops], engine: engine.value, limit, no_cache: true, sort_keys: ds.sort_keys }, 'chart')
}

async function onPickDatasource(id: number | null) {
  dsId.value = id
  rows.value = []; error.value = ''
  const ds = selectedDs.value
  baseCols.value = ds?.columns ? ds.columns.map((c) => ({ name: c.name, dtype: c.dtype })) : []
  tableCols.value = baseCols.value
  if (ds) apply()
}

// the current view's operations: filters + computed fields + any pivot. Shared by table,
// chart and export.
function buildTableOps(): Operation[] {
  const ops = [...baseOps.value]
  const pIndex = pivot.value.index.filter(Boolean)
  const pOn = pivot.value.on.filter(Boolean)
  if (pivotOn.value && pOn.length && pivot.value.values && pIndex.length) {
    ops.push({ type: 'pivot', params: { index: pIndex, on: pOn, values: pivot.value.values, func: pivot.value.func } })
  }
  return ops
}

async function apply() {
  const ds = selectedDs.value
  if (!ds) return
  const seq = ++applySeq
  loading.value = true; error.value = ''
  try {
    const ops = buildTableOps()
    // `baseCols` are the columns WITHOUT pivot: the chart's and pivot's pickers need them.
    // Without a pivot they match the table's, so they are read from the result that arrives
    // anyway — and the schema query disappears. It cost as much as the other (a LIMIT 1
    // after a filter must scan the same: there is no index on parquet), that is it DOUBLED
    // every Apply.
    const withPivot = ops.length > baseOps.value.length
    if (withPivot) {
      // only here the table has columns other than the ones it is configured on
      const base = await previewSlots.preview({ bucket: ds.bucket, input_key: ds.key, operations: baseOps.value, engine: engine.value, limit: 1, no_cache: true, sort_keys: ds.sort_keys }, 'table')
      baseCols.value = base.columns
    }
    const res = await previewSlots.preview({ bucket: ds.bucket, input_key: ds.key, operations: ops, engine: engine.value, limit: ROW_LIMIT, no_cache: true, sort_keys: ds.sort_keys }, 'table')
    rows.value = res.rows
    truncated.value = !!res.truncated
    tableCols.value = res.columns
    if (!withPivot) baseCols.value = res.columns
  } catch (e) {
    // an Apply overtaken by a newer Apply is not an error to show: the new one governs state
    // and spinner
    if (isSuperseded(e)) return
    error.value = errMessage(e); rows.value = []
  } finally {
    if (applySeq === seq) loading.value = false
  }
}

// A formatter per numeric column, from the values in view: the same decimals on the
// whole column and integers without a thousands separator (see useFormat). Recomputed
// when the data changes, not at every key in the filters.
const isNumCol = (dtype: string) => /Int|Float|Decimal/i.test(dtype)
const colFormat = computed(() => {
  const m: Record<string, (v: unknown) => string> = {}
  for (const c of tableCols.value) {
    if (isNumCol(c.dtype)) m[c.name] = fmtx.columnFormatter(displayRows.value.map((r: any) => r[c.name]))
  }
  return m
})
const cell = (col: string, v: any) =>
  v === null || v === undefined ? '' : colFormat.value[col] ? colFormat.value[col](v) : typeof v === 'object' ? JSON.stringify(v) : String(v)

// export of the current view (csv/xlsx), written by a worker: Polars writes the file,
// whatever the engine chosen for the display.
const exporting = ref('')
async function exportView(format: 'csv' | 'xlsx') {
  const ds = selectedDs.value
  if (!ds) return
  exporting.value = format
  error.value = ''
  try {
    const filename = `${ds.name}_viewer.${format}`.toLowerCase().replace(/[^a-z0-9._-]+/g, '_')
    await api.exportData({ bucket: ds.bucket, input_key: ds.key, operations: buildTableOps(), format, filename, engine: engine.value })
  } catch (e) {
    error.value = errMessage(e)
  } finally {
    exporting.value = ''
  }
}

// rows to show: in the "outline" view the values of the row dimensions are NOT repeated
// — the prefix shared with the previous row is left empty (e.g. the region shows once,
// then only the provinces). No subtotal.
const displayRows = computed<Record<string, any>[]>(() => {
  if (!outline.value || !pivotOn.value) return rows.value
  const idx = pivot.value.index.filter(Boolean)
  if (idx.length < 2) return rows.value // with one dimension only there is nothing to compress
  const out: Record<string, any>[] = []
  let prev: Record<string, any> | null = null
  for (const r of rows.value) {
    const d = { ...r }
    if (prev) {
      // how many leading levels match the previous row
      let depth = 0
      while (depth < idx.length && String(prev[idx[depth]]) === String(r[idx[depth]])) depth++
      for (let j = 0; j < depth; j++) d[idx[j]] = '' // empties the shared prefix
    }
    out.push(d)
    prev = r
  }
  return out
})

// ── saved views ───────────────────────────────────────────────────────────
// A view is THIS configuration with a name, inside a folder. It holds a reference to the
// datasource and no rows: reopening it tomorrow reads the current snapshot again — it
// follows the data, it does not freeze it.
const savedViews = ref<SavedView[]>([])
const projects = ref<Project[]>([])
const currentViewId = ref<number | null>(null)
const saveOpen = ref(false)
const savePop = ref<HTMLElement | null>(null)
usePopover(saveOpen, savePop, () => { saveOpen.value = false })
const saveName = ref('')
const saveProjectId = ref<number | null>(null)
const saving = ref(false)

const savedViewOptions = computed(() =>
  savedViews.value.map((v) => ({ value: v.id, label: v.name, hint: pathOf(v.project_id) })),
)

function buildSpec(): SavedViewSpec {
  return {
    engine: engine.value,
    filters: filters.value.map((f) => ({ ...f })),
    computedFields: computedFields.value.map((c) => ({ ...c })),
    pivotOn: pivotOn.value,
    pivot: {
      index: [...pivot.value.index],
      on: [...pivot.value.on],
      values: pivot.value.values,
      func: pivot.value.func,
    },
    outline: outline.value,
    tab: view.value,
    chart: chart.value,
  }
}

// Defensive by construction: a view saved by an earlier version (or with one field less)
// must not break the page — every missing piece falls back on the starting value.
function applySpec(raw: string) {
  let s: Partial<SavedViewSpec> = {}
  try { s = JSON.parse(raw || '{}') } catch { s = {} }
  if (s.engine) engine.value = s.engine
  filters.value = Array.isArray(s.filters) ? s.filters.map((f) => ({ ...f })) : []
  computedFields.value = Array.isArray(s.computedFields) ? s.computedFields.map((c) => ({ ...c })) : []
  pivotOn.value = !!s.pivotOn
  pivot.value = {
    index: s.pivot?.index?.length ? [...s.pivot.index] : [''],
    on: s.pivot?.on?.length ? [...s.pivot.on] : [''],
    values: s.pivot?.values ?? '',
    func: s.pivot?.func ?? 'sum',
  }
  outline.value = !!s.outline
  // the tab and the chart: a view saved on its chart opened on the table, the chart reset
  view.value = s.tab === 'chart' ? 'chart' : 'table'
  chart.value = s.chart ?? null
}

async function openSavedView(id: number | null) {
  if (id == null) return
  currentViewId.value = id
  error.value = ''
  try {
    const v = await viewsApi.get(id)
    const ds = datasources.value.find((d) => d.id === v.datasource_id)
    if (!ds) {
      // the datasource was removed (or is no longer readable): the view stays, but we say so
      // instead of showing an empty table without explanation
      error.value = t('viewer.viewDatasourceMissing', { name: v.datasource_name ?? '—' })
      return
    }
    dsId.value = ds.id
    baseCols.value = ds.columns ? ds.columns.map((c) => ({ name: c.name, dtype: c.dtype })) : []
    tableCols.value = baseCols.value
    applySpec(v.spec)
    await apply()
  } catch (e) {
    error.value = errMessage(e)
  }
}

function openSaveDialog() {
  if (!selectedDs.value) return
  const current_ = savedViews.value.find((v) => v.id === currentViewId.value)
  saveName.value = current_?.name ?? ''
  saveProjectId.value = current_?.project_id ?? selectedDs.value.project_id ?? projects.value[0]?.id ?? null
  saveOpen.value = true
}

async function saveView() {
  const ds = selectedDs.value
  const viewName = saveName.value.trim()
  if (!ds || !viewName || saveProjectId.value == null) return
  saving.value = true
  try {
    const spec = JSON.stringify(buildSpec())
    // the same name in the same folder = I update that one, instead of making the user hit a
    // 409 they would not know how to solve
    const existing = savedViews.value.find(
      (v) => v.name === viewName && v.project_id === saveProjectId.value,
    )
    // ...but only when it is a view of this same datasource: updating another
    // datasource's view would give it columns that are not there
    if (existing && existing.datasource_id !== ds.id) {
      toast.error(t('viewer.viewNameTakenOtherDs', { name: viewName }))
      return
    }
    const savedOne = existing
      ? await viewsApi.update(existing.id, { spec, name: viewName })
      : await viewsApi.create(saveProjectId.value, { name: viewName, datasource_id: ds.id, spec })
    currentViewId.value = savedOne.id
    savedViews.value = await viewsApi.list()
    saveOpen.value = false
    toast.success(t('viewer.viewSaved', { name: viewName }))
  } catch (e) {
    toast.error(errMessage(e))
  } finally {
    saving.value = false
  }
}

onMounted(async () => {
  try { datasources.value = await dsApi.list() } catch (e) { error.value = errMessage(e) }
  try {
    engines.value = await api.engines()
    // the favourite may not be available: applies the robust fallback
    engine.value = defaultEngine(engines.value)
  } catch { /* fallback polars */ }
  // views are an extra: if they do not load, the Viewer stays usable
  try { savedViews.value = await viewsApi.list() } catch { savedViews.value = [] }
  try { projects.value = await projectsApi.list() } catch { projects.value = [] }
  // direct opening from a link (?view=<id>): that is how the folder leads here. It goes
  // AFTER loading the datasources, because the view must find one.
  const linked = Number(useRoute().query.view)
  if (Number.isInteger(linked) && linked > 0) await openSavedView(linked)
})
</script>

<template>
  <AppShell fluid>
   <div class="viewer">
    <div class="page-head">
      <h1><PieChart :size="18" /> {{ $t('viewer.pageTitle') }}</h1>
      <div class="head-actions">
        <label class="hl"><Bookmark :size="13" /> {{ $t('viewer.savedViewsLabel') }}</label>
        <Select
          :model-value="currentViewId"
          :options="savedViewOptions"
          searchable
          :placeholder="$t('viewer.savedViewsPlaceholder')"
          class="vwsel"
          @update:model-value="openSavedView"
        />
        <div class="savewrap">
          <button class="btn-link" :disabled="!selectedDs" :title="$t('viewer.saveViewTitle')" @click="openSaveDialog">
            <Save :size="14" /> {{ $t('viewer.saveViewButton') }}
          </button>
          <div v-if="saveOpen" class="menu-backdrop" @click="saveOpen = false" />
          <div v-if="saveOpen" ref="savePop" class="savepop">
            <label class="lbl">{{ $t('viewer.viewNameLabel') }}</label>
            <input v-model="saveName" type="text" :placeholder="$t('viewer.viewNamePlaceholder')" @keyup.enter="saveView" />
            <label class="lbl">{{ $t('viewer.viewFolderLabel') }}</label>
            <Select
              v-model="saveProjectId"
              :options="projects.map((p) => ({ value: p.id, label: p.name }))"
              :placeholder="$t('viewer.viewFolderPlaceholder')"
            />
            <p class="muted small">{{ $t('viewer.saveViewHint') }}</p>
            <button class="primary" :disabled="!saveName.trim() || saveProjectId == null || saving" @click="saveView">
              {{ saving ? '…' : $t('viewer.saveViewConfirm') }}
            </button>
          </div>
        </div>
        <label class="hl"><Cpu :size="13" /> {{ $t('viewer.engineLabel') }}</label>
        <Select v-model="engine" :options="engineOptions" class="engsel" />
        <Select
          :model-value="dsId"
          :options="dsOptions"
          searchable
          :placeholder="$t('viewer.datasourcePlaceholder')"
          :aria-label="$t('viewer.datasourcePlaceholder')"
          class="dssel"
          @update:model-value="onPickDatasource"
        />
        <ContractBadge v-if="selectedDs" :contract="selectedDs.contract" :size="16" />
      </div>
    </div>

    <p v-if="!selectedDs" class="muted empty">{{ $t('viewer.emptyStateHint') }}</p>

    <div v-else class="body" :style="{ gridTemplateColumns: `${sidebarW}px 6px 1fr` }">
      <!-- panel of the on-the-fly transformations -->
      <aside class="cfg">
        <div class="cfg-sec">
          <div class="cfg-head"><Filter :size="13" /> {{ $t('viewer.filtersTitle') }} <button class="add" @click="addFilter"><Plus :size="13" /></button></div>
          <p v-if="!filters.length" class="muted small">{{ $t('viewer.noFilters') }}</p>
          <div v-for="(f, i) in filters" :key="i" class="frow">
            <Select v-model="f.column" :options="colNames" :placeholder="$t('viewer.columnPlaceholder')" class="fc" />
            <Select v-model="f.operator" :options="FILTER_OPS" class="fo" />
            <template v-if="f.operator === 'between'">
              <input v-model="f.value" :type="temporalType(f.column)" class="fv" :placeholder="$t('viewer.fromPlaceholder')" />
              <input v-model="f.value2" :type="temporalType(f.column)" class="fv" :placeholder="$t('viewer.toPlaceholder')" />
            </template>
            <input
              v-else-if="!NO_VALUE.has(f.operator)"
              v-model="f.value"
              :type="temporalType(f.column)"
              class="fv"
              :placeholder="$t('viewer.valuePlaceholder')"
            />
            <button class="rm" @click="filters.splice(i, 1)"><X :size="13" /></button>
          </div>
        </div>

        <div class="cfg-sec">
          <div class="cfg-head"><Sigma :size="13" /> {{ $t('viewer.computedFieldsTitle') }} <button class="add" @click="addComputed"><Plus :size="13" /></button></div>
          <p class="muted small">{{ $t('viewer.computedFieldsHint', { engine }) }}</p>
          <div v-for="(c, i) in computedFields" :key="i" class="crow">
            <div class="crow-top">
              <input v-model="c.name" type="text" class="cn" :placeholder="$t('viewer.fieldNamePlaceholder')" />
              <button class="rm" @click="computedFields.splice(i, 1)"><X :size="13" /></button>
            </div>
            <textarea v-model="c.expr" class="ce" rows="2" :placeholder="$t('viewer.exprPlaceholder')" />
          </div>
        </div>

        <div class="cfg-sec">
          <div class="cfg-head">
            <Rows3 :size="13" /> {{ $t('viewer.pivotTitle') }}
            <label class="toggle"><input v-model="pivotOn" type="checkbox" /> {{ $t('viewer.pivotEnable') }}</label>
          </div>
          <template v-if="pivotOn">
            <label class="lbl">{{ $t('viewer.pivotRowsLabel') }}</label>
            <div v-for="(_, i) in pivot.index" :key="'ix' + i" class="dimrow">
              <Select v-model="pivot.index[i]" :options="colNames" :placeholder="$t('viewer.columnPlaceholder')" class="full" />
              <button class="rm" :disabled="pivot.index.length === 1" @click="pivot.index.splice(i, 1)"><X :size="13" /></button>
            </div>
            <button class="add-dim" @click="pivot.index.push('')"><Plus :size="12" /> {{ $t('viewer.addRow') }}</button>

            <label class="lbl">{{ $t('viewer.pivotColumnsLabel') }}</label>
            <div v-for="(_, i) in pivot.on" :key="'on' + i" class="dimrow">
              <Select v-model="pivot.on[i]" :options="colNames" :placeholder="$t('viewer.columnPlaceholder')" class="full" />
              <button class="rm" :disabled="pivot.on.length === 1" @click="pivot.on.splice(i, 1)"><X :size="13" /></button>
            </div>
            <button class="add-dim" @click="pivot.on.push('')"><Plus :size="12" /> {{ $t('viewer.addColumn') }}</button>

            <label class="lbl">{{ $t('viewer.valuesLabel') }}</label>
            <Select v-model="pivot.values" :options="colNames" :placeholder="$t('viewer.valueColumnPlaceholder')" class="full" />
            <label class="lbl">{{ $t('viewer.functionLabel') }}</label>
            <Select v-model="pivot.func" :options="AGG_FUNCS" class="full" />
            <label class="checkline" :title="$t('viewer.outlineHint')">
              <input v-model="outline" type="checkbox" /> {{ $t('viewer.outlineToggle') }}
            </label>
          </template>
        </div>
      </aside>

      <!-- the sidebar's resize handle -->
      <div class="resizer" :title="$t('viewer.resizeHint')" @mousedown="startResize" />

      <!-- result area: table or chart -->
      <section class="result">
        <div class="rtabs">
          <button :class="{ active: view === 'table' }" @click="view = 'table'"><Table2 :size="14" /> {{ $t('viewer.tableTab') }}</button>
          <button :class="{ active: view === 'chart' }" @click="view = 'chart'"><PieChart :size="14" /> {{ $t('viewer.chartTab') }}</button>
          <button class="primary apply" :disabled="!selectedDs || loading" @click="apply"><Play :size="14" /> {{ $t('viewer.applyButton') }}</button>
          <span v-if="loading" class="muted">{{ $t('viewer.computing') }}</span>
          <span v-else-if="error" class="err">{{ error }}</span>
          <span class="rmeta muted">{{ truncated ? $t('viewer.rowsColsMetaFirst', { n: rows.length, m: tableCols.length }) : $t('viewer.rowsColsMeta', { n: rows.length, m: tableCols.length }) }}</span>
          <div class="expbtns">
            <button :disabled="!rows.length || !!exporting" :title="$t('viewer.exportCsvTitle')" @click="exportView('csv')">
              <Download :size="13" /> {{ exporting === 'csv' ? '…' : 'CSV' }}
            </button>
            <button :disabled="!rows.length || !!exporting" :title="$t('viewer.exportExcelTitle')" @click="exportView('xlsx')">
              <FileSpreadsheet :size="13" /> {{ exporting === 'xlsx' ? '…' : 'Excel' }}
            </button>
          </div>
        </div>

        <div v-show="view === 'table'" class="tablewrap">
          <div v-if="loading" class="skeleton">
            <div class="sk-row sk-head"><span v-for="m in 7" :key="m" class="sk-cell" /></div>
            <div v-for="n in 14" :key="n" class="sk-row"><span v-for="m in 7" :key="m" class="sk-cell" /></div>
          </div>
          <table v-else-if="rows.length" class="dt">
            <thead>
              <tr>
                <th v-for="c in tableCols" :key="c.name" :class="{ num: isNumCol(c.dtype) }">
                  {{ c.name }}<span class="dtype">{{ c.dtype }}</span>
                </th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(row, i) in displayRows" :key="i">
                <td v-for="c in tableCols" :key="c.name" :class="{ num: isNumCol(c.dtype) }">{{ cell(c.name, row[c.name]) }}</td>
              </tr>
            </tbody>
          </table>
          <p v-else-if="!loading" class="muted empty">{{ $t('viewer.noDataHint') }}</p>
        </div>

        <div v-show="view === 'chart'" class="chartwrap">
          <ChartPanel :columns="chartCols" :query="chartQuery" :settings="chart" @update:settings="chart = $event" />
        </div>
      </section>
    </div>
   </div>
  </AppShell>
</template>

<style scoped src="~/assets/listpage.css"></style>
<style scoped>
.viewer { display: flex; flex-direction: column; flex: 1; min-height: 0; }
.head-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.vwsel { width: 200px; }
/* save box: the same mechanics as the "new flow" menu — a backdrop catching the click
   outside, and the panel on top. `flex-shrink: 0` + `nowrap` are the fix for the button
   twice as tall: the bar stays on ONE line, and the Selects next to it give up a few
   pixels (their text has the ellipsis already), not the button's label wrapping. */
.savewrap { position: relative; flex-shrink: 0; }
.savewrap .btn-link { white-space: nowrap; }
.menu-backdrop { position: fixed; inset: 0; z-index: 20; }
.savepop {
  position: absolute;
  top: calc(100% + 6px);
  right: 0;
  z-index: 21;
  width: 260px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 12px;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  box-shadow: 0 8px 24px rgb(0 0 0 / 0.18);
}
.savepop .lbl { font-size: 11px; color: var(--muted); }
.savepop input { width: 100%; }
.hl { display: inline-flex; align-items: center; gap: 4px; font-size: 12px; color: var(--muted); }
.engsel { width: 140px; }
.dssel { width: 260px; }
.empty { padding: 24px; }
.body { display: grid; grid-template-columns: 300px 6px 1fr; gap: 0; flex: 1; min-height: 0; }
.resizer { cursor: col-resize; background: transparent; position: relative; }
.resizer::before { content: ''; position: absolute; inset: 0 2px; border-radius: 2px; background: var(--border); transition: background 0.15s; }
.resizer:hover::before { background: var(--accent); }

.cfg { overflow-y: auto; display: flex; flex-direction: column; gap: 14px; padding: 0 10px 0 2px; }
.cfg-sec { border: 1px solid var(--border); border-radius: 10px; padding: 10px; }
.cfg-head { display: flex; align-items: center; gap: 6px; font-weight: 600; font-size: 13px; margin-bottom: 8px; }
.cfg-head .add { margin-left: auto; padding: 2px 6px; }
.toggle { margin-left: auto; font-size: 11px; color: var(--muted); display: inline-flex; align-items: center; gap: 4px; font-weight: 400; }
.small { font-size: 11px; }
.lbl { display: block; font-size: 11px; color: var(--muted); margin: 6px 0 3px; }
.full { width: 100%; margin-bottom: 2px; }
.frow { display: flex; align-items: center; gap: 4px; margin-bottom: 6px; flex-wrap: wrap; }
.crow { display: flex; flex-direction: column; gap: 4px; margin-bottom: 12px; }
.crow-top { display: flex; align-items: center; gap: 5px; }
.fc { flex: 1 1 100%; min-width: 0; } .fo { width: 104px; flex-shrink: 0; } .fv { flex: 1; min-width: 0; }
.crow .cn { flex: 1; min-width: 0; }
.crow .ce {
  width: 100%;
  min-height: 52px;
  resize: vertical;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
  line-height: 1.4;
}
.rm { padding: 5px 6px; flex-shrink: 0; }
.dimrow { display: flex; align-items: center; gap: 4px; margin-bottom: 4px; }
.dimrow .full { flex: 1; min-width: 0; }
.add-dim { font-size: 11px; padding: 3px 8px; margin: 2px 0 4px; }
.checkline { display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--text); margin-top: 8px; cursor: pointer; }

.result { display: flex; flex-direction: column; min-height: 0; border: 1px solid var(--border); border-radius: 10px; overflow: hidden; }
.rtabs { display: flex; align-items: center; gap: 6px; padding: 8px 10px; border-bottom: 1px solid var(--border); }
.rtabs button { padding: 5px 10px; }
.rtabs button.active { border-color: var(--accent); background: color-mix(in srgb, var(--accent) 12%, transparent); }
.rtabs .apply { margin-left: 8px; }
.rmeta { margin-left: auto; font-size: 12px; }
.expbtns { display: flex; gap: 4px; }
.expbtns button { padding: 5px 9px; }
.err { color: var(--danger); font-size: 12px; }
.tablewrap { flex: 1; overflow: auto; min-height: 0; }
.chartwrap { flex: 1; min-height: 0; padding: 12px; }
.dt { border-collapse: collapse; font-size: 12px; width: max-content; min-width: 100%; }
.dt th, .dt td {
  border-bottom: 1px solid var(--border-soft);
  border-right: 1px solid var(--border-soft); /* column separator */
  padding: 5px 12px;
  text-align: left;
  white-space: nowrap;
  vertical-align: top;
}
.dt th:last-child, .dt td:last-child { border-right: none; }
.dt th { position: sticky; top: 0; background: var(--panel); z-index: 1; font-weight: 600; vertical-align: bottom; }
.dt .dtype { display: block; font-size: 9px; color: var(--muted); font-weight: 400; text-transform: lowercase; }
/* the numbers on the right with tabular digits: unit under unit, as the login promises */
.dt td.num, .dt th.num { text-align: right; }
.dt td.num { font-variant-numeric: tabular-nums; }
.dt tbody tr:hover { background: var(--panel-2); }

/* loading skeleton (shimmer) */
.skeleton { padding: 12px 14px; }
.sk-row { display: flex; gap: 12px; margin-bottom: 11px; }
.sk-row.sk-head .sk-cell { height: 20px; opacity: 0.85; }
.sk-cell {
  flex: 1;
  height: 13px;
  border-radius: 4px;
  background: linear-gradient(90deg, var(--panel-2) 25%, var(--border) 37%, var(--panel-2) 63%);
  background-size: 400% 100%;
  animation: sk-shimmer 1.4s ease infinite;
}
@keyframes sk-shimmer { 0% { background-position: 100% 50%; } 100% { background-position: 0 50%; } }
</style>
