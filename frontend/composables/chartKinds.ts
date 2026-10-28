// The chart kinds the Viewer's panel (ChartPanel) and the chat's chart (MiniChart) share
// past the first ones: combo, waterfall, funnel, heatmap, gauge, bullet, and the KPI's
// trend line. Pure functions from numbers aggregated already to an ECharts option, so the
// two places draw a kind the same way. The layouts follow the reports' presets
// (backend/app/reports/charts.py, the source of truth there: gauge on a 240° arc with the
// value in the middle, waterfall with the total last, bullet on a second category axis…),
// in the app's colours: the validated palette for the data, the theme's tokens for the
// chrome and for up and down.
//
// The tooltips' formatters build HTML that ECharts puts in innerHTML: every dynamic piece
// goes through `esc` (a category is a cell's raw value — audit 2026-09-19, A6). The labels
// drawn on the canvas are text, never markup.
import { computed, type ComputedRef, type Ref } from 'vue'
import { useI18n } from 'vue-i18n'
import type { ChartUi } from '~/composables/useChartTheme'
import { withAlpha } from '~/composables/useChartTheme'
import { useFormat } from '~/composables/useFormat'

// VALIDATED categorical palette (lightness band, CVD, contrast): mid-tone hues that stay
// readable on both dark and light backgrounds. MAX 5 series: never cycle the hues past
// the slots.
export const PALETTE = ['#3987e5', '#199e70', '#c98500', '#9085e9', '#d55181']

/** Text → safe HTML, for the tooltips' formatters only. */
export const esc = (v: unknown) =>
  String(v).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c] as string)

/** A number out of a cell, or null: an empty cell, a text or NaN draws nothing. */
export function toNumber(v: unknown): number | null {
  if (v == null || v === '' || typeof v === 'boolean') return null
  const n = typeof v === 'number' ? v : Number(v)
  return Number.isFinite(n) ? n : null
}

/** What the builders need from whoever draws: the theme, and numbers in the interface's language. */
export interface KindCtx {
  ui: ChartUi
  /** in full: 1,234,567.89 */
  num: (n: number) => string
  /** in short, where the room is little (labels, scales): 1.2M */
  short: (n: number) => string
  /** in short with three figures, for a headline: 5.01M */
  brief: (n: number) => string
  /** a share: 0.125 → 12.5% */
  pct: (n: number) => string
  t: (key: string, params?: Record<string, unknown>) => string
}

/** The context of the component that draws: its theme, the interface's language. */
export function useKindCtx(ui: Ref<ChartUi>): ComputedRef<KindCtx> {
  const { t } = useI18n()
  const fmtx = useFormat()
  return computed(() => {
    const compact = new Intl.NumberFormat(fmtx.locale.value, { notation: 'compact', maximumFractionDigits: 1 })
    const brief = new Intl.NumberFormat(fmtx.locale.value, { notation: 'compact', maximumSignificantDigits: 3 })
    const percent = new Intl.NumberFormat(fmtx.locale.value, { style: 'percent', maximumFractionDigits: 1 })
    return {
      ui: ui.value,
      num: (n) => fmtx.number(n, 2),
      short: (n) => compact.format(n),
      brief: (n) => brief.format(n),
      pct: (n) => percent.format(n),
      t,
    }
  })
}

/** The figure a headline shows: in full while it reads at a glance, else in short. */
export const headline = (k: KindCtx, n: number) => (k.num(n).length > 10 ? k.brief(n) : k.num(n))

/** The end of a scale a little past `v` (1, 1.2, 1.5, 2, 2.5… × a power of ten), as in the reports. */
export function niceMax(v: number): number {
  if (!(v > 0)) return 1
  const magnitude = 10 ** Math.floor(Math.log10(v))
  for (const step of [1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10]) {
    if (step * magnitude >= v * (1 - 1e-12)) return step * magnitude
  }
  return 10 * magnitude
}

const tooltip = (c: ChartUi) => ({ backgroundColor: c.panel2, borderColor: c.border, textStyle: { color: c.text, fontSize: 12 } })
const legend = (c: ChartUi) => ({ textStyle: { color: c.muted, fontSize: 11 }, top: 0, icon: 'circle' })
const axis = (c: ChartUi) => ({
  axisLabel: { color: c.muted, hideOverlap: true },
  axisLine: { lineStyle: { color: c.border } },
  axisTick: { show: false },
})
// categories along the side: a long name is cut with an ellipsis (the tooltip has it whole)
// instead of squeezing the plot into a strip
const sideLabels = (c: ChartUi) => ({ color: c.muted, width: 180, overflow: 'truncate' as const })
const valueAxis = (k: KindCtx, short = false) => ({
  type: 'value' as const,
  axisLabel: { color: k.ui.muted, formatter: (v: number) => (short ? k.short(v) : k.num(v)) },
  splitLine: { lineStyle: { color: k.ui.border } },
})

