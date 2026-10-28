<script setup lang="ts">
// The selected node's chart. ALL the kinds work on aggregates the engine computes over
// the WHOLE dataset (a group_by appended to the node's chain, streaming + cache) — never
// on the preview's sample. The scatter is aggregated too: every point is a group (e.g. a
// product), X and Y two aggregations. The gauge and the KPI are ONE aggregate over the
// whole dataset (the KPI's trend line a second query, by period).
//
// The kinds past the first six (combo, waterfall, funnel, heatmap, KPI, gauge, bullet) are
// drawn by composables/chartKinds.ts, which the chat's chart shares; their look follows
// the reports' presets.
import { ref, computed, watch, h, toRaw, type FunctionalComponent, type VNode } from 'vue'
import { useI18n } from 'vue-i18n'
import { useFormat } from '~/composables/useFormat'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { BarChart, LineChart, PieChart, TreemapChart, ScatterChart, GaugeChart, FunnelChart, HeatmapChart } from 'echarts/charts'
import { GridComponent, TooltipComponent, LegendComponent, VisualMapContinuousComponent } from 'echarts/components'
import VChart from 'vue-echarts'
import {
  RefreshCw,
  BarChart3,
  ChartLine,
  ChartArea,
  ChartPie,
  LayoutGrid,
  ChartScatter,
  ChartNoAxesCombined,
  Funnel,
  Grid3x3,
  TrendingUp,
  Gauge,
} from 'lucide-vue-next'
import type { ColumnInfo, Operation, PreviewResult } from '~/composables/useApi'
import type { ChartSettings } from '~/composables/useSavedViews'
import { errMessage } from '~/composables/useApi'
import { AGG_LABELS } from '~/composables/useFlowModel'
import { useChartTheme } from '~/composables/useChartTheme'
import KpiCard from '~/components/ui/KpiCard.vue'
import {
  PALETTE,
  bulletOption,
  comboOption,
  funnelOption,
  gaugeOption,
  heatmapOption,
  lastChange,
  notAboveZero,
  pieSlices,
  toNumber,
  treemapOption,
  useKindCtx,
  waterfallOption,
  type PieTotals,
  type Spark,
} from '~/composables/chartKinds'

use([
  CanvasRenderer, BarChart, LineChart, PieChart, TreemapChart, ScatterChart, GaugeChart, FunnelChart, HeatmapChart,
  GridComponent, TooltipComponent, LegendComponent, VisualMapContinuousComponent,
])

const { t } = useI18n()
const fmtx = useFormat()

const props = defineProps<{
  columns: ColumnInfo[] // columns out of the selected node
  query: (ops: Operation[], limit?: number) => Promise<PreviewResult | null>
  // the chart's settings, for who keeps them (a saved view): given back when it opens
  settings?: ChartSettings | null
}>()
const emit = defineEmits<{ (e: 'update:settings', s: ChartSettings): void }>()

// PALETTE (chartKinds.ts): the VALIDATED categorical palette, MAX 5 series — never cycle
// the hues past the slots, the extra ones are flagged.
const OTHER_COLOR = '#8b93a7' // neutral grey for the "Other" slice
const MAX_SERIES = PALETTE.length

// "chrome" colours (axes, texts, tooltip backgrounds) from the current theme: reading the
// tokens lives in a shared composable — it was copied identically here, in ScheduleLoad
// and in audit.vue, and was missing altogether in RunCalendar and RunGantt.
const { ui } = useChartTheme()

// Two kinds lucide has no picture for, drawn in its grammar (a 24-unit box, 2px round
// strokes, the bars as strokes on the L of the axes, like its chart-column)
const lucideLike = (paths: string[]): FunctionalComponent<{ size?: number }> => (p) =>
  h('svg', {
    xmlns: 'http://www.w3.org/2000/svg', width: p.size ?? 24, height: p.size ?? 24, viewBox: '0 0 24 24',
    fill: 'none', stroke: 'currentColor', 'stroke-width': 2, 'stroke-linecap': 'round', 'stroke-linejoin': 'round',
  }, paths.map((d): VNode => h('path', { d })))
const AXES = 'M3 3v16a2 2 0 0 0 2 2h16'
const WaterfallIcon = lucideLike([AXES, 'M8 17V9', 'M13 9v3', 'M18 17v-5']) // up, down, the total
const BulletIcon = lucideLike([AXES, 'M7 8h8', 'M18 5v6', 'M7 15h5', 'M15 12v6']) // a bar each, and its target

// the first six as they always were, then the kinds that compose, then those of one number
const CHART_GROUPS = [
  [
    { id: 'bar', icon: BarChart3 },
    { id: 'line', icon: ChartLine },
    { id: 'area', icon: ChartArea },
    { id: 'pie', icon: ChartPie },
    { id: 'treemap', icon: LayoutGrid },
    { id: 'scatter', icon: ChartScatter },
  ],
  [
    { id: 'combo', icon: ChartNoAxesCombined },
    { id: 'waterfall', icon: WaterfallIcon },
    { id: 'funnel', icon: Funnel },
    { id: 'heatmap', icon: Grid3x3 },
  ],
  [
    { id: 'kpi', icon: TrendingUp },
    { id: 'gauge', icon: Gauge },
    { id: 'bullet', icon: BulletIcon },
  ],
] as const
type ChartType = (typeof CHART_GROUPS)[number][number]['id']
const KNOWN_TYPES = new Set<string>(CHART_GROUPS.flat().map((ct) => ct.id))

