<script setup lang="ts">
// The selected node's chart. ALL the kinds work on aggregates the engine computes over
// the WHOLE dataset (a group_by appended to the node's chain, streaming + cache) — never
// on the preview's sample. The scatter is aggregated too: every point is a group (e.g. a
// product), X and Y two aggregations.
import { ref, computed, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useFormat } from '~/composables/useFormat'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { BarChart, LineChart, PieChart, TreemapChart, ScatterChart } from 'echarts/charts'
import { GridComponent, TooltipComponent, LegendComponent } from 'echarts/components'
import VChart from 'vue-echarts'
import {
  RefreshCw,
  BarChart3,
  ChartLine,
  ChartArea,
  ChartPie,
  LayoutGrid,
  ChartScatter,
} from 'lucide-vue-next'
import type { ColumnInfo, Operation, PreviewResult } from '~/composables/useApi'
import type { ChartSettings } from '~/composables/useSavedViews'
import { errMessage } from '~/composables/useApi'
import { AGG_LABELS } from '~/composables/useFlowModel'
import { useChartTheme } from '~/composables/useChartTheme'

use([CanvasRenderer, BarChart, LineChart, PieChart, TreemapChart, ScatterChart, GridComponent, TooltipComponent, LegendComponent])

const { t } = useI18n()
const fmtx = useFormat()

const props = defineProps<{
  columns: ColumnInfo[] // columns out of the selected node
  query: (ops: Operation[], limit?: number) => Promise<PreviewResult | null>
  // the chart's settings, for who keeps them (a saved view): given back when it opens
  settings?: ChartSettings | null
}>()
const emit = defineEmits<{ (e: 'update:settings', s: ChartSettings): void }>()

// VALIDATED categorical palette (lightness band, CVD, contrast): mid-tone hues that stay
// readable on both dark and light backgrounds. MAX 5 series: never cycle the hues past
// the slots — the extra ones are flagged.
const PALETTE = ['#3987e5', '#199e70', '#c98500', '#9085e9', '#d55181']
const OTHER_COLOR = '#8b93a7' // neutral grey for the "Other" slice
const MAX_SERIES = PALETTE.length

// "chrome" colours (axes, texts, tooltip backgrounds) from the current theme: reading the
// tokens lives in a shared composable — it was copied identically here, in ScheduleLoad
// and in audit.vue, and was missing altogether in RunCalendar and RunGantt.
const { ui } = useChartTheme()

const CHART_TYPES = [
  { id: 'bar', icon: BarChart3 },
  { id: 'line', icon: ChartLine },
  { id: 'area', icon: ChartArea },
  { id: 'pie', icon: ChartPie },
  { id: 'treemap', icon: LayoutGrid },
  { id: 'scatter', icon: ChartScatter },
] as const
type ChartType = (typeof CHART_TYPES)[number]['id']

