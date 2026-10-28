<script setup lang="ts">
// The chart the assistant asks for together with a query: a fixed spec, without controls.
// The Viewer's panel is another thing — there the user builds the chart choosing columns
// and aggregations; here the model has made the choice already and the user looks at it.
// A panel of controls under every answer would be noise in a conversation.
//
// The rows are those shown in the table above already: no extra query, and the chart
// cannot disagree with the numbers next to it.
//
// The kinds past the first ones (combo, waterfall, funnel, heatmap, gauge, KPI, bullet)
// are drawn by composables/chartKinds.ts, as in the Viewer.
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { BarChart, LineChart, PieChart, ScatterChart, TreemapChart, GaugeChart, FunnelChart, HeatmapChart } from 'echarts/charts'
import { GridComponent, TooltipComponent, LegendComponent, VisualMapContinuousComponent } from 'echarts/components'
import VChart from 'vue-echarts'
import { Download } from 'lucide-vue-next'
import type { AiChartSpec } from '~/composables/useAi'
import { useChartTheme } from '~/composables/useChartTheme'
import { useFormat } from '~/composables/useFormat'
import KpiCard from '~/components/ui/KpiCard.vue'
import {
  PALETTE,
  bulletOption,
  comboOption,
  funnelOption,
  gaugeOption,
  heatmapOption,
  lastChange,
  toNumber,
  treemapOption,
  useKindCtx,
  waterfallOption,
  type KpiChange,
} from '~/composables/chartKinds'

use([
  CanvasRenderer, BarChart, LineChart, PieChart, ScatterChart, TreemapChart, GaugeChart, FunnelChart, HeatmapChart,
  GridComponent, TooltipComponent, LegendComponent, VisualMapContinuousComponent,
])

const props = defineProps<{ spec: AiChartSpec; rows: Record<string, any>[]; name?: string }>()
const { t } = useI18n()
const { ui } = useChartTheme()
const kindCtx = useKindCtx(ui)
const chart = ref<any>(null)

const num = (v: unknown) => (typeof v === 'number' ? v : Number(v))
const fmtx = useFormat()
const fmt = { format: (n: number) => fmtx.number(n, 2) } // in the interface's language
const label = (v: unknown) => String(v ?? '—')
// the periods of a KPI in their order, whatever the query's: numbers as numbers, texts
// with their digits read as numbers (2025-2 before 2025-10)
const byCell = (a: unknown, b: unknown) =>
  typeof a === 'number' && typeof b === 'number' ? a - b : label(a).localeCompare(label(b), undefined, { numeric: true })

// text → safe HTML: ECharts' formatters end up in innerHTML
const esc = (v: unknown) =>
  String(v).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c] as string)

/** The rows grouped by series, when the spec asks for one. */
const seriesList = computed(() => {
  const { x, y, series } = props.spec
  if (!series) return [{ name: y, rows: props.rows }]
  const bySeries = new Map<string, Record<string, any>[]>()
  for (const r of props.rows) {
    const k = String(r[series] ?? '—')
    if (!bySeries.has(k)) bySeries.set(k, [])
    bySeries.get(k)!.push(r)
  }
  return [...bySeries.entries()].map(([name, rows]) => ({ name, rows }))
    .sort((a, b) => a.name.localeCompare(b.name))
    .slice(0, PALETTE.length)
})

/** The KPI: the last period's value with its change and trend, or one row against `y2`. */
const kpi = computed(() => {
  const { type, x, y, y2 } = props.spec
  if (type !== 'kpi' || !props.rows.length) return null
  const k = kindCtx.value
  const versus = (value: number | null, base: unknown): KpiChange | null => {
    const b = toNumber(base)
    return value != null && b ? { ratio: (value - b) / Math.abs(b), text: k.t('chartPanel.vsValue', { name: y2 }) } : null
  }
  if (x && props.rows.length > 1) {
    const rows = [...props.rows].sort((a, b) => byCell(a[x], b[x]))
    const last = rows[rows.length - 1]
    const spark = { cats: rows.map((r) => label(r[x])), values: rows.map((r) => toNumber(r[y])) }
    const value = toNumber(last[y])
    return { label: `${y} · ${label(last[x])}`, value, spark, change: y2 ? versus(value, last[y2]) : lastChange(k, spark) }
  }
  const value = toNumber(props.rows[0][y])
  return { label: y, value, spark: null, change: y2 ? versus(value, props.rows[0][y2]) : null }
})

