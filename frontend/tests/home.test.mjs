// The home page without a browser: a widget moved, resized, added and removed on the dashboards'
// grid; the gateway's limits held before it is asked; undo and redo; the widgets' small rules.
// The module is the app's own TypeScript, loaded through alias-hooks.mjs (`~/…`).
// Run: npm test
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { register } from 'node:module'

register('./alias-hooks.mjs', import.meta.url)
const H = await import('../composables/homeLayout.ts')

const w = (id, type, x, y, ww, h, options = {}) => ({ id, type, x, y, w: ww, h, options })
const layout = () => [w('clock', 'clock', 0, 0, 3, 3), w('recent', 'recent', 3, 0, 6, 8), w('note', 'note', 0, 3, 3, 4)]
const at = (ws, id) => { const x = ws.find((y) => y.id === id); return [x.x, x.y, x.w, x.h] }
const overlap = (ws) => ws.some((a, i) => ws.some((b, j) => i < j && a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h))

test('a widget moved pushes what it lands on down, and nothing overlaps', () => {
  const next = H.moved(layout(), 'clock', 3, 0)
  assert.deepEqual(at(next, 'clock'), [3, 0, 3, 3])
  assert.deepEqual(at(next, 'recent'), [3, 3, 6, 8])
  assert.equal(overlap(next), false)
  // what was not in the way is the same object: nothing to draw again
  assert.equal(next.find((x) => x.id === 'note'), next.find((x) => x.id === 'note'))
})

test('a move stops at the edges of the 12 columns and at the first row', () => {
  assert.deepEqual(at(H.moved(layout(), 'clock', -5, -5), 'clock'), [0, 0, 3, 3])
  assert.deepEqual(at(H.moved(layout(), 'clock', 50, 0), 'clock'), [9, 0, 3, 3])
})

test('a resize holds the smallest size of the type, on the edge that moved', () => {
  // the clock is at least 2 × 2
  assert.deepEqual(at(H.resized(layout(), 'clock', 'se', -9, -9), 'clock'), [0, 0, 2, 2])
  assert.deepEqual(at(H.resized([w('r', 'recent', 4, 0, 6, 8)], 'r', 'w', 9, 0), 'r'), [7, 0, 3, 8])
  // and grows to the columns' edge, pushing what is under it
  const grown = H.resized(layout(), 'clock', 'se', 9, 2)
  assert.deepEqual(at(grown, 'clock'), [0, 0, 12, 5])
  assert.equal(overlap(grown), false)
  // never taller than the gateway takes
  assert.equal(at(H.resized(layout(), 'note', 's', 0, 200), 'note')[3], H.HOME_MAX_HEIGHT)
})

test('a box the gateway would refuse is refused here first', () => {
  assert.equal(H.fits('recent', { x: 0, y: 0, w: 2, h: 8 }), false)
  assert.equal(H.fits('clock', { x: 11, y: 0, w: 2, h: 2 }), false)
  assert.equal(H.fits('clock', { x: 0, y: 501, w: 2, h: 2 }), false)
  assert.equal(H.fits('clock', { x: 10, y: 500, w: 2, h: 2 }), true)
  assert.equal(H.withBox(layout(), 'clock', { x: 0, y: 0, w: 1, h: 1 }), null)
  assert.equal(H.withBox(layout(), 'nobody', { x: 0, y: 0, w: 3, h: 3 }), null)
})

test('a new widget goes where it first fits, with an id of its own, up to thirty', () => {
  const first = H.added(layout(), 'note')
  assert.equal(first.id, 'note-2')
  assert.equal(overlap(first.widgets), false)
  assert.deepEqual(at(first.widgets, 'note-2').slice(2), H.HOME_NEW.note)
  assert.equal(H.added(first.widgets, 'failed_runs').id, 'failed-runs')
  let many = []
  for (let i = 0; i < H.HOME_MAX_WIDGETS; i++) many = H.added(many, 'note').widgets
  assert.equal(many.length, 30)
  assert.equal(new Set(many.map((x) => x.id)).size, 30)
  assert.equal(overlap(many), false)
  assert.equal(H.added(many, 'note'), null)
})

test('removing and setting options leave the others as they were', () => {
  const ws = layout()
  assert.deepEqual(H.removed(ws, 'recent').map((x) => x.id), ['clock', 'note'])
  const next = H.withOptions(ws, 'note', { text: 'hello' })
  assert.deepEqual(next.find((x) => x.id === 'note').options, { text: 'hello' })
  assert.equal(next[0], ws[0])
})

test('the gateway is sent the layout alone', () => {
  const body = H.bodyOf([{ ...w('a', 'note', 0, 0, 2, 2, { text: 'x' }), extra: 1 }])
  assert.deepEqual(body, { v: 1, widgets: [{ id: 'a', type: 'note', x: 0, y: 0, w: 2, h: 2, options: { text: 'x' } }] })
})

test('a type this interface cannot draw is left out', () => {
  assert.deepEqual(H.knownWidgets([w('a', 'note', 0, 0, 2, 2), w('b', 'from_a_newer_gateway', 2, 0, 2, 2)]).map((x) => x.id), ['a'])
  for (const t of H.HOME_TYPES) assert.ok(H.HOME_MIN[t] && H.HOME_NEW[t], t)
})

test('undo and redo go through whole layouts, and a new change forgets the redo', () => {
  const h = H.history()
  assert.equal(h.undo('a'), null)
  h.record('a'); h.record('b')
  assert.equal(h.undo('c'), 'b')
  assert.equal(h.undo('b'), 'a')
  assert.equal(h.canUndo(), false)
  assert.equal(h.redo('a'), 'b')
  h.record('b')
  assert.equal(h.canRedo(), false)
})

