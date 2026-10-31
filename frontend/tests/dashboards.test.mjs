// Dashboards without a browser: what a new one starts from, where a search result leads, the grid
// at each width, which answer counts for a tile, what stays after a failure, when to read again,
// the parameters in the address, a report's style on any theme, and every chart kind's option.
// The modules are the app's own TypeScript, loaded through alias-hooks.mjs (`~/…`).
// Run: npm test
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { register } from 'node:module'

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
