<script setup lang="ts">
// Grafico del nodo selezionato. TUTTI i tipi lavorano su aggregati calcolati
// dall'engine sull'INTERO dataset (group_by appesa alla catena del nodo,
// streaming + cache) — mai sul campione della preview. Anche lo scatter è
// aggregato: ogni punto è un gruppo (es. un prodotto), X e Y due aggregazioni.
import { ref, computed, watch } from 'vue'
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
import { errMessage } from '~/composables/useApi'
import { AGG_LABELS } from '~/composables/useFlowModel'

use([CanvasRenderer, BarChart, LineChart, PieChart, TreemapChart, ScatterChart, GridComponent, TooltipComponent, LegendComponent])

const props = defineProps<{
  columns: ColumnInfo[] // colonne in uscita dal nodo selezionato
  query: (ops: Operation[], limit?: number) => Promise<PreviewResult | null>
}>()

// palette categorica VALIDATA per superficie scura (lightness band, CVD, contrasto).
// MAX 5 serie: mai ciclare le tinte oltre gli slot — le eccedenti si segnalano.
const PALETTE = ['#3987e5', '#199e70', '#c98500', '#9085e9', '#d55181']
const OTHER_COLOR = '#8b93a7' // grigio neutro per la fetta "Altro"
const MAX_SERIES = PALETTE.length

const CHART_TYPES = [
  { id: 'bar', label: 'Barre', icon: BarChart3 },
  { id: 'line', label: 'Linee', icon: ChartLine },
  { id: 'area', label: 'Area', icon: ChartArea },
  { id: 'pie', label: 'Torta', icon: ChartPie },
  { id: 'treemap', label: 'Treemap', icon: LayoutGrid },
  { id: 'scatter', label: 'Scatter', icon: ChartScatter },
] as const
type ChartType = (typeof CHART_TYPES)[number]['id']

const AGGS = ['count', 'sum', 'mean', 'min', 'max', 'median', 'n_unique']
const ADDITIVE = new Set(['count', 'sum']) // combinabili client-side (fetta "Altro")

// opzioni funzione col label leggibile (senza count per gli assi dello scatter)
const funcOptions = (noCount = false) =>
  AGGS.filter((a) => !noCount || a !== 'count').map((a) => ({ value: a, label: AGG_LABELS[a] ?? a }))

const chartType = ref<ChartType>('bar')
const xCol = ref('') // dimensione (o "punto" per lo scatter)
const byCol = ref('') // stratificazione in serie (vuoto = nessuna)
const func = ref('count')
const yCol = ref('')
// scatter: due aggregazioni indipendenti
const xFunc = ref('sum')
const xNumCol = ref('')
const yFunc = ref('sum')
const yNumCol = ref('')
const topN = ref(20)

const numericCols = computed(() =>
  props.columns.filter((c) => /Int|Float|Decimal/i.test(c.dtype)).map((c) => c.name),
)
const allCols = computed(() => props.columns.map((c) => c.name))

const isScatter = computed(() => chartType.value === 'scatter')
const isPartWhole = computed(() => chartType.value === 'pie' || chartType.value === 'treemap')
const supportsBy = computed(() => !isPartWhole.value) // pie/treemap: la categoria È la X
const needsY = computed(() => !isScatter.value && func.value !== 'count' && func.value !== 'n_unique')

const rows = ref<Record<string, any>[]>([])
const loading = ref(false)
const error = ref('')
const droppedSeries = ref(0) // serie oltre i 5 slot della palette

