// Dashboards without a browser: what a new one starts from, where a search result leads, the grid
// at each width, which answer counts for a tile, what stays after a failure, when to read again,
// the parameters in the address, a report's style on any theme, and every chart kind's option.
// The modules are the app's own TypeScript, loaded through alias-hooks.mjs (`~/…`).
// Run: npm test
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { register } from 'node:module'
import { readFileSync } from 'node:fs'

register('./alias-hooks.mjs', import.meta.url)
const { starterDashboard } = await import('../composables/useDashboards.ts')
const { searchHitTarget } = await import('../composables/useSearch.ts')

test('a new dashboard is empty, refreshed by hand, in the language of whoever makes it', () => {
  assert.deepEqual(starterDashboard('it', 'Europe/Rome'), {
    v: 1, locale: 'it', timezone: 'Europe/Rome', refresh_seconds: 0, parameters: [], datasets: [], tiles: [],
  })
})

test('a language or a zone the definition does not take falls back to what it does', () => {
  const started = starterDashboard('pt', '')
  assert.equal(started.locale, 'en')
  assert.equal(started.timezone, 'UTC')
})

test('a dashboard found by the search opens on its page', () => {
  assert.equal(searchHitTarget({ kind: 'dashboard', id: 7, name: 'Live' }), '/dashboard?id=7')
})

// ── the grid at three widths ────────────────────────────────────────────────
const { gridWidth, columnsOf, placeTiles, GRID_COLUMNS } = await import('../composables/dashboardGrid.ts')

const box = (id, x, y, w, h, type = 'kpi') => ({ id, type, grid: { x, y, w, h } })
const at = (placed) => Object.fromEntries(placed.map((p) => [p.id, [p.x, p.y, p.w, p.h]]))
const overlaps = (a, b) => a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h
function assertSound(placed, tiles, columns) {
  assert.deepEqual(placed.map((p) => p.id).sort(), tiles.map((t) => t.id).sort(), 'every tile once')
  for (const p of placed) assert.ok(p.x >= 0 && p.w >= 1 && p.x + p.w <= columns && p.y >= 0, `${p.id} inside the ${columns} columns`)
  for (let i = 0; i < placed.length; i++) {
    for (let j = i + 1; j < placed.length; j++) assert.ok(!overlaps(placed[i], placed[j]), `${placed[i].id} on ${placed[j].id}`)
  }
}

test('the three widths break at 700 and 1100 pixels', () => {
  assert.deepEqual([1920, 1100, 1099, 700, 699, 390].map(gridWidth), ['wide', 'wide', 'medium', 'medium', 'narrow', 'narrow'])
  assert.deepEqual(['wide', 'medium', 'narrow'].map(columnsOf), [GRID_COLUMNS, 6, 1])
})

test('wide draws the tiles where they were put, in reading order', () => {
  const tiles = [box('c', 6, 3, 6, 4, 'chart'), box('b', 3, 0, 3, 3), box('a', 0, 0, 3, 3)]
  const placed = placeTiles(tiles, 'wide')
  assert.deepEqual(placed.map((p) => p.id), ['a', 'b', 'c'])
  assert.deepEqual(at(placed), { a: [0, 0, 3, 3], b: [3, 0, 3, 3], c: [6, 3, 6, 4] })
})

test('medium halves the widths rounding up, keeping two halves side by side', () => {
  const placed = placeTiles([box('a', 0, 0, 6, 4, 'chart'), box('b', 6, 0, 6, 4, 'chart'), box('c', 0, 4, 12, 4, 'table')], 'medium')
  assert.deepEqual(at(placed), { a: [0, 0, 3, 4], b: [3, 0, 3, 4], c: [0, 4, 6, 4] })
})

test('medium keeps a tall tile beside the two it stood beside', () => {
  const placed = placeTiles([box('tall', 0, 0, 6, 6, 'chart'), box('top', 6, 0, 6, 3), box('low', 6, 3, 6, 3)], 'medium')
  assert.deepEqual(at(placed), { tall: [0, 0, 3, 6], top: [3, 0, 3, 3], low: [3, 3, 3, 3] })
})

test('medium never lays two tiles on the same cells when their halves no longer fit a row', () => {
  // four quarters: each is 3 of 12, so 2 of 6, and only three fit a row
  const tiles = [0, 3, 6, 9].map((x, i) => box(`k${i}`, x, 0, 3, 3))
  const placed = placeTiles(tiles, 'medium')
  assertSound(placed, tiles, 6)
  assert.deepEqual(at(placed), { k0: [0, 0, 2, 3], k1: [2, 0, 2, 3], k2: [4, 0, 2, 3], k3: [0, 3, 6, 3] })
})

test('medium leaves no hole at the end of a row: the last tile takes what is free beside it', () => {
  // 7 of 12 is 4 of 6, 5 of 12 is 3 of 6: they cannot stay together, and each fills its row
  const placed = placeTiles([box('a', 0, 0, 7, 4, 'chart'), box('b', 7, 0, 5, 4, 'chart')], 'medium')
  assert.deepEqual(at(placed), { a: [0, 0, 6, 4], b: [0, 4, 6, 4] })
})

test('medium closes the empty rows and columns the author left on the wide grid', () => {
  const placed = placeTiles([box('a', 6, 0, 6, 3), box('b', 0, 10, 12, 4, 'table')], 'medium')
  assert.deepEqual(at(placed), { a: [0, 0, 6, 3], b: [0, 3, 6, 4] })
})

test('medium uses a hole under a shorter tile for what comes next, and only what comes next', () => {
  const placed = placeTiles([box('tall', 0, 0, 6, 6, 'chart'), box('short', 6, 0, 6, 3), box('next', 0, 6, 6, 3)], 'medium')
  assert.deepEqual(at(placed), { tall: [0, 0, 3, 6], short: [3, 0, 3, 3], next: [3, 3, 3, 3] })
})

test('medium leaves no hole under a tile shorter than the one beside it, and does not grow the page', () => {
  const placed = placeTiles([box('tall', 0, 0, 6, 6, 'chart'), box('short', 6, 0, 6, 3)], 'medium')
  assert.deepEqual(at(placed), { tall: [0, 0, 3, 6], short: [3, 0, 3, 6] })
  // wide draws what the author drew, hole and all
  assert.deepEqual(at(placeTiles([box('tall', 0, 0, 6, 6, 'chart'), box('short', 6, 0, 6, 3)], 'wide')), { tall: [0, 0, 6, 6], short: [6, 0, 6, 3] })
})

test('on a thousand layouts medium and narrow keep every tile, once, without overlaps, in the order drawn', () => {
  let seed = 42
  const random = (n) => { seed = (seed * 1103515245 + 12345) & 0x7fffffff; return seed % n }
  for (let round = 0; round < 1000; round++) {
    const tiles = []
    for (let i = 0; i < 40; i++) {
      const w = 1 + random(12); const h = 1 + random(8)
      const t = box(`t${i}`, random(13 - w), random(40), w, h, ['kpi', 'chart', 'table', 'text', 'image'][random(5)])
      if (!tiles.some((o) => overlaps(o.grid, t.grid))) tiles.push(t)
    }
    const drawn = placeTiles(tiles, 'wide').map((p) => p.id)
    for (const [width, columns] of [['medium', 6], ['narrow', 1]]) {
      const placed = placeTiles(tiles, width)
      assertSound(placed, tiles, columns)
      const byId = new Map(placed.map((p) => [p.id, p]))
      for (let i = 1; i < drawn.length; i++) {
        assert.ok(byId.get(drawn[i]).y >= byId.get(drawn[i - 1]).y, `${width}: ${drawn[i]} above ${drawn[i - 1]}`)
      }
    }
  }
})

test('narrow is one column, top to bottom and left to right', () => {
  const placed = placeTiles([box('c', 0, 3, 6, 2, 'text'), box('b', 6, 0, 6, 3), box('a', 0, 0, 6, 2, 'text')], 'narrow')
  assert.deepEqual(placed.map((p) => p.id), ['a', 'b', 'c'])
  assert.deepEqual(at(placed), { a: [0, 0, 1, 2], b: [0, 2, 1, 3], c: [0, 5, 1, 2] })
})

test('narrow gives a chart and a table the height a phone can read', () => {
  const placed = placeTiles([box('c', 0, 0, 4, 4, 'chart'), box('t', 4, 0, 4, 4, 'table'), box('big', 8, 0, 4, 12, 'chart')], 'narrow')
  assert.deepEqual(at(placed), { c: [0, 0, 1, 6], t: [0, 6, 1, 8], big: [0, 14, 1, 12] })
})

test('a hidden tile takes no place, and the band it held alone closes', () => {
  const tiles = [box('a', 0, 0, 12, 3), box('gone', 0, 3, 12, 4), box('b', 0, 7, 12, 3)]
  assert.deepEqual(at(placeTiles(tiles, 'wide', new Set(['gone']))), { a: [0, 0, 12, 3], b: [0, 3, 12, 3] })
  assert.deepEqual(at(placeTiles(tiles, 'medium', new Set(['gone']))), { a: [0, 0, 6, 3], b: [0, 3, 6, 3] })
  assert.deepEqual(at(placeTiles(tiles, 'narrow', new Set(['gone']))), { a: [0, 0, 1, 3], b: [0, 3, 1, 3] })
})

test('a hidden tile beside another leaves the other where it was on the wide grid', () => {
  const tiles = [box('gone', 0, 0, 6, 3), box('a', 6, 0, 6, 3), box('b', 0, 3, 12, 3)]
  assert.deepEqual(at(placeTiles(tiles, 'wide', new Set(['gone']))), { a: [6, 0, 6, 3], b: [0, 3, 12, 3] })
})

// ── the numbers on screen: which answer counts, what stays after a failure ──
const dashboardTiles = await import('../composables/dashboardTiles.ts')
const {
  beginAsking, tookAnswer, tookFailure, oldestRead, askSourceAgain, refreshPause, hasNumbers, emptySlots, pagesFor, tileStates,
} = dashboardTiles

const kpiAnswer = (value, at = '2026-10-31T10:00:00+00:00') => ({ type: 'kpi', title: 'K', value, text: String(value), change: null, spark: null, computed_at: at, cached: false })
const ask = (slots, ids, seq, changed = false) => beginAsking(slots, ids, seq, changed)

test('a tile asked for the first time has nothing on screen and waits', () => {
  const slots = ask(emptySlots(['a', 'b']), ['a', 'b'], 1)
  assert.deepEqual(tileStates(slots.a), { loading: true, numbers: false, dim: false, trouble: null })
})

test('an answer goes on screen, and the tile stops waiting', () => {
  let slots = ask(emptySlots(['a']), ['a'], 1)
  slots = tookAnswer(slots, 1, { a: kpiAnswer(5) }, { keep: false })
  assert.equal(slots.a.answer.value, 5)
  assert.deepEqual(tileStates(slots.a), { loading: false, numbers: true, dim: false, trouble: null })
})

test('an old answer arriving after a newer one changes nothing', () => {
  let slots = ask(emptySlots(['a']), ['a'], 1)
  slots = ask(slots, ['a'], 2, true)
  slots = tookAnswer(slots, 2, { a: kpiAnswer(200) }, { keep: false })
  const after = tookAnswer(slots, 1, { a: kpiAnswer(100) }, { keep: false })
  assert.equal(after.a.answer.value, 200)
})

test('an old answer while a newer one is on its way changes nothing either, and the tile goes on waiting', () => {
  let slots = ask(emptySlots(['a']), ['a'], 1)
  slots = ask(slots, ['a'], 2, true)
  slots = tookAnswer(slots, 1, { a: kpiAnswer(100) }, { keep: false })
  assert.equal(slots.a.answer, null)
  assert.equal(tileStates(slots.a).loading, true)
})

test('a page asked for one table leaves the other tiles of a slower whole answer alone', () => {
  let slots = ask(emptySlots(['t', 'k']), ['t', 'k'], 1)
  slots = ask(slots, ['t'], 2, true)
  slots = tookAnswer(slots, 2, { t: { type: 'table', page: 1, rows: [] } }, { keep: false })
  slots = tookAnswer(slots, 1, { t: { type: 'table', page: 0, rows: [] }, k: kpiAnswer(7) }, { keep: false })
  assert.equal(slots.t.answer.page, 1)
  assert.equal(slots.k.answer.value, 7)
})

test('a tile untouched by an answer is the same object, so it is not drawn again', () => {
  let slots = ask(emptySlots(['t', 'k']), ['t', 'k'], 1)
  slots = tookAnswer(slots, 1, { t: { type: 'table', page: 0, rows: [] }, k: kpiAnswer(7) }, { keep: false })
  const kept = slots.k
  slots = ask(slots, ['t'], 2, true)
  slots = tookAnswer(slots, 2, { t: { type: 'table', page: 1, rows: [] } }, { keep: false })
  assert.equal(slots.k, kept)
})

test('while a new question is on its way the old numbers are dimmed, they are not the answer to it', () => {
  let slots = ask(emptySlots(['a']), ['a'], 1)
  slots = tookAnswer(slots, 1, { a: kpiAnswer(5) }, { keep: false })
  slots = ask(slots, ['a'], 2, true)
  assert.deepEqual(tileStates(slots.a), { loading: true, numbers: true, dim: true, trouble: null })
})

test('while the same question is asked again the numbers stay as they are, with their time', () => {
  let slots = ask(emptySlots(['a']), ['a'], 1)
  slots = tookAnswer(slots, 1, { a: kpiAnswer(5) }, { keep: false })
  slots = ask(slots, ['a'], 2, false)
  assert.deepEqual(tileStates(slots.a), { loading: true, numbers: true, dim: false, trouble: null })
})