const chartTypeLabels = computed<Record<ChartType, string>>(() => ({
  bar: t('chartPanel.barChart'),
  line: t('chartPanel.lineChart'),
  area: t('chartPanel.areaChart'),
  pie: t('chartPanel.pieChart'),
  treemap: t('chartPanel.treemapChart'),
  scatter: t('chartPanel.scatterChart'),
  combo: t('chartPanel.comboChart'),
  waterfall: t('chartPanel.waterfallChart'),
  funnel: t('chartPanel.funnelChart'),
  heatmap: t('chartPanel.heatmapChart'),
  kpi: t('chartPanel.kpiChart'),
  gauge: t('chartPanel.gaugeChart'),
  bullet: t('chartPanel.bulletChart'),
}))

const AGGS = ['count', 'sum', 'mean', 'min', 'max', 'median', 'n_unique']
const ADDITIVE = new Set(['count', 'sum']) // combinable client-side (the "Other" slice)

// function options with a readable label (without count for the scatter's axes)
const funcOptions = (noCount = false) =>
  AGGS.filter((a) => !noCount || a !== 'count').map((a) => ({ value: a, label: AGG_LABELS[a] ?? a }))

const chartType = ref<ChartType>('bar')
const xCol = ref('') // dimension (or "point" for the scatter)
const byCol = ref('') // stratification into series (empty = none)
const func = ref('count')
const yCol = ref('')
// scatter: two independent aggregations
const xFunc = ref('sum')
const xNumCol = ref('')
const yFunc = ref('sum')
const yNumCol = ref('')
const topN = ref(20)
// a second measure: the combo's line, the bullet's target
const func2 = ref('sum')
const y2Col = ref('')
// the KPI's periods: its trend line and its change (empty = the number alone)
const trendCol = ref('')
// the gauge's scale and target (null = automatic, none)
const scaleMin = ref<number | null>(null)
const scaleMax = ref<number | null>(null)
const target = ref<number | null>(null)

const numOrNull = (v: unknown) => (typeof v === 'number' && Number.isFinite(v) ? v : null)

// the settings in from who keeps them, and out at every change. A view saved before the
// later kinds has none of their fields: they go back to their starting values, and what
// it had opens as it was.
function applySettings(s: ChartSettings | null | undefined) {
  if (!s) return
  if (KNOWN_TYPES.has(s.type)) chartType.value = s.type as ChartType
  for (const [r, v] of [[xCol, s.x], [byCol, s.by], [func, s.func], [yCol, s.y], [xFunc, s.xFunc],
                        [xNumCol, s.xNum], [yFunc, s.yFunc], [yNumCol, s.yNum]] as const) {
    if (typeof v === 'string') r.value = v
  }
  if (typeof s.topN === 'number') topN.value = s.topN
  func2.value = typeof s.func2 === 'string' ? s.func2 : 'sum'
  y2Col.value = typeof s.y2 === 'string' ? s.y2 : ''
  trendCol.value = typeof s.trend === 'string' ? s.trend : ''
  scaleMin.value = numOrNull(s.min)
  scaleMax.value = numOrNull(s.max)
  target.value = numOrNull(s.target)
}
// What goes out comes back as the prop: applied again, an echo would be harmless while
// one change makes one emit — but a kind that picks its column by itself makes two in a
// row, and the first echo, arriving after, put the column back to empty. The settings
// this panel emitted are not applied again.
const emitted = new WeakSet<object>()
applySettings(props.settings)
watch(() => props.settings, (s) => {
  if (!s || !emitted.has(toRaw(s))) applySettings(s)
})
watch([chartType, xCol, byCol, func, yCol, xFunc, xNumCol, yFunc, yNumCol, topN, func2, y2Col, trendCol, scaleMin,
       scaleMax, target], () => {
  const s: ChartSettings = {
    type: chartType.value, x: xCol.value, by: byCol.value, func: func.value, y: yCol.value, xFunc: xFunc.value,
    xNum: xNumCol.value, yFunc: yFunc.value, yNum: yNumCol.value, topN: topN.value, func2: func2.value,
    y2: y2Col.value, trend: trendCol.value, min: scaleMin.value, max: scaleMax.value, target: target.value,
  }
  emitted.add(s)
  emit('update:settings', s)
})

const numericCols = computed(() =>
  props.columns.filter((c) => /Int|Float|Decimal/i.test(c.dtype)).map((c) => c.name),
)
const allCols = computed(() => props.columns.map((c) => c.name))

