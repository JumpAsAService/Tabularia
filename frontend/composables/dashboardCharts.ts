// A dashboard's chart: the numbers of a tile (docs/specs/dashboards.md, «The answer, tile by
// tile») as an ECharts option, in the app's five themes. The builders of composables/chartKinds.ts
// draw what they know; here the kinds that live in the Viewer's panel with their query (bars, lines,
// parts of a whole, points), and what a report's definition adds to the others (bands, a right axis).
//
// The numbers are written by the server in the dashboard's language and format, and travel with
// each point (`t`); what ECharts works out by itself (the ticks of an axis) is written here, in the same
// language. No animation: a chart must not draw itself again at every refresh, and a wall of them
// would never stop moving. Every dynamic piece of a tooltip goes through `esc` (chartKinds.ts).
import type { ChartUi } from '~/composables/useChartTheme'
import { withAlpha } from '~/composables/useChartTheme'
import {
  PALETTE, axis, bulletOption, comboOption, esc, funnelOption, gaugeOption, heatmapOption, legend, niceMax, sideLabels, tooltip,
  treemapOption, waterfallOption, type KindCtx,
} from '~/composables/chartKinds'
import type { TileNumbers } from '~/composables/useDashboards'

/** What the builders need, with the numbers in the dashboard's language and the words in the
 *  interface's. */
export function dashboardCtx(ui: ChartUi, locale: string, t: KindCtx['t']): KindCtx {
  const full = new Intl.NumberFormat(locale, { maximumFractionDigits: 2 })
  const compact = new Intl.NumberFormat(locale, { notation: 'compact', maximumFractionDigits: 1 })
  const brief = new Intl.NumberFormat(locale, { notation: 'compact', maximumSignificantDigits: 3 })
  const percent = new Intl.NumberFormat(locale, { style: 'percent', maximumFractionDigits: 1 })
  return { ui, num: (n) => full.format(n), short: (n) => compact.format(n), brief: (n) => brief.format(n), pct: (n) => percent.format(n), t }
}

/** The same context, whose `num` writes a value the server wrote as the server wrote it: the
 *  builders' tooltips then say the tile's format, `€ 1,234.00`, and not a plain number. On a
 *  chart of shares every number the builders write themselves is a share too (`format`). */
function readingTexts(k: KindCtx, pairs: Iterable<[number | null | undefined, string | null | undefined]>, format?: string | null): KindCtx {
  const texts = new Map<number, string>()
  for (const [v, text] of pairs) {
    if (v == null || !text) continue
    texts.set(v, text)
    if (v < 0) texts.set(-v, text.replace(/^[-−]\s*/, '')) // a waterfall writes the sign itself
  }
  const base = percentFormat(format) ? { ...k, num: k.pct, short: k.pct, brief: k.pct } : k
  return { ...base, num: (n) => texts.get(n) ?? base.num(n) }
}
const paired = (values: (number | null)[] = [], texts: (string | null)[] = []) =>
  values.map((v, i) => [v, texts[i]] as [number | null, string | null])

interface ChartSeries { name: string; values: (number | null)[]; texts: (string | null)[]; as?: string; axis?: string; format?: string | null }

const percentFormat = (format?: string | null) => !!format && format.includes('%')

// the palette has 5 validated hues, which are never cycled: past them a series is a neutral, and
// the tile says so (`greySeries`)
const MUTED_STEPS = [0.85, 0.65, 0.45]
export const greySeries = (a: TileNumbers) => Math.max(0, (a.data?.series?.length ?? 0) - PALETTE.length)
function colourOf(i: number, c: ChartUi): string {
  return i < PALETTE.length ? PALETTE[i] : withAlpha(c.muted, MUTED_STEPS[(i - PALETTE.length) % MUTED_STEPS.length])
}

function valueAxisOf(k: KindCtx, format: string | null | undefined, o: Record<string, any>) {
  return {
    type: 'value' as const,
    ...(o.min != null ? { min: o.min } : {}),
    ...(o.max != null ? { max: o.max } : {}),
    axisLabel: { color: k.ui.muted, hideOverlap: true, formatter: (v: number) => (percentFormat(format) ? k.pct(v) : k.short(v)) },
    splitLine: { lineStyle: { color: k.ui.borderSoft } },
    axisLine: { lineStyle: { color: k.ui.border } },
  }
}