async function refresh() {
  error.value = ''
  droppedSeries.value = 0
  if (!xCol.value) return
  if (needsY.value && !yCol.value) return
  if (isScatter.value && (!xNumCol.value || !yNumCol.value)) return
  loading.value = true
  try {
    const by = supportsBy.value && byCol.value ? [byCol.value] : []
    let ops: Operation[]
    if (isScatter.value) {
      // ogni punto = un gruppo; X e Y = due aggregazioni sull'intero dataset
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
    rows.value = res?.rows ?? []
  } catch (e) {
    rows.value = []
    error.value = errMessage(e)
  } finally {
    loading.value = false
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
watch([chartType, xCol, byCol, yCol, func, xFunc, xNumCol, yFunc, yNumCol, topN], refresh)

const fmt = new Intl.NumberFormat('it-IT', { maximumFractionDigits: 2 })

const seriesName = computed(() => {
  if (isScatter.value) return `${yFunc.value}(${yNumCol.value})`
  if (func.value === 'count') return 'conteggio'
  return `${func.value}(${yCol.value || xCol.value})`
})

// ── Pivot client-side per il "by" (top MAX_SERIES per totale) ───────────────
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

  // categorie X: per linee = ordine naturale (già sortato per X); per barre = per totale
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

// ── Opzione ECharts ──────────────────────────────────────────────────────────
const AXIS_STYLE = {
  axisLabel: { color: '#8b93a7', hideOverlap: true },
  axisLine: { lineStyle: { color: '#262e40' } },
  axisTick: { show: false },
}
const TOOLTIP_BASE = {
  backgroundColor: '#1b2130',
  borderColor: '#262e40',
  textStyle: { color: '#e8ebf2', fontSize: 12 },
  valueFormatter: (v: any) => fmt.format(v),
}
const LEGEND_BASE = { textStyle: { color: '#8b93a7', fontSize: 11 }, top: 0, icon: 'circle' }

const option = computed(() => {
  const t = chartType.value

  if (t === 'pie' || t === 'treemap') {
    // top-5 categorie + "Altro" (solo per funzioni additive: una media di medie
    // sarebbe sbagliata → in quel caso solo le top-5)
    const top = rows.value.slice(0, MAX_SERIES)
    const rest = rows.value.slice(MAX_SERIES)
    const data = top.map((r, i) => ({
      name: String(r[xCol.value]),
      value: r.__valore,
      itemStyle: t === 'pie' ? { color: PALETTE[i] } : undefined,
    }))
    if (rest.length && ADDITIVE.has(func.value)) {
      data.push({
        name: `Altro (${rest.length})`,
        value: rest.reduce((s, r) => s + (Number(r.__valore) || 0), 0),
        itemStyle: t === 'pie' ? { color: OTHER_COLOR } : undefined,
      })
    }
    if (t === 'pie') {
      return {
        backgroundColor: 'transparent',
        tooltip: { ...TOOLTIP_BASE, trigger: 'item' },
        series: [{
          name: seriesName.value,
          type: 'pie',
          radius: ['42%', '72%'], // donut
          itemStyle: { borderColor: '#141926', borderWidth: 2 }, // spacer tra fette
          label: { color: '#e8ebf2', fontSize: 11, formatter: '{b}\n{d}%' },
          labelLine: { lineStyle: { color: '#3d4a66' } },
          data,
        }],
      }
    }
    // treemap = magnitudine → UNA tinta con shading per valore (non identità)
    return {
      backgroundColor: 'transparent',
      tooltip: { ...TOOLTIP_BASE, trigger: 'item' },
      series: [{
        name: seriesName.value,
        type: 'treemap',
        roam: false,
        nodeClick: false,
        breadcrumb: { show: false },
        itemStyle: { color: PALETTE[0], borderColor: '#141926', borderWidth: 2 },
        colorAlpha: [0.45, 1], // shading sequenziale per valore
        label: { color: '#e8ebf2', fontSize: 11, formatter: '{b}' },
        data: rows.value.slice(0, Math.max(topN.value, 1)).map((r) => ({
          name: String(r[xCol.value]),
          value: r.__valore,
        })),
      }],
    }
  }

  if (t === 'scatter') {
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
      ...(seriesDefs.length > 1 ? { legend: LEGEND_BASE } : {}),
      tooltip: {
        ...TOOLTIP_BASE,
        trigger: 'item',
        formatter: (p: any) =>
          `${p.data.name}<br/>${xFunc.value}(${xNumCol.value}): <b>${fmt.format(p.data.value[0])}</b>` +
          `<br/>${yFunc.value}(${yNumCol.value}): <b>${fmt.format(p.data.value[1])}</b>`,
      },
      xAxis: { type: 'value', ...AXIS_STYLE, splitLine: { lineStyle: { color: '#1e2534' } } },
      yAxis: { type: 'value', ...AXIS_STYLE, splitLine: { lineStyle: { color: '#262e40' } } },
      series: seriesDefs.map((s) => ({
        name: s.name,
        type: 'scatter',
        symbolSize: 9,
        itemStyle: { opacity: 0.75, borderColor: '#141926', borderWidth: 1 },
        data: s.rows.map((r) => ({ name: String(r[xCol.value]), value: [r.__x, r.__y] })),
      })),
    }
  }

  // bar / line / area
  const isBar = t === 'bar'
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
            ...(t === 'area' ? { areaStyle: { opacity: 0.12 } } : {}),
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
            ...(t === 'area' ? { areaStyle: { opacity: 0.15 } } : {}),
          },
    ]
  }
  return {
    backgroundColor: 'transparent',
    color: PALETTE,
    grid: { left: 8, right: 16, top: seriesList.length > 1 ? 30 : 28, bottom: 8, containLabel: true },
    ...(seriesList.length > 1 ? { legend: LEGEND_BASE } : {}), // ≥2 serie → legenda sempre
    tooltip: {
      ...TOOLTIP_BASE,
      trigger: isBar ? 'item' : 'axis',
      axisPointer: isBar ? undefined : { type: 'cross', label: { backgroundColor: '#1b2130' } },
    },
    xAxis: { type: 'category', data: cats, ...AXIS_STYLE },
    yAxis: {
      type: 'value',
      axisLabel: { color: '#8b93a7', formatter: (v: number) => fmt.format(v) },
      splitLine: { lineStyle: { color: '#262e40' } },
    },
    series: seriesList,
  }
})
</script>