const isScatter = computed(() => chartType.value === 'scatter')
// one number over the whole dataset: no X
const isOne = computed(() => chartType.value === 'gauge' || chartType.value === 'kpi')
// the X stratified into series (bar, line, area, scatter), or the heatmap's rows (required
// there); the others read one category, the X itself
const supportsBy = computed(() => ['bar', 'line', 'area', 'scatter', 'heatmap'].includes(chartType.value))
const needsY = computed(() => {
  if (isScatter.value) return false
  // over the whole dataset there is no X whose values to count: a distinct count needs its column
  if (isOne.value) return func.value !== 'count'
  return func.value !== 'count' && func.value !== 'n_unique'
})
const yOptions = computed(() => (isOne.value && func.value === 'n_unique' ? allCols.value : numericCols.value))
const usesY2 = computed(() => chartType.value === 'combo' || chartType.value === 'bullet')
const needsY2 = computed(() => usesY2.value && func2.value !== 'count' && func2.value !== 'n_unique')
const y2Label = computed(() => (chartType.value === 'combo' ? t('chartPanel.lineMeasure') : t('chartPanel.target')))
// the «top» limits what is drawn: shown wherever it does (a line split in series draws them all)
const showsTop = computed(() =>
  ['bar', 'treemap', 'combo', 'waterfall', 'funnel', 'heatmap', 'bullet'].includes(chartType.value) ||
  ((chartType.value === 'line' || chartType.value === 'area') && !byCol.value))

// a kind that reads one more column starts with one chosen, instead of an empty chart
watch(chartType, (kind) => {
  if (kind === 'heatmap' && !byCol.value) {
    const others = allCols.value.filter((c) => c !== xCol.value)
    byCol.value = others.find((c) => !numericCols.value.includes(c)) ?? others[0] ?? ''
  }
  if (usesY2.value && needsY2.value && !y2Col.value) {
    y2Col.value = numericCols.value.find((c) => c !== yCol.value) ?? numericCols.value[0] ?? ''
  }
})

const rows = ref<Record<string, any>[]>([])
const spark = ref<Spark | null>(null) // the KPI's trend
const pieTotals = ref<PieTotals | null>(null) // the pie's every category, for «Other» and those left out
// What the rows are made of. Moving to a kind that reads other columns, the rows of the
// one before are not drawn by it (a bar's first category as a gauge's number) while the
// new ones are on their way; between kinds of the same shape (bars → lines) the chart
// stays up and changes when they arrive, as it always did.
const shapeOf = (kind: ChartType) =>
  kind === 'gauge' || kind === 'kpi' ? 'one' : kind === 'scatter' ? 'points' : kind === 'heatmap' ? 'grid'
    : kind === 'combo' || kind === 'bullet' ? 'two' : 'one-measure'
const rowsShape = ref('')
const drawable = computed(() => rows.value.length > 0 && rowsShape.value === shapeOf(chartType.value))
// the query answered, with no row (the filters leave none): «No data», not «choose the columns»
const noRows = computed(() => !rows.value.length && rowsShape.value === shapeOf(chartType.value))
const loading = ref(false)
const error = ref('')
const droppedSeries = ref(0) // series past the palette's 5 slots
const capped = ref(false) // the heatmap got only its largest cells

const HEAT_CELLS = 1000
const SPARK_POINTS = 24
// the gauge's and the KPI's group: a constant column (a group_by with no key is not
// something every engine takes); `count` counts it, that is the rows
const ALL = '__all'
const ONE_GROUP: Operation = { type: 'compute', params: { columns: [{ name: ALL, expr: '1' }] } }