test('a refresh that fails keeps the last numbers and says so', () => {
  let slots = ask(emptySlots(['a', 'b']), ['a'], 1)
  slots = tookAnswer(slots, 1, { a: kpiAnswer(5) }, { keep: false })
  slots = ask(slots, ['a', 'b'], 2)
  slots = tookFailure(slots, 2, 'The engine did not answer', { keep: true })
  assert.equal(slots.a.answer.value, 5)
  assert.deepEqual(tileStates(slots.a), { loading: false, numbers: true, dim: false, trouble: 'The engine did not answer' })
  assert.deepEqual(slots.b.answer, { error: 'The engine did not answer', code: 'request' }) // nothing to keep
})

test('the same numbers read again keep the object a chart is drawn from, with the new time', () => {
  let slots = ask(emptySlots(['a']), ['a'], 1)
  slots = tookAnswer(slots, 1, { a: kpiAnswer(5, '2026-10-31T10:00:00+00:00') }, { keep: false })
  const drawn = slots.a.answer
  slots = tookAnswer(ask(slots, ['a'], 2), 2, { a: { ...kpiAnswer(5, '2026-10-31T10:05:00+00:00'), cached: true } }, { keep: true })
  assert.equal(slots.a.answer, drawn)
  assert.equal(oldestRead(slots), '2026-10-31T10:05:00+00:00')
  slots = tookAnswer(ask(slots, ['a'], 3), 3, { a: kpiAnswer(6, '2026-10-31T10:06:00+00:00') }, { keep: true })
  assert.notEqual(slots.a.answer, drawn)
  assert.equal(slots.a.answer.value, 6)
})

test('a later answer takes the trouble away', () => {
  let slots = ask(emptySlots(['a']), ['a'], 1)
  slots = tookAnswer(slots, 1, { a: kpiAnswer(5) }, { keep: false })
  slots = ask(slots, ['a'], 2)
  slots = tookFailure(slots, 2, 'no', { keep: true })
  slots = ask(slots, ['a'], 3)
  slots = tookAnswer(slots, 3, { a: kpiAnswer(6) }, { keep: true })
  assert.deepEqual(tileStates(slots.a), { loading: false, numbers: true, dim: false, trouble: null })
  assert.equal(slots.a.answer.value, 6)
})

test('a failure of a new question does not leave the old numbers looking like its answer', () => {
  let slots = ask(emptySlots(['a']), ['a'], 1)
  slots = tookAnswer(slots, 1, { a: kpiAnswer(5) }, { keep: false })
  slots = ask(slots, ['a'], 2, true)
  slots = tookFailure(slots, 2, 'no', { keep: false })
  assert.deepEqual(slots.a.answer, { error: 'no', code: 'request' })
})

test('a failure of an old request changes nothing', () => {
  let slots = ask(emptySlots(['a']), ['a'], 1)
  slots = ask(slots, ['a'], 2)
  slots = tookAnswer(slots, 2, { a: kpiAnswer(5) }, { keep: false })
  assert.equal(tookFailure(slots, 1, 'no', { keep: false }).a.answer.value, 5)
})

test('a tile that breaks on a refresh keeps its numbers, with the reason', () => {
  let slots = ask(emptySlots(['a']), ['a'], 1)
  slots = tookAnswer(slots, 1, { a: kpiAnswer(5) }, { keep: false })
  for (const code of ['error', 'dataset']) {
    const next = tookAnswer(ask(slots, ['a'], 2), 2, { a: { error: 'A read failed', code } }, { keep: true })
    assert.equal(next.a.answer.value, 5, code)
    assert.equal(tileStates(next.a).trouble, 'A read failed', code)
  }
})

test('rights lost on a refresh take the numbers away at once, and so does a dataset past the cap', () => {
  let slots = ask(emptySlots(['a']), ['a'], 1)
  slots = tookAnswer(slots, 1, { a: kpiAnswer(5) }, { keep: false })
  for (const code of ['no_access', 'truncated']) {
    const next = tookAnswer(ask(slots, ['a'], 2), 2, { a: { error: 'No', code } }, { keep: true })
    assert.deepEqual(next.a.answer, { error: 'No', code }, code)
    assert.equal(hasNumbers(next.a), false, code)
  }
})

test('the time on screen is that of the oldest numbers shown, not of the last answer', () => {
  const slots = {
    a: { answer: kpiAnswer(1, '2026-10-31T10:05:00+00:00') },
    b: { answer: kpiAnswer(2, '2026-10-31T10:00:00+00:00') },
    c: { answer: { error: 'x', code: 'error' } },
    d: { answer: null },
  }
  assert.equal(oldestRead(slots), '2026-10-31T10:00:00+00:00')
  assert.equal(oldestRead({ c: slots.c }), null)
})

test('an automatic refresh asks the source only when the shared answer brings nothing newer', () => {
  const on = (at) => ({ a: { answer: kpiAnswer(1, at) }, e: { answer: { error: 'x', code: 'error' } } })
  // someone else read it meanwhile: it is new, the source is left alone
  assert.equal(askSourceAgain(on('2026-10-31T10:00:00Z'), on('2026-10-31T10:01:00Z')), false)
  // the numbers are those of a minute ago: time to read them
  assert.equal(askSourceAgain(on('2026-10-31T10:00:00Z'), on('2026-10-31T10:00:00Z')), true)
  // nothing to compare: nothing read
  assert.equal(askSourceAgain({ e: on('x').e }, { e: on('x').e }), false)
})

test('the pause between two refreshes is the interval and up to a tenth more, never less', () => {
  assert.equal(refreshPause(300, () => 0), 300_000)
  assert.equal(refreshPause(300, () => 0.999999) <= 330_000, true)
  assert.equal(refreshPause(60, () => 0.5), 63_000)
})

test('a request names the pages of the tiles it asks for, and no other', () => {
  assert.deepEqual(pagesFor({ t: 2, u: 0, v: 3 }, ['t', 'k']), { t: 2 })
  assert.deepEqual(pagesFor({ t: 2 }, null), { t: 2 })
})

test('a page past the last comes back as the last one, and that is the page from then on', () => {
  const { pagesAfter } = dashboardTiles
  const slots = { t: { asked: 3 } }
  assert.deepEqual(pagesAfter({ t: 9 }, slots, 3, { t: { type: 'table', page: 2 } }), { t: 2 })
})

test('a late answer does not undo the page asked for after it', () => {
  const { pagesAfter } = dashboardTiles
  const slots = { t: { asked: 5 } }
  const pages = { t: 1 }
  assert.equal(pagesAfter(pages, slots, 4, { t: { type: 'table', page: 0 } }), pages)
  assert.equal(pagesAfter(pages, slots, 5, { t: { error: 'x', code: 'error' } }), pages)
})

// ── the parameters in the address ───────────────────────────────────────────
const { dashboardQuery, formOfQuery, valuesInQuery } = await import('../composables/useDashboards.ts')

const P = {
  region: { name: 'region', type: 'text', required: false, multi: false, default: null, choices: [{ value: 'Nord' }, { value: 'Sud' }] },
  from: { name: 'from', type: 'date', required: true, multi: false, default: '2026-01-01' },
  tags: { name: 'tags', type: 'text', required: false, multi: true, default: null, choices: [{ value: 'a' }, { value: 'b' }] },
  free: { name: 'free', type: 'text', required: false, multi: true, default: ['x'] },
  id: { name: 'id', type: 'text', required: false, multi: false, default: null },
}
const ALL = Object.values(P)

test('a value in the address is read, a list repeats its name, and what the form does not know stays out', () => {
  const given = valuesInQuery({ id: '7', region: 'Nord', tags: ['a', 'b'], 'click.by_area': 'x', _id: 'abc' })
  assert.deepEqual(given, { region: ['Nord'], tags: ['a', 'b'], 'click.by_area': ['x'], id: ['abc'] })
  const form = formOfQuery(ALL, given)
  assert.deepEqual(form, { region: 'Nord', from: '2026-01-01', tags: ['a', 'b'], free: 'x', id: 'abc' })
})

test('{none} in the address is no value, a default or not', () => {
  assert.deepEqual(formOfQuery([P.free, P.tags, P.region], { free: ['{none}'], tags: ['{none}'], region: ['{none}'] }),
    { free: '', tags: [], region: '' })
})

test('only the values that differ from the defaults go in the address', () => {
  assert.deepEqual(dashboardQuery(7, ALL, { region: '', from: '2026-01-01', tags: [], free: 'x', id: '' }), { id: '7' })
  assert.deepEqual(dashboardQuery(7, ALL, { region: 'Sud', from: '2026-02-02', tags: ['a', 'b'], free: 'y, z', id: 'k' }),
    { id: '7', region: 'Sud', from: '2026-02-02', tags: ['a', 'b'], free: ['y', 'z'], _id: 'k' })
})

test('an optional parameter emptied of its default says so in the address', () => {
  assert.deepEqual(dashboardQuery(7, [P.free], { free: '' }), { id: '7', free: '{none}' })
})

test('the address carries a form there and back', () => {
  const form = { region: 'Sud', from: '2026-02-02', tags: ['b'], free: '', id: '' }
  const query = dashboardQuery(7, ALL, form)
  assert.deepEqual(formOfQuery(ALL, valuesInQuery(query)), form)
})

// ── how a table's cell and a tile look: a report's style, on any theme ──────
const { cellStyleOf, tileStyleOf } = await import('../composables/dashboardStyle.ts')
const DARK = '#141926'; const LIGHT = '#ffffff'

test('a cell takes the style a report writes', () => {
  assert.deepEqual(cellStyleOf({ color: '#ff8c00', weight: 700, italic: true, size: 9, align: 'right' }, DARK),
    { color: '#ff8c00', fontWeight: 700, fontStyle: 'italic', fontSize: '9pt', textAlign: 'right' })
})

test('a black or a white that cannot be read on this theme is left to the theme', () => {
  assert.equal(cellStyleOf({ color: '#000000' }, DARK).color, undefined)
  assert.equal(cellStyleOf({ color: '#ffffff' }, LIGHT).color, undefined)
  assert.equal(cellStyleOf({ color: '#000000' }, LIGHT).color, '#000000')
})

test('a rule\'s red too dark for this theme is lifted until it reads, and is still red', () => {
  const DRACULA = '#282a36'
  const lifted = cellStyleOf({ color: '#c92a2a' }, DRACULA).color
  assert.notEqual(lifted, '#c92a2a')
  const [r, g, b] = [1, 3, 5].map((at) => parseInt(lifted.slice(at, at + 2), 16))
  assert.ok(r > g + 40 && r > b + 40, lifted)
  assert.equal(cellStyleOf({ color: '#c92a2a' }, LIGHT).color, '#c92a2a') // where it reads, as written
})

test('a fill without a colour gets the ink that reads on it, on any theme', () => {
  assert.deepEqual(cellStyleOf({ fill: '#ffe066' }, DARK), { background: '#ffe066', color: '#0b0e14' })
  assert.deepEqual(cellStyleOf({ fill: '#1b2130' }, LIGHT), { background: '#1b2130', color: '#ffffff' })
})

test('a fill with its colour stays as it was chosen', () => {
  assert.deepEqual(cellStyleOf({ fill: '#ffe066', color: '#222222' }, DARK), { background: '#ffe066', color: '#222222' })
})

test('what is not a colour never reaches the page', () => {
  assert.deepEqual(cellStyleOf({ color: 'red;background:url(x)', fill: 'url(x)' }, DARK), {})
})

test('a text or a KPI tile takes the style of its definition', () => {
  assert.deepEqual(tileStyleOf({ color: '#ff8c00', background: '#101010', size: 14, weight: 'bold', italic: true, align: 'center', valign: 'middle' }, DARK), {
    color: '#ff8c00', background: '#101010', fontSize: '14pt', fontWeight: 700, fontStyle: 'italic', textAlign: 'center', justifyContent: 'center',
  })
  assert.deepEqual(tileStyleOf(undefined, DARK), {})
})

// ── a chart tile: every kind, from the server's numbers, in the theme's colours ──
const { dashboardCtx, tileChartOption, greySeries } = await import('../composables/dashboardCharts.ts')
const { PALETTE } = await import('../composables/chartKinds.ts')

const UI = { text: '#111111', muted: '#222222', border: '#333333', borderSoft: '#444444', panel: '#555555', panel2: '#666666', success: '#777777', danger: '#888888', accent: '#999999', violet: '#aaaaaa', isLight: false }
const K = dashboardCtx(UI, 'it', (key, params) => (params ? `${key} ${JSON.stringify(params)}` : key))
const series = (name, values, extra = {}) => ({ name, values, texts: values.map((v) => (v == null ? null : `€ ${v}`)), as: 'bar', axis: 'left', format: '#,##0', ...extra })
const two = { cats: ['Nord', 'Sud'], series: [series('Ricavo', [10, -2])] }
const chartOf = (kind, data, options = {}) => ({ type: 'chart', kind, format: '#,##0', empty: false, options: { stacked: false, labels: false, legend: null, min: null, max: null, ...options }, data, title: 'T', computed_at: '', cached: false })
const EVERY_KIND = {
  column: two, bar: two, line: two, area: two,
  combo: { cats: ['a', 'b'], series: [series('B', [1, 2]), series('L', [3, 4], { as: 'line' })], bars: [1, 2], line: [3, 4], barName: 'B', lineName: 'L' },
  pie: { ...two, name: 'Ricavo', slices: [{ name: 'Sud', value: 2, text: '€ 2' }, { name: 'Nord', value: 10, text: '€ 10' }], left_out: 1 },
  donut: { ...two, name: 'Ricavo', slices: [{ name: 'Nord', value: 10, text: '€ 10' }], left_out: 0 },
  treemap: { ...two, values: [10, 3], texts: ['€ 10', '€ 3'], name: 'Ricavo' },
  funnel: { ...two, values: [10, 3], texts: ['€ 10', '€ 3'], name: 'Ricavo' },
  waterfall: { ...two, values: [10, -2], texts: ['€ 10', '-€ 2'], name: 'Ricavo', total: 8, total_text: '€ 8', total_label: 'Totale' },
  heatmap: { cats: ['x'], series: [series('y', [5])], xCats: ['x'], yCats: ['y'], cells: [[0, 0, 5]], texts: ['€ 5'], name: 'Ricavo' },
  bullet: { ...two, values: [10, 2], texts: ['€ 10', '€ 2'], targets: [12, null], name: 'Ricavo', targetName: '', max: null, bands: [5, 20] },
  gauge: { value: 40, text: '€ 40', name: 'Ricavo', min: 0, max: 100, target: 60, bands: [] },
  scatter: { series: [{ name: 'S', points: [[1, 2, 'uno'], [3, 4, 'due']] }] },
}