const label = (k: KindCtx, show: boolean, position: string) => ({
  show,
  position,
  color: k.ui.text,
  fontSize: 10,
  formatter: (p: any) => p.data?.t ?? '',
})
const pointsOf = (s: ChartSeries, below?: string) => s.values.map((v, i) =>
  (v == null ? null : { value: v, t: s.texts?.[i] ?? '', ...(below && v < 0 ? { label: { position: below } } : {}) }))

const axisTooltip = (c: ChartUi) => ({
  ...tooltip(c),
  trigger: 'axis' as const,
  formatter: (ps: any) => {
    const list = Array.isArray(ps) ? ps : [ps]
    const lines = list.map((p: any) => `${p.marker ?? ''}${esc(p.seriesName ?? '')}: <b>${esc(p.data?.t ?? '—')}</b>`)
    return [esc(list[0]?.name ?? ''), ...lines].join('<br/>')
  },
})

// on one line, its pages in the theme's colours: many series must not eat the plot
const scrollLegend = (c: ChartUi) => ({
  ...legend(c), type: 'scroll' as const, pageTextStyle: { color: c.muted }, pageIconColor: c.text, pageIconInactiveColor: c.border,
})

const showLegend = (o: Record<string, any>, many: boolean) => (o.legend === null || o.legend === undefined ? many : !!o.legend)

/** Bars standing or lying, lines, areas, and a combo past one bar and one line. */
function axesOption(k: KindCtx, a: TileNumbers) {
  const c = k.ui
  const { cats, series } = a.data as { cats: string[]; series: ChartSeries[] }
  const o = a.options ?? {}
  const lying = a.kind === 'bar'
  const many = series.length > 1
  const room = showLegend(o, many)
  const right = series.some((s) => s.axis === 'right')
  const formatOf = (side: string) => series.find((s) => (s.axis ?? 'left') === side && s.format)?.format ?? a.format
  const cat = { type: 'category' as const, data: cats, ...axis(c) }
  const drawn = series.map((s, i) => {
    const line = a.kind === 'line' || a.kind === 'area' || (a.kind === 'combo' && s.as === 'line')
    return {
      name: s.name,
      type: line ? 'line' : 'bar',
      itemStyle: { color: colourOf(i, c) },
      data: pointsOf(s, line || o.stacked ? undefined : lying ? 'left' : 'bottom'),
      label: label(k, !!o.labels, lying ? 'right' : 'top'),
      labelLayout: { hideOverlap: true },
      ...(o.stacked ? { stack: 'total' } : {}),
      ...(right ? { yAxisIndex: s.axis === 'right' ? 1 : 0 } : {}),
      ...(line
        ? { symbol: 'circle', symbolSize: 6, showSymbol: cats.length <= 30, lineStyle: { width: 2, color: colourOf(i, c) },
            ...(a.kind === 'area' ? { areaStyle: { opacity: 0.15, color: colourOf(i, c) } } : {}) }
        : { barMaxWidth: lying ? 28 : 48, itemStyle: { color: colourOf(i, c), borderRadius: lying ? [0, 3, 3, 0] : [3, 3, 0, 0] } }),
    }
  })
  const values = valueAxisOf(k, formatOf('left'), o)
  return {
    backgroundColor: 'transparent',
    ...(room ? { legend: scrollLegend(c) } : {}),
    grid: { left: 8, right: 16, top: room ? 30 : 12, bottom: 8, containLabel: true },
    tooltip: axisTooltip(c),
    ...(lying
      ? { xAxis: values, yAxis: { ...cat, inverse: true, axisLabel: { ...cat.axisLabel, ...sideLabels(c) } } }
      : { xAxis: cat, yAxis: right ? [values, { ...valueAxisOf(k, formatOf('right'), o), splitLine: { show: false } }] : values }),
    series: drawn,
  }
}

/** A pie or a donut: every slice above zero, from the largest (the first in the palette's hues,
 *  the rest neutrals), each with its number as the server wrote it. */