// Only the LAST refresh writes the state. The chart's queries now cancel each other
// (whoever hosts the panel sends them on a slot): without this guard a cancelled request,
// coming back AFTER the newest, would write "error" and empty the rows above the chart
// just drawn.
let refreshSeq = 0
async function refresh() {
  const seq = ++refreshSeq
  const shape = shapeOf(chartType.value)
  error.value = ''
  droppedSeries.value = 0
  capped.value = false
  if ((!xCol.value && !isOne.value) || (needsY.value && !yCol.value) || (isScatter.value && (!xNumCol.value || !yNumCol.value)) ||
      (chartType.value === 'heatmap' && !byCol.value) || (needsY2.value && !y2Col.value)) {
    // incomplete configuration: the query in flight (if any) has an old seq and would no
    // longer turn the spinner off — it is turned off here, and the rows do not stay stale
    loading.value = false
    rows.value = []
    rowsShape.value = ''
    spark.value = null
    return
  }
  loading.value = true
  try {
    if (isOne.value) {
      await refreshOne(seq, shape)
      return
    }
    const by = supportsBy.value && byCol.value ? [byCol.value] : []
    let ops: Operation[]
    if (isScatter.value) {
      // every point = a group; X and Y = two aggregations over the whole dataset
      ops = [
        {
          type: 'group_by',
          params: {
            by: [xCol.value, ...by],
            aggregations: [
              { column: xNumCol.value, func: xFunc.value, alias: '__x' },
              { column: yNumCol.value, func: yFunc.value, alias: '__y' },
            ],
          },
        },
        { type: 'sort', params: { by: '__x', descending: true } },
        { type: 'limit', params: { n: 1000 } },
      ]
    } else {
      const aggCol = yCol.value || xCol.value
      const aggregations = [{ column: aggCol, func: func.value, alias: '__value' }]
      if (usesY2.value) aggregations.push({ column: y2Col.value || xCol.value, func: func2.value, alias: '__value2' })
      // the sequences go along their X; the rest from the largest
      const alongX = ['line', 'area', 'combo', 'waterfall'].includes(chartType.value)
      ops = [
        { type: 'group_by', params: { by: [xCol.value, ...by], aggregations } },
        alongX
          ? { type: 'sort', params: { by: xCol.value, descending: false } }
          : { type: 'sort', params: { by: '__value', descending: true } },
        { type: 'limit', params: { n: chartType.value === 'heatmap' ? HEAT_CELLS : by.length ? 1000 : Math.max(topN.value, 1) } },
      ]
    }
    const res = await props.query(ops, 1000)
    if (seq !== refreshSeq) return
    // the pie's «Other»: the measure over every category, which the rows (the first
    // ones only) cannot add up to. After the first query: on the same slot, two together
    // would cancel each other
    const totals = chartType.value === 'pie' ? await pieTotalsOf(yCol.value || xCol.value) : null
    if (seq === refreshSeq) {
      rows.value = res?.rows ?? []
      rowsShape.value = shape
      spark.value = null
      pieTotals.value = totals
      capped.value = chartType.value === 'heatmap' && rows.value.length >= HEAT_CELLS
    }
  } catch (e) {
    if (seq === refreshSeq) {
      rows.value = []
      rowsShape.value = ''
      spark.value = null
      error.value = errMessage(e)
    }
  } finally {
    if (seq === refreshSeq) loading.value = false
  }
}

// Over every category: the measure of those above zero added up, how many they are and
// how many categories there are in all — the categories' aggregates, then one group of
// them all (a pie draws only what is above zero)
async function pieTotalsOf(column: string): Promise<PieTotals | null> {
  const res = await props.query([
    { type: 'group_by', params: { by: [xCol.value], aggregations: [{ column, func: func.value, alias: '__value' }] } },
    { type: 'compute', params: { columns: [
      { name: ALL, expr: '1' },
      { name: '__up', expr: 'CASE WHEN __value > 0 THEN 1 ELSE 0 END' },
      { name: '__pos', expr: 'CASE WHEN __value > 0 THEN __value ELSE 0 END' },
    ] } },
    { type: 'group_by', params: { by: [ALL], aggregations: [
      { column: '__pos', func: 'sum', alias: '__total' },
      { column: '__up', func: 'sum', alias: '__cats' },
      { column: ALL, func: 'count', alias: '__every' },
    ] } },
  ], 1)
  const row = res?.rows?.[0]
  const value = toNumber(row?.__total)
  const count = toNumber(row?.__cats)
  const every = toNumber(row?.__every)
  return value == null || count == null ? null : { value, count, ...(every != null ? { every } : {}) }
}

// The gauge's and the KPI's number: one aggregate over the whole dataset. Then, for a KPI
// with its periods, the same aggregate per period: the last SPARK_POINTS of them (asked
// from the latest, drawn in their order). One query after the other: on the same slot,
// two together would cancel each other.
async function refreshOne(seq: number, shape: string) {
  const agg = { column: needsY.value ? yCol.value : ALL, func: func.value, alias: '__value' }
  const total = await props.query([ONE_GROUP, { type: 'group_by', params: { by: [ALL], aggregations: [agg] } }], 1)
  if (seq !== refreshSeq) return
  let trend: Spark | null = null
  const period = chartType.value === 'kpi' ? trendCol.value : ''
  if (period) {
    const res = await props.query([
      ONE_GROUP,
      { type: 'group_by', params: { by: [period], aggregations: [agg] } },
      { type: 'sort', params: { by: period, descending: true } },
      { type: 'limit', params: { n: SPARK_POINTS } },
    ], SPARK_POINTS)
    if (seq !== refreshSeq) return
    const points = (res?.rows ?? []).filter((r) => r[period] != null).reverse()
    trend = { cats: points.map((r) => String(r[period])), values: points.map((r) => toNumber(r.__value)) }
  }
  rows.value = total?.rows ?? []
  rowsShape.value = shape
  spark.value = trend
}

watch(
  () => props.columns,
  (cols) => {
    if (!cols.length) return
    if (!cols.some((c) => c.name === xCol.value)) xCol.value = cols[0].name
    for (const r of [yCol, byCol, xNumCol, yNumCol, y2Col, trendCol]) {
      if (r.value && !cols.some((c) => c.name === r.value)) r.value = ''
    }
  },
  { immediate: true },
)
// immediate: the columns' watch above already set the axes before this one existed, and the
// chart stayed blank («Choose the columns…») with them chosen, until a control changed
// (the gauge's scale and target are drawn, not asked: they do not query again)
watch([chartType, xCol, byCol, yCol, func, xFunc, xNumCol, yFunc, yNumCol, topN, func2, y2Col, trendCol], refresh,
      { immediate: true })