// ── gauge: one number on its scale ───────────────────────────────────────────
export interface GaugeData {
  value: number | null
  name: string
  min?: number | null
  max?: number | null
  target?: number | null
}

/** `dense`: the chat's size, where the whole gauge is a couple of hundred pixels. */
export function gaugeOption(k: KindCtx, d: GaugeData, dense = false) {
  const c = k.ui
  const band = dense ? 10 : 16
  const v = d.value ?? 0
  const target = d.target ?? null
  const lo = d.min ?? Math.min(0, v, target ?? 0)
  let hi = d.max ?? niceMax(Math.max(v, target ?? 0, lo) * 1.1)
  if (!(hi > lo)) hi = lo + (Math.abs(lo) || 1)
  const arc = { min: lo, max: hi, startAngle: 210, endAngle: -30, radius: '86%', center: ['50%', '58%'] }
  const bare = { axisTick: { show: false }, splitLine: { show: false }, anchor: { show: false } }
  // the value and its target in the same notation («5.01M» beside «6,000,000» reads as two
  // scales); in short when either is long, and always in the chat's small gauge
  const short = dense || [v, target ?? 0].some((n) => k.num(n).length > 10)
  const fig = (n: number) => (short ? k.brief(n) : k.num(n))
  // the texts under the value, clear of the scale's two ends (about half the radius down)
  const at = dense ? { detail: '-10%', title: '16%', target: '33%' } : { detail: '-2%', title: '26%', target: '44%' }
  const series: any[] = [{
    type: 'gauge',
    name: d.name,
    ...arc,
    ...bare,
    splitNumber: 1, // the scale's two ends, nothing in between
    axisLine: { roundCap: true, lineStyle: { width: band, color: [[1, withAlpha(PALETTE[0], 0.18)]] } },
    progress: { show: true, roundCap: true, width: band, itemStyle: { color: PALETTE[0] } },
    pointer: { show: false },
    // the scale's two ends just outside the arc's: inside, they ran into the texts under the value
    axisLabel: { distance: -(band + 20), color: c.muted, fontSize: dense ? 10 : 11, formatter: (x: number) => k.short(x) },
    title: { show: !!d.name, offsetCenter: [0, at.title], color: c.muted, fontSize: dense ? 11 : 13 },
    detail: {
      offsetCenter: [0, at.detail],
      fontSize: dense ? 22 : 34,
      fontWeight: 600,
      color: c.text,
      formatter: () => (d.value == null ? '—' : fig(d.value)),
    },
    data: [{ value: v, name: d.name }],
  }]
  if (target != null) {
    // the target: a tick across the arc (a second gauge with only its pointer, as the
    // reports draw it), and its value under the measure's name
    const share = target ? ` · ${k.pct(v / target)}` : ''
    series.push({
      type: 'gauge',
      ...arc,
      ...bare,
      silent: true,
      axisLine: { show: false },
      progress: { show: false },
      axisLabel: { show: false },
      // across the band, to its outer edge ([0.89, 1.01] of the radius): past it, it ran into
      // the scale's end when the target is the end
      pointer: { show: true, icon: 'rect', length: '12%', width: 3, offsetCenter: [0, '-89%'], itemStyle: { color: c.text } },
      title: { show: false },
      detail: {
        offsetCenter: [0, at.target],
        fontSize: dense ? 11 : 12,
        fontWeight: 'normal',
        color: c.muted,
        formatter: () => k.t('chartPanel.targetValue', { value: fig(target) }) + share,
      },
      data: [{ value: target }],
    })
  }
  return {
    backgroundColor: 'transparent',
    tooltip: {
      ...tooltip(c),
      trigger: 'item',
      formatter: () =>
        `${esc(d.name)}: <b>${esc(d.value == null ? '—' : k.num(d.value))}</b>` +
        (target != null ? `<br/>${esc(k.t('chartPanel.targetValue', { value: k.num(target) }))}` : ''),
    },
    series,
  }
}