<template>
  <div class="chartpanel">
    <!-- controlli in una riga sopra il grafico -->
    <div class="controls">
      <div class="typebtns">
        <button
          v-for="t in CHART_TYPES"
          :key="t.id"
          :class="{ active: chartType === t.id }"
          :title="t.label"
          @click="chartType = t.id"
        ><component :is="t.icon" :size="14" /></button>
      </div>

      <label>{{ isScatter ? 'punto' : 'X' }}</label>
      <Select v-model="xCol" :options="allCols" class="csel" />

      <template v-if="isScatter">
        <label>X</label>
        <Select v-model="xFunc" :options="funcOptions(true)" class="fsel" />
        <Select v-model="xNumCol" :options="numericCols" placeholder="colonna numerica" class="csel" />
        <label>Y</label>
        <Select v-model="yFunc" :options="funcOptions(true)" class="fsel" />
        <Select v-model="yNumCol" :options="numericCols" placeholder="colonna numerica" class="csel" />
      </template>

      <template v-else>
        <label>f</label>
        <Select v-model="func" :options="funcOptions()" class="fsel" />
        <template v-if="needsY">
          <label>Y</label>
          <Select v-model="yCol" :options="numericCols" placeholder="colonna numerica" class="csel" />
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

      <button class="mini" title="Aggiorna" @click="refresh"><RefreshCw :size="13" /></button>
      <span v-if="loading" class="muted">calcolo sull'intero dataset…</span>
      <span v-else-if="error" class="err">{{ error }}</span>
      <span v-else-if="droppedSeries" class="muted">+{{ droppedSeries }} serie oltre le 5 mostrate</span>
    </div>

    <div class="plot">
      <VChart v-if="rows.length" :option="option" autoresize />
      <p v-else-if="!loading" class="muted empty">
        Scegli le colonne — il grafico è calcolato dall'engine su tutte le righe, non sul campione.
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
.typebtns button.active { border-color: var(--accent); background: rgba(79, 140, 255, 0.12); }
button.mini { padding: 4px 8px; }
.plot { flex: 1; min-height: 0; }
.plot > * { width: 100%; height: 100%; }
.empty { padding: 18px; font-size: 12px; }
.err { color: var(--danger); font-size: 12px; }
</style>
