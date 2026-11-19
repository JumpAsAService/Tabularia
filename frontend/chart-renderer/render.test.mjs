// The reports' chart service (render.mjs): every preset the engine builds is drawn, names are
// values and never code, only the named formatters run, no image or path gets in, and the
// HTTP service answers as the workers expect.
// Run: node --test chart-renderer/render.test.mjs (from frontend/, with its node_modules).
// presets.sample.json is one chart of every kind as backend/app/reports/charts.py builds it,
// written again with: cd backend && python -m tests.report_samples ../frontend/chart-renderer/presets.sample.json
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { readFileSync } from 'node:fs'

import { render } from './render.mjs'

const presets = JSON.parse(readFileSync(new URL('./presets.sample.json', import.meta.url), 'utf8'))
const chart = (option, width = 300, height = 200) => ({ width, height, option })

test('every kind of chart the engine builds is drawn', () => {
  assert.equal(presets.length, 18)
  for (const spec of presets) {
    const svg = render(spec)
    assert.match(svg, /^<svg /, spec.name)
    assert.ok(svg.length > 500, `${spec.name} draws something`)
    // the title has <script> in it: a text, escaped
    assert.ok(!svg.includes('<script>'), spec.name)
  }
})

test('an xy chart: a band filled between its two edges, below zero too, and dates along x', () => {
  // the band as the engine draws it: its low edge, then its width stacked on it («stackStrategy: all»)
  const edge = { type: 'line', stack: 'b', stackStrategy: 'all', showSymbol: false, lineStyle: { opacity: 0 } }
  const svg = render({ width: 120, height: 120, option: { animation: false, grid: { left: 10, right: 10, top: 10, bottom: 10 },
    xAxis: { type: 'value', min: 0, max: 10, show: false }, yAxis: { type: 'value', min: -2, max: 3, show: false },
    series: [{ ...edge, data: [[0, -1], [4, -1], [10, -1]] },
      { ...edge, data: [[0, 2], [4, 3], [10, 2]], areaStyle: { color: '#ff0000', opacity: 0.5 } }] } })
  // 20 px a unit from y = 3 at the top: the high edge at 1, 2, 1, the low one at −1
  assert.match(svg, /<path d="M10 50L50 30L110 50L110 90L50 90L10 90Z" fill="#ff0000"/)
  const xy = presets.find((p) => p.name === 'xy')
  const types = xy.option.series.map((s) => s.type)
  for (const type of ['scatter', 'line', 'bar']) assert.ok(types.includes(type), type)
  assert.ok(xy.option.series.some((s) => s.step === 'end') && xy.option.series.some((s) => s.areaStyle))
  // an axis of dates writes them by a template of numbers (no month's name in English on an Italian page)
  const dated = render(chart({ xAxis: { type: 'time', axisLabel: { formatter: { year: '{yyyy}', month: '{yyyy}-{MM}' } } },
    yAxis: { type: 'value' }, series: [{ type: 'line', data: [[Date.UTC(2023, 0, 1), 1], [Date.UTC(2025, 6, 1), 2]] }] }, 400))
  assert.match(dated, />2024</)
  assert.ok(!/>(Jan|Feb|Mar|Apr|Jul)</.test(dated))
})

test('the named formatters write the engine\'s texts and the locale\'s numbers', () => {
  const svg = render(chart({
    xAxis: { type: 'category', data: ['a'] },
    yAxis: { type: 'value', axisLabel: { formatter: { $fmt: 'compact', locale: 'de' } } },
    series: [{ type: 'bar', data: [{ value: 1234567, t: '1.234.567,00 €' }], label: { show: true, formatter: { $fmt: 'label' } } }],
  }))
  assert.ok(svg.includes('1.234.567,00 €'))
  assert.ok(svg.includes('Mio.'))
})

test('an axis over values around 1e9 that differ by tens writes whole numbers: its ticks are told apart', () => {
  const svg = render(chart({
    xAxis: { type: 'category', data: ['a', 'b'] },
    yAxis: { type: 'value', scale: true, axisLabel: { formatter: { $fmt: 'compact', locale: 'en', digits: 11 } } },
    series: [{ type: 'boxplot', data: [[1000000010, 1000000030, 1000000045, 1000000060, 1000000080],
      [1000000020, 1000000040, 1000000050, 1000000070, 1000000090]] }],
  }, 600))
  const ticks = [...svg.matchAll(/>(1,000,000,\d{3})</g)].map((m) => m[1])
  assert.ok(ticks.length >= 3 && new Set(ticks).size === ticks.length, `whole, distinct ticks: ${ticks}`)
  assert.ok(!svg.includes('>1B<'))
})

