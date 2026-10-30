// The logic of the report viewer and of the Viewer's and the chat's charts, without a
// browser: where a click on a page lands (the hotspots' boxes), the values a link or a
// script carries, a pie's «Other» slice. The modules are the app's own TypeScript, loaded
// through alias-hooks.mjs (`~/…`).
// Run: npm test
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { register } from 'node:module'

register('./alias-hooks.mjs', import.meta.url)
const reports = await import('../composables/useReports.ts')
const kinds = await import('../composables/chartKinds.ts')

// ── the hotspots ─────────────────────────────────────────────────────────────
const DEFINITION = {
  v: 1,
  page: { size: 'A4', orientation: 'portrait', margins: [15, 12, 15, 12] },
  theme: { font: 'IBM Plex Sans', size: 8, color: '#1f2933', accent: '#3b5bdb' },
  sections: {
    page_header: { items: [] },
    page_footer: { items: [] },
    body: {
      items: [{
        id: 'detail', type: 'table', x: 0, y: 94, w: 186, h: 30,
        columns: [{ header: 'Month' }, { header: 'Revenue' }, { header: 'Cost' }],
      }],
    },
  },
}
const close = (a, b) => Math.abs(a - b) < 1e-6

test('a hotspot with the box the engine measured lands exactly on it', () => {
  const box = { x: 40.2, y: 126.4, w: 38.5, h: 4.1 }
  const [placed] = reports.placeHotspots([
    { kind: 'sort', item: 'detail', column: 1, page: 1, x: 41.7, y: 129.46, box },
  ], DEFINITION)
  assert.deepEqual(placed.box, box)
  assert.equal(placed.header, 'Revenue') // the label still comes from the definition
})

test('a group\'s hotspot with its box: the box, not the width of the whole table', () => {
  const box = { x: 12, y: 130.6, w: 120, h: 4.5 }
  const [placed] = reports.placeHotspots([
    { kind: 'toggle', item: 'detail', key: ['Salami'], closed: false, page: 2, x: 13.5, y: 133.83, box },
  ], DEFINITION)
  assert.deepEqual(placed.box, box)
})

test('without a box (a render from before), the box is rebuilt from the table\'s layout', () => {
  const hot = [
    { kind: 'sort', item: 'detail', column: 0, page: 1, x: 13.5, y: 129.46 },
    { kind: 'sort', item: 'detail', column: 1, page: 1, x: 41.5, y: 129.46 },
    { kind: 'toggle', item: 'detail', key: ['Salami'], closed: true, page: 1, x: 13.5, y: 133.83 },
  ]
  const [first, last, group] = reports.placeHotspots(hot, DEFINITION)
  // a header spans its column: from its padding to the next column's start
  assert.ok(close(first.box.x, 12) && close(first.box.x + first.box.w, 40))
  // the last one to the table's right edge (margin 12 + x 0 + w 186)
  assert.ok(close(last.box.x + last.box.w, 198))
  // a group's row spans the table
  assert.ok(close(group.box.x, 12) && close(group.box.w, 186))
  assert.ok(first.box.h > 0 && group.box.h > 0)
})

test('a box that is not one (missing or not a number) is rebuilt as before', () => {
  const base = { kind: 'sort', item: 'detail', column: 2, page: 1, x: 80, y: 129.46 }
  const [rebuilt] = reports.placeHotspots([base], DEFINITION)
  for (const box of [null, {}, { x: 1, y: 2, w: 'wide', h: 3 }, { x: 1, y: 2, w: 0, h: 3 }, { x: Number.NaN, y: 1, w: 2, h: 3 }]) {
    const [placed] = reports.placeHotspots([{ ...base, box }], DEFINITION)
    assert.deepEqual(placed.box, rebuilt.box, JSON.stringify(box))
  }
})

// ── a pie's «Other» ──────────────────────────────────────────────────────────
test('the pie\'s «Other» holds every category past the first five, not only those fetched', () => {
  // 30 categories, worth 30, 29, … 1: the chart's query brings the first 20 only
  const rows = Array.from({ length: 20 }, (_, i) => ({ name: `c${i + 1}`, value: 30 - i }))
  const total = (30 * 31) / 2
  const { slices, other } = kinds.pieSlices(rows, 5, { value: total, count: 30 })
  assert.deepEqual(slices.map((s) => s.value), [30, 29, 28, 27, 26])
  assert.equal(other.count, 25)
  assert.equal(other.value, total - (30 + 29 + 28 + 27 + 26))
})

test('without the totals (a measure that does not add up), the pie keeps its first slices and no «Other»', () => {
  const rows = Array.from({ length: 8 }, (_, i) => ({ name: `c${i}`, value: 10 - i }))
  const { slices, other } = kinds.pieSlices(rows, 5, null)
  assert.equal(slices.length, 5)
  assert.equal(other, null)
})