function partsOption(k: KindCtx, a: TileNumbers) {
  const c = k.ui
  const slices = [...(a.data.slices ?? [])].sort((x: any, y: any) => y.value - x.value)
  return {
    backgroundColor: 'transparent',
    tooltip: {
      ...tooltip(c),
      trigger: 'item' as const,
      formatter: (p: any) => `${p.marker ?? ''}${esc(p.name)}: <b>${esc(p.data?.t ?? '')}</b> (${esc(k.pct((p.percent ?? 0) / 100))})`,
    },
    series: [{
      type: 'pie',
      name: a.data.name,
      radius: a.kind === 'donut' ? ['38%', '62%'] : [0, '62%'],
      showEmptyCircle: false,
      minShowLabelAngle: 4,
      itemStyle: { borderColor: c.panel, borderWidth: 2 },
      // against the tile's edges: ECharts then gives each name the room up to the edge, and cuts it
      // with an ellipsis there (the tooltip has it whole); free, a long name ran off the canvas
      label: { color: c.text, fontSize: 11, formatter: '{b}\n{d}%', alignTo: 'edge', edgeDistance: 6, overflow: 'truncate', lineHeight: 14 },
      labelLine: { length: 12, length2: 0, maxSurfaceAngle: 80, lineStyle: { color: c.border } },
      data: slices.map((s: any, i: number) => ({ name: s.name, value: s.value, t: s.text, itemStyle: { color: colourOf(i, c) } })),
    }],
    // a tile too narrow for names beside the pie: «Sa…» says nothing, a legend says them all
    legend: { ...scrollLegend(c), show: false, top: undefined, bottom: 0 },
    media: [{
      query: { maxWidth: 340 },
      option: { legend: { show: true }, series: [{ label: { show: false }, labelLine: { show: false }, center: ['50%', '45%'], radius: a.kind === 'donut' ? ['42%', '72%'] : [0, '72%'] }] },
    }],
  }
}

function pointsOption(k: KindCtx, a: TileNumbers) {
  const c = k.ui
  const series = a.data.series as { name: string; points: [number, number, string][] }[]
  const many = series.length > 1
  const room = showLegend(a.options ?? {}, many)
  const values = (o: Record<string, any> = {}) => ({ ...valueAxisOf(k, a.format, o), scale: true })
  return {
    backgroundColor: 'transparent',
    ...(room ? { legend: scrollLegend(c) } : {}),
    grid: { left: 8, right: 16, top: room ? 30 : 12, bottom: 8, containLabel: true },
    tooltip: {
      ...tooltip(c),
      trigger: 'item' as const,
      formatter: (p: any) => {
        const [x, y, text] = p.value as [number, number, string]
        return `${p.marker ?? ''}${esc(text ?? p.seriesName ?? '')}<br/>${esc(k.num(x))} · <b>${esc(k.num(y))}</b>`
      },
    },
    xAxis: values(),
    yAxis: values(a.options ?? {}),
    series: series.map((s, i) => ({
      type: 'scatter',
      name: s.name,
      symbolSize: 8,
      large: s.points.length > 2000,
      itemStyle: { color: colourOf(i, c), opacity: 0.75 },
      data: s.points,
    })),
  }
}

/** A treemap split: each category a box holding its parts, as a report nests them. */
function nestedTreemap(k: KindCtx, a: TileNumbers) {
  const c = k.ui
  const { cats, series, name } = a.data as { cats: string[]; series: ChartSeries[]; name: string }
  const nodes = cats.flatMap((cat, ci) => {
    const children = series.flatMap((s) => {
      const v = s.values[ci]
      return v != null && v > 0 ? [{ name: s.name, value: v, t: s.texts[ci] }] : []
    })
    return children.length ? [{ name: cat, children }] : []
  })
  return {
    backgroundColor: 'transparent',
    tooltip: {
      ...tooltip(c),
      trigger: 'item' as const,
      formatter: (p: any) => `${esc((p.treePathInfo ?? []).slice(1).map((n: any) => n.name).join(' · '))}: <b>${esc(p.data?.t ?? k.num(p.value))}</b>`,
    },
    series: [{
      type: 'treemap',
      name,
      roam: false,
      nodeClick: false,
      breadcrumb: { show: false },
      left: 0, right: 0, top: 0, bottom: 0,
      itemStyle: { borderColor: c.panel, borderWidth: 1, gapWidth: 1, color: withAlpha(PALETTE[0], 0.7) },
      label: { color: c.text, fontSize: 11, formatter: '{b}' },
      upperLabel: { show: true, height: 18, color: c.text, fontSize: 11 },
      levels: [{ itemStyle: { borderWidth: 0, gapWidth: 2, borderColor: c.panel } }, { colorAlpha: [0.45, 1], itemStyle: { gapWidth: 1, borderColor: c.panel } }],
      data: nodes,
    }],
  }
}

