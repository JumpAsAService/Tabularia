<script setup lang="ts">
// Viewer: a READ-ONLY mini BI tool on a Tabularia Datasource. It builds filters +
// computed fields + pivot on the fly and runs them with the chosen engine through the
// PREVIEW (which reads the datasource's snapshot, it does NOT change it). The table and
// the charts (ChartPanel, aggregation over the whole dataset) run on the same
// transformed data. Nothing is saved.
//
// The table starts EMPTY and is built as a cross table, by dragging the datasource's fields
// onto three areas, as the matrix of the report designer: ROWS at the left (the row
// headings), COLUMNS across the top (a field spread into columns), MEASURES in the middle
// (a field aggregated, with its function). Rows alone are the rows as they are, with those
// columns; with measures the engine groups; with columns it pivots. Fields go to the filters
// and to the chart the same way. Until a field is placed nothing is asked of the engine, and
// then only what the table shows is read (composables/viewerColumns.ts holds the rules; the
// names and types come from the datasource's metadata, never from a query).
import { DEFAULT_ENGINE_OPTION } from '~/composables/useEngine'
import { ref, computed, watch, nextTick, onBeforeUnmount } from 'vue'
import { useI18n } from 'vue-i18n'
import { useFormat } from '~/composables/useFormat'
import { usePopover } from '~/composables/usePopover'
import {
  PieChart, Table2, Filter, Sigma, Plus, X, Play, Cpu, Rows3, Download, FileSpreadsheet, Bookmark, Save, RotateCw,
  Hash, CaseSensitive, Calendar, ToggleLeft, Braces, SquareFunction, Check, ListPlus, Search, ChartColumn, Columns3, Ellipsis,
} from 'lucide-vue-next'
import { useApi, errMessage, type Operation, type ColumnInfo, type PreviewResult, type ProfileResult, type TopValuesResult, type HistogramResult } from '~/composables/useApi'
import DataGrid, { type DetailAsk } from '~/components/ui/DataGrid.vue'
import GridKeys from '~/components/ui/GridKeys.vue'
import { rowsPhrase, shortDtype, type RowCount } from '~/composables/previewGrid'
import { usePreviewSlots, isSuperseded } from '~/composables/usePreviewSlots'
import { useDatasources, type DatasourceInfo } from '~/composables/useDatasources'
import ContractBadge from '~/components/ui/ContractBadge.vue'
import { useSavedViews, type ChartSettings, type SavedView, type SavedViewSpec } from '~/composables/useSavedViews'
import { useProjects, type Project } from '~/composables/useProjects'
import { useFolderPaths } from '~/composables/useFolderPaths'
import ChartPanel from '~/components/ui/ChartPanel.vue'
import FieldShelf, { type ShelfChip } from '~/components/viewer/FieldShelf.vue'
import { draggedField, endFieldDrag, fieldDragOf, fieldDragOutside, startFieldDrag, type FieldDragPayload } from '~/composables/fieldDrag'
import {
  baseKeyOf, chartSlotOf, countNeeded, defaultFunc, fieldOffShelves, keepKnownShelves, MEASURE_FUNCS, measureAliases,
  placeOnShelf, profileMissing, removeFromShelf, rowsPlan, selectStep, shelfOps, shelvesOfSpec, shiftColumn,
  shownColumns, specOfShelves, tableMode, viewerFields,
  type Measure, type RowsState, type ShelfArea, type ShelfFrom, type Shelves, type ViewerField,
} from '~/composables/viewerColumns'
import { keyNames, platformIsMac } from '~/composables/appKeys'
import { useKeyGroups } from '~/composables/useKeyHelp'

const api = useApi()
// Table and chart on two slots: an Apply (or a touch to the chart) knocks down the
// previous one instead of queueing behind it — see usePreviewSlots.
const previewSlots = usePreviewSlots('vw')
onUnmounted(() => previewSlots.cancelAll())
let applySeq = 0 // only the last request for rows turns the spinner off
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
const engines = ref<{ id: string; label: string; available: boolean }[]>([DEFAULT_ENGINE_OPTION])
// default = the user's favourite engine (DuckDB as fallback until the catalog is loaded;
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
// a measure's functions: the ones the engine's group by and pivot share
const FUNC_LABELS = computed<Record<string, string>>(() => ({
  sum: t('viewer.aggSum'), mean: t('viewer.aggMean'), min: t('viewer.aggMin'), max: t('viewer.aggMax'),
  count: t('viewer.aggCount'), n_unique: t('viewer.aggDistinct'),
}))
const measureFuncs = computed(() => MEASURE_FUNCS.map((f) => ({ value: f as string, label: FUNC_LABELS.value[f] })))

interface FilterRow { column: string; operator: string; value: string; value2: string }
interface CompRow { name: string; expr: string }

const filters = ref<FilterRow[]>([])
const computedFields = ref<CompRow[]>([])

// sidebar width (resizable)
const sidebarW = ref(280)
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
const outline = ref(false) // do not repeat the values of the row dimensions (compact view)

const addFilter = () => filters.value.push({ column: '', operator: 'eq', value: '', value2: '' })
const addComputed = () => computedFields.value.push({ name: '', expr: '' })

// ── the fields, the table's columns, the rows on screen ──────────────────────────
// the types the results said of the computed fields (by name and expression), and of every
// column a result held
const computedTypes = ref<Record<string, string>>({})
const resultTypes = ref<Record<string, string>>({})
const fields = computed<ViewerField[]>(() => {
  const ds = selectedDs.value
  if (!ds) return []
  const known: Record<string, string> = {}
  for (const c of computedFields.value) {
    const dtype = computedTypes.value[`${c.name.trim()}\u0001${c.expr.trim()}`]
    if (dtype) known[c.name.trim()] = dtype
  }
  return viewerFields(ds.columns ?? [], ds.column_descriptions, computedFields.value, known)
})
const fieldNames = computed(() => fields.value.map((f) => f.name))
// the columns the chart's pickers offer: every field, shown in the table or not
const baseCols = computed<ColumnInfo[]>(() => fields.value.map((f) => ({ name: f.name, dtype: f.dtype })))
// the computed fields whose type no result said yet: asked with the rows, not shown
const typeless = computed(() => fields.value.filter((f) => f.computed && !f.dtype).map((f) => f.name))
// The three areas the table is built on, empty at the start. `chosen` is ROWS: alone, its
// fields are the table's columns (the rows as they are); `colFields` is COLUMNS, `measures`
// MEASURES.
const chosen = ref<string[]>([])
const colFields = ref<string[]>([])
const measures = ref<Measure[]>([])
const shelves = computed<Shelves>(() => ({ rows: chosen.value, cols: colFields.value, measures: measures.value }))
const mode = computed(() => tableMode(shelves.value))
function setShelves(s: Shelves) {
  chosen.value = s.rows
  colFields.value = s.cols
  measures.value = s.measures
}
const shownCols = computed<ColumnInfo[]>(() => shownColumns(chosen.value, fields.value, resultTypes.value))
// an aggregated table (a group by, totals, a cross table) has its own columns
const tableCols = ref<ColumnInfo[]>([])
const pivotShown = ref(false)
const rows = ref<Record<string, any>[]>([])
// the columns the rows on screen hold (more than the shown ones after a removal: putting
// one back asks nothing), and what they were computed on (null: no rows)
const held = ref<string[]>([])
const heldKey = ref<string | null>(null)
// more rows than ROW_LIMIT: the table shows the first ones, and says so
const truncated = ref(false)
const view = ref<'table' | 'chart'>('table')
// the chart's settings, kept in a saved view
const chart = ref<ChartSettings | null>(null)
// the chart is made the first time its tab is shown: a table never asks the chart's query
const chartSeen = ref(false)
const chartRef = ref<InstanceType<typeof ChartPanel> | null>(null)
watch(view, (v) => { if (v === 'chart') chartSeen.value = true }, { immediate: true })
const loading = ref(false)
// the rows on screen are of other filters: dimmed until the new ones come
const replacing = ref(false)
const error = ref('')
const ROW_LIMIT = 500

const colNames = computed(() => fieldNames.value)
// the filters come before the computed fields: they read the datasource's own columns
const filterCols = computed(() => fields.value.filter((f) => !f.computed).map((f) => f.name))
const chartCols = baseCols
const isNum = (name: string) => /Int|Float|Decimal/i.test(baseCols.value.find((c) => c.name === name)?.dtype || '')
// a field renamed or removed (a computed field edited, another datasource): it leaves the table
watch(fieldNames, (names) => {
  const kept = keepKnownShelves(shelves.value, names)
  if (kept.rows.length !== chosen.value.length || kept.cols.length !== colFields.value.length || kept.measures.length !== measures.value.length) setShelves(kept)
})