// Text → safe HTML. Needed ONLY inside ECharts' tooltip formatters, which are the only
// point of the frontend where we build HTML from a string.
const esc = (v: unknown) =>
  String(v).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c] as string)

const fmt = { format: (n: number) => fmtx.number(n, 2) } // in the interface's language

const seriesName = computed(() => {
  if (isScatter.value) return `${yFunc.value}(${yNumCol.value})`
  if (func.value === 'count') return t('chartPanel.countLabel')
  return `${func.value}(${yCol.value || xCol.value})`
})
const seriesName2 = computed(() =>
  func2.value === 'count' ? t('chartPanel.countLabel') : `${func2.value}(${y2Col.value || xCol.value})`)

// what the shared builders (chartKinds.ts) need: the theme and the numbers in the interface's language
const kindCtx = useKindCtx(ui)

const kpi = computed(() => {
  if (chartType.value !== 'kpi' || !drawable.value) return null
  return {
    value: toNumber(rows.value[0].__value),
    spark: spark.value?.cats.length ? spark.value : null,
    change: spark.value ? lastChange(kindCtx.value, spark.value) : null,
  }
})

// the heatmap's two axes: the topN categories of each with the largest totals, in their
// natural order (months, days, names: 2 before 10)
function heatAxes() {
  const pick = (col: string) => {
    const totals = new Map<string, number>()
    for (const r of rows.value) {
      const k = String(r[col])
      totals.set(k, (totals.get(k) ?? 0) + Math.abs(toNumber(r.__value) ?? 0))
    }
    return [...totals.entries()].sort((a, b) => b[1] - a[1]).slice(0, Math.max(topN.value, 1)).map(([k]) => k)
      .sort((a, b) => a.localeCompare(b, undefined, { numeric: true }))
  }
  const xCats = pick(xCol.value)
  const yCats = pick(byCol.value)
  const xi = new Map(xCats.map((c, i) => [c, i]))
  const yi = new Map(yCats.map((c, i) => [c, i]))
  const cells: [number, number, number][] = []
  for (const r of rows.value) {
    const x = xi.get(String(r[xCol.value]))
    const y = yi.get(String(r[byCol.value]))
    const v = toNumber(r.__value)
    if (x !== undefined && y !== undefined && v != null) cells.push([x, y, v])
  }
  return { xCats, yCats, cells }
}

// ── Client-side pivot for the "by" (top MAX_SERIES by total) ─────────────────
function pivotBydata() {
  const by = byCol.value
  const totals = new Map<string, number>()
  for (const r of rows.value) {
    const k = String(r[by])
    totals.set(k, (totals.get(k) ?? 0) + Math.abs(Number(r.__value) || 0))
  }
  const ranked = [...totals.entries()].sort((a, b) => b[1] - a[1]).map(([k]) => k)
  const kept = ranked.slice(0, MAX_SERIES)
  droppedSeries.value = Math.max(0, ranked.length - kept.length)

  // X categories: for lines = natural order (sorted by X already); for bars = by total
  const catTotals = new Map<string, number>()
  const catOrder: string[] = []
  for (const r of rows.value) {
    const c = String(r[xCol.value])
    if (!catTotals.has(c)) catOrder.push(c)
    catTotals.set(c, (catTotals.get(c) ?? 0) + (Number(r.__value) || 0))
  }
  const cats =
    chartType.value === 'bar'
      ? [...catTotals.entries()].sort((a, b) => b[1] - a[1]).slice(0, topN.value).map(([c]) => c)
      : catOrder

  const catIdx = new Map(cats.map((c, i) => [c, i]))
  const series = kept.map((k) => ({ name: k, data: cats.map(() => null as number | null) }))
  const sIdx = new Map(kept.map((k, i) => [k, i]))
  for (const r of rows.value) {
    const si = sIdx.get(String(r[by]))
    const ci = catIdx.get(String(r[xCol.value]))
    if (si !== undefined && ci !== undefined) series[si].data[ci] = r.__value
  }
  return { cats, series }
}

// ── the parts of a whole: only what is above zero ───────────────────────────
// the pie: top-5 categories + "Other", every category past them over the whole dataset
// (only for additive functions: an average of averages would be wrong → only the top-5)
const pie = computed(() => pieSlices(
  rows.value.map((r) => ({ name: String(r[xCol.value]), value: toNumber(r.__value) })),
  MAX_SERIES,
  pieTotals.value,
  ADDITIVE.has(func.value),
))
const PARTS = new Set<ChartType>(['pie', 'treemap', 'funnel'])
// the categories a pie, a treemap or a funnel leaves out (at zero or below, or empty), and
// none to draw at all: an all-zero pie was drawn as equal slices
const notDrawn = computed(() => {
  if (!drawable.value || !PARTS.has(chartType.value)) return 0
  if (chartType.value === 'pie') return pie.value.leftOut
  return notAboveZero(rows.value.slice(0, Math.max(topN.value, 1)).map((r) => toNumber(r.__value)))
})
const nothingToDraw = computed(() => drawable.value && PARTS.has(chartType.value)
  && !rows.value.some((r) => (toNumber(r.__value) ?? 0) > 0))