/** A gauge with its thresholds: the arc in their colours and a pointer, as a report draws it. The
 *  colours mean bad and good on this theme. */
function withGaugeBands(option: any, k: KindCtx, a: TileNumbers, lo: number, hi: number, small: boolean) {
  const bands = [...(a.data.bands ?? [])].sort((x: number, y: number) => x - y)
  if (!bands.length || !(hi > lo)) return option
  const c = k.ui
  const colours = ({ 1: [c.success], 2: [c.danger, c.success], 3: [c.danger, PALETTE[2], c.success] } as Record<number, string[]>)[bands.length]
    ?? bands.map(() => withAlpha(c.muted, 0.6))
  const stops: [number, string][] = bands.map((b: number, i: number) => [Math.min(Math.max((b - lo) / (hi - lo), 0), 1), colours[i]])
  if (stops[stops.length - 1][0] < 1) stops.push([1, c.borderSoft])
  const main = option.series[0]
  main.axisLine = { ...main.axisLine, lineStyle: { ...main.axisLine.lineStyle, color: stops } }
  main.progress = { show: false }
  // the pointer's hub takes the middle: the texts go under it, in the arc's opening
  main.pointer = { show: true, length: '52%', width: small ? 3 : 4, itemStyle: { color: c.text } }
  main.detail = { ...main.detail, offsetCenter: [0, small ? '30%' : '34%'] }
  main.title = { ...main.title, offsetCenter: [0, '58%'] }
  const target = option.series[1]
  if (target) target.detail = { ...target.detail, offsetCenter: [0, small ? '80%' : '76%'] }
  return option
}

/** A bullet with its bands: shades of the neutral behind the bars, in place of the one track. */
function withBulletBands(option: any, k: KindCtx, bands: number[], rows: number) {
  const sorted = [...bands].sort((x, y) => x - y)
  if (!sorted.length) return option
  const shades = [0.32, 0.22, 0.14, 0.08]
  let before = 0
  const behind = sorted.map((b, i) => {
    const step = b - before
    before = b
    return { type: 'bar', stack: 'bands', yAxisIndex: 0, silent: true, barWidth: '64%', barMaxWidth: 28, z: 1, tooltip: { show: false },
             itemStyle: { color: withAlpha(k.ui.muted, shades[Math.min(i, shades.length - 1)]) }, data: Array.from({ length: rows }, () => step) }
  })
  option.series = [...behind, ...option.series.slice(1)]
  return option
}

// what the axis on the left and the chart's margins leave, about; and the least room in which a name
// cut with an ellipsis still says something: under it, ECharts' own choice of which to show stands
const BESIDE_THE_PLOT_PX = 70
const LEAST_NAME_PX = 44
const WIDEST_NAME_PX = 120

/** Names along the bottom cut to the room each has. Left to `axis` (chartKinds.ts), at 120 px each,
 *  on a tile a third of the screen wide the longer ones overlapped and ECharts hid them: bars with
 *  no name. Each is cut instead (the tooltip has it whole), when all of them can be shown. */
function namesInTheirRoom(option: any, width: number) {
  for (const axisOf of [option.xAxis].flat()) {
    if (axisOf?.type !== 'category' || !axisOf.data?.length) continue
    const room = Math.floor((width - BESIDE_THE_PLOT_PX) / axisOf.data.length) - 4
    if (room >= LEAST_NAME_PX && room < WIDEST_NAME_PX) axisOf.axisLabel = { ...axisOf.axisLabel, width: room, overflow: 'truncate' }
  }
  return option
}