test('a shortcut is a page of this installation, never an address of elsewhere', () => {
  for (const ok of ['/flows', '/viewer?ds=3', '/admin?tab=users']) assert.equal(H.internalPath(ok), true, ok)
  for (const no of ['', 'flows', '//elsewhere.example', 'https://elsewhere.example', 'javascript:alert(1)', '/a\\b', '/a\nb', '/' + 'x'.repeat(200)]) assert.equal(H.internalPath(no), false, no)
})

test('the clock shows its zone, the browser\'s when none is chosen', () => {
  const noon = new Date(Date.UTC(2026, 10, 17, 12, 0, 5))
  const rome = H.clockFace(noon, { zone: 'Europe/Rome' }, 'en-GB')
  assert.equal(rome.time, '13:00')
  assert.equal(rome.zone, 'Europe/Rome')
  assert.match(rome.date, /Tuesday.*17.*November.*2026/)
  assert.equal(H.clockFace(noon, { zone: 'America/New_York', hour12: true, seconds: true }, 'en-US').time, '07:00:05 AM')
  assert.equal(H.clockFace(noon, {}, 'en-GB').zone, Intl.DateTimeFormat().resolvedOptions().timeZone)
})

test('failed runs: within the widget\'s days, and the person\'s own when it says mine', () => {
  const now = Date.parse('2026-11-17T12:00:00Z')
  const runs = [
    { id: 1, launched_by: 7, started_at: '2026-11-17T10:00:00+00:00', finished_at: '2026-11-17T10:01:00+00:00' },
    { id: 2, launched_by: 9, started_at: '2026-11-16T10:00:00+00:00', finished_at: null },
    { id: 3, launched_by: 7, started_at: '2026-11-01T10:00:00+00:00', finished_at: '2026-11-01T10:01:00+00:00' },
  ]
  assert.deepEqual(H.failedFor(runs, { scope: 'all', days: 7 }, 7, now).map((r) => r.id), [1, 2])
  assert.deepEqual(H.failedFor(runs, { scope: 'mine', days: 7 }, 7, now).map((r) => r.id), [1])
  assert.deepEqual(H.failedFor(runs, { scope: 'mine', days: 30 }, 7, now).map((r) => r.id), [1, 3])
  assert.deepEqual(H.failedFor(runs, { scope: 'mine' }, null, now), [])
})

test('a duration and a size read as a person would say them', () => {
  assert.deepEqual([4.23, 42.4, 65, 3725, null].map(H.durationText), ['4.2 s', '42 s', '1 m 05 s', '1 h 02 m', '–'])
  assert.deepEqual([512, 2048, 5_000_000, 3 * 1024 ** 3, null].map(H.bytesText), ['512 B', '2 KB', '4.8 MB', '3 GB', '–'])
  assert.equal(H.median([5, 1, 3]), 3)
  assert.equal(H.median([4, 1, 3, 2]), 2.5)
  assert.equal(H.median([]), null)
})

test('a flow\'s runs as bars: as tall as they lasted, coloured by how they ended', () => {
  const runs = [
    { id: 1, status: 'SUCCESS', trigger: 'schedule', started_at: null, seconds: 10 },
    { id: 2, status: 'FAILURE', trigger: 'manual', started_at: null, seconds: 40 },
    { id: 3, status: 'SUCCESS', trigger: 'schedule', started_at: null, seconds: 0.1 },
    { id: 4, status: 'STARTED', trigger: 'manual', started_at: null, seconds: null },
  ]
  const bars = H.runBars(runs)
  assert.deepEqual(bars.map((b) => b.tone), ['ok', 'ko', 'ok', 'live'])
  assert.deepEqual(bars.map((b) => b.height), [25, 100, 8, 25]) // the shortest keeps a floor; the one going stands at the median
  assert.deepEqual(H.runBars([{ id: 1, status: 'STARTED', trigger: 'manual', started_at: null, seconds: null }]).map((b) => b.height), [40])
  assert.deepEqual(H.runBars([]), [])
})

test('runs by day: every day of the span, the empty ones too, against the busiest', () => {
  const today = new Date(2026, 10, 17)
  const cols = H.dayColumns([
    { key: '2026-11-17', total: 10, success: 6, failure: 3 },
    { key: '2026-11-15', total: 5, success: 5, failure: 0 },
    { key: '2026-10-01', total: 99, success: 99, failure: 0 }, // out of the span
  ], 4, today)
  assert.deepEqual(cols.map((c) => c.key), ['2026-11-14', '2026-11-15', '2026-11-16', '2026-11-17'])
  assert.deepEqual(cols.map((c) => c.total), [0, 5, 0, 10])
  assert.deepEqual([cols[3].okH, cols[3].koH, cols[3].otherH], [60, 30, 10])
  assert.equal(cols[1].okH, 50)
  assert.equal(H.dayColumns([], 7, today).every((c) => c.total === 0 && c.okH === 0), true)
})

test('the greeting follows the hour', () => {
  assert.deepEqual([3, 6, 11, 12, 17, 18, 23].map(H.greetingKey), ['evening', 'morning', 'morning', 'afternoon', 'afternoon', 'evening', 'evening'])
})

test('every widget the interface draws has its sizes, and they hold together', () => {
  for (const t of H.HOME_TYPES) {
    const [mw, mh] = H.HOME_MIN[t]; const [w, h] = H.HOME_NEW[t]
    assert.ok(w >= mw && h >= mh && w <= 12, t)
  }
})