// ── ECharts option ──────────────────────────────────────────────────────────
// "chrome" styles derived from the current theme (computed on `ui`)
const AXIS_STYLE = computed(() => ({
  // a long category cut with an ellipsis (the tooltip has it whole): uncut, all the labels
  // but one were hidden, that one clipped at the chart's edge
  axisLabel: { color: ui.value.muted, hideOverlap: true, interval: 0, width: 120, overflow: 'truncate' as const },
  axisLine: { lineStyle: { color: ui.value.border } },
  axisTick: { show: false },
}))
const TOOLTIP_BASE = computed(() => ({
  backgroundColor: ui.value.panel2,
  borderColor: ui.value.border,
  textStyle: { color: ui.value.text, fontSize: 12 },
  valueFormatter: (v: any) => fmt.format(v),
}))
const LEGEND_BASE = computed(() => ({ textStyle: { color: ui.value.muted, fontSize: 11 }, top: 0, icon: 'circle' }))

const option = computed(() => {
  const kind = chartType.value
  const AXIS = AXIS_STYLE.value
  const TOOLTIP = TOOLTIP_BASE.value
  const LEGEND = LEGEND_BASE.value
  const c = ui.value

  // the later kinds: drawn by the builders the chat shares
  const kc = kindCtx.value
  const catsOf = () => rows.value.map((r) => String(r[xCol.value]))
  const valsOf = (key = '__value') => rows.value.map((r) => toNumber(r[key]))
  switch (kind) {
    case 'kpi': return {} // a KpiCard, not a chart
    case 'gauge':
      return gaugeOption(kc, { value: toNumber(rows.value[0]?.__value), name: seriesName.value, min: scaleMin.value,
                              max: scaleMax.value, target: target.value })
    case 'waterfall': return waterfallOption(kc, { cats: catsOf(), values: valsOf(), name: seriesName.value })
    case 'funnel': return funnelOption(kc, { cats: catsOf(), values: valsOf(), name: seriesName.value })
    case 'heatmap': return heatmapOption(kc, { ...heatAxes(), name: seriesName.value })
    case 'combo':
      return comboOption(kc, { cats: catsOf(), bars: valsOf(), line: valsOf('__value2'), barName: seriesName.value,
                              lineName: seriesName2.value })
    case 'bullet':
      return bulletOption(kc, { cats: catsOf(), values: valsOf(), targets: valsOf('__value2'), name: seriesName.value,
                               targetName: seriesName2.value })
  }

  if (kind === 'pie') {
    const { slices, other } = pie.value
    const data = slices.map((s, i) => ({ ...s, itemStyle: { color: PALETTE[i] } }))
    if (other) {
      data.push({ name: t('chartPanel.otherGroup', { n: other.count }), value: other.value, itemStyle: { color: OTHER_COLOR } })
    }
    return {
      backgroundColor: 'transparent',
      tooltip: { ...TOOLTIP, trigger: 'item' },
      series: [{
        name: seriesName.value,
        type: 'pie',
        radius: ['42%', '72%'], // donut
        itemStyle: { borderColor: c.panel, borderWidth: 2 }, // spacer between slices
        label: { color: c.text, fontSize: 11, formatter: '{b}\n{d}%' },
        labelLine: { lineStyle: { color: c.border } },
        data,
      }],
    }
  }
  if (kind === 'treemap') {
    const top = rows.value.slice(0, Math.max(topN.value, 1))
    return treemapOption(kc, { cats: top.map((r) => String(r[xCol.value])), values: top.map((r) => toNumber(r.__value)),
                               name: seriesName.value })
  }

  if (kind === 'scatter') {
    const by = byCol.value
    let seriesDefs: { name: string; rows: Record<string, any>[] }[]
    if (by) {
      const groups = new Map<string, Record<string, any>[]>()
      for (const r of rows.value) {
        const k = String(r[by])
        if (!groups.has(k)) groups.set(k, [])
        groups.get(k)!.push(r)
      }
      const ranked = [...groups.entries()].sort((a, b) => b[1].length - a[1].length)
      droppedSeries.value = Math.max(0, ranked.length - MAX_SERIES)
      seriesDefs = ranked.slice(0, MAX_SERIES).map(([name, rs]) => ({ name, rows: rs }))
    } else {
      seriesDefs = [{ name: seriesName.value, rows: rows.value }]
    }
    return {
      backgroundColor: 'transparent',
      color: PALETTE,
      grid: { left: 8, right: 16, top: seriesDefs.length > 1 ? 30 : 24, bottom: 8, containLabel: true },
      ...(seriesDefs.length > 1 ? { legend: LEGEND } : {}),
      tooltip: {
        ...TOOLTIP,
        trigger: 'item',
        // esc() REQUIRED on every dynamic piece: a FUNCTION formatter does not go through
        // ECharts' escaping (it does that only on the default formatter) and the result ends up
        // in innerHTML. `p.data.name` is a cell's raw value and the column names come from the
        // dataset: without escaping, an `<img src=x onerror=...>` in a text column runs script in
        // whoever hovers on a dot.
        // Audit 2026-09-19, A6.
        formatter: (p: any) =>
          `${esc(p.data.name)}<br/>${esc(xFunc.value)}(${esc(xNumCol.value)}): <b>${esc(fmt.format(p.data.value[0]))}</b>` +
          `<br/>${esc(yFunc.value)}(${esc(yNumCol.value)}): <b>${esc(fmt.format(p.data.value[1]))}</b>`,
      },
      xAxis: { type: 'value', ...AXIS, splitLine: { lineStyle: { color: c.borderSoft } } },
      yAxis: { type: 'value', ...AXIS, splitLine: { lineStyle: { color: c.border } } },
      series: seriesDefs.map((s) => ({
        name: s.name,
        type: 'scatter',
        symbolSize: 9,
        itemStyle: { opacity: 0.75, borderColor: c.panel, borderWidth: 1 },
        data: s.rows.map((r) => ({ name: String(r[xCol.value]), value: [r.__x, r.__y] })),
      })),
    }
  }

  // bar / line / area
  const isBar = kind === 'bar'
  let cats: string[]
  let seriesList: any[]
  if (byCol.value) {
    const piv = pivotBydata()
    cats = piv.cats
    seriesList = piv.series.map((s) =>
      isBar
        ? { name: s.name, type: 'bar', data: s.data, barMaxWidth: 20, itemStyle: { borderRadius: [3, 3, 0, 0] } }
        : {
            name: s.name,
            type: 'line',
            data: s.data,
            lineStyle: { width: 2 },
            symbol: 'circle',
            symbolSize: 7,
            showSymbol: cats.length <= 30,
            ...(kind === 'area' ? { areaStyle: { opacity: 0.12 } } : {}),
          },
    )
  } else {
    cats = rows.value.map((r) => String(r[xCol.value]))
    const vals = rows.value.map((r) => r.__value)
    seriesList = [
      isBar
        ? { name: seriesName.value, type: 'bar', data: vals, barMaxWidth: 28, itemStyle: { borderRadius: [4, 4, 0, 0] } }
        : {
            name: seriesName.value,
            type: 'line',
            data: vals,
            lineStyle: { width: 2 },
            symbol: 'circle',
            symbolSize: 8,
            showSymbol: vals.length <= 30,
            ...(kind === 'area' ? { areaStyle: { opacity: 0.15 } } : {}),
          },
    ]
  }
  return {
    backgroundColor: 'transparent',
    color: PALETTE,
    grid: { left: 8, right: 16, top: seriesList.length > 1 ? 30 : 28, bottom: 8, containLabel: true },
    ...(seriesList.length > 1 ? { legend: LEGEND } : {}), // ≥2 series → legend always
    tooltip: {
      ...TOOLTIP,
      trigger: isBar ? 'item' : 'axis',
      axisPointer: isBar ? undefined : { type: 'cross', label: { backgroundColor: c.panel2 } },
    },
    xAxis: { type: 'category', data: cats, ...AXIS },
    yAxis: {
      type: 'value',
      axisLabel: { color: c.muted, formatter: (v: number) => fmt.format(v) },
      splitLine: { lineStyle: { color: c.border } },
    },
    series: seriesList,
  }
})
</script>