// ── waterfall: signed steps, then the total ───────────────────────────────────
export function waterfallOption(k: KindCtx, d: { cats: string[]; values: (number | null)[]; name: string }) {
  const c = k.ui
  const signed = (n: number) => (n > 0 ? '+' : n < 0 ? '−' : '') + k.short(Math.abs(n))
  const bases: number[] = []
  const bars: { value: number; t: string; full: string; itemStyle: { color: string } }[] = []
  let running = 0
  for (const raw of d.values) {
    const v = raw ?? 0
    if (v >= 0) {
      bases.push(running)
      running += v
      bars.push({ value: v, t: signed(v), full: (v > 0 ? '+' : '') + k.num(v), itemStyle: { color: c.success } })
    } else {
      running += v
      bases.push(running)
      bars.push({ value: -v, t: signed(v), full: '−' + k.num(-v), itemStyle: { color: c.danger } })
    }
  }
  bases.push(0)
  bars.push({ value: running, t: k.short(running), full: k.num(running), itemStyle: { color: PALETTE[0] } })
  const cats = [...d.cats, k.t('chartPanel.total')]
  // 'all': a step stacks on the running total even where that is below zero (the default
  // stacks a positive value only on positive ones, and the step would jump back to 0)
  const stack = { stack: 'steps', stackStrategy: 'all' as const }
  return {
    backgroundColor: 'transparent',
    grid: { left: 8, right: 16, top: 24, bottom: 8, containLabel: true },
    tooltip: {
      ...tooltip(c),
      trigger: 'item',
      formatter: (p: any) => `${esc(p.name)}: <b>${esc(p.data?.full ?? '')}</b>`,
    },
    xAxis: { type: 'category', data: cats, ...axis(c) },
    yAxis: valueAxis(k),
    series: [
      { type: 'bar', ...stack, silent: true, data: bases, itemStyle: { color: 'transparent' }, tooltip: { show: false } },
      {
        type: 'bar',
        ...stack,
        name: d.name,
        data: bars,
        barMaxWidth: 36,
        itemStyle: { borderRadius: 3 },
        label: { show: cats.length <= 24, position: 'top', color: c.muted, fontSize: 10, formatter: (p: any) => p.data?.t ?? '' },
      },
    ],
  }
}

// ── funnel: stages, from the largest ──────────────────────────────────────────
export function funnelOption(k: KindCtx, d: { cats: string[]; values: (number | null)[]; name: string }) {
  const c = k.ui
  const items = d.cats
    .map((name, i) => ({ name, value: d.values[i] }))
    .filter((it): it is { name: string; value: number } => it.value != null)
    .sort((a, b) => b.value - a.value)
  const first = items[0]?.value ?? 0
  const n = items.length
  return {
    backgroundColor: 'transparent',
    tooltip: {
      ...tooltip(c),
      trigger: 'item',
      formatter: (p: any) =>
        `${esc(p.name)}: <b>${esc(k.num(p.value))}</b>` +
        (first > 0 && p.dataIndex > 0 ? `<br/>${esc(k.t('chartPanel.ofFirst', { pct: k.pct(p.value / first) }))}` : ''),
    },
    series: [{
      type: 'funnel',
      name: d.name,
      sort: 'descending',
      gap: 2,
      // the stages on the left, their names on the right: a name stays whole, on two
      // lines if it must, and never sits on a fill it could vanish into
      left: 8,
      top: 12,
      bottom: 12,
      width: '58%',
      minSize: '4%',
      // an ordinal ramp of the first hue: the stages are one thing, ever smaller
      data: items.map((it, i) => ({
        ...it,
        itemStyle: { color: PALETTE[0], opacity: n > 1 ? 1 - (0.6 * i) / (n - 1) : 1 },
      })),
      itemStyle: { borderColor: c.panel, borderWidth: 1 },
      label: {
        show: true,
        position: 'right',
        color: c.text,
        fontSize: 11,
        width: 170,
        overflow: 'break',
        formatter: (p: any) => `${p.name}  ${k.short(p.value)}`,
      },
      labelLine: { length: 10, lineStyle: { color: c.border } },
      emphasis: { label: { fontWeight: 600 } },
    }],
  }
}

// ── heatmap: a measure over two dimensions ────────────────────────────────────
export interface HeatData {
  xCats: string[]
  yCats: string[]
  cells: [number, number, number][] // [x index, y index, value]
  name: string
}