/** The kinds the Viewer shares, from the rows as the assistant returned them. */
function sharedOption() {
  const k = kindCtx.value
  const { type, x, y, y2, series } = props.spec
  const rows = props.rows
  const cats = rows.map((r) => label(r[x]))
  const values = (col?: string) => rows.map((r) => (col ? toNumber(r[col]) : null))
  switch (type) {
    case 'gauge':
      return gaugeOption(k, { value: toNumber(rows[0]?.[y]), name: y, target: y2 ? toNumber(rows[0]?.[y2]) : null,
                             max: props.spec.max ?? null }, true)
    case 'waterfall': return waterfallOption(k, { cats, values: values(y), name: y })
    case 'treemap': return treemapOption(k, { cats, values: values(y), name: y })
    case 'funnel': return funnelOption(k, { cats, values: values(y), name: y })
    case 'combo': return comboOption(k, { cats, bars: values(y), line: values(y2), barName: y, lineName: y2 ?? '' })
    case 'bullet':
      return bulletOption(k, { cats, values: values(y), targets: values(y2), name: y, targetName: y2 ?? '',
                              max: props.spec.max ?? null })
    case 'heatmap': {
      // the two axes in the order the rows bring them: the query chose it
      const xCats = [...new Set(cats)]
      const yCats = [...new Set(rows.map((r) => label(series ? r[series] : '')))]
      const xi = new Map(xCats.map((v, i) => [v, i]))
      const yi = new Map(yCats.map((v, i) => [v, i]))
      const cells: [number, number, number][] = []
      for (const r of rows) {
        const v = toNumber(r[y])
        if (v != null) cells.push([xi.get(label(r[x]))!, yi.get(label(series ? r[series] : ''))!, v])
      }
      return heatmapOption(k, { xCats, yCats, cells, name: y })
    }
  }
  return null
}

// a chart with a row per category grows with them, up to a point: past it the labels thin out
const height = computed(() => {
  const n = (col?: string) => new Set(props.rows.map((r) => label(col ? r[col] : ''))).size
  const { type, x, series } = props.spec
  if (type === 'bullet') return Math.min(Math.max(240, 30 * n(x) + 50), 520)
  if (type === 'heatmap') return Math.min(Math.max(240, 26 * n(series) + 90), 520)
  return 240
})