test('five categories or fewer: no «Other»', () => {
  const rows = [{ name: 'a', value: 3 }, { name: 'b', value: 2 }]
  const { slices, other } = kinds.pieSlices(rows, 5, { value: 5, count: 2 })
  assert.equal(slices.length, 2)
  assert.equal(other, null)
})

// ── the values a link or a script carries ────────────────────────────────────
test('the API link leaves out the values that are the defaults: a default keeps moving with the calendar', () => {
  const parameters = [
    { name: 'since', type: 'date', multi: false, required: true, default: '2026-09-28' }, // {today-30}, resolved today
    { name: 'category', type: 'text', multi: false, required: false, default: null },
    { name: 'from_month', type: 'number', multi: false, required: true, default: 202401 },
    { name: 'cats', type: 'text', multi: true, required: false, default: ['Salami'] },
    { name: 'net', type: 'boolean', multi: false, required: false, default: true },
  ]
  const onScreen = { since: '2026-09-28', category: 'Salami', from_month: '202401', cats: ['Salami', 'Prosciutti'], net: 'true' }
  assert.deepEqual(reports.explicitParams(parameters, onScreen), { category: 'Salami', cats: ['Salami', 'Prosciutti'] })
  // a value chosen that is not the default is kept, whatever its type
  assert.deepEqual(reports.explicitParams(parameters, { since: '2026-01-01', from_month: '202501', net: 'false' }),
    { since: '2026-01-01', from_month: '202501', net: 'false' })
})

// ── the form's parameters ────────────────────────────────────────────────────
test('the choices asked again with the values of a link change the lists, never the defaults', () => {
  // GET /reports/{id}/parameters?n=7.5 answers with the value given as the «default»: the
  // form must keep the report's own, or a link without n would render with 7.5
  const own = [
    { name: 'n', type: 'number', multi: false, required: false, default: 5, choices: null, choices_truncated: false, choices_error: null },
    { name: 'town', type: 'text', multi: false, required: false, default: null, choices: [{ value: 'Bari', label: 'Bari' }], choices_truncated: false, choices_error: null },
  ]
  const given = [
    { ...own[0], default: 7.5 },
    { ...own[1], default: 'Ancona', choices: [{ value: 'Ancona', label: 'Ancona' }], choices_truncated: true },
  ]
  const merged = reports.withChoices(own, given)
  assert.deepEqual(merged.map((p) => p.default), [5, null])
  assert.deepEqual(merged[1].choices, [{ value: 'Ancona', label: 'Ancona' }])
  assert.equal(merged[1].choices_truncated, true)
})

// ── the charts' edge cases (the builders the Viewer and the chat share) ─────
const UI = { text: '#e8ebf2', muted: '#8b93a7', border: '#262e40', borderSoft: '#1e2534', panel: '#141926', panel2: '#1b2130',
  success: '#6ee7b7', danger: '#ff6b6b', accent: '#4f8cff', violet: '#a78bfa', isLight: false }
const K = { ui: UI, num: (n) => String(n), short: (n) => String(n), brief: (n) => String(n), pct: (n) => `${n * 100}%`, t: (k) => k }

test('a pie leaves out the categories at zero or below and says how many: it cannot draw them', () => {
  const rows = [{ name: 'a', value: 5 }, { name: 'b', value: 3 }, { name: 'c', value: 0 }, { name: 'd', value: -2 }, { name: 'e', value: null }]
  const { slices, other, leftOut } = kinds.pieSlices(rows, 5, null)
  assert.deepEqual(slices.map((s) => s.name), ['a', 'b'])
  assert.equal(other, null)
  assert.equal(leftOut, 3)
  // with the totals over every category, the count of those left out comes from them
  const many = kinds.pieSlices(rows, 1, { value: 20, count: 4, every: 9 })
  assert.deepEqual(many.other, { count: 3, value: 15 })
  assert.equal(many.leftOut, 5)
  // nothing above zero: nothing to draw (an all-zero pie was drawn as equal slices)
  assert.equal(kinds.pieSlices([{ name: 'z', value: 0 }], 5, null).slices.length, 0)
})

test('a treemap is one hue shaded by value, of the categories above zero', () => {
  const o = kinds.treemapOption(K, { cats: ['a', 'b', 'c', 'd'], values: [10, 4, 0, -3], name: 'sum(x)' })
  const s = o.series[0]
  assert.deepEqual(s.data.map((d) => d.name), ['a', 'b'])
  // each box its own colour, the first hue at an alpha by its value: left to ECharts, the
  // level's palette gave every box a hue of its own
  const [r, g, bl] = [1, 3, 5].map((i) => parseInt(kinds.PALETTE[0].slice(i, i + 2), 16))
  const alphas = s.data.map((d) => {
    const m = /^rgba\((\d+), (\d+), (\d+), ([\d.]+)\)$/.exec(d.itemStyle.color)
    assert.ok(m && +m[1] === r && +m[2] === g && +m[3] === bl, d.itemStyle.color)
    return +m[4]
  })
  assert.ok(alphas[0] > alphas[1], 'the larger, the fuller')
})