export function heatmapOption(k: KindCtx, d: HeatData) {
  const c = k.ui
  const values = d.cells.map((cell) => cell[2])
  const lo = values.length ? Math.min(...values) : 0
  const hiRaw = values.length ? Math.max(...values) : 1
  const hi = hiRaw > lo ? hiRaw : lo + 1
  return {
    backgroundColor: 'transparent',
    grid: { left: 8, right: 16, top: 12, bottom: 52, containLabel: true },
    tooltip: {
      ...tooltip(c),
      trigger: 'item',
      formatter: (p: any) =>
        `${esc(d.xCats[p.value[0]])} · ${esc(d.yCats[p.value[1]])}<br/>${esc(d.name)}: <b>${esc(k.num(p.value[2]))}</b>`,
    },
    xAxis: { type: 'category', data: d.xCats, ...axis(c) },
    // the first row on top, as a table reads
    yAxis: { type: 'category', data: d.yCats, inverse: true, ...axis(c), axisLabel: { ...axis(c).axisLabel, ...sideLabels(c) } },
    // magnitude → ONE hue, from almost nothing to full (sequential, never a rainbow), its
    // two ends written beside the scale
    visualMap: {
      type: 'continuous',
      min: lo,
      max: hi,
      calculable: false,
      orient: 'horizontal',
      left: 'center',
      bottom: 4,
      itemWidth: 10,
      itemHeight: 180,
      text: [k.short(hi), k.short(lo)],
      textGap: 8,
      textStyle: { color: c.muted, fontSize: 11 },
      inRange: { color: [withAlpha(PALETTE[0], 0.1), PALETTE[0]] },
    },
    series: [{
      type: 'heatmap',
      name: d.name,
      data: d.cells,
      itemStyle: { borderColor: c.panel, borderWidth: 2, borderRadius: 3 },
      // a number in each cell while there is room: the ink of the text, ringed with the
      // panel's colour, reads on the palest cell and on the fullest
      label: {
        show: d.cells.length <= 160,
        color: c.text,
        fontSize: 10,
        textBorderColor: c.panel,
        textBorderWidth: 2,
        formatter: (p: any) => k.short(p.value[2]),
      },
      emphasis: { itemStyle: { borderColor: c.text, borderWidth: 1 } },
    }],
  }
}

// ── combo: bars and a line, each on its axis ──────────────────────────────────
export interface ComboData {
  cats: string[]
  bars: (number | null)[]
  line: (number | null)[]
  barName: string
  lineName: string
}

export function comboOption(k: KindCtx, d: ComboData) {
  const c = k.ui
  // each axis says what it measures: two scales side by side are read wrong otherwise
  const named = (name: string, align: 'left' | 'right') => ({
    ...valueAxis(k, true),
    name,
    nameGap: 12,
    nameTextStyle: { color: c.muted, fontSize: 11, align },
  })
  return {
    backgroundColor: 'transparent',
    color: PALETTE,
    legend: legend(c),
    grid: { left: 8, right: 8, top: 44, bottom: 8, containLabel: true },
    tooltip: {
      ...tooltip(c),
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      valueFormatter: (v: any) => (v == null ? '—' : k.num(v)),
    },
    xAxis: { type: 'category', data: d.cats, ...axis(c) },
    yAxis: [named(d.barName, 'left'), { ...named(d.lineName, 'right'), alignTicks: true, splitLine: { show: false } }],
    series: [
      { name: d.barName, type: 'bar', data: d.bars, barMaxWidth: 28, itemStyle: { borderRadius: [4, 4, 0, 0] } },
      {
        name: d.lineName,
        type: 'line',
        yAxisIndex: 1,
        data: d.line,
        lineStyle: { width: 2 },
        symbol: 'circle',
        symbolSize: 7,
        showSymbol: d.cats.length <= 30,
        itemStyle: { borderColor: c.panel, borderWidth: 1 },
      },
    ],
  }
}

// ── bullet: each category's measure against its target ────────────────────────
export interface BulletData {
  cats: string[]
  values: (number | null)[]
  targets: (number | null)[]
  name: string
  targetName: string
  max?: number | null
}