const option = computed(() => {
  const c = ui.value
  const { type, x, y } = props.spec
  const shared = sharedOption()
  if (shared) return shared
  // a long category cut with an ellipsis (the tooltip has it whole)
  const axis = { axisLine: { lineStyle: { color: c.border } }, axisLabel: { color: c.muted, fontSize: 11, hideOverlap: true, interval: 0, width: 120, overflow: 'truncate' as const } }
  const base = {
    backgroundColor: 'transparent',
    color: PALETTE,
    tooltip: {
      backgroundColor: c.panel, borderColor: c.border, textStyle: { color: c.text, fontSize: 12 },
    },
    ...(seriesList.value.length > 1 ? { legend: { textStyle: { color: c.muted, fontSize: 11 }, top: 0 } } : {}),
  }

  if (type === 'pie' || type === 'donut') {
    return {
      ...base,
      tooltip: { ...base.tooltip, trigger: 'item', formatter: (p: any) => `${esc(p.name)}: <b>${esc(fmt.format(p.value))}</b> (${p.percent}%)` },
      series: [{
        type: 'pie',
        radius: type === 'donut' ? ['46%', '72%'] : '72%',
        label: { color: c.text, fontSize: 11 },
        // a slice is a part above zero: ECharts drew an all-zero pie as equal slices
        data: props.rows.map((r) => ({ name: String(r[x] ?? '—'), value: num(r[y]) })).filter((d) => d.value > 0),
      }],
    }
  }

  if (type === 'scatter') {
    return {
      ...base,
      tooltip: { ...base.tooltip, trigger: 'item', formatter: (p: any) => `${esc(p.data[2] ?? '')}<br/>${esc(x)}: <b>${esc(fmt.format(p.data[0]))}</b><br/>${esc(y)}: <b>${esc(fmt.format(p.data[1]))}</b>` },
      grid: { left: 8, right: 16, top: 16, bottom: 8, containLabel: true },
      xAxis: { type: 'value', ...axis, splitLine: { lineStyle: { color: c.borderSoft } } },
      yAxis: { type: 'value', ...axis, splitLine: { lineStyle: { color: c.borderSoft } } },
      series: [{ type: 'scatter', symbolSize: 9, itemStyle: { opacity: 0.75 }, data: props.rows.map((r) => [num(r[x]), num(r[y]), r[x]]) }],
    }
  }

  // horizontal bars: the categories sit on the Y axis, and that is why they exist — long
  // labels can be read
  const horizontal = type === 'hbar'
  const categories = [...new Set(props.rows.map((r) => String(r[x] ?? '—')))]
  const values = { type: 'value' as const, ...axis, splitLine: { lineStyle: { color: c.borderSoft } } }
  const cat = { type: 'category' as const, data: categories, ...axis }
  return {
    ...base,
    tooltip: { ...base.tooltip, trigger: 'axis', axisPointer: { type: 'shadow' } },
    grid: { left: 8, right: 16, top: seriesList.value.length > 1 ? 26 : 12, bottom: 8, containLabel: true },
    xAxis: horizontal ? values : cat,
    yAxis: horizontal ? cat : values,
    series: seriesList.value.map((s) => ({
      name: s.name,
      type: type === 'line' || type === 'area' ? 'line' : 'bar',
      smooth: false,
      ...(type === 'area' ? { areaStyle: { opacity: 0.18 } } : {}),
      data: categories.map((k) => {
        const r = s.rows.find((row) => String(row[x] ?? '—') === k)
        return r ? num(r[y]) : null
      }),
    })),
  }
})

/**
 * The PNG is what is seen, at double density: a downloaded chart ends up in a
 * presentation, where 1× looks grainy.
 */
function downloadPng() {
  const inst = chart.value?.chart ?? chart.value
  if (!inst?.getDataURL) return
  const url = inst.getDataURL({ type: 'png', pixelRatio: 2, backgroundColor: ui.value.panel })
  const a = document.createElement('a')
  a.href = url
  a.download = `${(props.name || 'chart').replace(/[^a-z0-9._-]+/gi, '_')}.png`.toLowerCase()
  a.click()
}

// the theme changes underfoot: the chart does not read the CSS variables, it receives them
watch(ui, () => chart.value?.setOption?.(option.value, true))

defineExpose({ downloadPng })
</script>

<template>
  <figure class="mini">
    <figcaption v-if="spec.title">{{ spec.title }}</figcaption>
    <!-- a KPI is text in the page, with its trend line: there is no picture of it to save -->
    <KpiCard v-if="kpi" dense :label="kpi.label" :value="kpi.value" :change="kpi.change" :spark="kpi.spark" />
    <p v-else-if="!rows.length" class="nodata">{{ t('chartPanel.noRows') }}</p>
    <template v-else>
      <VChart ref="chart" class="canvas" :style="{ height: `${height}px` }" :option="option" autoresize />
      <button type="button" class="png" :title="t('chat.downloadPng')" :aria-label="t('chat.downloadPng')" @click="downloadPng">
        <Download :size="12" />
      </button>
    </template>
  </figure>
</template>

<style scoped>
.mini { position: relative; margin: 8px 0 0; padding: 0; }
.mini figcaption { font-size: 12px; color: var(--muted); margin-bottom: 4px; }
.canvas { height: 240px; width: 100%; }
.nodata { margin: 0; font-size: 12px; color: var(--muted); }
/* the button sits on the chart but not over the data: top corner, it appears on hover */
.png {
  position: absolute; top: 0; right: 0; padding: 3px 6px; min-height: 22px;
  opacity: 0; transition: opacity 0.15s ease;
}
.mini:hover .png, .png:focus-visible { opacity: 1; }
@media (pointer: coarse) { .png { opacity: 1; } }
</style>