function colours(node, seen = new Set()) {
  if (typeof node === 'string') for (const m of node.matchAll(/#[0-9a-f]{6}\b/gi)) seen.add(m[0].toLowerCase())
  else if (Array.isArray(node)) node.forEach((x) => colours(x, seen))
  else if (node && typeof node === 'object') Object.values(node).forEach((x) => colours(x, seen))
  return seen
}

for (const [kind, data] of Object.entries(EVERY_KIND)) {
  test(`a ${kind} tile does not animate and uses only the theme's colours and the palette`, () => {
    const option = tileChartOption(K, chartOf(kind, data))
    assert.equal(option.animation, false)
    assert.ok(option.series.length >= 1)
    const allowed = new Set([...Object.values(UI).filter((v) => typeof v === 'string'), ...PALETTE.map((p) => p.toLowerCase())])
    for (const c of colours(option)) assert.ok(allowed.has(c), `${kind} uses ${c}`)
  })
}

test('bars lie along the value and a column stands on it', () => {
  const lying = tileChartOption(K, chartOf('bar', two))
  assert.equal(lying.yAxis.type, 'category')
  assert.equal(lying.xAxis.type, 'value')
  const standing = tileChartOption(K, chartOf('column', two))
  assert.equal(standing.xAxis.type, 'category')
  assert.deepEqual(standing.xAxis.data, ['Nord', 'Sud'])
})

test('on a tile a third of the screen wide every bar keeps a name, cut to its room', () => {
  const five = { cats: ['Prosciutti', 'Salumi stagionati', 'Salami', 'Insaccati cotti', 'Insaccati da cuocere'], series: [series('R', [5, 4, 5, 3, 1])] }
  const label = (kind, width, data = five) => tileChartOption(K, chartOf(kind, data), { width }).xAxis.axisLabel
  assert.equal(label('column', 430).width, 68)
  assert.equal(label('column', 430).overflow, 'truncate')
  // wide enough for the widest name: nothing to cut
  assert.equal(label('column', 1400).width, 120)
  // too many for a readable piece of each: ECharts still chooses which to show, whole
  const months = { cats: Array.from({ length: 36 }, (_, i) => `2023${i}`), series: [series('R', Array(36).fill(1))] }
  assert.equal(label('line', 430, months).width, 120)
  // lying down, the names have the side
  assert.equal(tileChartOption(K, chartOf('bar', five), { width: 430 }).yAxis.axisLabel.width, 180)
})

test('a point carries the text the server wrote, and a tooltip says that one', () => {
  const option = tileChartOption(K, chartOf('column', two))
  assert.deepEqual(option.series[0].data[0], { value: 10, t: '€ 10' })
  const said = option.tooltip.formatter([{ name: 'Nord', seriesName: 'Ricavo', data: { t: '€ 10' }, marker: '' }])
  assert.equal(said, 'Nord<br/>Ricavo: <b>€ 10</b>')
})

test('a category that holds markup stays text in a tooltip', () => {
  const option = tileChartOption(K, chartOf('column', { cats: ['<img src=x onerror=1>'], series: [series('<b>', [1])] }))
  const said = option.tooltip.formatter([{ name: '<img src=x onerror=1>', seriesName: '<b>', data: { t: '<i>' }, marker: '' }])
  assert.ok(!said.includes('<img') && !said.includes('<i>') && !said.includes('<b><'), said)
})

test('a value with no number leaves a hole, not a zero', () => {
  const option = tileChartOption(K, chartOf('line', { cats: ['a', 'b'], series: [series('S', [null, 1])] }))
  assert.equal(option.series[0].data[0], null)
})

test('stacked, the labels, the scale and a right axis come from the definition', () => {
  const data = { cats: ['a'], series: [series('L', [1]), series('R', [0.5], { axis: 'right', as: 'line', format: '0%' }), series('L2', [1])] }
  const option = tileChartOption(K, chartOf('combo', data, { stacked: true, labels: true, min: 0, max: 9 }))
  assert.equal(option.series[0].stack, 'total')
  assert.equal(option.series[0].label.show, true)
  assert.equal(option.series[1].type, 'line')
  assert.equal(option.series[1].yAxisIndex, 1)
  assert.equal(option.yAxis[0].max, 9)
  assert.equal(option.yAxis[1].axisLabel.formatter(0.5), K.pct(0.5)) // that axis' format
})

test('a legend shows with two series, or as the definition says', () => {
  const many = { cats: ['a'], series: [series('A', [1]), series('B', [2])] }
  assert.ok(tileChartOption(K, chartOf('column', many)).legend)
  assert.equal(tileChartOption(K, chartOf('column', two)).legend, undefined)
  assert.ok(tileChartOption(K, chartOf('column', two, { legend: true })).legend)
  assert.equal(tileChartOption(K, chartOf('column', many, { legend: false })).legend, undefined)
})

test('the hues are never cycled: past the fifth a series is a neutral, and the tile can say how many', () => {
  const data = { cats: ['a'], series: Array.from({ length: 7 }, (_, i) => series(`S${i}`, [i])) }
  const chart = chartOf('column', data)
  const fills = tileChartOption(K, chart).series.map((s) => s.itemStyle.color)
  assert.deepEqual(fills.slice(0, 5), PALETTE)
  assert.ok(fills.slice(5).every((f) => f.startsWith('rgba(34, 34, 34')), fills.join())
  assert.equal(greySeries(chart), 2)
  assert.equal(greySeries(chartOf('column', two)), 0)
})

test('a pie on a tile too narrow for names gives them to a legend, and nothing is cut', () => {
  const option = tileChartOption(K, chartOf('pie', EVERY_KIND.pie))
  const narrow = option.media.find((m) => m.query.maxWidth)
  assert.equal(narrow.option.series[0].label.show, false)
  assert.equal(narrow.option.legend.show, true)
})

test('a pie goes from the largest slice, each with the server\'s text', () => {
  const option = tileChartOption(K, chartOf('pie', EVERY_KIND.pie))
  assert.deepEqual(option.series[0].data.map((d) => [d.name, d.t]), [['Nord', '€ 10'], ['Sud', '€ 2']])
})

test('a gauge writes the server\'s figure, and draws its bands with a pointer', () => {
  const plain = tileChartOption(K, chartOf('gauge', EVERY_KIND.gauge))
  assert.equal(plain.series[0].detail.formatter(), '€ 40')
  assert.equal(plain.series[0].pointer.show, false)
  const banded = tileChartOption(K, chartOf('gauge', { ...EVERY_KIND.gauge, bands: [30, 70, 100] }))
  assert.equal(banded.series[0].pointer.show, true)
  assert.deepEqual(banded.series[0].axisLine.lineStyle.color.map(([at]) => at), [0.3, 0.7, 1])
  assert.equal(banded.series[0].axisLine.lineStyle.color[0][1], UI.danger)
})

test('a waterfall ends in the total the server names', () => {
  const option = tileChartOption(K, chartOf('waterfall', EVERY_KIND.waterfall))
  assert.deepEqual(option.xAxis.data, ['Nord', 'Sud', 'Totale'])
  assert.equal(option.series[1].data[2].full, '€ 8')
})

test('a bullet without a name for its target takes the interface\'s word, and its bands stack behind the bars', () => {
  const option = tileChartOption(K, chartOf('bullet', EVERY_KIND.bullet))
  assert.ok(option.legend.data.some((l) => l.name === 'chartPanel.target'))
  const bands = option.series.filter((s) => s.stack === 'bands')
  assert.deepEqual(bands.map((b) => b.data[0]), [5, 15])
})

test('a treemap split nests its parts inside each category', () => {
  const data = { cats: ['Nord', 'Sud'], series: [series('A', [1, 2]), series('B', [3, null])], values: [1, 2], texts: ['€ 1', '€ 2'], name: 'R' }
  const option = tileChartOption(K, chartOf('treemap', data))
  assert.deepEqual(option.series[0].data.map((n) => [n.name, n.children.map((c) => c.name)]), [['Nord', ['A', 'B']], ['Sud', ['A']]])
})

test('the ticks of an axis are in the dashboard\'s language, whatever the interface\'s', () => {
  const de = dashboardCtx(UI, 'de', (x) => x)
  const en = dashboardCtx(UI, 'en', (x) => x)
  assert.equal(en.num(1234.5), '1,234.5')
  assert.equal(de.num(1234.5), '1.234,5')
})

// ── a table's view, as the viewer of reports sends it ───────────────────────
const { stateAfter } = await import('../composables/useReports.ts')

test('a click on a header sorts up, then down, then back to the table\'s own order', () => {
  const hot = { kind: 'sort', item: 't', column: 1 }
  let state = stateAfter({ items: {} }, hot)
  assert.deepEqual(state.items.t.sort, { column: 1, dir: 'asc' })
  state = stateAfter(state, hot)
  assert.deepEqual(state.items.t.sort, { column: 1, dir: 'desc' })
  assert.deepEqual(stateAfter(state, hot), { items: {} })
})

test('a click on a group closes it, and again opens it', () => {
  const closing = { kind: 'toggle', item: 't', key: ['North'], closed: false }
  const state = stateAfter({ items: {} }, closing)
  assert.deepEqual(state.items.t.collapsed, [['North']])
  assert.deepEqual(stateAfter(state, { ...closing, closed: true }), { items: {} })
})

test('a gauge of a share writes its scale and its target as shares', () => {
  const option = tileChartOption(K, chartOf('gauge', { ...EVERY_KIND.gauge, value: 0.26, text: '26.4%', min: 0, max: 1, target: 0.5 }))
  assert.equal(option.series[0].axisLabel.formatter(1), K.pct(1))
  assert.ok(option.series[1].detail.formatter().includes(K.pct(0.5)), option.series[1].detail.formatter())
})

test('a combo\'s two axes write their numbers in the format of their series', () => {
  const data = { ...EVERY_KIND.combo, series: [series('B', [1, 2]), series('L', [0.3, 0.4], { as: 'line', axis: 'right', format: '0.0%' })] }
  const option = tileChartOption(K, chartOf('combo', data))
  assert.equal(option.yAxis[1].axisLabel.formatter(0.5), K.pct(0.5))
  assert.equal(option.yAxis[0].axisLabel.formatter(1500), K.short(1500))
})

test('a combo\'s line on the same axis as its bars goes by the series, as a report draws it', () => {
  const option = tileChartOption(K, chartOf('combo', EVERY_KIND.combo))
  assert.equal(Array.isArray(option.yAxis), false)
  assert.deepEqual(option.series.map((s) => s.type), ['bar', 'line'])
})

test('a tile that reads no data says nothing of the time of the numbers', () => {
  const { readingData } = dashboardTiles
  const slots = { logo: { answer: kpiAnswer(1, '2026-10-31T09:00:00+00:00') }, k: { answer: kpiAnswer(2, '2026-10-31T10:00:00+00:00') } }
  const reading = readingData(slots, [{ id: 'logo', type: 'image' }, { id: 'k', type: 'kpi', dataset: 'sales' }])
  assert.deepEqual(Object.keys(reading), ['k'])
  assert.equal(oldestRead(reading), '2026-10-31T10:00:00+00:00')
})

// ── a click that filters the others ─────────────────────────────────────────
const {
  MAX_CLICK_VALUES, askedAfterClicks, canFilter, clickAfter, clickLabel, clickSends, clickedValue, clicksInQuery, clicksOfTile, clicksQuery, hasFilters, namingTheDataset, readClick, sameClicks,
  settledClicks, tilesItChanges, valueText, writeClick,
} = await import('../composables/dashboardClicks.ts')

const barsTile = (id, more = {}) => ({ id, type: 'chart', kind: 'column', dataset: 'sales', category: '[area]', ...more })
const groupsTile = (id = 'g', more = {}) => ({ id, type: 'table', dataset: 'sales', groups: [{ by: '[area]' }], ...more })
const matrixTile = (id = 'm') => ({ id, type: 'table', dataset: 'sales', groups: [{ by: '[area]' }], matrix: { columns_by: '[year]', value: 'SUM([x])' } })

test('what can filter: a chart with a category, a table with groups, a matrix on both sides', () => {
  assert.ok(canFilter(barsTile('a')))
  assert.ok(canFilter(groupsTile()))
  assert.ok(canFilter(matrixTile(), 'columns'))
  assert.ok(!canFilter(groupsTile(), 'columns'))
  assert.ok(!canFilter(barsTile('a', { filters_others: false })))
  assert.ok(!canFilter(barsTile('a', { kind: 'gauge' })))
  assert.ok(!canFilter(barsTile('a', { category: null })))
  assert.ok(!canFilter({ id: 'k', type: 'kpi', dataset: 'sales' }))
  assert.ok(!canFilter({ id: 't', type: 'text' }))
  assert.ok(!canFilter({ id: 't', type: 'table', dataset: 'sales' }))
  assert.ok(hasFilters([{ id: 'k', type: 'kpi' }, barsTile('a')]))
  assert.ok(!hasFilters([{ id: 'k', type: 'kpi' }]))
})

test('in an address a value is its text, an empty group {none}, and whatever could be read as something else is escaped', () => {
  assert.equal(writeClick('Nord-Est'), 'Nord-Est')
  assert.equal(writeClick(202307), '202307')
  assert.equal(writeClick(true), 'true')
  assert.equal(writeClick(null), '{none}')
  assert.equal(writeClick('{none}'), '\\{none}')
  assert.equal(writeClick(['a', null, 3]), '["a",null,3]')
  for (const v of ['Nord-Est', "it's", 'say "hi"', '{none}', '{none', '[amount] > 0', '["x"]', '\\', '\\\\x', '', ' ', '北☃', '=SUM(1)', 'null', null, ['a', 'b'], [null, 1], ['[x]', '{none}']]) {
    assert.deepEqual(readClick(writeClick(v)), v, `${JSON.stringify(v)} comes back`)
  }
  for (const v of [202307, 1.5, true, false]) assert.equal(valueText(readClick(writeClick(v))), valueText(v), 'a number comes back as its text: the server finds the number')
  assert.equal(readClick('[not json'), '[not json', 'a text a person typed that is no list stays a text')
  assert.equal(readClick('[{"a":1}]'), '[{"a":1}]', 'what is no list of plain values is a text, not a value to send')
})

test('the address holds the clicks, one key per tile and side, and reads back the same', () => {
  const tiles = [barsTile('by_area'), matrixTile('m'), groupsTile('g')]
  const clicks = [
    { tile: 'by_area', on: 'rows', values: ['Nord-Est', null] },
    { tile: 'm', on: 'columns', values: [[2023]] },
    { tile: 'm', on: 'rows', values: [['a']] },
  ]
  const q = clicksQuery(clicks)
  assert.deepEqual(q, { 'click.by_area': ['Nord-Est', '{none}'], 'click.m.columns': '[2023]', 'click.m': '["a"]' })
  const read = clicksInQuery({ id: '7', region: 'Nord', ...q }, tiles)
  assert.ok(sameClicks(read, clicks))
  assert.deepEqual(clicksInQuery({ 'click.by_area': 'Nord-Est', region: 'x' }, tiles), [{ tile: 'by_area', on: 'rows', values: ['Nord-Est'] }])
})

test('an old link: a tile gone, or switched off, or a side it has not, or too many values, opens without them', () => {
  const tiles = [barsTile('here'), barsTile('off', { filters_others: false }), { id: 'k', type: 'kpi' }, groupsTile('g')]
  assert.deepEqual(clicksInQuery({ 'click.gone': 'x', 'click.off': 'x', 'click.k': 'x', 'click.g.columns': 'x', 'click.here': 'y' }, tiles),
    [{ tile: 'here', on: 'rows', values: ['y'] }])
  const many = Array.from({ length: MAX_CLICK_VALUES + 50 }, (_, i) => String(i))
  assert.equal(clicksInQuery({ 'click.here': many }, tiles)[0].values.length, MAX_CLICK_VALUES)
  assert.deepEqual(clicksInQuery({ 'click.here': null }, tiles), [], 'a key with nothing in it is nothing')
  assert.deepEqual(clicksInQuery({ 'click.': 'x', 'clicks': 'y' }, tiles), [])
})

test('a click chooses, a second click on the same value clears, another value takes its place', () => {
  const one = clickAfter([], 'a', 'rows', 'x', false)
  assert.deepEqual(one, [{ tile: 'a', on: 'rows', values: ['x'] }])
  assert.deepEqual(clickAfter(one, 'a', 'rows', 'y', false), [{ tile: 'a', on: 'rows', values: ['y'] }])
  assert.deepEqual(clickAfter(one, 'a', 'rows', 'x', false), [])
  const others = clickAfter(one, 'b', 'rows', 'k', false)
  assert.equal(others.length, 2, 'a click on another tile leaves this one alone')
})

test('Ctrl adds a value, on it again takes it away, and the last one away leaves no click', () => {
  let c = clickAfter([], 'a', 'rows', 'x', true)
  c = clickAfter(c, 'a', 'rows', null, true)
  assert.deepEqual(c[0].values, ['x', null])
  c = clickAfter(c, 'a', 'rows', 'x', true)
  assert.deepEqual(c[0].values, [null])
  assert.deepEqual(clickAfter(c, 'a', 'rows', null, true), [])
  const full = [{ tile: 'a', on: 'rows', values: Array.from({ length: MAX_CLICK_VALUES }, (_, i) => i) }]
  assert.equal(clickAfter(full, 'a', 'rows', 'one more', true)[0].values.length, MAX_CLICK_VALUES, 'past the most it adds nothing')
  assert.deepEqual(clickAfter(full, 'a', 'rows', 0, true)[0].values.length, MAX_CLICK_VALUES - 1)
})

test('a number from the address and the same number from a chart are one value', () => {
  assert.equal(valueText('202307'), valueText(202307))
  assert.equal(valueText('true'), valueText(true))
  assert.notEqual(valueText(null), valueText('{none}'))
  assert.notEqual(valueText(null), valueText(''))
  assert.notEqual(valueText(null), valueText('null'))
  assert.equal(valueText(['a']), valueText('a'), 'a path of one is its value')
  assert.notEqual(valueText(['a', 'b']), valueText('a,b'))
  const c = clickAfter([{ tile: 'a', on: 'rows', values: ['202307'] }], 'a', 'rows', 202307, false)
  assert.deepEqual(c, [], 'clicking what the link chose clears it')
  assert.ok(sameClicks([{ tile: 'a', on: 'rows', values: ['1', '2'] }], [{ tile: 'a', on: 'rows', values: [2, 1] }]), 'in any order')
  assert.ok(!sameClicks([{ tile: 'a', on: 'rows', values: ['1'] }], [{ tile: 'a', on: 'columns', values: ['1'] }]))
})

test('what the request sends: the side only when it is not the rows, and never an expression', () => {
  assert.deepEqual(clickSends([{ tile: 'a', on: 'rows', values: ['x'] }, { tile: 'm', on: 'columns', values: [[1]] }]),
    [{ tile: 'a', values: ['x'] }, { tile: 'm', on: 'columns', values: [[1]] }])
})

test('the server says what stayed: a value gone goes, a click with none goes, a tile refused takes its click with it', () => {
  const clicks = [{ tile: 'a', on: 'rows', values: ['202307', 'gone'] }, { tile: 'b', on: 'rows', values: ['x'] }, { tile: 'c', on: 'rows', values: ['y'] }, { tile: 'd', on: 'rows', values: ['z'] }]
  const settled = settledClicks(clicks, {
    a: { type: 'chart', chosen: { rows: [202307] } }, b: { type: 'chart', chosen: { rows: [] } },
    c: { error: 'no', code: 'no_access' }, d: { error: 'try later', code: 'error' },
  })
  assert.deepEqual(settled, [{ tile: 'a', on: 'rows', values: [202307] }, { tile: 'd', on: 'rows', values: ['z'] }])
  assert.equal(settledClicks(clicks.slice(3), { d: { type: 'chart', chosen: { rows: ['z'] } } }), null, 'nothing changes: nothing to say')
  assert.deepEqual(settledClicks(clicks.slice(3), { d: { error: 'needs a parameter', code: 'dataset' } }), [], 'a tile not resolved filtered nothing either: the strip must not say it did')
  assert.equal(settledClicks(clicks, {}), null, 'tiles not in this answer say nothing')
  assert.equal(settledClicks(clicks.slice(1, 2), { b: { hidden: true, type: 'chart' } }), null, 'hidden, it says nothing either')
})

test('a text that names no dataset reads the only one, and a click changes it', () => {
  const tiles = [{ id: 'a', type: 'chart', category: '[x]', dataset: 'sales' }, { id: 'words', type: 'text' }, { id: 'logo', type: 'image' }]
  const named = namingTheDataset(tiles, [{ name: 'sales' }])
  assert.deepEqual(tilesItChanges(named, 'a'), ['a', 'words'])
  assert.deepEqual(askedAfterClicks(named, [], [{ tile: 'a', on: 'rows', values: ['x'] }]).ids, ['a', 'words'])
  // with several, it reads none of them as its own: the server's rule too
  assert.deepEqual(tilesItChanges(namingTheDataset(tiles, [{ name: 'sales' }, { name: 'more' }]), 'a'), ['a'])
})

test('a click changes the tiles on its dataset, and asks the tile clicked only what is chosen', () => {
  const tiles = [barsTile('a'), barsTile('b'), { id: 'k', type: 'kpi', dataset: 'sales' }, barsTile('other', { dataset: 'more' }), { id: 'i', type: 'image' }]
  assert.deepEqual(tilesItChanges(tiles, 'a'), ['a', 'b', 'k'])
  assert.deepEqual(tilesItChanges(tiles, 'other'), ['other'])
  assert.deepEqual(tilesItChanges(tiles, 'nope'), [])
})

test('going from some clicks to others asks the tiles they change, and the tile clicked keeps its look', () => {
  const tiles = [barsTile('a'), barsTile('b'), { id: 'k', type: 'kpi', dataset: 'sales' }, barsTile('other', { dataset: 'more' })]
  const on = (tile, ...values) => ({ tile, on: 'rows', values })
  assert.deepEqual(askedAfterClicks(tiles, [], [on('a', 'x')]), { ids: ['a', 'b', 'k'], still: ['a'] })
  assert.deepEqual(askedAfterClicks(tiles, [on('a', 'x')], [on('a', 'x', 'y')]), { ids: ['a', 'b', 'k'], still: ['a'] })
  assert.deepEqual(askedAfterClicks(tiles, [on('a', 'x')], [on('a', 'x'), on('other', 'z')]), { ids: ['other'], still: ['other'] })
  const cleared = askedAfterClicks(tiles, [on('a', 'x'), on('b', 'y')], [])
  assert.deepEqual(cleared, { ids: ['a', 'b', 'k'], still: [] }, 'each was filtered by the other: all of them change')
  assert.deepEqual(askedAfterClicks(tiles, [on('a', 'x')], [on('a', '1'), on('a', 'x')].slice(1)), { ids: [], still: [] })
})

test('clicksOfTile says what is chosen on a tile, by side', () => {
  const clicks = [{ tile: 'm', on: 'columns', values: [[1]] }, { tile: 'm', on: 'rows', values: [['a']] }, { tile: 'x', on: 'rows', values: ['y'] }]
  assert.deepEqual(clicksOfTile(clicks, 'm', 'columns'), [[1]])
  assert.deepEqual(clicksOfTile(clicks, 'm'), [['a']])
  assert.deepEqual(clicksOfTile(clicks, 'none'), [])
})

const axisAnswer = (kind, more = {}) => ({ type: 'chart', kind, format: null, options: {}, data: {
  cats: ['North', 'South', 'Other'], keys: ['N', null], split_keys: [], series: [{ name: 'A', values: [1, 2, 3], texts: ['1', '2', '3'] }], ...more } })

test('a click on a bar names the raw value of its category, whatever is written on the axis', () => {
  assert.equal(clickedValue(axisAnswer('column'), { dataIndex: 0, seriesIndex: 0, seriesType: 'bar' }), 'N')
  assert.equal(clickedValue(axisAnswer('column'), { dataIndex: 1, seriesIndex: 0, seriesType: 'bar' }), null, 'an empty group is a value')
  assert.equal(clickedValue(axisAnswer('column'), { dataIndex: 2, seriesIndex: 0, seriesType: 'bar' }), undefined, '«Other» names nothing')
  assert.equal(clickedValue(axisAnswer('bar'), { dataIndex: 0, seriesIndex: 0 }), 'N')
})

test('a part of a split bar names both its values', () => {
  const a = axisAnswer('column', { split_keys: [2023, 2024] })
  assert.deepEqual(clickedValue(a, { dataIndex: 1, seriesIndex: 1, seriesType: 'bar' }), [null, 2024])
})

test('every kind names what was clicked', () => {
  const pie = axisAnswer('pie', { slices: [{ name: 'South', value: 2, text: '2', at: 1 }] })
  assert.equal(clickedValue(pie, { data: { at: 1 } }), null)
  assert.equal(clickedValue(axisAnswer('treemap', { values: [1, 2, 3] }), { data: { at: 0 } }), 'N')
  assert.equal(clickedValue(axisAnswer('funnel', { values: [1, 2, 3] }), { data: { at: 0 } }), 'N')
  assert.equal(clickedValue(axisAnswer('waterfall', { values: [1, 2, 3] }), { dataIndex: 0, seriesIndex: 1 }), 'N')
  assert.equal(clickedValue(axisAnswer('waterfall', { values: [1, 2, 3] }), { dataIndex: 3, seriesIndex: 1 }), undefined, 'its total names nothing')
  assert.equal(clickedValue(axisAnswer('bullet', { values: [1, 2, 3] }), { dataIndex: 1, seriesType: 'bar' }), null)
  assert.equal(clickedValue(axisAnswer('bullet', { values: [1, 2, 3] }), { seriesType: 'scatter', value: [50, 0] }), 'N')
  assert.deepEqual(clickedValue(axisAnswer('heatmap', { split_keys: ['y0', 'y1'] }), { value: [1, 1, 9] }), [null, 'y1'])
  assert.equal(clickedValue(axisAnswer('gauge'), { dataIndex: 0 }), undefined)
  assert.equal(clickedValue({ type: 'chart', kind: 'scatter', data: { series: [{ name: 'A', points: [[3, 1, 't']] }], split_keys: [] } }, { value: [3, 1, 't'], seriesIndex: 0 }), 3)
  assert.deepEqual(clickedValue({ type: 'chart', kind: 'scatter', data: { series: [], split_keys: ['p', 'q'] } }, { value: [3, 1, 't'], seriesIndex: 1 }), [3, 'q'])
  const nested = axisAnswer('treemap', { split_keys: ['p', 'q'], series: [{ name: 'p', values: [1] }, { name: 'q', values: [1] }] })
  assert.deepEqual(clickedValue(nested, { data: { at: 1, part: 1 } }), [null, 'q'])
  assert.equal(clickedValue(nested, { data: { at: 0 } }), 'N', 'on its box: the category alone')
})

test('what the strip says of a value: the words of the chart, of the row or of the header, whatever was sent', () => {
  const a = axisAnswer('column', { keys: [202307, null], cats: ['Jul 2023', '(blank)'] })
  assert.equal(clickLabel(a, 'rows', '202307'), 'Jul 2023')
  assert.equal(clickLabel(a, 'rows', null), '(blank)')
  assert.equal(clickLabel(a, 'rows', 'unknown'), undefined)
  const split = axisAnswer('column', { keys: ['N'], cats: ['North'], split_keys: [2023], series: [{ name: 'in 2023' }] })
  assert.equal(clickLabel(split, 'rows', ['N', 2023]), 'North · in 2023')
  const t = { type: 'table', rows: [{ kind: 'g', path: ['n'], label: 'North' }, { kind: 'g', path: ['n', 1], label: 'One' }, { kind: 'd', path: ['s'], cells: [{ text: 'South' }] }, { kind: 'f', path: ['w'], label: 'Total West' }],
                head: [[{ t: 'Region' }, { t: '2023', key: [2023] }, { t: 'Total' }], [{ t: 'Jan', key: [2023, 1] }]] }
  assert.equal(clickLabel(t, 'rows', ['n']), 'North')
  assert.equal(clickLabel(t, 'rows', ['n', 1]), 'North · One')
  assert.equal(clickLabel(t, 'rows', ['s']), 'South')
  assert.equal(clickLabel(t, 'rows', ['w']), 'Total West')
  assert.equal(clickLabel(t, 'columns', ['2023']), '2023')
  assert.equal(clickLabel(t, 'columns', [2023, 1]), '2023 · Jan')
  assert.equal(clickLabel({ error: 'x', code: 'error' }, 'rows', 'n'), undefined)
})

// the tiles' answers, with a click
const { beginAsking: beginning, emptySlots: noSlots, tookAnswer: took, tileStates: statesOf, tookFailure: failed2 } = await import('../composables/dashboardTiles.ts')

test('the tile clicked is asked the same question: it does not dim and an error keeps its numbers, the others do', () => {
  const answer = { type: 'chart', computed_at: '2026-01-01T00:00:00Z', cached: false, data: { x: 1 } }
  let s = took(beginning(noSlots(['a', 'b']), ['a', 'b'], 1, false), 1, { a: answer, b: answer }, { keep: true })
  s = beginning(s, ['a', 'b'], 2, true, ['a'])
  assert.equal(statesOf(s.a).dim, false)
  assert.equal(statesOf(s.b).dim, true)
  const after = took(s, 2, { a: { error: 'late', code: 'error' }, b: { error: 'late', code: 'error' } }, { keep: false, still: ['a'] })
  assert.ok(after.a.answer.data, 'the tile clicked keeps its bars')
  assert.equal(after.a.trouble, 'late')
  assert.equal(after.b.answer.error, 'late')
  const down = failed2(s, 2, 'down', { keep: false, still: ['a'] })
  assert.ok(down.a.answer.data && down.a.trouble === 'down')
  assert.equal(down.b.answer.error, 'down')
})

test('a slow answer to an old click puts nothing over the answer to the new one', () => {
  const said = (n) => ({ type: 'kpi', value: n, computed_at: '2026-01-01T00:00:00Z', cached: false })
  let s = noSlots(['k'])
  s = beginning(s, ['k'], 1, true)
  s = beginning(s, ['k'], 2, true)
  s = took(s, 2, { k: said(2) }, { keep: false })
  s = took(s, 1, { k: said(1) }, { keep: false })
  assert.equal(s.k.answer.value, 2)
})

// what is chosen, on a chart
const shown = (d) => JSON.stringify(d)
const K2 = dashboardCtx({ text: '#111', muted: '#555', border: '#ccc', borderSoft: '#ddd', panel: '#fff', panel2: '#eee', success: '#0a0', danger: '#a00', accent: '#00a' }, 'en', (k) => k)

test('on a chart the chosen stay whole with an edge, the others fade: not by colour alone', () => {
  const a = axisAnswer('column', { keys: ['N', 'S'], cats: ['North', 'South'], series: [{ name: 'A', values: [1, 2], texts: ['1', '2'] }] })
  const plain = tileChartOption(K2, a)
  const option = tileChartOption(K2, a, { chosen: ['N'] })
  const [on, off] = option.series[0].data
  assert.ok(on.itemStyle.borderWidth >= 2 && on.itemStyle.borderColor === '#111' && on.itemStyle.opacity === undefined)
  assert.ok(off.itemStyle.opacity < 1 && !off.itemStyle.borderWidth)
  assert.equal(on.value, 1, 'the numbers do not change')
  assert.deepEqual(option.xAxis.data, plain.xAxis.data)
  assert.equal(option.animation, false, 'and nothing moves')
  assert.equal(shown(tileChartOption(K2, a, { chosen: [] }).series[0].data), shown(plain.series[0].data), 'nothing chosen: as before')
})

test('every kind shows what is chosen, the value from a link as much as from a click', () => {
  const base = { keys: [1, null], cats: ['One', '(blank)'], series: [{ name: 'A', values: [3, 4], texts: ['3', '4'], as: 'bar' }], values: [3, 4], texts: ['3', '4'], name: 'A',
                 slices: [{ name: 'One', value: 3, text: '3', at: 0 }, { name: '(blank)', value: 4, text: '4', at: 1 }], targets: [1, 1] }
  const ring = (d) => d?.itemStyle?.borderWidth >= 1
  const items = (o, i = 0) => o.series[i].data
  for (const [kind, seriesAt] of [['column', 0], ['bar', 0], ['pie', 0], ['donut', 0], ['treemap', 0], ['funnel', 0], ['waterfall', 1], ['bullet', 1]]) {
    const o = tileChartOption(K2, { type: 'chart', kind, options: {}, format: null, data: base }, { chosen: ['1'] })
    const on = items(o, seriesAt).filter(ring)
    assert.equal(on.length, 1, `${kind}: one chosen`)
    assert.ok(items(o, seriesAt).filter((d) => d?.itemStyle?.opacity < 0.5).length >= 1, `${kind}: the others fade`)
  }
  const line = tileChartOption(K2, { type: 'chart', kind: 'line', options: {}, format: null, data: base }, { chosen: ['1'] })
  assert.ok(items(line)[0].symbolSize > items(line)[1].symbolSize || items(line)[1].symbol === 'none')
  const many = { ...base, cats: Array.from({ length: 40 }, (_, i) => `c${i}`), keys: Array.from({ length: 40 }, (_, i) => i), series: [{ name: 'A', values: Array.from({ length: 40 }, () => 1), texts: [] }] }
  const crowded = tileChartOption(K2, { type: 'chart', kind: 'line', options: {}, format: null, data: many }, { chosen: [3] })
  assert.equal(crowded.series[0].showSymbol, true, 'on a crowded line the chosen point is still drawn')
  assert.equal(crowded.series[0].data[0].symbol, 'none')
  const heat = tileChartOption(K2, { type: 'chart', kind: 'heatmap', options: {}, format: null, data: { xCats: ['a', 'b'], yCats: ['y'], keys: ['a', 'b'], split_keys: ['y'], cells: [[0, 0, 1], [1, 0, 2]], texts: ['1', '2'], name: 'H', series: [] } }, { chosen: [['a', 'y']] })
  assert.ok(ring(heat.series[0].data[0]) && !ring(heat.series[0].data[1]))
  assert.deepEqual(heat.series[0].data[0].value, [0, 0, 1])
  const dots = tileChartOption(K2, { type: 'chart', kind: 'scatter', options: {}, format: null, data: { series: [{ name: 'S', points: [[3, 1, 't'], [4, 2, 'u']] }], split_keys: [] } }, { chosen: [3] })
  assert.ok(ring(dots.series[0].data[0]) && !ring(dots.series[0].data[1]))
  assert.equal(dots.series[0].large, false)
  const split = tileChartOption(K2, { type: 'chart', kind: 'column', options: {}, format: null, data: { keys: ['a'], cats: ['a'], split_keys: ['p', 'q'], series: [{ name: 'p', values: [1], texts: ['1'] }, { name: 'q', values: [2], texts: ['2'] }] } }, { chosen: [['a', 'q']] })
  assert.ok(!ring(split.series[0].data[0]) && ring(split.series[1].data[0]))
  const whole = tileChartOption(K2, { type: 'chart', kind: 'column', options: {}, format: null, data: { keys: ['a'], cats: ['a'], split_keys: ['p', 'q'], series: [{ name: 'p', values: [1], texts: ['1'] }, { name: 'q', values: [2], texts: ['2'] }] } }, { chosen: ['a'] })
  assert.ok(ring(whole.series[0].data[0]) && ring(whole.series[1].data[0]), 'a category chosen takes all its parts')
  const nest = tileChartOption(K2, { type: 'chart', kind: 'treemap', options: {}, format: null, data: { keys: ['a', 'b'], cats: ['a', 'b'], split_keys: ['p', 'q'], series: [{ name: 'p', values: [1, 2], texts: ['1', '2'] }, { name: 'q', values: [3, 4], texts: ['3', '4'] }], values: [1, 2], name: 'x' } }, { chosen: [['a', 'q']] })
  const kids = nest.series[0].data[0].children
  assert.deepEqual([kids[0].at, kids[0].part, kids[1].part], [0, 0, 1])
  assert.ok(!ring(kids[0]) && ring(kids[1]))
})

test('a slice and a box carry their place, whatever order the chart draws them in', () => {
  const data = { keys: ['a', 'b', 'c'], cats: ['a', 'b', 'c'], series: [{ name: 'A', values: [1, 0, 3], texts: ['1', '0', '3'] }], values: [1, 0, 3], texts: ['1', '0', '3'], name: 'A', slices: [{ name: 'a', value: 1, text: '1', at: 0 }, { name: 'c', value: 3, text: '3', at: 2 }] }
  for (const kind of ['pie', 'treemap', 'funnel']) {
    const o = tileChartOption(K2, { type: 'chart', kind, options: {}, format: null, data })
    const placed = Object.fromEntries(o.series[0].data.map((d) => [d.name, d.at]))
    assert.deepEqual(placed, { a: 0, c: 2 }, kind)
  }
})

// ── the designer: the grid's rules ──────────────────────────────────────────
const {
  MIN_TILE, boxMoved, boxResized, cellsOf, freeSpot, lowestRow, pushed, MAX_GRID_ROW,
} = await import('../composables/dashboardGrid.ts')

const onGrid = (tiles) => Object.fromEntries(tiles.map((t) => [t.id, [t.grid.x, t.grid.y, t.grid.w, t.grid.h]]))
function assertNoOverlap(tiles) {
  for (let i = 0; i < tiles.length; i++) {
    for (let j = i + 1; j < tiles.length; j++) assert.ok(!overlaps(tiles[i].grid, tiles[j].grid), `${tiles[i].id} on ${tiles[j].id}`)
  }
}

test('the smallest tiles are the ones the gateway refuses under', () => {
  const source = readFileSync(new URL('../../gateway/app/services/dashboards.py', import.meta.url), 'utf8')
  const written = /MIN_TILE = \{(.*)\}/.exec(source)[1]
  const gateway = Object.fromEntries([...written.matchAll(/"(\w+)": \((\d+), (\d+)\)/g)].map((m) => [m[1], [Number(m[2]), Number(m[3])]]))
  assert.deepEqual(MIN_TILE, gateway)
})

test('a tile put on another pushes it down, and what is under that one follows', () => {
  const tiles = [box('a', 0, 0, 6, 3), box('b', 0, 3, 6, 3), box('c', 0, 6, 6, 3), box('far', 6, 0, 6, 3)]
  const after = pushed(tiles, 'a', { x: 0, y: 2, w: 6, h: 3 })
  assert.deepEqual(onGrid(after), { a: [0, 2, 6, 3], b: [0, 5, 6, 3], c: [0, 8, 6, 3], far: [6, 0, 6, 3] })
})

test('a tile with no one in its way leaves the others as they are, the same objects', () => {
  const tiles = [box('a', 0, 0, 3, 3), box('b', 6, 0, 3, 3)]
  const after = pushed(tiles, 'a', { x: 3, y: 0, w: 3, h: 3 })
  assert.equal(after[1], tiles[1])
  assert.deepEqual(after[0].grid, { x: 3, y: 0, w: 3, h: 3 })
})

test('a tile that has moved on leaves what it pushed where it was: a gesture starts from the same layout', () => {
  const start = [box('a', 0, 0, 6, 3), box('b', 0, 3, 6, 3)]
  const through = pushed(start, 'a', { x: 0, y: 2, w: 6, h: 3 })
  assert.deepEqual(onGrid(through).b, [0, 5, 6, 3])
  const past = pushed(start, 'a', { x: 6, y: 2, w: 6, h: 3 })
  assert.deepEqual(onGrid(past).b, [0, 3, 6, 3])
})

test('no way of moving and resizing ever leaves two tiles on one cell', () => {
  let seed = 7
  const next = (n) => { seed = (seed * 1103515245 + 12345) % 2147483648; return seed % n }
  let tiles = Array.from({ length: 40 }, (_, i) => box(`t${i}`, (i % 4) * 3, Math.floor(i / 4) * 3, 3, 3))
  for (let step = 0; step < 500; step++) {
    const t = tiles[next(tiles.length)]
    const to = step % 2
      ? boxMoved(t.grid, next(25) - 12, next(21) - 10)
      : boxResized(t.grid, ['n', 's', 'e', 'w', 'ne', 'nw', 'se', 'sw'][next(8)], next(13) - 6, next(13) - 6, t.type)
    tiles = pushed(tiles, t.id, to)
    assertNoOverlap(tiles)
    for (const x of tiles) assert.ok(x.grid.x >= 0 && x.grid.x + x.grid.w <= 12 && x.grid.y >= 0, `${x.id} inside the grid`)
  }
})

test('a layout that would go past the last row the server takes is not made', () => {
  const tiles = [box('a', 0, 0, 12, 90), box('b', 0, MAX_GRID_ROW - 5, 12, 3)]
  assert.equal(pushed(tiles, 'a', { x: 0, y: MAX_GRID_ROW - 80, w: 12, h: 90 }), null)
})

test('a move stays inside the twelve columns and under the first row', () => {
  const g = { x: 4, y: 2, w: 4, h: 3 }
  assert.deepEqual(boxMoved(g, -9, -9), { x: 0, y: 0, w: 4, h: 3 })
  assert.deepEqual(boxMoved(g, 9, 1), { x: 8, y: 3, w: 4, h: 3 })
  assert.deepEqual(boxMoved(g, 0, 5000).y, MAX_GRID_ROW)
})

test('a handle moves its edges only, down to the smallest tile of the type and up to the grid', () => {
  const g = { x: 4, y: 4, w: 6, h: 6 }
  assert.deepEqual(boxResized(g, 'se', 1, 2, 'chart'), { x: 4, y: 4, w: 7, h: 8 })
  assert.deepEqual(boxResized(g, 'e', 50, 50, 'chart'), { x: 4, y: 4, w: 8, h: 6 }, 'the east handle has no say on the height, and stops at the twelfth column')
  assert.deepEqual(boxResized(g, 'se', -50, -50, 'chart'), { x: 4, y: 4, w: 4, h: 4 }, 'a chart is at least 4 by 4')
  assert.deepEqual(boxResized(g, 'se', -50, -50, 'text'), { x: 4, y: 4, w: 2, h: 1 }, 'a text at least 2 by 1')
  assert.deepEqual(boxResized(g, 'nw', -2, -1, 'chart'), { x: 2, y: 3, w: 8, h: 7 }, 'the far corner stays where it was')
  assert.deepEqual(boxResized(g, 'nw', 50, 50, 'kpi'), { x: 8, y: 8, w: 2, h: 2 }, 'and does when it stops at the smallest')
  assert.deepEqual(boxResized(g, 'w', -50, 0, 'chart'), { x: 0, y: 4, w: 10, h: 6 })
  assert.deepEqual(boxResized(g, 'n', 0, -50, 'chart'), { x: 4, y: 0, w: 6, h: 10 })
  assert.deepEqual(boxResized({ x: 0, y: 0, w: 12, h: 99 }, 's', 0, 50, 'table').h, 100, 'a tile is at most 100 rows high')
})

test('pixels become cells by the same sizes the viewer draws with', () => {
  // twelve columns of 70 and eleven gaps of 10: a cell and its gap are 80 across, 50 down
  assert.deepEqual(cellsOf(79, 24, 950), { cols: 1, rows: 0 })
  assert.deepEqual(cellsOf(-41, 26, 950), { cols: -1, rows: 1 })
  assert.deepEqual(cellsOf(0, 0, 950), { cols: 0, rows: 0 })
})

test('a new tile goes where it first fits, reading the grid, and under the rest when it fits nowhere', () => {
  assert.deepEqual(freeSpot([], 6, 6), { x: 0, y: 0 })
  assert.deepEqual(freeSpot([box('a', 0, 0, 3, 3)], 3, 3), { x: 3, y: 0 })
  assert.deepEqual(freeSpot([box('a', 0, 0, 6, 3), box('b', 6, 0, 6, 6)], 6, 3), { x: 0, y: 3 })
  assert.deepEqual(freeSpot([box('a', 0, 0, 12, 3)], 12, 2), { x: 0, y: 3 })
  assert.equal(lowestRow([box('a', 0, 2, 6, 3), box('b', 6, 0, 6, 9)]), 9)
  assert.equal(lowestRow([]), 0)
})

// ── the designer: a dashboard as the report designer's store holds it ───────
const {
  boardOf, definitionOf, duplicateTile, fieldOnTile, newTile, placeTile, previewSignatures, problemsOfBoard, readyToAsk, sizeOfNew, tilesChanged, withoutTiles,
} = await import('../composables/dashboardDesignModel.ts')
const { createDesignStore } = await import('../composables/reportDesignStore.ts')

const salesFields = [{ name: 'area', type: 'string' }, { name: 'day', type: 'date' }, { name: 'id', type: 'integer' }, { name: 'amount', type: 'float' }]
const sales = { name: 'sales', source: { kind: 'datasource', datasource_id: 7, filters: [] }, fields: salesFields, calculated: [] }
const definition = (tiles, more = {}) => ({ v: 1, locale: 'it', timezone: 'Europe/Rome', refresh_seconds: 300, parameters: [], datasets: [sales], tiles, ...more })
const kpiTile = (id, x, y, more = {}) => ({ id, type: 'kpi', dataset: 'sales', grid: { x, y, w: 3, h: 3 }, value: 'SUM([amount])', ...more })

test('a dashboard goes into the report designer\'s store and comes out as it was', () => {
  const d = definition([kpiTile('a', 0, 0, { title: 'A', label: 'Revenue', format: '#,##0', visible: null }), { id: 't', type: 'text', title: '', grid: { x: 3, y: 0, w: 2, h: 1 }, value: 'Hello' }])
  const out = definitionOf(boardOf(d))
  assert.deepEqual(Object.keys(out).sort(), ['datasets', 'locale', 'parameters', 'refresh_seconds', 'tiles', 'timezone', 'v'])
  assert.deepEqual(out.tiles[0], { id: 'a', type: 'kpi', dataset: 'sales', grid: { x: 0, y: 0, w: 3, h: 3 }, value: 'SUM([amount])', title: 'A', label: 'Revenue', format: '#,##0' })
  assert.deepEqual(out.tiles[1], { id: 't', type: 'text', grid: { x: 3, y: 0, w: 2, h: 1 }, value: 'Hello' }, 'what is left empty is not there')
  assert.equal(out.refresh_seconds, 300)
  assert.equal(out.locale, 'it')
})

test('what the store adds for a table of a report does not reach the definition', async () => {
  const store = createDesignStore()
  store.reset(boardOf(definition([{ id: 't', type: 'table', dataset: 'sales', grid: { x: 0, y: 0, w: 6, h: 6 }, columns: [{ header: 'A', value: '[area]' }], groups: [] }])))
  store.find('t').item.groups.push({ by: '[area]' })
  await new Promise((resolve) => setImmediate(resolve))
  const [tile] = definitionOf(store.doc.value).tiles
  assert.deepEqual(Object.keys(tile).filter((k) => ['x', 'y', 'w', 'h'].includes(k)), [])
})

test('a drag of many cells, with the tiles it pushes, is one undo and one redo', async () => {
  const store = createDesignStore()
  store.reset(boardOf(definition([kpiTile('a', 0, 0), kpiTile('b', 0, 3)])))
  const start = store.doc.value.sections.body.items.map((t) => ({ id: t.id, type: t.type, grid: t.grid }))
  store.gesture.value = true
  for (const y of [1, 2, 3, 4]) placeTile(store.doc.value, pushed(start, 'a', { x: 0, y, w: 3, h: 3 }))
  await new Promise((resolve) => setImmediate(resolve))
  store.gesture.value = false
  await new Promise((resolve) => setImmediate(resolve))
  const moved = onGrid(definitionOf(store.doc.value).tiles)
  assert.deepEqual(moved, { a: [0, 4, 3, 3], b: [0, 7, 3, 3] })
  store.undo()
  assert.deepEqual(onGrid(definitionOf(store.doc.value).tiles), { a: [0, 0, 3, 3], b: [0, 3, 3, 3] })
  assert.equal(store.canUndo.value, false, 'one step, and nothing before it')
  store.redo()
  assert.deepEqual(onGrid(definitionOf(store.doc.value).tiles), moved)
})

test('a new tile opens with data already on it, from the fields of the dataset', () => {
  const doc = boardOf(definition([]))
  const k = newTile(doc, 'kpi', { x: 0, y: 0 })
  assert.equal(k.dataset, 'sales')
  assert.equal(k.value, 'SUM([amount])')
  assert.equal(k.label, 'amount')
  assert.deepEqual(k.grid, { x: 0, y: 0, ...sizeOfNew('kpi') })
  const c = newTile(doc, 'chart', { x: 0, y: 3 })
  assert.equal(c.kind, 'column')
  assert.equal(c.category, '[area]')
  assert.deepEqual(c.series, [{ value: 'SUM([amount])', name: 'amount' }])
  const t = newTile(doc, 'table', { x: 0, y: 9 })
  assert.deepEqual(t.columns.map((x) => x.value), ['[area]', '[day]', '[id]', '[amount]'])
  const m = newTile(doc, 'matrix', { x: 0, y: 15 })
  assert.equal(m.type, 'table')
  assert.equal(m.matrix.columns_by, '[day]')
  assert.equal(m.matrix.value, 'SUM([amount])')
  assert.equal(m.groups[0].by, '[area]')
  assert.equal(m.id, 'matrix1')
  for (const kind of ['kpi', 'chart', 'table', 'matrix', 'text', 'image']) {
    const [w, h] = MIN_TILE[kind === 'matrix' ? 'table' : kind]
    assert.ok(sizeOfNew(kind).w >= w && sizeOfNew(kind).h >= h, `${kind} born above its minimum`)
  }
})

test('with no numbers to add up a new tile counts rows, and without a dataset it waits for one', () => {
  const words = boardOf(definition([], { datasets: [{ ...sales, fields: [{ name: 'area', type: 'string' }] }] }))
  assert.equal(newTile(words, 'kpi', { x: 0, y: 0 }).value, 'COUNT()')
  assert.deepEqual(newTile(words, 'chart', { x: 0, y: 0 }).series, [{ value: 'COUNT()', name: '' }])
  const none = boardOf(definition([], { datasets: [] }))
  assert.equal(newTile(none, 'kpi', { x: 0, y: 0 }).dataset, '')
})

test('a tile half made is not asked of the server, and the rest are', () => {
  const doc = boardOf(definition([]))
  const ready = (tile) => readyToAsk(doc, tile)
  assert.equal(ready(kpiTile('a', 0, 0)), true)
  assert.equal(ready(kpiTile('a', 0, 0, { value: '' })), false)
  assert.equal(ready(kpiTile('a', 0, 0, { dataset: 'gone' })), false)
  const chart = { id: 'c', type: 'chart', kind: 'column', dataset: 'sales', category: '[area]', series: [{ value: 'SUM([amount])' }] }
  assert.equal(ready(chart), true)
  assert.equal(ready({ ...chart, category: '' }), false)
  assert.equal(ready({ ...chart, kind: 'gauge', category: null }), true, 'a gauge has no category')
  assert.equal(ready({ ...chart, series: [{ value: '' }] }), false)
  const table = { id: 't', type: 'table', dataset: 'sales', columns: [{ header: 'A', value: '[area]' }] }
  assert.equal(ready(table), true)
  assert.equal(ready({ ...table, columns: [] }), false)
  assert.equal(ready({ ...table, columns: [{ header: '', value: '' }] }), false)
  assert.equal(ready({ ...table, columns: [{ header: '', value: '' }], matrix: { columns_by: '[day]', value: 'SUM([amount])' }, groups: [{ by: '[area]' }] }), true)
  assert.equal(ready({ ...table, columns: [{ header: '', value: '' }], matrix: { columns_by: '', value: 'SUM([amount])' }, groups: [{ by: '[area]' }] }), false)
  assert.equal(ready({ id: 'x', type: 'text', value: '' }), false)
  assert.equal(ready({ id: 'x', type: 'text', value: 'Hello' }), true)
  assert.equal(ready({ id: 'i', type: 'image', asset: '' }), false)
  const full = definition([kpiTile('a', 0, 0), kpiTile('b', 3, 0, { value: '' })])
  assert.deepEqual(withoutTiles(full, new Set(['b'])).tiles.map((t) => t.id), ['a'])
})

test('moving a tile asks nothing again; a property asks its tile; a dataset or a value asks them all', () => {
  const asked = (d, values = {}) => previewSignatures(d, values)
  const base = definition([kpiTile('a', 0, 0), kpiTile('b', 3, 0)])
  const before = asked(base)
  const moved = definition([kpiTile('a', 6, 6), kpiTile('b', 3, 0)])
  assert.deepEqual(tilesChanged(before, asked(moved)), [])
  const format = definition([kpiTile('a', 0, 0, { format: '0.0' }), kpiTile('b', 3, 0)])
  assert.deepEqual(tilesChanged(before, asked(format)), ['a'])
  const source = definition([kpiTile('a', 0, 0), kpiTile('b', 3, 0)], { datasets: [{ ...sales, source: { kind: 'datasource', datasource_id: 8, filters: [] } }] })
  assert.deepEqual(tilesChanged(before, asked(source)).sort(), ['a', 'b'])
  assert.deepEqual(tilesChanged(before, asked(base, { region: 'Nord' })).sort(), ['a', 'b'])
  assert.deepEqual(tilesChanged(before, asked(definition([kpiTile('a', 0, 0), kpiTile('b', 3, 0), kpiTile('c', 6, 0)]))), ['c'])
  assert.deepEqual(tilesChanged(before, asked(definition([kpiTile('a', 0, 0)]))), [], 'one taken away has nothing to ask')
})

test('a title, which the screen knows, asks nothing again', () => {
  const before = previewSignatures(definition([kpiTile('a', 0, 0, { title: 'One' })]), {})
  assert.deepEqual(tilesChanged(before, previewSignatures(definition([kpiTile('a', 0, 0, { title: 'Two' })]), {})), [])
})

test('the fields of a dataset, read while one works, ask nothing again', () => {
  const before = previewSignatures(definition([kpiTile('a', 0, 0)]), {})
  const read = definition([kpiTile('a', 0, 0)], { datasets: [{ ...sales, fields: [...salesFields, { name: 'new', type: 'float' }] }] })
  assert.deepEqual(tilesChanged(before, previewSignatures(read, {})), [])
})

test('a copy of a tile has its own id and goes where it fits, pushing no one', () => {
  const doc = boardOf(definition([kpiTile('kpi1', 0, 0, { label: 'Total' }), kpiTile('other', 3, 0)]))
  const copy = duplicateTile(doc, doc.sections.body.items[0])
  assert.equal(copy.id, 'kpi2')
  assert.equal(copy.label, 'Total')
  assert.deepEqual(copy.grid, { x: 6, y: 0, w: 3, h: 3 })
})

test('a field clicked joins what is selected, as what it can be there', () => {
  const num = salesFields[3]
  const text = salesFields[0]
  const table = { id: 't', type: 'table', dataset: 'sales', columns: [] }
  assert.equal(fieldOnTile(table, 'sales', num), true)
  assert.deepEqual(table.columns.map((c) => c.value), ['[amount]'])
  const chart = { id: 'c', type: 'chart', kind: 'column', dataset: 'sales', category: '', series: [{ value: '' }] }
  assert.equal(fieldOnTile(chart, 'sales', text), true)
  assert.equal(chart.category, '[area]')
  assert.equal(fieldOnTile(chart, 'sales', num), true)
  assert.deepEqual(chart.series, [{ value: 'SUM([amount])', name: 'amount' }], 'the first empty series takes it')
  fieldOnTile(chart, 'sales', { name: 'id', type: 'integer' })
  assert.equal(chart.series.length, 2, 'the next is one more')
  const kpi = { id: 'k', type: 'kpi', dataset: 'sales', value: '' }
  assert.equal(fieldOnTile(kpi, 'sales', num), true)
  assert.equal(kpi.value, 'SUM([amount])')
  assert.equal(fieldOnTile(kpi, 'sales', text), false, 'a text does not add up: not on a KPI')
  assert.equal(fieldOnTile({ ...kpi, dataset: 'other' }, 'sales', num), false, 'not from another dataset')
  const matrix = { id: 'm', type: 'table', dataset: 'sales', columns: [{ header: '', value: '' }], groups: [{ by: '' }], matrix: { columns_by: '', value: '' } }
  fieldOnTile(matrix, 'sales', text)
  fieldOnTile(matrix, 'sales', { name: 'day', type: 'date' })
  fieldOnTile(matrix, 'sales', num)
  assert.equal(matrix.groups[0].by, '[area]', 'a matrix: its rows first')
  assert.equal(matrix.matrix.columns_by, '[day]', 'then its columns')
  assert.equal(matrix.matrix.value, 'SUM([amount])', 'and a number in its cells')
})

test('a problem of the server lands on its tile and its property, by the tile\'s id or its place', () => {
  const doc = boardOf(definition([kpiTile('a', 0, 0), kpiTile('b', 3, 0)]))
  const byItem = problemsOfBoard(doc, { problems: [{ item: 'b', field: 'value', pos: 3, length: 2, message: 'no such field' }] })
  assert.deepEqual([byItem[0].item, byItem[0].path, byItem[0].pos], ['b', 'value', 3])
  const refused = problemsOfBoard(doc, { message: 'x', where: 'tiles[1].grid', item: 'b', problems: [{ item: 'b', field: 'grid', pos: null, length: 0, message: 'lies on' }] })
  assert.deepEqual([refused[0].item, refused[0].path], ['b', 'grid'])
  const byPlace = problemsOfBoard(doc, { problems: [{ item: null, field: 'tiles[1].type', pos: null, length: 0, message: 'A tile is one of' }] })
  assert.deepEqual([byPlace[0].item, byPlace[0].path], ['b', 'type'])
  const dataset = problemsOfBoard(doc, { problems: [{ item: null, field: 'datasets[sales].source', pos: null, length: 0, message: 'gone' }] })
  assert.deepEqual([dataset[0].item, dataset[0].dataset, dataset[0].path], [null, 'sales', 'source'])
  const whole = problemsOfBoard(doc, { problems: [{ item: null, field: 'refresh_seconds', pos: null, length: 0, message: 'The refresh is' }] })
  assert.deepEqual([whole[0].item, whole[0].dataset, whole[0].path], [null, null, 'refresh_seconds'])
})

// ── the designer: its live view ─────────────────────────────────────────────
const { createLiveView } = await import('../composables/dashboardLiveView.ts')

function liveHarness (tiles) {
  const calls = []
  const state = { def: definition(tiles), values: {}, moving: false, slots: {}, problem: '' }
  const view = createLiveView({
    build: () => state.def,
    values: () => state.values,
    slots: () => state.slots,
    setSlots: (s) => { state.slots = s },
    ready: (tile) => !!(tile.value ?? true),
    local: () => null,
    say: (p) => { state.problem = p },
    call: (definition, ids, values, signal) => new Promise((resolve, reject) => {
      const c = { definition, ids, values, signal, resolve, reject }
      signal.addEventListener('abort', () => { c.aborted = true; reject(Object.assign(new Error('aborted'), { name: 'AbortError' })) })
      calls.push(c)
    }),
    errorOf: (e) => String(e.message ?? e),
  })
  return { calls, state, view }
}
const numbersOf = (value) => ({ type: 'kpi', title: '', computed_at: '2026-11-01T00:00:00Z', cached: false, value, text: String(value) })
const tick = () => new Promise((resolve) => setImmediate(resolve))

test('the live view asks what changed and nothing else', async () => {
  const { calls, state, view } = liveHarness([kpiTile('a', 0, 0), kpiTile('b', 3, 0)])
  view.run()
  assert.deepEqual(calls[0].ids.sort(), ['a', 'b'])
  calls[0].resolve({ tiles: { a: numbersOf(1), b: numbersOf(2) }, truncated: {} })
  await tick()
  assert.equal(state.slots.a.answer.value, 1)
  state.def = definition([kpiTile('a', 0, 0, { format: '0.0' }), kpiTile('b', 9, 9)])
  view.run()
  assert.deepEqual(calls[1].ids, ['a'], 'one property: one tile, and one moved is not asked')
  assert.deepEqual(calls[1].definition.tiles.map((t) => t.id), ['a', 'b'], 'the whole definition goes, an expression may name another dataset')
  calls[1].resolve({ tiles: { a: numbersOf(10) }, truncated: {} })
  await tick()
  view.run()
  assert.equal(calls.length, 2, 'nothing changed since: nothing asked')
})

test('an old answer never covers a new one', async () => {
  const { calls, state, view } = liveHarness([kpiTile('a', 0, 0), kpiTile('b', 3, 0)])
  view.run()
  calls[0].resolve({ tiles: { a: numbersOf(1), b: numbersOf(2) }, truncated: {} })
  await tick()
  state.def = definition([kpiTile('a', 0, 0, { format: '0.0' }), kpiTile('b', 3, 0, { format: '0.0' })])
  view.run()
  const old = calls[1]
  state.def = definition([kpiTile('a', 0, 0, { format: '0.00' }), kpiTile('b', 3, 0, { format: '0.0' })])
  view.run()
  const fresh = calls[2]
  assert.deepEqual(fresh.ids, ['a'])
  assert.equal(old.aborted, undefined, 'the older one still answers for b')
  fresh.resolve({ tiles: { a: numbersOf(100) }, truncated: {} })
  await tick()
  old.resolve({ tiles: { a: numbersOf(-1), b: numbersOf(20) }, truncated: {} })
  await tick()
  assert.equal(state.slots.a.answer.value, 100, 'the late answer does not touch a')
  assert.equal(state.slots.b.answer.value, 20, 'and still counts for b')
})

test('a request no tile is waiting for any more is dropped', async () => {
  const { calls, state, view } = liveHarness([kpiTile('a', 0, 0)])
  view.run()
  state.def = definition([kpiTile('a', 0, 0, { format: '0.0' })])
  view.run()
  assert.equal(calls[0].aborted, true)
})

test('a tile half made says so by itself, and is not sent', async () => {
  const { calls, state, view } = liveHarness([kpiTile('a', 0, 0), kpiTile('b', 3, 0, { value: '' })])
  view.run()
  assert.deepEqual(calls[0].ids, ['a'])
  assert.deepEqual(calls[0].definition.tiles.map((t) => t.id), ['a'])
  calls[0].resolve({ tiles: { a: numbersOf(1) }, truncated: {} })
  await tick()
  assert.equal(state.slots.b.answer, null)
})

test('a tile the server refuses says why in the tile, and the others go on', async () => {
  const { calls, state, view } = liveHarness([kpiTile('a', 0, 0), kpiTile('b', 3, 0)])
  view.run([{ item: 'b', dataset: null, parameter: null, path: 'value', message: 'there is no field [x]', pos: 0, length: 3 }])
  assert.deepEqual(calls[0].ids, ['a'])
  assert.deepEqual(calls[0].definition.tiles.map((t) => t.id), ['a'], 'one the server refuses would stop the whole request')
  assert.deepEqual(state.slots.b.answer, { error: 'there is no field [x]', code: 'error' })
  calls[0].resolve({ tiles: { a: numbersOf(1) }, truncated: {} })
  await tick()
  assert.equal(state.slots.a.answer.value, 1)
  assert.deepEqual(state.slots.b.answer, { error: 'there is no field [x]', code: 'error' })
})

test('a tile mended is asked again', async () => {
  const { calls, state, view } = liveHarness([kpiTile('a', 0, 0), kpiTile('b', 3, 0)])
  view.run([{ item: 'b', dataset: null, parameter: null, path: 'value', message: 'no', pos: null, length: 0 }])
  calls[0].resolve({ tiles: { a: numbersOf(1) }, truncated: {} })
  await tick()
  view.run([])
  assert.deepEqual(calls[1].ids, ['b'])
})

test('a problem of the whole dashboard is said once and no tile is asked', async () => {
  const { calls, state, view } = liveHarness([kpiTile('a', 0, 0)])
  view.run([{ item: null, dataset: 'sales', parameter: null, path: 'source', message: 'gone', pos: null, length: 0 }])
  assert.equal(calls.length, 0)
  assert.equal(state.problem, 'gone')
  view.run([])
  assert.equal(calls.length, 1)
  assert.equal(state.problem, '')
})

test('a request that fails says so in the tiles it asked, and they are asked again at the next change', async () => {
  const { calls, state, view } = liveHarness([kpiTile('a', 0, 0)])
  view.run()
  calls[0].reject(new Error('the engine fell'))
  await tick()
  assert.equal(state.slots.a.answer.error, 'the engine fell')
  view.run()
  assert.equal(calls.length, 2)
})

test('a refused request lands on its tiles, and the others are asked without them', async () => {
  const { calls, state, view } = liveHarness([kpiTile('a', 0, 0), kpiTile('b', 3, 0)])
  view.run()
  const refused = Object.assign(new Error('refused'), { problems: [{ item: 'b', dataset: null, parameter: null, path: 'value', message: 'no such field', pos: null, length: 0 }] })
  calls[0].reject(refused)
  await tick()
  assert.deepEqual(state.slots.b.answer, { error: 'no such field', code: 'error' })
  assert.deepEqual(calls[1].ids, ['a'])
})

test('Refresh asks every tile again', async () => {
  const { calls, view } = liveHarness([kpiTile('a', 0, 0), kpiTile('b', 3, 0)])
  view.run()
  calls[0].resolve({ tiles: { a: numbersOf(1), b: numbersOf(2) }, truncated: {} })
  await tick()
  view.run(undefined, { again: true })
  assert.deepEqual(calls[1].ids.sort(), ['a', 'b'])
})

test('a tile taken away has no slot, and one that comes back starts over', async () => {
  const { calls, state, view } = liveHarness([kpiTile('a', 0, 0), kpiTile('b', 3, 0)])
  view.run()
  calls[0].resolve({ tiles: { a: numbersOf(1), b: numbersOf(2) }, truncated: {} })
  await tick()
  state.def = definition([kpiTile('a', 0, 0)])
  view.run()
  assert.deepEqual(Object.keys(state.slots), ['a'])
  state.def = definition([kpiTile('a', 0, 0), kpiTile('b', 3, 0)])
  view.run()
  assert.deepEqual(calls[1].ids, ['b'])
})

// ── its documents ────────────────────────────────────────────────────────────
test('the page of its documents comes out as it went in, and none chosen stays none', () => {
  const page = { size: 'Letter', orientation: 'portrait' }
  const base = { v: 1, locale: 'en', timezone: 'UTC', refresh_seconds: 0, parameters: [], datasets: [], tiles: [] }
  assert.deepEqual(definitionOf(boardOf({ ...base, page })).page, page)
  assert.equal('page' in definitionOf(boardOf(base)), false)
})

test('a Pushmail cell previews the names of its dashboards\' files too', async () => {
  const { pushmailPreviewRequest } = await import('../composables/useFlowGroups.ts')
  const req = pushmailPreviewRequest({
    label: '', connectionId: null, to: [], cc: [], subject: 'S', body: '', bodyIsHtml: false, timezone: 'UTC', attachments: [],
    dashboards: [{ dashboardId: 1, format: 'png', params: {}, fileName: ' board_{today:yyyy} ' }],
  }, 'G')
  assert.equal(req.templates.dashboard_0, 'board_{today:yyyy}')
  assert.equal(req.targets.dashboard_0, 'file')
})

// ── an AI comment (docs/specs/ai-comment.md) ─────────────────────────────────
const {
  commentIds, commentsReading, createComments, readableBy, showsOnlyRows, PENDING_TRIES,
} = await import('../composables/dashboardComments.ts')
const { dropRead, renameRead } = await import('../composables/reportDesignModel.ts')

const said = (text, more = {}) => ({
  state: 'ready', text, model: 'fake (Fake)', numbers_at: '2026-11-01T10:00:00+00:00', written_at: '2026-11-01T10:00:01+00:00', stale: false, can_ask: false, message: '', reason: '', ...more,
})
const none = (state, more = {}) => ({ ...said('', { state, model: '', numbers_at: null, written_at: null, message: `says ${state}` }), ...more })

function commentHarness(ids = ['story'], how = {}) {
  const state = { slots: Object.fromEntries(ids.map((id) => [id, { answer: null, asking: false }])), hidden: false, written: [], shown: [] }
  const calls = []
  const timers = []
  const comments = createComments({
    slots: () => state.slots,
    setSlots: (s) => { state.slots = s; state.shown.push(s) },
    hidden: () => state.hidden,
    call: (tile, moment, signal) => new Promise((resolve, reject) => {
      const c = { tile, moment, signal, resolve, reject }
      signal.addEventListener('abort', () => { c.aborted = true; reject(Object.assign(new Error('aborted'), { name: 'AbortError' })) })
      calls.push(c)
    }),
    failed: () => 'Not available here',
    wrote: (id) => state.written.push(id),
    later: (fn) => { timers.push(fn); return timers.length },
    forget: (t) => { timers[t - 1] = null },
    ...how,
  })
  return { calls, state, comments, timers }
}

test('a comment tile is at least 4 by 3, in the grid and in the designer', () => {
  assert.deepEqual(MIN_TILE.comment, [4, 3])
  assert.deepEqual(sizeOfNew('comment'), { w: 6, h: 4 })
  assert.equal(boxResized({ x: 0, y: 0, w: 6, h: 4 }, 'se', -9, -9, 'comment').w, 4)
  assert.equal(boxResized({ x: 0, y: 0, w: 6, h: 4 }, 'se', -9, -9, 'comment').h, 3)
})

test('a new AI comment reads nothing until its author picks, and is not asked until then', () => {
  const doc = boardOf(definition([kpiTile('a', 0, 0)]))
  const tile = newTile(doc, 'comment', { x: 0, y: 3 })
  assert.deepEqual([tile.id, tile.type, tile.instruction, tile.reads], ['comment1', 'comment', '', []])
  assert.equal(readyToAsk(doc, tile), false)
  assert.equal(readyToAsk(doc, { ...tile, reads: ['a'] }), true)
  assert.equal(readyToAsk(doc, { ...tile, reads: ['a', 'gone'] }), false, 'what it reads is there')
  const half = boardOf(definition([kpiTile('a', 0, 0, { value: '' })]))
  assert.equal(readyToAsk(half, { ...tile, reads: ['a'] }), false, 'a read half made stops it: the server would refuse the id')
})

test('writing an instruction or picking what it reads asks nothing of the server', () => {
  const tile = { id: 'c', type: 'comment', grid: { x: 0, y: 0, w: 6, h: 4 }, instruction: 'A', reads: ['a'] }
  const before = previewSignatures(definition([kpiTile('a', 0, 0), tile]), {})
  const after = previewSignatures(definition([kpiTile('a', 0, 0), { ...tile, instruction: 'AB', reads: ['a', 'b'], title: 'T' }]), {})
  assert.deepEqual(tilesChanged(before, after), [])
})

test('a tile removed leaves what comments on it, and a tile renamed is followed', () => {
  const doc = boardOf(definition([kpiTile('a', 0, 0), kpiTile('b', 3, 0), { id: 'c', type: 'comment', grid: { x: 0, y: 3, w: 6, h: 4 }, reads: ['a', 'b'] }]))
  renameRead(doc, 'a', 'revenue')
  assert.deepEqual(doc.sections.body.items[2].reads, ['revenue', 'b'])
  dropRead(doc, 'b')
  assert.deepEqual(doc.sections.body.items[2].reads, ['revenue'])
})

test('the store takes a removed tile out of what reads it, and an undo brings both back', async () => {
  const store = createDesignStore()
  store.reset(boardOf(definition([kpiTile('a', 0, 0), kpiTile('b', 3, 0), { id: 'c', type: 'comment', grid: { x: 0, y: 3, w: 6, h: 4 }, reads: ['a', 'b'] }])))
  store.remove('a')
  const comment = () => store.doc.value.sections.body.items.find((t) => t.id === 'c')
  assert.deepEqual(comment().reads, ['b'])
  store.undo()
  assert.deepEqual(comment().reads, ['a', 'b'])
})

test('only a KPI, a chart or a table can be read, and which comments read what', () => {
  const tiles = [kpiTile('a', 0, 0), { id: 't', type: 'text' }, { id: 'c', type: 'chart' }, { id: 'x', type: 'table' }, { id: 'i', type: 'image' },
    { id: 'one', type: 'comment', reads: ['a'] }, { id: 'two', type: 'comment', reads: ['c', 'x'] }]
  assert.deepEqual(readableBy(tiles).map((t) => t.id), ['a', 'c', 'x'])
  assert.deepEqual(commentIds(tiles), ['one', 'two'])
  assert.deepEqual(commentsReading(tiles, ['x']), ['two'])
  assert.deepEqual(commentsReading(tiles, ['t']), [])
})

test('what shows only rows is what the gateway leaves out, from the numbers on screen', () => {
  const table = { id: 't', type: 'table', groups: [] }
  assert.equal(showsOnlyRows(table, { type: 'table', rows: [{ kind: 'd', cells: [] }] }), true)
  // on paper there are no numbers to look at: what the definition says
  assert.equal(showsOnlyRows({ type: 'chart', kind: 'scatter', series: [{ value: '[amount]' }] }, null), true)
  assert.equal(showsOnlyRows({ type: 'chart', kind: 'scatter', series: [{ value: 'SUM([amount])' }] }, null), false)
  assert.equal(showsOnlyRows({ type: 'chart', kind: 'column', series: [{ value: '[amount]' }] }, null), false)
  assert.equal(showsOnlyRows({ type: 'kpi' }, null), false)
  assert.equal(showsOnlyRows(table, { type: 'table', rows: [{ kind: 'd' }, { kind: 't' }] }), false, 'a total is a number')
  assert.equal(showsOnlyRows(table, { type: 'table', matrix: true, rows: [{ kind: 'd' }] }), false, 'in a matrix the same kind is a group')
  assert.equal(showsOnlyRows(table, { type: 'table', empty: true, rows: [] }), false)
  const scatter = { id: 'c', type: 'chart', kind: 'scatter' }
  assert.equal(showsOnlyRows(scatter, { type: 'chart', kind: 'scatter', data: { aggregated: false } }), true)
  assert.equal(showsOnlyRows(scatter, { type: 'chart', kind: 'scatter', data: { aggregated: true } }), false)
  assert.equal(showsOnlyRows(kpiTile('a', 0, 0), { type: 'kpi', text: '1' }), false)
  // no numbers yet: what the definition can say
  assert.equal(showsOnlyRows({ type: 'table', groups: [], grand_total: false }, null), true)
  assert.equal(showsOnlyRows({ type: 'table', groups: [{ by: '[a]' }] }, null), false)
  assert.equal(showsOnlyRows({ type: 'table', groups: [], grand_total: true }, { error: 'x', code: 'no_access' }), false)
  assert.equal(showsOnlyRows({ type: 'table', matrix: { columns_by: '[a]' }, groups: [] }, null), false)
})

test('a comment asked shows what comes back, and the moment goes with the request', async () => {
  const { calls, state, comments } = commentHarness()
  comments.ask(['story'], 'open')
  assert.deepEqual([calls[0].tile, calls[0].moment], ['story', 'open'])
  assert.equal(state.slots.story.answer, null, 'a skeleton until it answers')
  calls[0].resolve(said('Up.'))
  await tick()
  assert.equal(state.slots.story.answer.text, 'Up.')
  assert.equal(state.slots.story.asking, false)
})

test('a click takes the text away at once, before anything is answered', async () => {
  const { calls, state, comments } = commentHarness()
  comments.ask(['story'], 'open')
  calls[0].resolve(said('Up.'))
  await tick()
  comments.blank(['story'])
  assert.equal(state.slots.story.answer, null)
  comments.ask(['story'], 'open')
  calls[1].resolve(none('not_asked', { can_ask: true }))
  await tick()
  assert.equal(state.slots.story.answer.can_ask, true)
  comments.ask(['story'], 'asked')
  assert.equal(calls[2].moment, 'asked')
  assert.equal(state.slots.story.asking, 'asked', 'the button says it is at work')
  calls[2].resolve(said('Only the North.'))
  await tick()
  assert.equal(state.slots.story.answer.text, 'Only the North.')
  assert.deepEqual(state.written, ['story'], 'the page is told: focus and announcement')
})

test('an old answer never covers a new one, nor a request knocked down', async () => {
  const { calls, state, comments } = commentHarness()
  comments.ask(['story'], 'open')
  comments.blank(['story'])
  comments.ask(['story'], 'open')
  assert.equal(calls[0].aborted, true)
  calls[1].resolve(said('New.'))
  await tick()
  calls[0].resolve(said('Old.'))
  await tick()
  assert.equal(state.slots.story.answer.text, 'New.')
})

test('a refresh with the same text and the same time changes nothing on the screen', async () => {
  const { calls, state, comments } = commentHarness()
  comments.ask(['story'], 'open')
  calls[0].resolve(said('Same.'))
  await tick()
  const shown = state.slots.story.answer
  comments.ask(['story'], 'refresh')
  assert.equal(state.slots.story.answer, shown, 'no skeleton while it asks')
  calls[1].resolve(said('Same.'))
  await tick()
  assert.equal(state.slots.story.answer, shown, 'the same object: nothing is drawn again')
})

test('a refresh keeps the text through a failure of its own, and says the time of its numbers when the server does', async () => {
  const { calls, state, comments } = commentHarness()
  comments.ask(['story'], 'open')
  calls[0].resolve(said('Up.'))
  await tick()
  comments.ask(['story'], 'refresh')
  calls[1].reject(new Error('the network'))
  await tick()
  assert.equal(state.slots.story.answer.text, 'Up.')
  comments.ask(['story'], 'refresh')
  calls[2].resolve(said('Up.', { stale: true }))
  await tick()
  assert.equal(state.slots.story.answer.stale, true)
})

test('a request that fails with no text on screen says so, and is no comment', async () => {
  const { calls, state, comments } = commentHarness()
  comments.ask(['story'], 'open')
  calls[0].reject(new Error('the network'))
  await tick()
  assert.deepEqual([state.slots.story.answer.state, state.slots.story.answer.message], ['failed', 'Not available here'])
})

test('a refresh does not knock down what a person asked for', async () => {
  const { calls, state, comments } = commentHarness()
  comments.ask(['story'], 'asked')
  comments.ask(['story'], 'refresh')
  assert.equal(calls.length, 1)
  assert.equal(calls[0].aborted, undefined)
  calls[0].resolve(said('Written.'))
  await tick()
  assert.equal(state.slots.story.answer.text, 'Written.')
  comments.ask(['story'], 'open')
  comments.ask(['story'], 'refresh')
  assert.equal(calls.length, 2)
})

test('with the tab hidden nothing is asked: the question waits for the tab, once, and an opening outranks a refresh', async () => {
  const { calls, state, comments } = commentHarness()
  state.hidden = true
  comments.ask(['story'], 'refresh')
  comments.ask(['story'], 'open')
  comments.ask(['story'], 'refresh')
  assert.equal(calls.length, 0)
  state.hidden = false
  comments.resume()
  assert.deepEqual(calls.map((c) => c.moment), ['open'])
  comments.resume()
  assert.equal(calls.length, 1)
})

test('what is being written is looked at again, a few times, and costs no new request meanwhile', async () => {
  const { calls, state, comments, timers } = commentHarness()
  comments.ask(['story'], 'open')
  calls[0].resolve(none('pending'))
  await tick()
  assert.equal(state.slots.story.answer.state, 'pending')
  assert.equal(calls.length, 1, 'not at once')
  timers.shift()()
  assert.equal(calls[1].moment, 'open')
  calls[1].resolve(said('Done.'))
  await tick()
  assert.equal(state.slots.story.answer.text, 'Done.')
  assert.equal(timers.length, 0)
})

test('what is being written leaves the text on screen, and it stops looking after a while', async () => {
  const { calls, state, comments, timers } = commentHarness()
  comments.ask(['story'], 'open')
  calls[0].resolve(said('Old.'))
  await tick()
  comments.ask(['story'], 'refresh')
  for (let i = 0; i <= PENDING_TRIES; i++) {
    calls.at(-1).resolve(none('pending'))
    await tick()
    assert.equal(state.slots.story.answer.text, 'Old.')
    if (i < PENDING_TRIES) timers.shift()()
  }
  assert.equal(timers.length, 0, 'it stops')
})

test('hidden meanwhile, what is being written waits for the tab', async () => {
  const { calls, state, comments, timers } = commentHarness()
  comments.ask(['story'], 'open')
  calls[0].resolve(none('pending'))
  await tick()
  state.hidden = true
  timers.shift()()
  assert.equal(calls.length, 1)
  state.hidden = false
  comments.resume()
  assert.equal(calls.length, 2)
})

test('stop leaves nothing asking and nothing to come', async () => {
  const { calls, state, comments, timers } = commentHarness()
  comments.ask(['story'], 'open')
  calls[0].resolve(none('pending'))
  await tick()
  comments.ask(['story'], 'open')
  comments.stop()
  assert.equal(calls[1].aborted, true)
  assert.deepEqual(timers.filter(Boolean).length, 0)
  state.hidden = true
  comments.ask(['story'], 'open')
  comments.stop()
  state.hidden = false
  comments.resume()
  assert.equal(calls.length, 2)
})

test('each comment has its own request and its own slot: another one is not drawn again', async () => {
  const { calls, state, comments } = commentHarness(['one', 'two'])
  comments.ask(['one', 'two'], 'open')
  assert.deepEqual(calls.map((c) => c.tile), ['one', 'two'])
  calls[0].resolve(said('First.'))
  await tick()
  const first = state.slots.one
  calls[1].resolve(said('Second.'))
  await tick()
  assert.equal(state.slots.one, first)
  assert.equal(state.slots.two.answer.text, 'Second.')
})

test('the words of a comment are never markup: no v-html on the way to the screen', () => {
  for (const file of ['../components/dashboard/TileComment.vue', '../components/dashboard/DashboardTile.vue', '../components/report/designer/RdCommentProps.vue']) {
    assert.ok(!readFileSync(new URL(file, import.meta.url), 'utf8').includes('v-html'), file)
  }
})

test('the viewer never says a moment the gateway keeps for documents', () => {
  const page = readFileSync(new URL('../pages/dashboard.vue', import.meta.url), 'utf8')
  assert.ok(!page.includes("'document'"))
})