export function bulletOption(k: KindCtx, d: BulletData) {
  const c = k.ui
  const nums = [...d.values, ...d.targets].filter((v): v is number => v != null)
  const lo = Math.min(0, ...nums)
  let hi = d.max ?? niceMax(Math.max(0, ...nums) * 1.05)
  if (!(hi > lo)) hi = lo + 1
  const category = { type: 'category' as const, data: d.cats, inverse: true }
  const row = (i: number) =>
    `${esc(d.cats[i])}<br/>${esc(d.name)}: <b>${esc(d.values[i] == null ? '—' : k.num(d.values[i]!))}</b>` +
    (d.targets[i] != null
      ? `<br/>${esc(d.targetName)}: <b>${esc(k.num(d.targets[i]!))}</b>` +
        (d.targets[i] && d.values[i] != null ? ` · ${esc(k.pct(d.values[i]! / d.targets[i]!))}` : '')
      : '')
  return {
    backgroundColor: 'transparent',
    legend: { ...legend(c), data: [{ name: d.name }, { name: d.targetName, icon: 'rect' }] },
    grid: { left: 8, right: 20, top: 30, bottom: 8, containLabel: true },
    tooltip: { ...tooltip(c), trigger: 'item', formatter: (p: any) => row(p.seriesType === 'scatter' ? p.value[1] : p.dataIndex) },
    // the measure on a second category axis laid over the first: the track and the bar,
    // of different widths, each centred in its own row (the reports' layout)
    yAxis: [
      { ...category, ...axis(c), axisLabel: { ...axis(c).axisLabel, ...sideLabels(c) } },
      { ...category, show: false },
    ],
    xAxis: { ...valueAxis(k, true), min: lo, max: hi, splitLine: { lineStyle: { color: c.borderSoft } } },
    series: [
      {
        type: 'bar',
        yAxisIndex: 0,
        silent: true,
        barWidth: '64%',
        data: d.cats.map(() => hi),
        itemStyle: { color: withAlpha(PALETTE[0], 0.14), borderRadius: 3 },
        tooltip: { show: false },
        z: 1,
      },
      {
        type: 'bar',
        name: d.name,
        yAxisIndex: 1,
        barWidth: '30%',
        data: d.values,
        itemStyle: { color: PALETTE[0], borderRadius: [0, 3, 3, 0] },
        z: 2,
      },
      {
        type: 'scatter',
        name: d.targetName,
        yAxisIndex: 0,
        symbol: 'rect',
        symbolSize: [3, 18],
        data: d.targets.flatMap((t, i) => (t == null ? [] : [[t, i]])),
        itemStyle: { color: c.text },
        z: 3,
      },
    ],
  }
}

// ── the KPI: its trend line, and the change it announces ──────────────────────
export interface Spark {
  cats: string[]
  values: (number | null)[]
}

export interface KpiChange {
  ratio: number
  /** what the change compares: «2025-12 vs 2025-11», «vs target» */
  text: string
}

/** The last point against the one before it (the points without a value skipped). */
export function lastChange(k: KindCtx, s: Spark): KpiChange | null {
  const idx = s.values.flatMap((v, i) => (v == null ? [] : [i]))
  if (idx.length < 2) return null
  const last = idx[idx.length - 1]
  const prev = idx[idx.length - 2]
  const base = s.values[prev]!
  if (!base) return null
  return {
    ratio: (s.values[last]! - base) / Math.abs(base),
    text: k.t('chartPanel.periodVsPeriod', { last: s.cats[last], prev: s.cats[prev] }),
  }
}

export function sparkOption(k: KindCtx, s: Spark & { name: string }) {
  const c = k.ui
  let last = -1
  s.values.forEach((v, i) => { if (v != null) last = i })
  return {
    backgroundColor: 'transparent',
    grid: { left: 4, right: 8, top: 8, bottom: 4 },
    xAxis: { type: 'category', show: false, data: s.cats, boundaryGap: false },
    yAxis: { type: 'value', show: false, scale: true },
    tooltip: {
      ...tooltip(c),
      trigger: 'axis',
      axisPointer: { type: 'line', lineStyle: { color: c.border } },
      formatter: (ps: any) => {
        const p = Array.isArray(ps) ? ps[0] : ps
        const v = toNumber(p?.value)
        return `${esc(p?.name ?? '')}: <b>${esc(v == null ? '—' : k.num(v))}</b>`
      },
    },
    // the trend in the quiet colour, the current period in the accent
    series: [{
      type: 'line',
      name: s.name,
      data: s.values.map((v, i) =>
        i === last ? { value: v, symbol: 'circle', symbolSize: 8, itemStyle: { color: PALETTE[0], borderColor: c.panel, borderWidth: 1.5 } } : v),
      symbol: 'none',
      showSymbol: true,
      connectNulls: true,
      lineStyle: { width: 1.6, color: c.muted },
      itemStyle: { color: c.muted },
      areaStyle: { color: c.muted, opacity: 0.1 },
    }],
  }
}