/** The option of a chart tile. `small`: a tile only a few rows high, where a gauge's figure takes
 *  the chat's size. `width`: of the tile's chart in px, when known. */
export function tileChartOption(k: KindCtx, a: TileNumbers, opts: { small?: boolean; width?: number } = {}): Record<string, any> {
  const d = a.data
  const o = a.options ?? {}
  const format = a.format
  let option: any
  switch (a.kind) {
    case 'pie':
    case 'donut':
      option = partsOption(k, a)
      break
    case 'scatter':
      option = pointsOption(k, a)
      break
    case 'gauge': {
      // a share is known by its format, or by what the server wrote when only the series has one
      const ctx = readingTexts(k, [[d.value, d.text]], percentFormat(format) || /%\s*$/.test(d.text ?? '') ? '%' : null)
      option = gaugeOption(ctx, { value: d.value, name: d.name, min: d.min, max: d.max, target: d.target }, !!opts.small)
      option.series[0].detail = { ...option.series[0].detail, formatter: () => d.text ?? '—' }
      option = withGaugeBands(option, k, a, option.series[0].min, option.series[0].max, !!opts.small)
      break
    }
    case 'heatmap': {
      const pairs = (d.cells as [number, number, number][]).map((cell, i) => [cell[2], d.texts?.[i]] as [number, string])
      option = heatmapOption(readingTexts(k, pairs, format), { xCats: d.xCats, yCats: d.yCats, cells: d.cells, name: d.name })
      break
    }
    case 'treemap':
      option = d.series?.length > 1 ? nestedTreemap(k, a) : treemapOption(readingTexts(k, paired(d.values, d.texts), format), d)
      break
    case 'funnel':
      option = funnelOption(readingTexts(k, paired(d.values, d.texts), format), d)
      break
    case 'waterfall': {
      const ctx = readingTexts(k, [...paired(d.values, d.texts), [d.total, d.total_text]], format)
      option = waterfallOption(ctx, d)
      if (d.total_label) option.xAxis.data[option.xAxis.data.length - 1] = d.total_label
      break
    }
    case 'bullet': {
      const max = d.max ?? (d.bands?.length ? niceMax(Math.max(...d.bands, ...(d.values ?? []).map((v: number | null) => v ?? 0), ...(d.targets ?? []).map((v: number | null) => v ?? 0))) : null)
      const ctx = readingTexts(k, paired(d.values, d.texts), format)
      option = withBulletBands(bulletOption(ctx, { ...d, max, targetName: d.targetName || k.t('chartPanel.target') }), k, d.bands ?? [], d.cats.length)
      break
    }
    case 'combo': {
      // one bar and one line on its own axis, without what only the definition asks: the panel's
      // combo, with its two axes named. Any other mix goes by the series, as a report draws it
      const series = (d.series ?? []) as ChartSeries[]
      const line = series.find((s) => s.as === 'line')
      const bar = series.find((s) => s.as !== 'line')
      if (series.length !== 2 || !line || !bar || line.axis !== 'right' || bar.axis === 'right' || o.stacked || o.labels) {
        option = axesOption(k, a)
        break
      }
      option = comboOption(readingTexts(k, series.flatMap((s) => paired(s.values, s.texts))),
        { cats: d.cats, bars: d.bars, line: d.line, barName: d.barName, lineName: d.lineName })
      // each axis in its series' format: a margin in % beside amounts
      for (const [axisAt, s] of [[0, bar], [1, line]] as [number, ChartSeries][]) {
        const shown = s.format ?? format
        option.yAxis[axisAt].axisLabel = { ...option.yAxis[axisAt].axisLabel, formatter: (v: number) => (percentFormat(shown) ? k.pct(v) : k.short(v)) }
      }
      break
    }
    default:
      option = axesOption(k, a)
  }
  return { ...(opts.width ? namesInTheirRoom(option, opts.width) : option), animation: false }
}