test('a long name ends in «…», a pie keeps its share on a line of its own, small numbers keep their digits', () => {
  const long = 'Società Cooperativa Agricola Allevatori dell\'Appennino Tosco-Emiliano'
  const svg = render(chart({
    xAxis: { type: 'category', data: [long, 'b'], axisLabel: { interval: 0, formatter: { $fmt: 'short', max: 20 } } },
    yAxis: { type: 'value', axisLabel: { formatter: { $fmt: 'compact', locale: 'en' } } },
    series: [{ type: 'bar', data: [0.05, 0.15] }],
  }, 600))
  assert.ok(svg.includes('Società Cooperativa…') && !svg.includes('Allevatori'))
  const ticks = [...svg.matchAll(/>(0\.\d+)</g)].map((m) => m[1])
  assert.ok(ticks.length >= 3 && new Set(ticks).size === ticks.length, `the ticks of a small scale are told apart: ${ticks}`)
  const pie = render(chart({ series: [{ type: 'pie', data: [{ name: long, value: 3 }, { name: 'b', value: 1 }],
    label: { formatter: { $fmt: 'namePercent', locale: 'it', max: 20 } } }] }, 600, 300))
  assert.ok(pie.includes('Società Cooperativa…') && pie.includes('75%'))
})

test('a name is a value, never a formatter or code', () => {
  const svg = render(chart({
    xAxis: { type: 'category', data: ['fmt:compact', '<script>alert(1)</script>', '{a}{b}'], axisLabel: { interval: 0 } },
    yAxis: { type: 'value' },
    series: [{ type: 'bar', data: [1, 2, 3] }],
  }, 900))
  assert.ok(svg.includes('fmt:compact') && svg.includes('&lt;script&gt;') && !svg.includes('<script>'))
})

test('only the formatters of the list, and no images', () => {
  assert.throws(() => render(chart({ series: [{ type: 'bar', label: { formatter: { $fmt: 'eval' } } }] })), /unknown formatter/)
  assert.throws(() => render(chart({ series: [{ type: 'bar', label: { formatter: { $fmt: 'toString' } } }] })), /unknown formatter/)
  assert.throws(() => render(chart({ graphic: [{ type: 'image', style: { image: 'http://x' } }] })), /not allowed/)
  assert.throws(() => render(chart({ series: [{ type: 'scatter', symbol: 'image://http://x/a.png', data: [[1, 1]] }] })), /no images/)
  assert.throws(() => render(chart({}, 5, 5)), /between 10 and 4000/)
  // a key named __proto__ in JSON does not reach the prototype
  const option = JSON.parse('{"__proto__": {"polluted": true}, "series": []}')
  render(chart(option))
  assert.equal({}.polluted, undefined)
})

test('the lines of a label on two lines are a font size apart, not on top of each other', () => {
  // zrender takes a line's height from the width of «国»: the table of the PDF's font has no CJK
  const svg = render(chart({ series: [{ type: 'treemap', data: [{ name: 'Alpha', value: 10, t: '12,345' }],
    label: { fontSize: 8, formatter: { $fmt: 'nameLabel' } } }] }))
  const ys = [...svg.matchAll(/font-size:8px;[^>]*y="(-?[\d.]+)"[^>]*>(Alpha|12,345)</g)].map((m) => Number(m[1]))
  assert.equal(ys.length, 2)
  assert.ok(ys[1] - ys[0] >= 8, `lines ${ys[1] - ys[0]} px apart`)
})

test('the service answers SVG, says what is wrong, and has a health check', async () => {
  const port = 18090 + Math.floor(Math.random() * 1000)
  const child = spawn(process.execPath, [new URL('./render.mjs', import.meta.url).pathname, '--serve', String(port)], { stdio: 'pipe' })
  try {
    const base = `http://127.0.0.1:${port}`
    for (let i = 0; i < 50; i++) {
      try {
        if ((await fetch(`${base}/healthz`)).ok) break
      } catch {
        await new Promise((r) => setTimeout(r, 100))
      }
    }
    const ok = await fetch(`${base}/render`, { method: 'POST', body: JSON.stringify(presets[1]) })
    assert.equal(ok.status, 200)
    assert.match(ok.headers.get('content-type'), /image\/svg\+xml/)
    assert.match(await ok.text(), /^<svg /)
    const bad = await fetch(`${base}/render`, { method: 'POST', body: '{"width": 100}' })
    assert.equal(bad.status, 400)
    assert.match(await bad.text(), /expected \{width, height, option\}/)
    assert.equal((await fetch(`${base}/other`)).status, 404)
  } finally {
    child.kill('SIGTERM')
  }
})