test('long categories along a horizontal axis: every label tried, the overlapping ones hidden', () => {
  const o = kinds.comboOption(K, { cats: ['a', 'b'], bars: [1, 2], line: [1, 2], barName: 'a', lineName: 'b' })
  assert.equal(o.xAxis.axisLabel.interval, 0)
  assert.equal(o.xAxis.axisLabel.hideOverlap, true)
})

test('a funnel draws the stages above zero, on a scale from zero', () => {
  const o = kinds.funnelOption(K, { cats: ['a', 'b', 'c', 'd'], values: [10, -4, 0, 6], name: 'n' })
  const s = o.series[0]
  assert.deepEqual(s.data.map((d) => d.name), ['a', 'd'])
  assert.equal(s.min, 0) // a negative stage widened the scale and drew an hourglass
})

test('long categories along a horizontal axis are cut to a width, not hidden but for one', () => {
  const long = (i) => `Category ${i} — a very long name, with accents àèìòù, to see how the labels are cut`
  const cats = [1, 2, 3, 4, 5].map(long)
  const vals = [1, 2, 3, 4, 5]
  for (const o of [
    kinds.waterfallOption(K, { cats, values: vals, name: 'n' }),
    kinds.comboOption(K, { cats, bars: vals, line: vals, barName: 'a', lineName: 'b' }),
    kinds.heatmapOption(K, { xCats: cats, yCats: ['y'], cells: [[0, 0, 1]], name: 'n' }),
  ]) {
    const label = o.xAxis.axisLabel
    assert.equal(label.overflow, 'truncate')
    assert.ok(label.width > 0 && label.width <= 160)
  }
})

test('a bullet with one category keeps its bar slim, as with many', () => {
  const o = kinds.bulletOption(K, { cats: ['only'], values: [5], targets: [6], name: 'n', targetName: 't' })
  assert.ok(o.series[0].barMaxWidth > 0 && o.series[1].barMaxWidth > 0)
  assert.ok(o.series[1].barMaxWidth < o.series[0].barMaxWidth)
})

test('a pie of a measure that does not add up: no «Other», and those left out still counted over the dataset', () => {
  const rows = [{ name: 'a', value: 5 }, { name: 'b', value: 3 }]
  const { other, leftOut } = kinds.pieSlices(rows, 1, { value: 99, count: 4, every: 6 }, false)
  assert.equal(other, null)
  assert.equal(leftOut, 2)
})

// ── the Pushmail cell ────────────────────────────────────────────────────────
test('the cell previews its subject and file names as the email writes them: in the sender\'s language', async () => {
  const groups = await import('../composables/useFlowGroups.ts')
  const req = groups.pushmailPreviewRequest({
    label: '', connectionId: 1, to: [], cc: [], subject: 'Margins {today:MMMM yyyy}', body: '', bodyIsHtml: false,
    timezone: 'Europe/Rome', attachments: [{ datasourceId: 3, fileName: ' sales_{today} ', maxRows: 10 }],
    reports: [{ reportId: 9, format: 'pdf', params: {}, fileName: 'margins_{today:yyyyMM}', inBody: true }],
  }, 'Morning')
  assert.equal(req.pushmail, true) // the gateway then formats in the language of whoever runs the group
  assert.deepEqual(req.templates, { subject: 'Margins {today:MMMM yyyy}', file_0: 'sales_{today}', report_0: 'margins_{today:yyyyMM}' })
  assert.deepEqual(req.targets, { subject: 'text', file_0: 'file', report_0: 'file' })
  assert.equal(req.kind, 'output')
  assert.equal(req.flow, 'Morning')
})

// ── the form's values, as a render takes them ────────────────────────────────
test('an empty field: «no value» for an optional parameter, whatever its default', () => {
  const ps = [
    { name: 'region', multi: false, required: false, default: 'North' }, // emptied
    { name: 'area', multi: false, required: false, default: null }, // nothing to say
    { name: 'tags', multi: true, required: false, default: ['a', 'b'] }, // emptied
    { name: 'free', multi: true, required: false, default: ['a'] },
    { name: 'top', multi: false, required: true, default: 10 }, // the default holds
    { name: 'must', multi: false, required: true, default: null }, // the form asks for it
    { name: 'kept', multi: false, required: false, default: 'x' },
  ]
  assert.deepEqual(reports.paramsOfForm(ps, { region: '', area: '', tags: [], free: ' a , ,b', top: '', must: ' ', kept: ' y ' }),
    { region: '{none}', tags: '{none}', free: ['a', 'b'], kept: 'y' })
  assert.equal(reports.hasDefault({ default: [] }), false)
  assert.equal(reports.hasDefault({ default: 0 }), true)
  assert.equal(reports.hasDefault({ default: false }), true)
  // a link and a script carry it: it is not the default
  assert.deepEqual(reports.explicitParams(ps, { region: '{none}', top: 10 }), { region: '{none}' })
  assert.equal(reports.paramsQuery({ region: '{none}' }), 'region=%7Bnone%7D')
})