const chartTypeLabels = computed<Record<ChartType, string>>(() => ({
  bar: t('chartPanel.barChart'),
  line: t('chartPanel.lineChart'),
  area: t('chartPanel.areaChart'),
  pie: t('chartPanel.pieChart'),
  treemap: t('chartPanel.treemapChart'),
  scatter: t('chartPanel.scatterChart'),
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

// the settings in from who keeps them, and out at every change
function applySettings(s: ChartSettings | null | undefined) {
  if (!s) return
  chartType.value = (s.type as ChartType) || chartType.value
  for (const [r, v] of [[xCol, s.x], [byCol, s.by], [func, s.func], [yCol, s.y], [xFunc, s.xFunc],
                        [xNumCol, s.xNum], [yFunc, s.yFunc], [yNumCol, s.yNum]] as const) {
    if (typeof v === 'string') r.value = v
  }
  if (typeof s.topN === 'number') topN.value = s.topN
}
applySettings(props.settings)
watch(() => props.settings, (s) => applySettings(s))
watch([chartType, xCol, byCol, func, yCol, xFunc, xNumCol, yFunc, yNumCol, topN], () => emit('update:settings', {
  type: chartType.value, x: xCol.value, by: byCol.value, func: func.value, y: yCol.value, xFunc: xFunc.value,
  xNum: xNumCol.value, yFunc: yFunc.value, yNum: yNumCol.value, topN: topN.value,
}))

const numericCols = computed(() =>
  props.columns.filter((c) => /Int|Float|Decimal/i.test(c.dtype)).map((c) => c.name),
)
const allCols = computed(() => props.columns.map((c) => c.name))

const isScatter = computed(() => chartType.value === 'scatter')
const isPartWhole = computed(() => chartType.value === 'pie' || chartType.value === 'treemap')
const supportsBy = computed(() => !isPartWhole.value) // pie/treemap: the category IS the X
const needsY = computed(() => !isScatter.value && func.value !== 'count' && func.value !== 'n_unique')

const rows = ref<Record<string, any>[]>([])
const loading = ref(false)
const error = ref('')
const droppedSeries = ref(0) // series past the palette's 5 slots

// Only the LAST refresh writes the state. The chart's queries now cancel each other
// (whoever hosts the panel sends them on a slot): without this guard a cancelled request,
// coming back AFTER the newest, would write "error" and empty the rows above the chart
// just drawn.
let refreshSeq = 0
async function refresh() {
  const seq = ++refreshSeq
  error.value = ''
  droppedSeries.value = 0
  if (!xCol.value || (needsY.value && !yCol.value) || (isScatter.value && (!xNumCol.value || !yNumCol.value))) {
    // incomplete configuration: the query in flight (if any) has an old seq and would no
    // longer turn the spinner off — it is turned off here, and the rows do not stay stale
    loading.value = false
    rows.value = []
    return
  }
  loading.value = true
  try {
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
      ops = [
        {
          type: 'group_by',
          params: { by: [xCol.value, ...by], aggregations: [{ column: aggCol, func: func.value, alias: '__valore' }] },
        },
        chartType.value === 'line' || chartType.value === 'area'
          ? { type: 'sort', params: { by: xCol.value, descending: false } }
          : { type: 'sort', params: { by: '__valore', descending: true } },
        { type: 'limit', params: { n: by.length ? 1000 : Math.max(topN.value, 1) } },
      ]
    }
    const res = await props.query(ops, 1000)
    if (seq === refreshSeq) rows.value = res?.rows ?? []
  } catch (e) {
    if (seq === refreshSeq) {
      rows.value = []
      error.value = errMessage(e)
    }
  } finally {
    if (seq === refreshSeq) loading.value = false
  }
}

watch(
  () => props.columns,
  (cols) => {
    if (!cols.length) return
    if (!cols.some((c) => c.name === xCol.value)) xCol.value = cols[0].name
    for (const r of [yCol, byCol, xNumCol, yNumCol]) {
      if (r.value && !cols.some((c) => c.name === r.value)) r.value = ''
    }
  },
  { immediate: true },
)
// immediate: the columns' watch above already set the axes before this one existed, and the
// chart stayed blank («Choose the columns…») with them chosen, until a control changed
watch([chartType, xCol, byCol, yCol, func, xFunc, xNumCol, yFunc, yNumCol, topN], refresh, { immediate: true })

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

// ── Client-side pivot for the "by" (top MAX_SERIES by total) ─────────────────
function pivotBydata() {
  const by = byCol.value
  const totals = new Map<string, number>()
  for (const r of rows.value) {
    const k = String(r[by])
    totals.set(k, (totals.get(k) ?? 0) + Math.abs(Number(r.__valore) || 0))
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
    catTotals.set(c, (catTotals.get(c) ?? 0) + (Number(r.__valore) || 0))
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
    if (si !== undefined && ci !== undefined) series[si].data[ci] = r.__valore
  }
  return { cats, series }
}

// ── ECharts option ──────────────────────────────────────────────────────────
// "chrome" styles derived from the current theme (computed on `ui`)
const AXIS_STYLE = computed(() => ({
  axisLabel: { color: ui.value.muted, hideOverlap: true },
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

  if (kind === 'pie' || kind === 'treemap') {
    // top-5 categories + "Other" (only for additive functions: an average of averages would
    // be wrong → in that case only the top-5)
    const top = rows.value.slice(0, MAX_SERIES)
    const rest = rows.value.slice(MAX_SERIES)
    const data = top.map((r, i) => ({
      name: String(r[xCol.value]),
      value: r.__valore,
      itemStyle: kind === 'pie' ? { color: PALETTE[i] } : undefined,
    }))
    if (rest.length && ADDITIVE.has(func.value)) {
      data.push({
        name: t('chartPanel.otherGroup', { n: rest.length }),
        value: rest.reduce((s, r) => s + (Number(r.__valore) || 0), 0),
        itemStyle: kind === 'pie' ? { color: OTHER_COLOR } : undefined,
      })
    }
    if (kind === 'pie') {
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
    // treemap = magnitude → ONE hue with shading by value (not identity)
    return {
      backgroundColor: 'transparent',
      tooltip: { ...TOOLTIP, trigger: 'item' },
      series: [{
        name: seriesName.value,
        type: 'treemap',
        roam: false,
        nodeClick: false,
        breadcrumb: { show: false },
        itemStyle: { color: PALETTE[0], borderColor: c.panel, borderWidth: 2 },
        colorAlpha: [0.45, 1], // sequential shading by value
        label: { color: c.text, fontSize: 11, formatter: '{b}' },
        data: rows.value.slice(0, Math.max(topN.value, 1)).map((r) => ({
          name: String(r[xCol.value]),
          value: r.__valore,
        })),
      }],
    }
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
    const vals = rows.value.map((r) => r.__valore)
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
      <div class="typebtns">
        <button
          v-for="ct in CHART_TYPES"
          :key="ct.id"
          :class="{ active: chartType === ct.id }"
          :title="chartTypeLabels[ct.id]"
          @click="chartType = ct.id"
        ><component :is="ct.icon" :size="14" /></button>
      </div>

      <label>{{ isScatter ? $t('chartPanel.point') : 'X' }}</label>
      <Select v-model="xCol" :options="allCols" class="csel" />

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
          <Select v-model="yCol" :options="numericCols" :placeholder="$t('chartPanel.numericColumnPlaceholder')" class="csel" />
        </template>
      </template>

      <template v-if="supportsBy">
        <label>by</label>
        <Select
          v-model="byCol"
          :options="[{ value: '', label: '—' }, ...allCols.filter((c) => c !== xCol)]"
          class="csel"
        />
      </template>

      <template v-if="chartType === 'bar' || chartType === 'treemap'">
        <label>top</label>
        <input v-model.number="topN" type="number" min="1" max="200" class="topn" />
      </template>

      <button class="mini" :title="$t('chartPanel.refresh')" :aria-label="$t('chartPanel.refresh')" @click="refresh"><RefreshCw :size="13" /></button>
      <span v-if="loading" class="muted">{{ $t('chartPanel.computingFullDataset') }}</span>
      <span v-else-if="error" class="err">{{ error }}</span>
      <span v-else-if="droppedSeries" class="muted">{{ $t('chartPanel.droppedSeries', { n: droppedSeries }) }}</span>
    </div>

    <div class="plot">
      <VChart v-if="rows.length" :option="option" autoresize />
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
.typebtns { display: flex; gap: 2px; }
.typebtns button { padding: 4px 8px; }
.typebtns button.active { border-color: var(--accent); background: color-mix(in srgb, var(--accent) 12%, transparent); }
button.mini { padding: 4px 8px; }
.plot { flex: 1; min-height: 0; }
.plot > * { width: 100%; height: 100%; }
.empty { padding: 18px; font-size: 12px; }
.err { color: var(--danger); font-size: 12px; }
</style>
