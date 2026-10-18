// The landing page's placeholder resolver against the product's own cases:
// backend/tests/test_api_placeholders.py, backend/tests/test_placeholders.py (outputs),
// and the examples in the README's "Dynamic inputs and outputs" table.
// Run: npm test
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { check, render, renderOutput, PlaceholderError, checkZone, type Ctx, type Value } from '../app/utils/placeholders.ts'

// a Saturday evening in UTC, already Sunday in Tokyo
const NOW = new Date(Date.UTC(2026, 9, 17, 22, 30, 5))

function ctx (o: { tz?: string, lastRefresh?: string | null, variables?: Record<string, Value>, now?: Date } = {}): Ctx {
  return { now: o.now || NOW, zone: o.tz || 'UTC', lastRefresh: o.lastRefresh ? new Date(o.lastRefresh) : null, variables: o.variables || {} }
}
const r = (t: string, o: Parameters<typeof ctx>[0] = {}) => render(t, ctx(o))
const text = (value: string): Value => ({ kind: 'text', value })
const throwsLike = (fn: () => unknown, re?: RegExp | string) => {
  assert.throws(fn, (e: unknown) => {
    assert.ok(e instanceof PlaceholderError, `expected a PlaceholderError, got ${e}`)
    if (re) assert.match((e as Error).message, typeof re === 'string' ? new RegExp(re.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')) : re)
    return true
  })
}

// ── test_api_placeholders.py ──────────────────────────────────────────────────
test('today, yesterday, now in ISO', () => {
  assert.equal(r('{today}'), '2026-10-17')
  assert.equal(r('{yesterday}'), '2026-10-16')
  assert.equal(r('{now}'), '2026-10-17T22:30:05+00:00')
})

test('the time zone defines today', () => {
  assert.equal(r('{today}', { tz: 'Asia/Tokyo' }), '2026-10-18')
  assert.equal(r('{now}', { tz: 'Europe/Rome' }), '2026-10-18T00:30:05+02:00')
  assert.equal(r('{today}', { tz: 'America/New_York' }), '2026-10-17')
})

for (const [t, want] of [
  ['{today-1}', '2026-10-16'], ['{today-7d}', '2026-10-10'], ['{today+1d}', '2026-10-18'], ['{today-2w}', '2026-10-03'],
  ['{today-1m}', '2026-09-17'], ['{today-1y}', '2025-10-17'], ['{start_of_month}', '2026-10-01'], ['{start_of_month-1m}', '2026-09-01'],
  ['{start_of_week}', '2026-10-12'], ['{start_of_year}', '2026-01-01'], ['{now-2h}', '2026-10-17T20:30:05+00:00'],
  ['{now-30min}', '2026-10-17T22:00:05+00:00'], ['{now+15s}', '2026-10-17T22:30:20+00:00'], ['{now-1d}', '2026-10-16T22:30:05+00:00'],
] as const) test(`offset ${t}`, () => assert.equal(r(t), want))

test('months are clamped to the end of the month', () => {
  const march31 = new Date(Date.UTC(2026, 2, 31, 12))
  assert.equal(r('{today-1m}', { now: march31 }), '2026-02-28')
  assert.equal(r('{today+11m}', { now: march31 }), '2027-02-28')
  assert.equal(r('{today+1y}', { now: new Date(Date.UTC(2024, 1, 29)) }), '2025-02-28')
})

for (const [t, want] of [
  ['{today:%Y%m%d}', '20261017'], ['{today:%d/%m/%Y}', '17/10/2026'], ['{now:%Y-%m-%dT%H:%M:%SZ}', '2026-10-17T22:30:05Z'],
  ['{now:epoch}', '1792276205'], ['{now:epoch_ms}', '1792276205000'], ['{today:epoch}', '1792195200'], ['{today-1:%Y%m%d}', '20261016'],
] as const) test(`format ${t}`, () => assert.equal(r(t), want))

test('epoch of a date is its midnight in the time zone', () => {
  assert.equal(r('{today:epoch}', { tz: 'Europe/Rome' }), String(Date.UTC(2026, 9, 18) / 1000 - 2 * 3600))
})

test('text around and several placeholders', () => assert.equal(r('/v1/{today-30}..?d={yesterday}'), '/v1/2026-09-17..?d=2026-10-16'))

test('double braces are literal braces', () => {
  assert.equal(r('{{today}} {{ and }}'), '{today} { and }')
  assert.equal(r('{{{today}}}'), '{2026-10-17}')
})

test('variables, and quote applies to values only', () => {
  assert.equal(r('/quote/{symbol}', { variables: { symbol: text('ENI.MI') } }), '/quote/ENI.MI')
  assert.equal(render('/q/{symbol}/x', ctx({ variables: { symbol: text('A/B C') } }), encodeURIComponent), '/q/A%2FB%20C/x')
})

test('last_refresh is in the time zone, with offsets and formats', () => {
  assert.equal(r('{last_refresh}', { lastRefresh: '2026-10-16T06:00:00Z', tz: 'Europe/Rome' }), '2026-10-16T08:00:00+02:00')
  assert.equal(r('{last_refresh:%Y-%m-%d}', { lastRefresh: '2026-10-16T23:30:00Z', tz: 'Europe/Rome' }), '2026-10-17')
  assert.equal(r('{last_refresh-1h}', { lastRefresh: '2026-10-16T06:00:00Z' }), '2026-10-16T05:00:00+00:00')
  throwsLike(() => r('{last_refresh}'), 'initial')
})

for (const [t, msg] of [
  ['{tomorrow}', 'unknown placeholder'], ['{today-}', 'bad placeholder'], ['{today*2}', 'bad placeholder'], ['{today-2h}', 'hours'],
  ['{now-2}', 'unit'], ['{today:%}', null], ['{today:weird}', 'format'], ['{symbol-1}', 'variable'], ['{symbol:%Y}', 'variable'],
  ['{today', 'not closed'], ['today}', 'single'], ['{}', 'empty'], ['{today-99999999d}', 'bad placeholder'],
] as const) {
  test(`refused: ${t}`, () => throwsLike(() => {
    check(t, { variables: ['symbol'], hasInitial: true })
    r(t, { variables: { symbol: text('X') } })
  }, msg || undefined))
}

test('check finds the variables used and wants an initial value for last_refresh', () => {
  assert.deepEqual([...check('/q/{symbol}?d={today}&x={{literal}}', { variables: ['symbol', 'unused'] })], ['symbol'])
  throwsLike(() => check('{last_refresh}'), 'initial')
  assert.equal(check('{last_refresh}', { hasInitial: true }).size, 0)
})

test('an unknown time zone is an error', () => {
  throwsLike(() => checkZone('Mars/Olympus'), 'time zone')
  throwsLike(() => checkZone('../../etc/passwd'), 'time zone')
})

test('names are case-sensitive', () => throwsLike(() => check('{Today}'), 'unknown placeholder'))

for (const [t, want] of [
  ['{today:yyyy-MM-dd}', '2026-10-17'], ['{today:ddMMyyyy}', '17102026'], ['{today:ddmmyyyy}', '17102026'], ['{today:dd/MM/yy}', '17/10/26'],
  ['{today-1:yyyyMMdd}', '20261016'], ['{now:yyyy-MM-dd HH:mm:ss}', '2026-10-17 22:30:05'], ['{now:HHmm}', '2230'], ['{now:mm:ss}', '30:05'],
  ["{now:yyyyMMdd'T'HHmmss}", '20261017T223005'],
] as const) test(`readable ${t}`, () => assert.equal(r(t), want))

for (const t of ['{today:dd-qq-yyyy}', '{today:ddd}', '{today:yyy}']) test(`unknown readable token ${t}`, () => throwsLike(() => check(t)))

for (const [t, msg] of [
  ['{now:%s}', 'epoch'], ['{today:%-s}', 'epoch'], ['{today:%z}', 'date'], ['{today:%Z}', 'date'], ['{now:hh:mm}', 'HH'],
  ['{today:MMMM}', 'twice'], ['{today:yyyy-MM-ddTHH:mm}', "'T'"], ["{today:''}", 'no date'], ['{today:-}', 'no date'],
] as const) test(`review refusal ${t}`, () => throwsLike(() => check(t), msg))

test('a time zone offset is fine on a date-time; quotes in readable formats', () => {
  assert.equal(r('{now:%z}', { tz: 'Europe/Rome' }), '+0200')
  assert.equal(r("{today:yyyy'''s'}"), "2026's")
  assert.equal(r("{today:'it''s' yyyy}"), "it's 2026")
  assert.equal(r("{today:'%s' yyyy}"), '%s 2026')
})

// ── test_placeholders.py: outputs ─────────────────────────────────────────────
const octx = (o: { tz?: string, runId?: number | null, flow?: string } = {}): Ctx => ({
  now: NOW, zone: o.tz || 'UTC', output: true,
  variables: { ...(o.runId === null ? {} : { run_id: text(String(o.runId ?? 42)) }), flow: text(o.flow ?? 'Report/mensile') },
})
const out = (t: string, kind: 'file' | 'path' | 'text' = 'file', o: Parameters<typeof octx>[0] = {}) => renderOutput(t, octx(o), kind, 'file name')

for (const [t, want] of [
  ['vendite_{date}', 'vendite_2026-10-17'], ['vendite_{datetime}', 'vendite_2026-10-17_223005'], ['run_{run_id}', 'run_42'],
  ['{flow}_{date}', 'Report_mensile_2026-10-17'], ['{today:yyyyMMdd}', '20261017'], ['{yesterday:ddMMyyyy}', '16102026'],
  ["{now:HH'h'mm}", '22h30'], ['{date-1d}', '2026-10-16'], ['{start_of_month:yyyy-MM}', '2026-10'], ['{{x}}_{today}', '{x}_2026-10-17'],
] as const) test(`output ${t}`, () => assert.equal(out(t), want))

test('an output resolves in its time zone', () => {
  assert.equal(out('{date}_{datetime}', 'file', { tz: 'Asia/Tokyo' }), '2026-10-18_2026-10-18_073005')
  assert.equal(out('{today}', 'file', { tz: 'Europe/Rome' }), '2026-10-18')
})

test('outputs: flow, last_refresh, run_id rules', () => {
  assert.equal(out('Report {flow} {today}', 'text'), 'Report Report/mensile 2026-10-17')
  throwsLike(() => check('{last_refresh}', { output: true }), 'Output')
  throwsLike(() => check('{run_id:%Y}', { output: true }), 'no offset or format')
  throwsLike(() => check('{flow-1}', { output: true }), 'no offset or format')
  throwsLike(() => out('x_{run_id}', 'file', { runId: null }), 'only known when the flow runs')
  throwsLike(() => check('x_{week}', { output: true }), /unknown placeholder \{week\}.*\{run_id\}.*\{flow\}/)
  throwsLike(() => check('{run_id}'), /unknown placeholder \{run_id\}/)
  assert.deepEqual([...check('{date}', { variables: ['date'] })], ['date'])
})

test('file names, paths and texts', () => {
  throwsLike(() => out('{today:yyyy/MM/dd}.csv'), "'/'")
  throwsLike(() => out("{today:yyyy'\\'MM}x"), "'/'")
  assert.equal(out('exports/{today:yyyy/MM}/v.csv', 'path'), 'exports/2026/10/v.csv')
  throwsLike(() => out('exports/../v.csv', 'path'), "'..'")
  assert.equal(out('x/{flow}/y', 'path', { flow: '../../etc' }), 'x/etc/y')
  assert.equal(out('{flow}', 'file', { flow: '..' }), 'flow')
  throwsLike(() => check('x_{today', { output: true }), 'not closed')
  throwsLike(() => check('{now:%s}', { output: true }), '%s is not supported')
  for (const f of ['%n', '%t']) throwsLike(() => check('a{today:' + f + '}.csv', { output: true }))
  throwsLike(() => out('Report {flow}', 'text', { flow: 'a\nBcc: x@evil' }), 'line break')
  throwsLike(() => out('x'.repeat(250) + '_{date}'), '255')
  throwsLike(() => out('a/' + 'y'.repeat(250) + '{date}/b', 'path'), '255')
  assert.equal(out('a/{date}/b', 'path'), 'a/2026-10-17/b')
})

test('a range value takes formats and offsets', () => {
  const c = ctx({ variables: { day: { kind: 'date', date: { y: 2026, m: 10, d: 14 } }, day_end: { kind: 'date', date: { y: 2026, m: 10, d: 15 } } } })
  const t = 's_{day:yyyyMMdd}_{day_end:ddMMyyyy}_{day-1d}_{day:epoch}'
  assert.deepEqual([...check(t, { ranges: ['day'] })], ['day'])
  assert.equal(render(t, c), 's_20261014_15102026_2026-10-13_1791936000')
})

// ── the README's table, «on 18 October 2026» (09:30 in Rome) ──────────────────
test('README examples', () => {
  const c: Ctx = { now: new Date(Date.UTC(2026, 9, 18, 7, 30)), zone: 'Europe/Rome', variables: {} }
  const rr = (t: string) => render(t, c)
  assert.equal(rr('{today} · {yesterday} · {today-7} · {today-1m}'), '2026-10-18 · 2026-10-17 · 2026-10-11 · 2026-09-18')
  assert.equal(rr('{start_of_month} · {start_of_week}'), '2026-10-01 · 2026-10-12')
  assert.equal(rr("{yesterday:ddMMyyyy} · {today:yyyy/MM/dd} · {now:yyyy-MM-dd'T'HH:mm}"), '17102026 · 2026/10/18 · 2026-10-18T09:30')
  assert.equal(rr('{today:%Y%m%d}'), '20261018')
  assert.equal(renderOutput('report_{today:yyyyMMdd}_{run_id}', { ...c, variables: { run_id: text('381'), flow: text('x') } }, 'file', 'file name'), 'report_20261018_381')
})

test('autumn DST: a day offset keeps the wall-clock time', () => {
  // 25 Oct 2026 03:30 in Rome is after the change (+01:00); one day earlier is +02:00 at the same wall time
  const c: Ctx = { now: new Date(Date.UTC(2026, 9, 25, 2, 30)), zone: 'Europe/Rome', variables: {} }
  assert.equal(render('{now}', c), '2026-10-25T03:30:00+01:00')
  assert.equal(render('{now-1d}', c), '2026-10-24T03:30:00+02:00')
})