<template>
  <div class="chartpanel">
    <!-- controls in one row above the chart -->
    <div class="controls">
      <div class="typebtns" role="group" :aria-label="$t('chartPanel.chartType')">
        <template v-for="(group, gi) in CHART_GROUPS" :key="gi">
          <span v-if="gi" class="typesep" aria-hidden="true" />
          <button
            v-for="ct in group"
            :key="ct.id"
            type="button"
            :class="{ active: chartType === ct.id }"
            :title="chartTypeLabels[ct.id]"
            :aria-label="chartTypeLabels[ct.id]"
            :aria-pressed="chartType === ct.id"
            @click="chartType = ct.id"
          ><component :is="ct.icon" :size="14" aria-hidden="true" /></button>
        </template>
      </div>

      <template v-if="!isOne">
        <label>{{ isScatter ? $t('chartPanel.point') : 'X' }}</label>
        <Select v-model="xCol" :options="allCols" class="csel" />
      </template>

      <template v-if="isScatter">
        <label>X</label>
        <Select v-model="xFunc" :options="funcOptions(true)" class="fsel" />
        <Select v-model="xNumCol" :options="numericCols" :placeholder="$t('chartPanel.numericColumnPlaceholder')" class="csel" />
        <label>Y</label>
        <Select v-model="yFunc" :options="funcOptions(true)" class="fsel" />
        <Select v-model="yNumCol" :options="numericCols" :placeholder="$t('chartPanel.numericColumnPlaceholder')" class="csel" />
      </template>

      <template v-else>
        <label>f</label>
        <Select v-model="func" :options="funcOptions()" class="fsel" />
        <template v-if="needsY">
          <label>Y</label>
          <Select v-model="yCol" :options="yOptions" :placeholder="$t('chartPanel.numericColumnPlaceholder')" class="csel" />
        </template>
      </template>

      <!-- the second measure: the combo's line, the bullet's target (its name and its pickers
           wrap together, never the name alone at the end of a row) -->
      <span v-if="usesY2" class="cgrp">
        <label>{{ y2Label }}</label>
        <Select v-model="func2" :options="funcOptions()" class="fsel" :aria-label="`${y2Label}: f`" />
        <Select v-if="needsY2" v-model="y2Col" :options="numericCols" :placeholder="$t('chartPanel.numericColumnPlaceholder')" class="csel"
                :aria-label="`${y2Label}: Y`" />
      </span>

      <template v-if="supportsBy">
        <label>by</label>
        <Select
          v-model="byCol"
          :options="chartType === 'heatmap' ? allCols.filter((c) => c !== xCol) : [{ value: '', label: '—' }, ...allCols.filter((c) => c !== xCol)]"
          class="csel"
        />
      </template>

      <span v-if="chartType === 'kpi'" class="cgrp">
        <label>{{ $t('chartPanel.trend') }}</label>
        <Select v-model="trendCol" :options="[{ value: '', label: '—' }, ...allCols]" class="csel" :aria-label="$t('chartPanel.trend')" />
      </span>

      <template v-if="chartType === 'gauge'">
        <label class="numf">{{ $t('chartPanel.min') }}
          <input type="number" class="numin" :value="scaleMin ?? ''" :placeholder="$t('chartPanel.auto')"
                 @input="scaleMin = numOrNull(($event.target as HTMLInputElement).valueAsNumber)" /></label>
        <label class="numf">{{ $t('chartPanel.max') }}
          <input type="number" class="numin" :value="scaleMax ?? ''" :placeholder="$t('chartPanel.auto')"
                 @input="scaleMax = numOrNull(($event.target as HTMLInputElement).valueAsNumber)" /></label>
        <label class="numf">{{ $t('chartPanel.target') }}
          <input type="number" class="numin" :value="target ?? ''" placeholder="—"
                 @input="target = numOrNull(($event.target as HTMLInputElement).valueAsNumber)" /></label>
      </template>

      <template v-if="showsTop">
        <label>top</label>
        <input v-model.number="topN" type="number" min="1" max="200" class="topn" />
      </template>

      <button class="mini" :title="$t('chartPanel.refresh')" :aria-label="$t('chartPanel.refresh')" @click="refresh"><RefreshCw :size="13" /></button>
      <span v-if="loading" class="muted">{{ $t('chartPanel.computingFullDataset') }}</span>
      <span v-else-if="error" class="err">{{ error }}</span>
      <span v-else-if="droppedSeries" class="muted">{{ $t('chartPanel.droppedSeries', { n: droppedSeries }) }}</span>
      <span v-else-if="capped" class="muted">{{ $t('chartPanel.largestCells', { n: HEAT_CELLS }) }}</span>
      <span v-else-if="notDrawn && !nothingToDraw" class="muted">{{ $t('chartPanel.notDrawn', { n: notDrawn }, notDrawn) }}</span>
    </div>

    <div class="plot">
      <KpiCard v-if="kpi" :label="seriesName" :value="kpi.value" :change="kpi.change" :spark="kpi.spark" />
      <p v-else-if="nothingToDraw" class="muted empty">{{ $t('chartPanel.nothingAboveZero') }}</p>
      <p v-else-if="noRows && !loading && !error" class="muted empty">{{ $t('chartPanel.noRows') }}</p>
      <VChart v-else-if="drawable" :option="option" autoresize />
      <p v-else-if="!loading" class="muted empty">
        {{ $t('chartPanel.chooseColumnsHint') }}
      </p>
    </div>
  </div>