function parseVal(v: string, col: string, operator = 'in'): any {
  if (v === '' || v == null) return v
  // lists for in/not_in only: a text with a comma compared with = is one value
  if ((operator === 'in' || operator === 'not_in') && v.includes(',')) return v.split(',').map((s) => s.trim())
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
      params.value = [parseVal(f.value, f.column, f.operator), parseVal(f.value2, f.column, f.operator)]
    } else if (!NO_VALUE.has(f.operator)) {
      // a filter still waiting for its value (a field just dropped on the filters) filters
      // nothing: before, it asked for the rows equal to the empty text, that is none
      if (f.value === '') continue
      params.value = parseVal(f.value, f.column, f.operator)
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

function dropRows() {
  rows.value = []
  held.value = []
  heldKey.value = null
  truncated.value = false
  pivotShown.value = false
  tableCols.value = []
  rowCount.value = { state: 'none' }
  countedKey = null
}
async function onPickDatasource(id: number | null) {
  if (settleTimer) { clearTimeout(settleTimer); settleTimer = null }
  applySeq++
  previewSlots.cancelAll()
  dsId.value = id
  error.value = ''
  loading.value = false
  stopProfile()
  dropRows()
  resultTypes.value = {}
  // the three areas empty: nothing is asked until a field is put on one
  setShelves({ rows: [], cols: [], measures: [] })
}

// the steps that aggregate (a group by, totals, the pivot of a cross table): none for the
// rows as they are. Shared by the table and the export.
const aggOps = computed<Operation[]>(() => shelfOps(shelves.value))
// what the rows depend on besides their columns (the same key = the same rows and count)
const baseKey = computed(() => baseKeyOf(selectedDs.value, engine.value, baseOps.value))
const rowsState = (): RowsState =>
  ({ chosen: chosen.value, held: held.value, heldKey: heldKey.value, baseKey: baseKey.value, typeless: typeless.value })

/** What a result said of its columns' types (the computed fields' are known from here only). */
function learnTypes(columns: ColumnInfo[]) {
  const types = { ...resultTypes.value }
  const made = { ...computedTypes.value }
  for (const c of columns) {
    types[c.name] = c.dtype
    const f = computedFields.value.find((x) => x.name.trim() === c.name)
    if (f) made[`${c.name}\u0001${f.expr.trim()}`] = c.dtype
  }
  resultTypes.value = types
  computedTypes.value = made
}

/** «Apply»: the view computed again. An aggregated table, or the rows of the Rows' fields
 *  (nothing while the areas are empty, or Columns waits for its measure); the chart too,
 *  when it is the one on screen. */
async function apply() {
  if (!selectedDs.value) return
  if (settleTimer) { clearTimeout(settleTimer); settleTimer = null }
  if (view.value === 'chart') void chartRef.value?.refresh()
  if (aggOps.value.length) return await applyAgg(aggOps.value)
  if (mode.value !== 'detail') {
    // nothing to show: what was on screen goes, and nothing is asked
    applySeq++; previewSlots.cancel('table'); stopProfile(); dropRows(); loading.value = false
    if (typeless.value.length) void learnTypesOnly(selectedDs.value, baseOps.value, [...typeless.value], engine.value)
    return
  }
  pivotShown.value = false
  await fetchRows('apply')
}

// Columns added one after the other (a double click on a field, then on the next) do not ask
// once each: the first goes at once, the ones that follow within BURST_MS wait SETTLE_MS for
// the next, their names in the table meanwhile. Moving and removing a column never come
// here with something to ask.
const SETTLE_MS = 450
const BURST_MS = 1200
let settleTimer: ReturnType<typeof setTimeout> | null = null
let lastAskAt = -Infinity
onBeforeUnmount(() => { if (settleTimer) clearTimeout(settleTimer) })
function columnsChanged() {
  if (!selectedDs.value) return
  if (mode.value !== 'detail') {
    // an aggregated table is asked whole at every change of its areas (the engine
    // aggregates); an empty one, or Columns still without a measure, asks nothing
    if (settleTimer) { clearTimeout(settleTimer); settleTimer = null }
    if (!aggOps.value.length) { void apply(); return }
    loading.value = true
    const now = performance.now()
    const wait = now - lastAskAt < BURST_MS ? SETTLE_MS : 0
    lastAskAt = now
    if (!wait) void apply()
    else settleTimer = setTimeout(() => { settleTimer = null; void apply() }, wait)
    return
  }
  // back from an aggregated table: its rows are not these
  if (pivotShown.value) { stopProfile(); dropRows() }
  if (!rowsPlan(rowsState(), 'columns').columns) {
    // every shown column is on screen: at most the profile of one put back
    if (heldKey.value !== null && chosen.value.length) void profileShown(applySeq)
    return
  }
  loading.value = true
  if (settleTimer) clearTimeout(settleTimer)
  const now = performance.now()
  const wait = now - lastAskAt < BURST_MS ? SETTLE_MS : 0
  lastAskAt = now
  if (!wait) void fetchRows('columns')
  else settleTimer = setTimeout(() => { settleTimer = null; void fetchRows('columns') }, wait)
}

/** The rows of the chosen columns: one request, a `select` at the end of the chain so that
 *  the engine reads those columns only (and the filters' and computed fields' own). */
async function fetchRows(why: 'columns' | 'apply') {
  const ds = selectedDs.value
  if (!ds) return
  if (settleTimer) { clearTimeout(settleTimer); settleTimer = null }
  const key = baseKey.value
  const ops0 = baseOps.value
  const eng = engine.value
  const plan = rowsPlan(rowsState(), why)
  if (!plan.columns || plan.onlyTypes) {
    // an empty table: the rows of other filters are not kept for the next field
    if (why === 'apply') { applySeq++; previewSlots.cancel('table'); stopProfile(); dropRows() }
    if (!previewSlots.busy('table')) loading.value = false
    if (plan.columns) void learnTypesOnly(ds, ops0, plan.columns, eng)
    return
  }
  const seq = ++applySeq
  lastAskAt = performance.now()
  const sameRows = heldKey.value === key && !plan.replaces
  if (!sameRows) {
    if (previewSlots.busy('count')) previewSlots.stop('count')
    stopProfile()
    // the rows on screen keep their «of N» while the new ones are computed (they are dimmed);
    // a count of theirs still going is dropped
    if (rowCount.value.state === 'counting') rowCount.value = { state: 'none' }
    countError.value = ''
  }
  loading.value = true; replacing.value = plan.replaces; error.value = ''
  try {
    const res = await previewSlots.preview({
      bucket: ds.bucket, input_key: ds.key, operations: [...ops0, selectStep(plan.columns)], engine: eng,
      limit: ROW_LIMIT, no_cache: true, sort_keys: ds.sort_keys,
    }, 'table')
    if (seq !== applySeq) return
    learnTypes(res.columns)
    pivotShown.value = false
    rows.value = res.rows
    held.value = res.columns.map((c) => c.name)
    heldKey.value = key
    truncated.value = !!res.truncated
    viewOps = { ds, ops: ops0, key, engine: eng }
    if (!res.truncated) {
      rowCount.value = { state: 'done', rows: res.rows.length, over: false }
      countedKey = key
      void profileShown(seq)
    } else if (countNeeded(countedKey, key, why) || rowCount.value.state !== 'done') {
      // the real count, after the rows and apart from them: once per filters, not per column
      void countRows(ds, ops0, seq, key, eng)
    } else {
      void profileShown(seq)
    }
  } catch (e) {
    // rows overtaken by a newer request are not an error to show: the new one governs state
    // and spinner
    if (isSuperseded(e) || seq !== applySeq) return
    error.value = errMessage(e)
    if (why === 'apply') { stopProfile(); dropRows() }
    // a column that could not be added leaves: the table keeps the ones it had
    else if (mode.value === 'detail') chosen.value = chosen.value.filter((c) => held.value.includes(c))
  } finally {
    if (applySeq === seq) { loading.value = false; replacing.value = false }
  }
}

/** The types of the computed fields, when the table shows no column to bring them: one row. */
async function learnTypesOnly(ds: DatasourceInfo, ops0: Operation[], columns: string[], eng: string) {
  try {
    const res = await previewSlots.preview({
      bucket: ds.bucket, input_key: ds.key, operations: [...ops0, selectStep(columns)], engine: eng, limit: 1,
      no_cache: true, sort_keys: ds.sort_keys,
    }, 'types')
    learnTypes(res.columns)
  } catch (e) {
    if (!isSuperseded(e)) error.value = errMessage(e)
  }
}

/** An aggregated table (a group by, totals, a cross table): one request, the engine
 *  aggregates and the browser gets the result's first rows. */
async function applyAgg(steps: Operation[]) {
  const ds = selectedDs.value
  if (!ds) return
  if (settleTimer) { clearTimeout(settleTimer); settleTimer = null }
  const seq = ++applySeq
  lastAskAt = performance.now()
  const eng = engine.value
  const ops = [...baseOps.value, ...steps]
  const key = `pivot\u0001${baseKeyOf(ds, eng, ops)}`
  if (previewSlots.busy('count')) previewSlots.stop('count')
  stopProfile()
  if (rowCount.value.state === 'counting') rowCount.value = { state: 'none' }
  countError.value = ''
  loading.value = true; replacing.value = true; error.value = ''
  if (typeless.value.length) void learnTypesOnly(ds, baseOps.value, [...typeless.value], eng)
  try {
    const res = await previewSlots.preview({ bucket: ds.bucket, input_key: ds.key, operations: ops, engine: eng, limit: ROW_LIMIT, no_cache: true, sort_keys: ds.sort_keys }, 'table')
    if (seq !== applySeq) return
    dropRows()
    rows.value = res.rows
    truncated.value = !!res.truncated
    tableCols.value = res.columns
    pivotShown.value = true
    viewOps = { ds, ops, key, engine: eng }
    // no profile by itself: on an aggregate it would be the aggregation computed a second
    // time; it is asked when a column is opened (`profileWhole`)
    if (res.truncated) void countRows(ds, ops, seq, key, eng)
    else rowCount.value = { state: 'done', rows: res.rows.length, over: false }
  } catch (e) {
    if (isSuperseded(e) || seq !== applySeq) return
    error.value = errMessage(e); stopProfile(); dropRows()
  } finally {
    if (applySeq === seq) { loading.value = false; replacing.value = false }
  }
}

// How many rows the view really has: read from the datasource's footer when nothing filters
// it, counted (up to a cap) otherwise. The count belongs to the filters, not to the columns:
// adding a column never asks it again.
const rowCount = ref<RowCount>({ state: 'none' })
const countError = ref('') // why the rows were not counted
let countedKey: string | null = null
let countAsk: { ds: DatasourceInfo; ops: Operation[]; key: string; engine: string } | null = null
async function countRows(ds: DatasourceInfo, ops: Operation[], seq: number, key: string, eng: string) {
  countAsk = { ds, ops, key, engine: eng }
  countError.value = ''
  rowCount.value = { state: 'counting' }
  try {
    const c = await previewSlots.count({ bucket: ds.bucket, input_key: ds.key, operations: ops, engine: eng, no_cache: true }, 'count')
    if (seq === applySeq) { rowCount.value = { state: 'done', rows: c.rows, over: c.over }; countedKey = key }
  } catch (e) {
    if (seq !== applySeq || isSuperseded(e)) return
    const status = (e as any)?.response?.status ?? (e as any)?.statusCode
    countError.value = whyNot(e, 'count')
    rowCount.value = { state: 'failed', slow: status === 504 }
  }
  // the columns' profile after the count, never beside it (an aggregate's when a column is opened)
  if (seq === applySeq && !pivotShown.value) void profileShown(seq)
}

/** «Try again» on a count that failed. */
function countRetry() {
  if (!countAsk || loading.value) return
  void countRows(countAsk.ds, countAsk.ops, applySeq, countAsk.key, countAsk.engine)
}
/** Why a count or a profile was not made, in words. */
function whyNot(e: unknown, what: 'count' | 'profile'): string {
  const status = (e as any)?.response?.status ?? (e as any)?.statusCode
  if (status === 504) return t(what === 'count' ? 'dataGrid.countSlowWhy' : 'dataGrid.profileSlowWhy')
  if (status === 503) return t('dataGrid.workerBusyWhy')
  return errMessage(e)
}

// The profile of the columns shown (nulls, distinct values, min and max), after their rows
// and the count. Only the columns not profiled yet on these rows are asked: a column added
// asks its own, one moved or removed asks nothing.
// On BigQuery every query is billed by the bytes of the columns it reads, a LIMIT included:
// there the profile asks by itself only what the parquet footer says (no query), and the
// whole one is made when a column is opened (`profileWhole`), as in the editor.
const billedByBytes = computed(() => engine.value === 'bigquery')
const profile = ref<ProfileResult | null>(null)
const profileState = ref<'none' | 'loading' | 'done' | 'failed'>('none')
const profileError = ref('')
let viewOps: { ds: DatasourceInfo; ops: Operation[]; key: string; engine: string } | null = null
let profiled: string[] = [] // the columns `profile` holds
let profiledKey: string | null = null
let wholeFor = '' // the rows and columns whose whole profile was asked (BigQuery)
const gridCols = () => (pivotShown.value ? tableCols.value : shownCols.value)
const wholeSig = () => `${viewOps?.key ?? ''}\u0001${gridCols().map((c) => c.name).sort().join('\u0001')}`
async function profileShown(seq: number, whole = false) {
  if (!viewOps) return
  const { ds, ops, key, engine: eng } = viewOps
  const cols = gridCols()
  const same = profiledKey === key && !!profile.value
  const names = whole ? cols.map((c) => c.name) : profileMissing(cols.map((c) => c.name), profiled, same)
  if (!names.length) return
  const merge = same && !whole
  profileError.value = ''
  if (!merge) profileState.value = 'loading'
  const footerOnly = eng === 'bigquery' && !whole
  try {
    const res = await previewSlots.profile({
      bucket: ds.bucket, input_key: ds.key, operations: ops, engine: eng, no_cache: true,
      columns: cols.filter((c) => names.includes(c.name)).map((c) => ({ name: c.name, dtype: c.dtype })),
      ...(footerOnly ? { footer_only: true } : {}),
    }, 'profile')
    if (seq !== applySeq || viewOps?.key !== key) return
    // a footer-only answer that comes after the whole was asked does not stand for it
    if (footerOnly && wholeFor === wholeSig()) return
    const before = merge ? profile.value : null
    profile.value = before
      ? { ...res, partial: before.partial || res.partial || undefined, columns: { ...before.columns, ...res.columns } }
      : res
    profiled = before ? [...new Set([...profiled, ...names])] : names
    profiledKey = key
    profileState.value = 'done'
  } catch (e) {
    if (seq !== applySeq || isSuperseded(e) || (footerOnly && wholeFor === wholeSig())) return
    profileError.value = whyNot(e, 'profile')
    profileState.value = 'failed'
  }
}
/** A column opened on BigQuery: the whole profile of the columns shown (one billed query). */
function profileWhole() {
  if (!viewOps || loading.value || (profile.value && !profile.value.partial) || wholeFor === wholeSig()) return
  wholeFor = wholeSig()
  void profileShown(applySeq, true)
}
function profileRetry() {
  if (viewOps && !loading.value) void profileShown(applySeq)
}
function stopProfile() {
  for (const slot of ['profile', 'top']) if (previewSlots.busy(slot)) previewSlots.stop(slot)
  profile.value = null
  profileState.value = 'none'
  profileError.value = ''
  profiled = []
  profiledKey = null
  wholeFor = ''
  viewOps = null
}
// an opened column's most frequent values, or a number's histogram
async function columnDetail(column: string, ask: DetailAsk): Promise<TopValuesResult | HistogramResult> {
  if (!viewOps) throw new Error('no view')
  const { ds, ops, engine: eng } = viewOps
  return await previewSlots.profile<TopValuesResult | HistogramResult>({
    bucket: ds.bucket, input_key: ds.key, operations: ops, engine: eng, no_cache: true, column,
    ...(ask.kind === 'histogram' ? { histogram: { lo: ask.lo, hi: ask.hi, bins: ask.bins } } : {}),
  }, 'top')
}
// A value of the profile kept or left out: a filter added to the view's Filters, applied.
// Not on a pivot (its columns are not the ones the filters read), nor on a computed field
// (the filters come before the computed fields): the datasource's own columns only.
const valueActions = computed(() => {
  if (!applied.value || pivotShown.value) return null
  return {
    columns: filterCols.value,
    filter: (column: string, value: unknown, exclude: boolean) => {
      if (value === null || value === undefined) {
        filters.value.push({ column, operator: exclude ? 'is_not_null' : 'is_null', value: '', value2: '' })
      } else {
        filters.value.push({ column, operator: exclude ? 'ne' : 'eq', value: String(value), value2: '' })
      }
      void apply()
    },
  }
})
// the table on screen: a pivot's, or the chosen columns over the rows that hold them (none
// before the first field, none while the first rows are on their way)
const gridResult = computed<PreviewResult | null>(() => {
  if (tableEmpty.value) return null
  if (pivotShown.value) return { columns: tableCols.value, rows: displayRows.value, row_count: rows.value.length, truncated: truncated.value }
  if (mode.value !== 'detail' || heldKey.value === null) return null
  return { columns: shownCols.value, rows: rows.value, row_count: rows.value.length, truncated: truncated.value }
})
const applied = computed(() => !!gridResult.value)
// the columns named in the table before their values came
const waitingCols = computed(() => (pivotShown.value || heldKey.value === null ? [] : chosen.value.filter((c) => !held.value.includes(c))))
const firstCols = computed(() => (mode.value === 'detail' && !pivotShown.value && heldKey.value === null && loading.value ? shownCols.value : null))
// nothing in the table and nothing coming: the invitation
const tableEmpty = computed(() => !!selectedDs.value && (mode.value === 'empty' || mode.value === 'needs-measure'))
const summaryRows = computed(() => rowsPhrase(rows.value.length, truncated.value, rowCount.value, t, (n) => fmtx.count(n)))
// one announcement an Apply, once the rows and their count have arrived (not one a change)
const liveText = ref('')
const settledSummary = computed(() => {
  if (!applied.value || loading.value || rowCount.value.state === 'counting') return ''
  const parts = [summaryRows.value]
  if (rowCount.value.state === 'failed') parts.push(rowCount.value.slow ? t('dataGrid.countSlow') : t('dataGrid.countFailed'))
  parts.push(t('dataGrid.columns', { n: gridResult.value?.columns.length ?? 0 }, gridResult.value?.columns.length ?? 0))
  return parts.join(' · ')
})
watch(settledSummary, (now) => {
  if (!now) return
  liveText.value = ''
  nextTick(() => { liveText.value = now })
})

// ── the fields: onto the table, the filters, the chart ───────────────────────────
// what happened to the table's columns, said once for a screen reader
const said = ref('')
function say(text: string) {
  said.value = ''
  nextTick(() => { said.value = text })
}
const inTable = (name: string) => chosen.value.includes(name) || colFields.value.includes(name) || measures.value.some((m) => m.column === name)
const areaTitle = (area: ShelfArea) => t(area === 'rows' ? 'viewer.areaRows' : area === 'cols' ? 'viewer.areaCols' : 'viewer.areaMeasures')
const dtypeOf = (name: string) => fields.value.find((f) => f.name === name)?.dtype ?? ''
/** A field put on one of the three areas: at the end, or at the slot it was dropped in;
 *  coming from another area (`from`) it leaves it. */
function putField(area: ShelfArea, name: string, at?: number, from?: ShelfFrom | null) {
  if (!fieldNames.value.includes(name)) return
  const before = shelves.value
  const next = placeOnShelf(before, area, name, dtypeOf(name), at, from)
  if (next === before) { say(t('viewer.saidAlready', { name, area: areaTitle(area) })); return }
  setShelves(next)
  const list = area === 'measures' ? next.measures.map((m) => m.column) : area === 'rows' ? next.rows : next.cols
  say(t('viewer.saidPlaced', { name, area: areaTitle(area), i: list.lastIndexOf(name) + 1, n: list.length }))
  columnsChanged()
}
/** A drop on an area: from the list, from the table's header (a row field), from a chip. */
function onShelfPlace(area: ShelfArea, p: FieldDragPayload, at: number) {
  const from: ShelfFrom | null = p.from === 'list' ? null
    : p.from === 'table' ? { area: 'rows', index: chosen.value.indexOf(p.name) }
      : { area: p.from, index: p.index ?? -1 }
  putField(area, p.name, at, from)
}
// the table's own header (the rows as they are): a field dropped between two names, a name moved
function addField(name: string, at?: number) { putField('rows', name, at, chosen.value.includes(name) ? { area: 'rows', index: chosen.value.indexOf(name) } : null) }
function shiftField(name: string, delta: number) {
  const next = shiftColumn(chosen.value, name, delta)
  if (next === chosen.value) return
  chosen.value = next
  say(t('viewer.saidPlaced', { name, area: areaTitle('rows'), i: next.indexOf(name) + 1, n: next.length }))
  columnsChanged()
}
function shelfShift(area: ShelfArea, index: number, delta: number) {
  const s = shelves.value
  const name = area === 'measures' ? s.measures[index]?.column : (area === 'rows' ? s.rows : s.cols)[index]
  if (name === undefined) return
  putField(area, name, delta > 0 ? index + 2 : index - 1, { area, index })
}
function shelfRemove(area: ShelfArea, index: number) {
  const s = shelves.value
  const name = area === 'measures' ? s.measures[index]?.column : (area === 'rows' ? s.rows : s.cols)[index]
  if (name === undefined) return
  setShelves(removeFromShelf(s, area, index))
  say(t('viewer.saidOff', { name, area: areaTitle(area) }))
  columnsChanged()
}
function shelfSend(area: ShelfArea, index: number, to: ShelfArea) {
  const s = shelves.value
  const name = area === 'measures' ? s.measures[index]?.column : (area === 'rows' ? s.rows : s.cols)[index]
  if (name !== undefined) putField(to, name, undefined, { area, index })
}
function setMeasureFunc(index: number, func: string) {
  const m = measures.value[index]
  if (!m || m.func === func) return
  measures.value = measures.value.map((x, i) => (i === index ? { ...x, func } : x))
  say(t('viewer.saidFunc', { name: m.column, func: FUNC_LABELS.value[func] ?? func }))
  columnsChanged()
}
/** A field off the table: its column (the table's header, ×, Del) or every area (the list's Del). */
function removeField(name: string) {
  const i = chosen.value.indexOf(name)
  if (i >= 0) shelfRemove('rows', i)
  if (!chosen.value.length) nextTick(() => { if (!document.activeElement || document.activeElement === document.body) focusField(fieldAt.value) })
}
function takeOff(name: string) {
  if (!inTable(name)) return
  setShelves(fieldOffShelves(shelves.value, name))
  say(t('viewer.saidRemoved', { name }))
  columnsChanged()
}
const allIn = computed(() => fields.value.length > 0 && fields.value.every((f) => chosen.value.includes(f.name)))
/** Every field as a row heading: the whole table, as it is. */
function addAll() {
  const more = fieldNames.value.filter((n) => !chosen.value.includes(n) && !colFields.value.includes(n))
  if (!more.length) return
  chosen.value = [...chosen.value, ...more]
  say(t('viewer.saidAllAdded', { n: more.length }))
  columnsChanged()
}
function clearTable() {
  if (mode.value === 'empty') return
  setShelves({ rows: [], cols: [], measures: [] })
  say(t('viewer.saidCleared'))
  columnsChanged()
}
// the chips of the three areas
const chipOf = (name: string): ShelfChip => ({ name, dtype: dtypeOf(name) })
const rowChips = computed(() => chosen.value.map(chipOf))
const colChips = computed(() => colFields.value.map(chipOf))
// a cross table has the engine's one measure: the first; the others wait, and say so
const measureChips = computed<ShelfChip[]>(() => measures.value.map((m, i) => ({ ...chipOf(m.column), func: m.func, unused: mode.value === 'cross' && i > 0 })))
// a field's other ways (a touch, a pointer that does not drag): a small menu on its row
const fieldMenu = ref('')
const fieldMenuEl = ref<HTMLElement | null>(null)
const fieldMenuOpen = computed(() => !!fieldMenu.value)
usePopover(fieldMenuOpen, fieldMenuEl, () => { fieldMenu.value = '' })
function fromMenu(act: () => void) { fieldMenu.value = ''; act() }
/** A filter on the field, ready for its value (the filters read the datasource's columns). */
function filterOn(name: string) {
  const f = fields.value.find((x) => x.name === name)
  if (!f) return
  if (f.computed) { say(t('viewer.saidFilterComputed', { name })); return }
  filters.value.push({ column: name, operator: 'eq', value: '', value2: '' })
  say(t('viewer.saidFilter', { name }))
  nextTick(() => {
    const rowsEl = document.querySelectorAll<HTMLElement>('.viewer .frow')
    rowsEl[rowsEl.length - 1]?.querySelector<HTMLInputElement>('input.fv')?.focus()
  })
}
/** The field on the chart: where it was dropped, or by its type (a number is the value). */
async function chartField(name: string, slot?: 'axis' | 'value') {
  const f = fields.value.find((x) => x.name === name)
  if (!f) return
  view.value = 'chart'
  chartSeen.value = true
  await nextTick()
  const where = slot ?? chartSlotOf(f.dtype)
  const ok = chartRef.value?.placeField(name, where)
  say(t(!ok ? 'viewer.saidChartNo' : where === 'axis' ? 'viewer.saidChartAxis' : 'viewer.saidChartValue', { name }))
}
function onChartPlaced(name: string, slot: 'axis' | 'value') {
  say(t(slot === 'axis' ? 'viewer.saidChartAxis' : 'viewer.saidChartValue', { name }))
}

// the list of fields: a filter above it, one tab stop, the arrows along it
const fieldQuery = ref('')
const shownFields = computed(() => {
  const q = fieldQuery.value.trim().toLowerCase()
  if (!q) return fields.value
  return fields.value.filter((f) => f.name.toLowerCase().includes(q) || f.description.toLowerCase().includes(q))
})
const fieldAt = ref(0)
const fieldList = ref<HTMLElement | null>(null)
watch(shownFields, (l) => { if (fieldAt.value >= l.length) fieldAt.value = Math.max(0, l.length - 1) })
function focusField(i: number) {
  const n = shownFields.value.length
  if (!n) return
  fieldAt.value = Math.max(0, Math.min(n - 1, i))
  nextTick(() => {
    const el = fieldList.value?.querySelectorAll<HTMLElement>('button.fld')[fieldAt.value]
    el?.focus({ preventScroll: true })
    el?.scrollIntoView({ block: 'nearest' })
  })
}
function onFieldKey(e: KeyboardEvent) {
  const btn = (e.target as HTMLElement).closest?.('button.fld') as HTMLElement | null
  const name = btn?.dataset.field
  if (!btn || !name || e.altKey) return
  const mod = e.ctrlKey || e.metaKey
  const i = fieldAt.value
  const done = () => { e.preventDefault(); e.stopPropagation() }
  // Ctrl/⌘+↑ ↓ are the first and the last, where there is no Home nor End
  if (e.key === 'ArrowDown') { done(); focusField(mod ? shownFields.value.length - 1 : i + 1); return }
  if (e.key === 'ArrowUp') { done(); focusField(mod ? 0 : i - 1); return }
  if (e.key === 'Home' && !mod) { done(); focusField(0); return }
  if (e.key === 'End' && !mod) { done(); focusField(shownFields.value.length - 1); return }
  // Del, or Ctrl/⌘+Backspace where there is no Del: the field leaves the table
  if ((e.key === 'Delete' && !mod) || (e.key === 'Backspace' && mod)) { done(); takeOff(name); return }
  if (mod || e.shiftKey) return
  if (e.key === 'Enter' || e.key === ' ' || e.key === 'r') { done(); putField('rows', name); return }
  if (e.key === 'c') { done(); putField('cols', name); return }
  if (e.key === 'm') { done(); putField('measures', name); return }
  if (e.key === 'f') { done(); filterOn(name); return }
  if (e.key === 'p') { done(); void chartField(name); return }
}
function kindIcon(f: ViewerField) {
  if (f.computed) return SquareFunction
  if (/Int|Float|Decimal/i.test(f.dtype)) return Hash
  if (/^(Date|Datetime|Time|Duration)/.test(f.dtype)) return Calendar
  if (/^Bool/.test(f.dtype)) return ToggleLeft
  if (/^(String|Utf8|Categorical|Enum)/.test(f.dtype)) return CaseSensitive
  return Braces
}
const fieldTitle = (f: ViewerField) =>
  [`${f.name} · ${f.computed ? t('viewer.computedField') : f.dtype}`, f.description, t('viewer.fieldHint')].filter(Boolean).join('\n')

// the targets a dragged field finds outside the table: the empty table, the filters, the
// Chart tab (which opens under a field held over it)
const overFilters = ref(false)
function onFiltersOver(e: DragEvent) {
  const p = fieldDragOf(e)
  if (!p) return
  fieldDragOutside.value = false
  overFilters.value = filterCols.value.includes(p.name)
  // a computed field is not taken (the filters come before it): the browser says «not here»
  if (!overFilters.value) return
  e.preventDefault()
  e.stopPropagation() // a column's name held over the filters is not «out of the table»
  if (e.dataTransfer) e.dataTransfer.dropEffect = 'copy'
}
function onFiltersDrop(e: DragEvent) {
  const p = fieldDragOf(e)
  overFilters.value = false
  if (!p || !filterCols.value.includes(p.name)) return
  e.preventDefault()
  e.stopPropagation()
  endFieldDrag()
  filterOn(p.name)
}
function leftZone(e: DragEvent, flag: { value: boolean }) {
  const to = e.relatedTarget as Node | null
  if (!to || !(e.currentTarget as HTMLElement).contains(to)) flag.value = false
}
const onFiltersLeave = (e: DragEvent) => leftZone(e, overFilters)
watch(draggedField, (d) => { if (!d) overFilters.value = false })
let springTimer: ReturnType<typeof setTimeout> | null = null
function springTab(e: DragEvent, tab: 'table' | 'chart') {
  if (!fieldDragOf(e) || view.value === tab || springTimer) return
  springTimer = setTimeout(() => { springTimer = null; if (draggedField.value) view.value = tab }, 450)
}
function springStop() {
  if (springTimer) { clearTimeout(springTimer); springTimer = null }
}
onBeforeUnmount(springStop)

// the keys of the list of fields, in the app's list («?») while the Viewer is on screen
const kn = keyNames(platformIsMac())
useKeyGroups(() => (selectedDs.value ? [{
  title: t('viewer.keysGroup'),
  keys: [
    ['↑ ↓', t('viewer.keysMove')],
    [`r · ${kn.enter}`, t('viewer.keysRows')],
    ['c', t('viewer.keysCols')],
    ['m', t('viewer.keysMeasures')],
    ['f', t('viewer.keysFilter')],
    ['p', t('viewer.keysChart')],
    [kn.ways({ pc: ['Del'], any: [kn.chord(kn.mod, kn.backspace)] }), t('viewer.keysRemove')],
    [`${kn.chord(kn.shift, '←')} · ${kn.chord(kn.shift, '→')}`, t('viewer.keysChipMove')],
    ['r · c · m', t('viewer.keysChipSend')],
  ] as [string, string][],
}] : []), 5)

// export of the current view (csv/xlsx), written by a worker: Polars writes the file,
// whatever the engine chosen for the display. It holds what the table holds: a pivot, or
// the columns shown in their order; every column of the datasource (and the computed
// fields) when the table shows none.
const exporting = ref('')
function exportOps(): Operation[] {
  if (aggOps.value.length) return [...baseOps.value, ...aggOps.value]
  return mode.value === 'detail' ? [...baseOps.value, selectStep(chosen.value)] : [...baseOps.value]
}
const exportTitle = (format: string) => {
  if (aggOps.value.length) return t('viewer.exportPivotTitle', { format })
  return mode.value === 'detail'
    ? t('viewer.exportShownTitle', { format, n: chosen.value.length }, chosen.value.length)
    : t('viewer.exportAllTitle', { format, n: fields.value.length })
}
async function exportView(format: 'csv' | 'xlsx') {
  const ds = selectedDs.value
  if (!ds) return
  exporting.value = format
  error.value = ''
  try {
    const filename = `${ds.name}_viewer.${format}`.toLowerCase().replace(/[^a-z0-9._-]+/g, '_')
    await api.exportData({ bucket: ds.bucket, input_key: ds.key, operations: exportOps(), format, filename, engine: engine.value })
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
  if (!outline.value || !pivotShown.value) return rows.value
  const idx = chosen.value
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
const saveByKey = ref(false)
const savePop = ref<HTMLElement | null>(null)
usePopover(saveOpen, savePop, () => { saveOpen.value = false })
const saveName = ref('')
const saveProjectId = ref<number | null>(null)
const saving = ref(false)
// Explore's «New → View» (?project=<folder>): the folder the new view is saved in, proposed by
// the save box. Nothing exists until then: leaving the page leaves nothing behind.
const newViewFolder = ref<number | null>(null)

const savedViewOptions = computed(() =>
  savedViews.value.map((v) => ({ value: v.id, label: v.name, hint: pathOf(v.project_id) })),
)

function buildSpec(): SavedViewSpec {
  return {
    engine: engine.value,
    // a filter still without its value filters nothing here: it is not kept, so that a report
    // reading the view (the gateway builds the same chain) does not look for the empty text
    filters: filters.value.filter((f) => NO_VALUE.has(f.operator) || (f.value !== '' && (f.operator !== 'between' || f.value2 !== ''))).map((f) => ({ ...f })),
    computedFields: computedFields.value.map((c) => ({ ...c })),
    // the three areas, and the pivot they are the face of in the shape the views always had
    ...specOfShelves(shelves.value),
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
  outline.value = !!s.outline
  // the tab and the chart: a view saved on its chart opened on the table, the chart reset
  view.value = s.tab === 'chart' ? 'chart' : 'table'
  chart.value = s.chart ?? null
  // after the computed fields: they are fields too. A view saved before the three areas has
  // none: its pivot becomes them (the same request), and without one every field is a row
  // heading, that is the whole table it always opened with
  setShelves(shelvesOfSpec(s, fieldNames.value))
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
    await onPickDatasource(ds.id)
    currentViewId.value = id
    applySpec(v.spec)
    await apply()
  } catch (e) {
    error.value = errMessage(e)
  }
}

function openSaveDialog(ev?: MouseEvent) {
  if (!selectedDs.value) return
  saveByKey.value = ev?.detail === 0 // opened from the keyboard: no motion
  const current_ = savedViews.value.find((v) => v.id === currentViewId.value)
  saveName.value = current_?.name ?? ''
  saveProjectId.value = current_?.project_id ?? newViewFolder.value ?? selectedDs.value.project_id ?? projects.value[0]?.id ?? null
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
  // side by side: one round trip, not four in a row
  await Promise.allSettled([
    dsApi.list().then((l) => { datasources.value = l }, (e) => { error.value = errMessage(e) }),
    api.engines().then((e) => {
      engines.value = e
      // the favourite may not be available: applies the robust fallback
      engine.value = defaultEngine(engines.value)
    }), // fallback: the default engine
    // views are an extra: if they do not load, the Viewer stays usable
    viewsApi.list().then((v) => { savedViews.value = v }, () => { savedViews.value = [] }),
    projectsApi.list().then((p) => { projects.value = p }, () => { projects.value = [] }),
  ])
  // direct opening from a link (?view=<id>): that is how the folder leads here. It goes
  // AFTER loading the datasources, because the view must find one.
  const linked = Number(useRoute().query.view)
  // ?ds=<id>: a datasource opened from Explore's panel (or in a new tab from its row)
  const ds = Number(useRoute().query.ds)
  const folder = Number(useRoute().query.project)
  if (Number.isInteger(folder) && folder > 0 && projects.value.some((p) => p.id === folder)) newViewFolder.value = folder
  if (Number.isInteger(linked) && linked > 0) await openSavedView(linked)
  // (the table starts empty: the fields and their types come from the list just loaded)
  else if (Number.isInteger(ds) && ds > 0 && datasources.value.some((d) => d.id === ds)) await onPickDatasource(ds)
  // a new view starts from its datasource: the focus on the selector that chooses it
  else if (newViewFolder.value != null) {
    nextTick(() => {
      const sel = document.querySelector<HTMLElement>('.viewer .dssel')
      ;(sel?.matches('button') ? sel : sel?.querySelector<HTMLElement>('button'))?.focus()
    })
  }
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
          <button class="btn-link" :disabled="!selectedDs" :title="$t('viewer.saveViewTitle')" @click="openSaveDialog($event)">
            <Save :size="14" /> {{ $t('viewer.saveViewButton') }}
          </button>
          <div v-if="saveOpen" class="menu-backdrop" @click="saveOpen = false" />
          <Transition name="pop" :css="!saveByKey">
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
          </Transition>
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

    <template v-if="!selectedDs">
      <p v-if="error" class="err empty" role="alert">{{ error }}</p>
      <p class="muted empty">{{ newViewFolder != null ? $t('viewer.newViewHint', { folder: projects.find((p) => p.id === newViewFolder)?.name ?? '' }) : $t('viewer.emptyStateHint') }}</p>
    </template>

    <div v-else class="body" :style="{ gridTemplateColumns: `var(--vw-fields) ${sidebarW}px 6px 1fr` }">
      <!-- the datasource's fields: dragged onto the table, the filters or the chart -->
      <aside class="flds" :class="{ out: draggedField?.from === 'table' && fieldDragOutside }" :aria-label="$t('viewer.fieldsTitle')">
        <div class="flds-head">
          <span class="flds-title">{{ $t('viewer.fieldsTitle') }} <span class="muted flds-n">{{ fmtx.count(fields.length) }}</span></span>
          <button v-if="mode !== 'empty'" type="button" class="mini" :title="$t('viewer.clearTableTitle')" @click="clearTable">{{ $t('viewer.clearTable') }}</button>
          <button type="button" class="mini" :disabled="allIn" :title="$t('viewer.addAllTitle', { n: fields.length })" @click="addAll">
            <ListPlus :size="13" aria-hidden="true" /> {{ $t('viewer.addAll') }}
          </button>
        </div>
        <label v-if="fields.length > 8" class="flds-search">
          <Search :size="13" aria-hidden="true" />
          <input v-model="fieldQuery" type="text" data-page-search :placeholder="$t('viewer.fieldsFilter')" :aria-label="$t('viewer.fieldsFilter')" />
        </label>
        <!-- a column dragged out of the table: released, it leaves -->
        <p v-if="draggedField?.from === 'table' && fieldDragOutside" class="flds-out">{{ $t('viewer.dropToRemove', { name: draggedField.name }) }}</p>
        <p v-if="!shownFields.length" class="muted small flds-none">{{ $t('viewer.fieldsNone') }}</p>
        <ul ref="fieldList" class="flds-list" @keydown="onFieldKey">
          <li v-for="(f, i) in shownFields" :key="f.name" :class="{ in: inTable(f.name), calc: f.computed }">
            <button
              type="button"
              class="fld"
              draggable="true"
              :data-field="f.name"
              :tabindex="i === fieldAt ? 0 : -1"
              :title="fieldTitle(f)"
              aria-describedby="vw-fld-keys"
              @dragstart="startFieldDrag($event, { name: f.name, dtype: f.dtype, from: 'list', computed: f.computed })"
              @dragend="endFieldDrag()"
              @dblclick="putField('rows', f.name)"
              @focus="fieldAt = i"
            >
              <component :is="kindIcon(f)" :size="13" class="fld-kind" aria-hidden="true" />
              <span class="fld-text">
                <span class="fld-line">
                  <span class="fld-name">{{ f.name }}</span>
                  <Check v-if="inTable(f.name)" :size="12" class="fld-in" aria-hidden="true" />
                  <span v-if="inTable(f.name)" class="sr-only">, {{ $t('viewer.fieldInTable') }}</span>
                  <span class="fld-type">{{ f.computed && !f.dtype ? $t('viewer.computedField') : shortDtype(f.dtype) }}</span>
                </span>
                <span v-if="f.description" class="fld-desc">{{ f.description }}</span>
              </span>
            </button>
            <!-- the way of a pointer that does not drag, and of a touch: where the field can go -->
            <button
              type="button"
              tabindex="-1"
              class="fld-more"
              :aria-label="$t('viewer.fieldMore', { name: f.name })"
              :title="$t('viewer.fieldMore', { name: f.name })"
              aria-haspopup="menu"
              :aria-expanded="fieldMenu === f.name"
              @click="fieldMenu = fieldMenu === f.name ? '' : f.name"
            ><Ellipsis :size="14" aria-hidden="true" /></button>
            <div v-if="fieldMenu === f.name" ref="fieldMenuEl" class="fld-menu" role="menu" :aria-label="f.name">
              <button type="button" role="menuitem" @click="fromMenu(() => putField('rows', f.name))"><Rows3 :size="13" aria-hidden="true" /> {{ $t('viewer.areaRows') }}</button>
              <button type="button" role="menuitem" @click="fromMenu(() => putField('cols', f.name))"><Columns3 :size="13" aria-hidden="true" /> {{ $t('viewer.areaCols') }}</button>
              <button type="button" role="menuitem" @click="fromMenu(() => putField('measures', f.name))"><Sigma :size="13" aria-hidden="true" /> {{ $t('viewer.areaMeasures') }}</button>
              <button type="button" role="menuitem" :disabled="f.computed" @click="fromMenu(() => filterOn(f.name))"><Filter :size="13" aria-hidden="true" /> {{ $t('viewer.filtersTitle') }}</button>
              <button type="button" role="menuitem" @click="fromMenu(() => chartField(f.name))"><ChartColumn :size="13" aria-hidden="true" /> {{ $t('viewer.chartTab') }}</button>
              <button v-if="inTable(f.name)" type="button" role="menuitem" @click="fromMenu(() => takeOff(f.name))"><X :size="13" aria-hidden="true" /> {{ $t('viewer.fieldRemove', { name: f.name }) }}</button>
            </div>
          </li>
        </ul>
        <span id="vw-fld-keys" class="sr-only">{{ $t('viewer.fieldKeysHint') }}</span>
        <span class="sr-only" role="status" aria-live="polite">{{ said }}</span>
      </aside>

      <!-- panel of the on-the-fly transformations -->
      <aside class="cfg">
        <div
          class="cfg-sec vw-filters"
          :class="{ over: overFilters, armed: !!draggedField }"
          @dragover="onFiltersOver"
          @dragleave="onFiltersLeave"
          @drop="onFiltersDrop"
        >
          <div class="cfg-head"><Filter :size="13" /> {{ $t('viewer.filtersTitle') }} <button class="add" :aria-label="$t('viewer.addFilter')" :title="$t('viewer.addFilter')" @click="addFilter"><Plus :size="13" aria-hidden="true" /></button></div>
          <p v-if="draggedField" class="muted small">{{ $t('viewer.dropFilterHint') }}</p>
          <p v-else-if="!filters.length" class="muted small">{{ $t('viewer.noFilters') }}</p>
          <div v-for="(f, i) in filters" :key="i" class="frow">
            <Select v-model="f.column" :options="filterCols" :placeholder="$t('viewer.columnPlaceholder')" class="fc" />
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
            <button class="rm" :aria-label="$t('viewer.removeFilter')" :title="$t('viewer.removeFilter')" @click="filters.splice(i, 1)"><X :size="13" aria-hidden="true" /></button>
          </div>
        </div>

        <div class="cfg-sec">
          <div class="cfg-head"><Sigma :size="13" /> {{ $t('viewer.computedFieldsTitle') }} <button class="add" :aria-label="$t('viewer.addComputed')" :title="$t('viewer.addComputed')" @click="addComputed"><Plus :size="13" aria-hidden="true" /></button></div>
          <p class="muted small">{{ $t('viewer.computedFieldsHint', { engine }) }}</p>
          <div v-for="(c, i) in computedFields" :key="i" class="crow">
            <div class="crow-top">
              <input v-model="c.name" type="text" class="cn" :placeholder="$t('viewer.fieldNamePlaceholder')" />
              <button class="rm" :aria-label="$t('viewer.removeComputed')" :title="$t('viewer.removeComputed')" @click="computedFields.splice(i, 1)"><X :size="13" aria-hidden="true" /></button>
            </div>
            <textarea v-model="c.expr" class="ce" rows="2" :placeholder="$t('viewer.exprPlaceholder')" />
          </div>
        </div>

      </aside>

      <!-- the sidebar's resize handle -->
      <div class="resizer" :title="$t('viewer.resizeHint')" @mousedown="startResize" />

      <!-- result area: table or chart -->
      <section class="result">
        <div v-roving.main class="rtabs" role="tablist" :aria-label="$t('appKeys.viewTabs')">
          <button role="tab" :class="{ active: view === 'table' }" :aria-selected="view === 'table'" @click="view = 'table'" @dragover="springTab($event, 'table')" @dragleave="springStop" @drop="springStop"><Table2 :size="14" aria-hidden="true" /> {{ $t('viewer.tableTab') }}</button>
          <button role="tab" :class="{ active: view === 'chart' }" :aria-selected="view === 'chart'" @click="view = 'chart'" @dragover="springTab($event, 'chart')" @dragleave="springStop" @drop="springStop"><PieChart :size="14" aria-hidden="true" /> {{ $t('viewer.chartTab') }}</button>
          <button class="primary apply" :disabled="!selectedDs || loading" @click="apply"><Play :size="14" /> {{ $t('viewer.applyButton') }}</button>
          <span v-if="loading" class="muted">{{ $t('dataGrid.computing') }}</span>
          <!-- an error with a table on screen (an export): here; without one, in the table's place -->
          <span v-else-if="error && applied" class="err" role="alert" :title="error">{{ error }}</span>
          <span id="vw-summary" class="rmeta muted">
            <template v-if="applied">
              {{ summaryRows }}<template v-if="rowCount.state === 'counting'"> · {{ $t('dataGrid.counting') }}</template><template
                v-else-if="rowCount.state === 'failed'"> · <span :title="rowCount.slow ? $t('dataGrid.countSlowTitle') : countError || undefined">{{ rowCount.slow ? $t('dataGrid.countSlow') : $t('dataGrid.countFailed') }}</span><button
                  type="button"
                  class="vw-retry"
                  :aria-label="$t('dataGrid.countAgain')"
                  :title="$t('dataGrid.countAgain')"
                  @click="countRetry"
                ><RotateCw :size="11" aria-hidden="true" /></button></template>
              · {{ $t('dataGrid.columns', { n: gridResult?.columns.length ?? 0 }, gridResult?.columns.length ?? 0) }}
            </template>
          </span>
          <span class="sr-only" role="status" aria-live="polite">{{ liveText }}</span>
          <GridKeys v-if="view === 'table' && applied" profile :arrange="mode === 'detail'" />
          <div class="expbtns">
            <button :disabled="!!exporting" :title="exportTitle('CSV')" @click="exportView('csv')">
              <Download :size="13" /> {{ exporting === 'csv' ? '…' : 'CSV' }}
            </button>
            <button :disabled="!!exporting" :title="exportTitle('Excel')" @click="exportView('xlsx')">
              <FileSpreadsheet :size="13" /> {{ exporting === 'xlsx' ? '…' : 'Excel' }}
            </button>
          </div>
        </div>

        <!-- the table, built on three areas: COLUMNS across the top, ROWS at the left,
             MEASURES in the middle with the result under them -->
        <div v-show="view === 'table'" class="xt">
          <div class="xt-corner" aria-hidden="true" />
          <FieldShelf
            area="cols" class="xt-cols" :title="$t('viewer.areaCols')" :hint="$t('viewer.areaColsHint')" :chips="colChips"
            @place="(p, at) => onShelfPlace('cols', p, at)" @remove="(i) => shelfRemove('cols', i)"
            @shift="(i, d) => shelfShift('cols', i, d)" @send="(i, to) => shelfSend('cols', i, to)"
          ><template #icon><Columns3 :size="12" aria-hidden="true" /></template></FieldShelf>
          <div class="xt-rows">
            <FieldShelf
              area="rows" vertical :title="$t('viewer.areaRows')" :hint="$t('viewer.areaRowsHint')" :chips="rowChips"
              @place="(p, at) => onShelfPlace('rows', p, at)" @remove="(i) => shelfRemove('rows', i)"
              @shift="(i, d) => shelfShift('rows', i, d)" @send="(i, to) => shelfSend('rows', i, to)"
            ><template #icon><Rows3 :size="12" aria-hidden="true" /></template></FieldShelf>
            <label v-if="pivotShown && chosen.length > 1" class="xt-outline" :title="$t('viewer.outlineHint')">
              <input v-model="outline" type="checkbox" /> {{ $t('viewer.outlineToggle') }}
            </label>
          </div>
          <div class="xt-main" :class="{ empty: tableEmpty }">
            <FieldShelf
              area="measures" class="xt-meas" :title="$t('viewer.areaMeasures')" :hint="$t('viewer.areaMeasuresHint')" :chips="measureChips"
              :funcs="measureFuncs" :note="mode === 'cross' && measures.length > 1 ? $t('viewer.crossOneMeasure') : undefined"
              @place="(p, at) => onShelfPlace('measures', p, at)" @remove="(i) => shelfRemove('measures', i)"
              @shift="(i, d) => shelfShift('measures', i, d)" @send="(i, to) => shelfSend('measures', i, to)" @func="setMeasureFunc"
            ><template #icon><Sigma :size="12" aria-hidden="true" /></template></FieldShelf>
            <!-- before the first field: what is there, and what the three areas make -->
            <div v-if="tableEmpty" class="xt-empty">
              <p v-if="error" class="err xt-empty-err" role="alert">{{ error }}</p>
              <p class="xt-empty-title">{{ mode === 'needs-measure' ? $t('viewer.needsMeasure') : $t('viewer.emptyTitle') }}</p>
              <p class="xt-empty-how muted">{{ $t('viewer.emptyHow') }}</p>
              <p class="xt-empty-meta">
                <template v-if="selectedDs?.rows != null">{{ $t('dataGrid.rows', { n: fmtx.count(selectedDs.rows) }) }} · </template>{{ $t('viewer.emptyFields', { n: fmtx.count(fields.length) }, fields.length) }}
              </p>
            </div>
            <DataGrid
              v-else
              :result="gridResult"
              :loading="loading && !gridResult"
              :stale="loading && replacing && !!gridResult"
              :pending-columns="firstCols"
              :waiting="waitingCols"
              :arrange="mode === 'detail' && !pivotShown"
              :error="applied ? '' : error"
              :summary="summaryRows"
              summary-id="vw-summary"
              :count="rowCount"
              :profile="profile"
              :profile-state="profileState"
              :profile-error="profileError"
              :profile-retry="profileRetry"
              :profile-whole="billedByBytes || pivotShown ? profileWhole : undefined"
              :column-detail="columnDetail"
              :value-actions="valueActions"
              :empty="$t('viewer.noDataHint')"
              :label="$t('viewer.tableTab')"
              @place="addField"
              @shift="shiftField"
              @remove="removeField"
            />
          </div>
        </div>

        <div v-if="chartSeen" v-show="view === 'chart'" class="chartwrap">
          <ChartPanel ref="chartRef" :columns="chartCols" :query="chartQuery" :settings="chart" manual @update:settings="chart = $event" @placed="onChartPlaced" />
        </div>
      </section>
    </div>
   </div>
  </AppShell>
</template>

<style scoped src="~/assets/listpage.css"></style>
<style scoped>
/* the Viewer is as tall as the window: the table scrolls inside it, and the header, Apply,
   the count and the export stay in view (critique of 2026-11-08: a page 13,000 px tall) */
.viewer {
  display: flex; flex-direction: column; flex: none; min-height: 420px;
  height: calc(100vh - 52px - 44px); height: calc(100dvh - 52px - 44px);
}
@media (max-width: 760px) {
  .viewer { height: calc(100vh - 52px - 32px); height: calc(100dvh - 52px - 32px); }
}
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
.body { --vw-fields: 232px; display: grid; grid-template-columns: var(--vw-fields) 280px 6px 1fr; gap: 0; flex: 1; min-height: 0; }
@media (max-width: 1180px) { .body { --vw-fields: 204px; } }

/* the list of fields: the report designer's rows (an icon of the kind, the name in mono, the
   short type), grabbed and dragged; a check on the ones in the table */
.flds {
  display: flex; flex-direction: column; min-height: 0; margin-right: 10px; padding: 10px 6px 6px;
  border: 1px solid var(--border); border-radius: 10px;
}
.flds.out { border-color: var(--danger); border-style: dashed; }
.flds-head { display: flex; align-items: center; gap: 4px; padding: 0 4px; margin-bottom: 8px; }
.flds-title { font-weight: 600; font-size: 13px; margin-right: auto; white-space: nowrap; }
.flds-n { font-weight: 400; font-size: 12px; font-variant-numeric: tabular-nums; }
.flds-head .mini { display: inline-flex; align-items: center; gap: 4px; padding: 3px 7px; font-size: 11.5px; white-space: nowrap; }
.flds-search {
  display: flex; align-items: center; gap: 6px; margin: 0 4px 6px; padding: 0 8px;
  border: 1px solid var(--control-border); border-radius: 8px; background: var(--bg-soft); color: var(--muted);
}
.flds-search:focus-within { border-color: var(--accent); }
.flds-search input { flex: 1; min-width: 0; border: 0; background: transparent; padding: 5px 0; font-size: 12.5px; box-shadow: none; outline: none; }
.flds-out { margin: 0 4px 6px; font-size: 12px; color: var(--danger-text, var(--danger)); overflow-wrap: anywhere; }
.flds-none { margin: 4px 6px; }
.flds-list { list-style: none; margin: 0; padding: 0 0 2px; flex: 1; min-height: 0; overflow-y: auto; display: flex; flex-direction: column; gap: 1px; }
.flds-list li { position: relative; }
.fld {
  display: flex; align-items: flex-start; gap: 7px; width: 100%; padding: 4px 6px; border: 0; border-radius: 6px;
  background: transparent; box-shadow: none; font-size: 12px; text-align: left; cursor: grab; color: var(--text);
  touch-action: manipulation;
}
.fld:hover { background: var(--panel-2); box-shadow: none; }
.fld:active { transform: none; cursor: grabbing; }
.fld:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
.fld-kind { flex: none; margin-top: 2px; color: var(--muted); }
li.in .fld-kind { color: var(--accent-hi, var(--accent)); }
li.calc .fld-kind { color: var(--violet, var(--accent-hi)); }
.fld-text { display: flex; flex-direction: column; min-width: 0; flex: 1; gap: 1px; }
.fld-line { display: flex; align-items: center; gap: 5px; min-width: 0; }
.fld-name { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 3ch; flex: 0 1 auto; }
.fld-in { flex: none; color: var(--accent-hi, var(--accent)); }
.fld-type { margin-left: auto; padding-left: 4px; flex: 0 6 auto; min-width: 4ch; overflow: hidden; text-overflow: ellipsis; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 10.5px; color: var(--muted); white-space: nowrap; }
.fld-desc { font-size: 11px; line-height: 1.3; color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
/* where a field can go without a drag: a «…» on its row (under the pointer, always on a touch screen) */
.fld-more {
  position: absolute; top: 2px; right: 3px; display: none; align-items: center; justify-content: center; width: 24px; height: 22px;
  padding: 0; border: 0; border-radius: 5px; background: var(--panel-2); box-shadow: -6px 0 6px -2px var(--panel-2); color: var(--muted);
}
.fld-more:hover, .fld-more[aria-expanded="true"] { color: var(--accent-hi, var(--accent)); background: var(--panel-2); box-shadow: -6px 0 6px -2px var(--panel-2); }
.fld-more[aria-expanded="true"] { display: inline-flex; }
.fld-menu {
  position: absolute; top: 26px; right: 3px; z-index: 30; min-width: 150px; padding: 4px; display: flex; flex-direction: column; gap: 1px;
  background: var(--panel); border: 1px solid var(--border); border-radius: 10px; box-shadow: var(--shadow-2, 0 8px 24px rgb(0 0 0 / 0.18));
}
.fld-menu button {
  display: flex; align-items: center; gap: 8px; width: 100%; padding: 6px 8px; border: 0; border-radius: 6px; background: transparent;
  box-shadow: none; font-size: 12.5px; text-align: left; color: var(--text);
}
.fld-menu button:hover:not(:disabled) { background: var(--panel-2); box-shadow: none; }
.fld-menu button:disabled { opacity: 0.4; }
.fld-menu svg { color: var(--muted); flex: none; }
@media (hover: hover) and (pointer: fine) {
  .flds-list li:hover .fld-more { display: inline-flex; }
}
@media (pointer: coarse) {
  .fld { padding: 9px 44px 9px 6px; }
  .fld-type { display: none; }
  .fld-more { display: inline-flex; top: 4px; width: 36px; height: 32px; background: none; box-shadow: none; }
  .fld-menu { top: 38px; }
  .fld-menu button { padding: 10px 8px; }
}

/* the table's three areas: Columns across the top, Rows down the left, Measures in the middle
   over the result. Regions of one panel, ruled off from each other: no card inside the card */
.xt {
  flex: 1; min-height: 0; display: grid; grid-template-columns: var(--vw-rows, 168px) minmax(0, 1fr); grid-template-rows: auto minmax(0, 1fr);
}
@media (max-width: 1180px) { .xt { --vw-rows: 148px; } }
.xt-corner { border-right: 1px solid var(--border); border-bottom: 1px solid var(--border); background: var(--bg-soft); }
.xt-cols { border-bottom: 1px solid var(--border); min-height: 40px; background: var(--bg-soft); }
.xt-rows { display: flex; flex-direction: column; min-height: 0; border-right: 1px solid var(--border); background: var(--bg-soft); }
.xt-rows > .fs { flex: 1; min-height: 0; justify-content: flex-start; }
.xt-outline { display: flex; align-items: flex-start; gap: 6px; padding: 8px; font-size: 11.5px; color: var(--muted); border-top: 1px solid var(--border-soft); cursor: pointer; }
.xt-main { display: flex; flex-direction: column; min-width: 0; min-height: 0; }
.xt-meas { border-bottom: 1px solid var(--border-soft); min-height: 40px; }
/* before the first measure the middle is the Measures area, whole: its name, its line, and what the datasource holds */
.xt-main.empty { position: relative; }
.xt-main.empty .xt-meas { position: absolute; inset: 0; align-items: flex-start; border-bottom: 0; padding: 14px 16px; }
.xt-main.empty .xt-meas:not(.empty) { position: static; border-bottom: 1px solid var(--border-soft); padding: 7px 10px; align-items: center; }
.xt-empty { position: relative; margin: 56px 16px 0; max-width: 64ch; pointer-events: none; }
.xt-main.empty .xt-meas:not(.empty) + .xt-empty { margin-top: 26px; }
.xt-empty-title { margin: 0; font-size: 15px; font-weight: 600; line-height: 1.35; text-wrap: balance; }
.xt-empty-how { margin: 6px 0 0; font-size: 13px; line-height: 1.5; text-wrap: pretty; }
.xt-empty-meta { margin: 16px 0 0; font-size: 12.5px; color: var(--muted); font-variant-numeric: tabular-nums; }
.xt-empty-err { margin: 0 0 14px; overflow-wrap: anywhere; pointer-events: auto; }
@media (prefers-reduced-motion: reduce) { .vw-filters { transition: none; } }

/* the filters take a field too: outlined while one is dragged, tinted under it */
.vw-filters { transition: border-color var(--dur-1) ease-out, background-color var(--dur-1) ease-out; }
.vw-filters.armed { border-style: dashed; border-color: var(--control-border); }
.vw-filters.over { border-style: dashed; border-color: var(--accent); background: color-mix(in srgb, var(--accent) 10%, transparent); }

/* a narrow window: the fields, the settings and the result one under the other */
@media (max-width: 900px) {
  .viewer { height: auto; }
  .body { display: flex; flex-direction: column; gap: 10px; }
  .flds { margin-right: 0; max-height: 34vh; }
  .cfg { overflow: visible; padding: 0; }
  .resizer { display: none; }
  .result { min-height: 70vh; }
  .xt { --vw-rows: 120px; }
}
.resizer { cursor: col-resize; background: transparent; position: relative; }
.resizer::before { content: ''; position: absolute; inset: 0 2px; border-radius: 2px; background: var(--border); transition: background var(--dur-2); }
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

.result { display: flex; flex-direction: column; min-height: 0; min-width: 0; border: 1px solid var(--border); border-radius: 10px; overflow: hidden; background: var(--panel); }
.rtabs { display: flex; align-items: center; gap: 6px; padding: 8px 10px; border-bottom: 1px solid var(--border); }
.rtabs button { padding: 5px 10px; }
.rtabs button.active { border-color: var(--accent); background: color-mix(in srgb, var(--accent) 12%, transparent); }
.rtabs .apply { margin-left: 8px; }
.vw-retry { margin: 0 0 0 4px; padding: 0 2px; border: 0; background: none; box-shadow: none; color: inherit; vertical-align: -1px; cursor: pointer; }
.vw-retry:hover { color: var(--text); background: none; box-shadow: none; }
.vw-retry:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; border-radius: 3px; }
.rtabs .err { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.rmeta { margin-left: auto; font-size: 12px; font-variant-numeric: tabular-nums; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.expbtns { display: flex; gap: 4px; }
.expbtns button { padding: 5px 9px; }
.err { color: var(--danger); font-size: 12px; }
.xt-main > .datagrid { flex: 1; min-height: 0; height: auto; }
.chartwrap { flex: 1; min-height: 0; padding: 12px; }
</style>