</template>

<style scoped>
.chartpanel { display: flex; flex-direction: column; height: 100%; gap: 6px; }
.controls {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}
.controls > label { font-size: 11px; color: var(--muted); }
.controls select { width: auto; max-width: 180px; padding: 4px 8px; font-size: 12px; }
.controls select.fsel { max-width: 110px; }
.topn { width: 64px; padding: 4px 8px; font-size: 12px; }
.typebtns { display: flex; align-items: center; gap: 2px; flex-wrap: wrap; }
.typebtns button { padding: 4px 8px; }
/* the three families of kinds: what compares, what composes, the one number */
.typesep { width: 1px; height: 18px; margin: 0 4px; background: var(--border); }
.numf, .cgrp { display: inline-flex; align-items: center; gap: 6px; }
.cgrp > label { font-size: 11px; color: var(--muted); }
.numin { width: 96px; padding: 4px 8px; font-size: 12px; }
.typebtns button.active { border-color: var(--accent); background: color-mix(in srgb, var(--accent) 12%, transparent); }
button.mini { padding: 4px 8px; }
.plot { flex: 1; min-height: 0; }
.plot > * { width: 100%; height: 100%; }
.empty { padding: 18px; font-size: 12px; }
.err { color: var(--danger); font-size: 12px; }
</style>
