// The data grid's arithmetic (composables/previewGrid.ts): how values and types are written,
// what a step changed, the one sentence about how many rows there are, which rows a scrolled
// grid draws, the memory of the previews seen.
// Run: npm test (Node strips the types of the modules it imports).
import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  Recent, ariaRowCount, columnChanges, consumedColumns, exactText, gridColumnFormatter, gridColumnFormatters,
  groupsThousands, isNumericDtype, nodeRowsPhrase, previewKey, rowsPhrase, shortDtype, suggestSample, toTsv, visibleWindow,
} from '../composables/previewGrid.ts'
import { opSubject } from '../composables/opSummary.ts'

// a stand-in for vue-i18n's t with the English texts
const EN = {
  'dataGrid.rows': '{n} rows', 'dataGrid.first': 'First {shown} rows', 'dataGrid.firstOf': 'First {shown} of {n} rows',
  'dataGrid.firstOfMore': 'First {shown} of more than {n} rows', 'dataGrid.moreThan': 'more than {n} rows',
  'dataGrid.firstShort': 'first {shown} rows',
}
const t = (k, p = {}) => EN[k].replace(/\{(\w+)\}/g, (_, x) => String(p[x]))
const fmt = (n) => n.toLocaleString('en')

test('a type is named short, the engine name kept for the tooltip', () => {
  assert.equal(shortDtype("Datetime(time_unit='us', time_zone=None)"), 'Datetime(us)')
  assert.equal(shortDtype("Datetime(time_unit='ns', time_zone='Europe/Rome')"), 'Datetime(ns, Europe/Rome)')
  assert.equal(shortDtype('Decimal(precision=38, scale=2)'), 'Decimal(38, 2)')
  assert.equal(shortDtype("Duration(time_unit='ms')"), 'Duration(ms)')
  assert.equal(shortDtype("List(Datetime(time_unit='us', time_zone=None))"), 'List(Datetime(us))')
  assert.equal(shortDtype("Struct({'a': Int64})"), 'Struct')
  assert.equal(shortDtype('Float64'), 'Float64')
  assert.equal(shortDtype(''), '')
})

test('numeric types are told by their name', () => {
  for (const d of ['Int64', 'UInt8', 'Float32', 'Float64', 'Decimal(precision=10, scale=2)']) assert.ok(isNumericDtype(d), d)
  for (const d of ['String', 'Date', 'Boolean', 'List(Int64)', 'Interval']) assert.ok(!isNumericDtype(d), d)
})

test('a column of floats keeps the same decimals on every row, up to four', () => {
  const f = gridColumnFormatter([60.163166835000006, 6.5, 12], 'en')
  assert.equal(f(60.163166835000006), '60.1632')
  assert.equal(f(6.5), '6.5000')
  assert.equal(f(12), '12.0000')
  const g = gridColumnFormatter([6.5, 6.25, 1], 'en')
  assert.deepEqual([g(6.5), g(6.25), g(1)], ['6.50', '6.25', '1.00'])
  // the language's separator, no thousands grouping lost on decimals
  assert.equal(gridColumnFormatter([1234.5], 'it')(1234.5), '1234,5'.replace('1234', '1234'))
  // a tiny value written with an exponent is no «-7 decimals»
  assert.equal(gridColumnFormatter([1e-7], 'en')(1e-7), '0.0000')
})

test('a column of integers is grouped only when it is a quantity: codes and periods stay readable', () => {
  const f = gridColumnFormatter([202607, 42007616], 'en')
  assert.equal(f(202607), '202607')
  assert.equal(f(42007616), '42007616')
  assert.equal(f(null), '')
  assert.equal(f('n/a'), 'n/a')
  const q = gridColumnFormatter([5629, 42007616], 'en', { group: true })
  assert.deepEqual([q(5629), q(42007616)], ['5,629', '42,007,616'])
  // the language's own separator, the same as every count of the interface
  assert.equal(gridColumnFormatter([42007616], 'it', { group: true })(42007616), '42.007.616')
})

test('an integer column is a quantity unless its name says identifier, code, year or period', () => {
  for (const n of ['id', 'ID', 'cliente_id', 'id_ordine', 'clienteId', 'agente_id', 'cap', 'zip', 'anno', 'year',
    'codice', 'codice_articolo', 'product_code', 'cod_fiscale', 'partita_iva', 'periodo', 'yyyymm', 'telefono', 'ean']) {
    assert.equal(groupsThousands(n), false, n)
  }
  for (const n of ['ordini', 'clienti', 'quantita', 'rows_in', 'valore', 'n_ordini', 'idle_seconds', 'capitale', 'anni_cliente', 'codes_count_total']) {
    assert.equal(groupsThousands(n), true, n)
  }
  // the grid's formatters follow the name
  const fs = gridColumnFormatters(
    [{ name: 'cliente_id', dtype: 'Int64' }, { name: 'clienti', dtype: 'Int64' }, { name: 'anno', dtype: 'Int32' }],
    [{ cliente_id: 1048576, clienti: 11123, anno: 2026 }], 'en')
  assert.deepEqual([fs.cliente_id(1048576), fs.clienti(11123), fs.anno(2026)], ['1048576', '11,123', '2026'])
})

test('a column of sums keeps two decimals at most, the exact value apart', () => {
  const f = gridColumnFormatter([281667430.9443, 559355154.3117, 12.5], 'en')
  assert.deepEqual([f(281667430.9443), f(12.5)], ['281,667,430.94', '12.50'])
  // small values keep their four
  assert.equal(gridColumnFormatter([0.12345, 3.5], 'en')(0.12345), '0.1235')
  assert.equal(exactText(281667430.9443), '281667430.9443')
})

test('the formatters of a result: numeric columns aligned, the others as they are', () => {
  const fs = gridColumnFormatters([{ name: 'v', dtype: 'Float64' }, { name: 's', dtype: 'String' }], [{ v: 1.5, s: 'x' }, { v: 2, s: { a: 1 } }], 'en')
  assert.equal(fs.v(2), '2.0')
  assert.equal(fs.s({ a: 1 }), '{"a":1}')
})

test('the exact value is what a copy takes, tab-separated as a spreadsheet pastes it', () => {
  assert.equal(exactText(60.163166835000006), '60.163166835000006')
  assert.equal(exactText(null), '')
  assert.equal(toTsv([[1, 'a b'], [60.163166835000006, null]]), '1\ta b\n60.163166835000006\t')
  assert.equal(toTsv([['tab\there', 'say "hi"']]), '"tab\there"\t"say ""hi"""')
})

test('what a step changed: new, renamed, retyped columns and the ones it removed', () => {
  const up = [{ name: 'id', dtype: 'Int64' }, { name: 'price', dtype: 'String' }, { name: 'note', dtype: 'String' }, { name: 'qty', dtype: 'Int64' }]
  const now = [{ name: 'row_id', dtype: 'Int64' }, { name: 'price', dtype: 'Float64' }, { name: 'qty', dtype: 'Int64' }, { name: 'total', dtype: 'Float64' }]
  const ch = columnChanges(up, now, { id: 'row_id' })
  assert.deepEqual(ch.marks, {
    row_id: { kind: 'renamed', from: 'id' },
    price: { kind: 'retyped', from: 'String' },
    total: { kind: 'new' },
  })
  assert.deepEqual(ch.removed, ['note'])
  // without the rename's mapping a renamed column is one removed and one new
  assert.deepEqual(columnChanges(up, now).removed, ['id', 'note'])
  // no input known (a source): nothing to say
  assert.deepEqual(columnChanges(null, now), { marks: {}, removed: [] })
  assert.deepEqual(columnChanges([], now), { marks: {}, removed: [] })
})

test('a pivot spreads a column and its values: they are reshaped, not removed', () => {
  const up = [{ name: 'regione', dtype: 'String' }, { name: 'segmento', dtype: 'String' }, { name: 'valore', dtype: 'Float64' }, { name: 'clienti', dtype: 'Int64' }]
  const now = [{ name: 'regione', dtype: 'String' }, { name: 'Champion', dtype: 'Float64' }, { name: 'Loyal', dtype: 'Float64' }]
  const params = { index: ['regione'], on: ['segmento'], values: 'valore', func: 'sum' }
  assert.deepEqual(consumedColumns('pivot', params), ['segmento', 'valore'])
  const ch = columnChanges(up, now, {}, consumedColumns('pivot', params))
  assert.deepEqual(ch.removed, ['clienti'])
  assert.deepEqual(Object.keys(ch.marks), ['Champion', 'Loyal'])
  assert.deepEqual(consumedColumns('unpivot', { index: ['id'], on: ['q1', 'q2'] }), ['q1', 'q2'])
  assert.deepEqual(consumedColumns('group_by', { by: ['regione'] }), [])
})

test('a node is named by what it works on, not only by its operation', () => {
  assert.equal(opSubject('group_by', { by: ['regione', 'segmento'], aggregations: [{ column: 'valore', func: 'sum' }] }), 'regione, segmento')
  assert.equal(opSubject('join', { how: 'left', on: ['ordine_id'] }), 'ordine_id')
  assert.equal(opSubject('join', { how: 'inner', left_on: ['id'], right_on: ['cliente_id'] }), 'id')
  assert.equal(opSubject('filter', { column: 'stato', operator: 'eq', value: 'evaso' }), 'stato = evaso')
  assert.equal(opSubject('group_by', {}), '')
})

test('aria-rowcount is the real total when it is known, -1 when it is not', () => {
  assert.equal(ariaRowCount(30, false, { state: 'none' }), 31)
  assert.equal(ariaRowCount(100, true, { state: 'done', rows: 42007616, over: false }), 42007617)
  assert.equal(ariaRowCount(100, true, { state: 'done', rows: 10000000, over: true }), -1)
  assert.equal(ariaRowCount(100, true, { state: 'counting' }), -1)
  assert.equal(ariaRowCount(100, true, { state: 'none' }), -1)
})

test('one way to say how many rows: all of them, the first of how many, or of more than the cap', () => {
  assert.equal(rowsPhrase(15, false, { state: 'none' }, t, fmt), '15 rows')
  assert.equal(rowsPhrase(100, true, { state: 'none' }, t, fmt), 'First 100 rows')
  assert.equal(rowsPhrase(100, true, { state: 'counting' }, t, fmt), 'First 100 rows')
  assert.equal(rowsPhrase(100, true, { state: 'done', rows: 764, over: false }, t, fmt), 'First 100 of 764 rows')
  assert.equal(rowsPhrase(100, true, { state: 'done', rows: 10_000_000, over: true }, t, fmt), 'First 100 of more than 10,000,000 rows')
  assert.equal(rowsPhrase(100, true, { state: 'failed', slow: true }, t, fmt), 'First 100 rows')
  // the same words, short, on a node of the canvas
  assert.equal(nodeRowsPhrase(15, false, { state: 'none' }, t, fmt), '15 rows')
  assert.equal(nodeRowsPhrase(100, true, { state: 'done', rows: 42_007_616, over: false }, t, fmt), '42,007,616 rows')
  assert.equal(nodeRowsPhrase(100, true, { state: 'done', rows: 10_000_000, over: true }, t, fmt), 'more than 10,000,000 rows')
  assert.equal(nodeRowsPhrase(100, true, { state: 'none' }, t, fmt), 'first 100 rows')
})

test('a sample is suggested for a big source, never for the time alone nor a small one', () => {
  // the source's rows decide: a Group by's 30 rows over 42 million still cost the 42 million
  assert.equal(suggestSample(42_007_616, { state: 'done', rows: 30, over: false }), true)
  assert.equal(suggestSample(764, { state: 'done', rows: 764, over: false }), false)
  assert.equal(suggestSample(150_000, { state: 'failed', slow: true }), false)
  // the source's rows not known: the step's own count
  assert.equal(suggestSample(null, { state: 'done', rows: 764, over: false }), false)
  assert.equal(suggestSample(null, { state: 'done', rows: 2_000_000, over: false }), true)
  assert.equal(suggestSample(undefined, { state: 'done', rows: 10_000_000, over: true }), true)
  assert.equal(suggestSample(null, { state: 'none' }), false)
  assert.equal(suggestSample(null, { state: 'failed', slow: true }), false)
})

test('a scrolled grid draws the rows in view and a margin, moving in steps', () => {
  assert.deepEqual(visibleWindow(0, 260, 26, 500), { start: 0, end: 34 })
  const mid = visibleWindow(26 * 200, 260, 26, 500)
  assert.ok(mid.start <= 200 - 8 && mid.start % 8 === 0, JSON.stringify(mid))
  assert.ok(mid.end >= 210 && mid.end <= 500)
  // a few pixels more do not change what is drawn
  assert.deepEqual(visibleWindow(26 * 200 + 5, 260, 26, 500), mid)
  const end = visibleWindow(26 * 499, 260, 26, 500)
  assert.equal(end.end, 500)
  assert.deepEqual(visibleWindow(0, 260, 26, 0), { start: 0, end: 0 })
  assert.deepEqual(visibleWindow(-50, 260, 26, 10), { start: 0, end: 10 })
})

test('the previews seen are kept up to a size, the least recently used forgotten first', () => {
  const r = new Recent(2)
  r.set('a', 1); r.set('b', 2); r.get('a'); r.set('c', 3)
  assert.equal(r.get('b'), undefined)
  assert.equal(r.get('a'), 1)
  assert.equal(r.get('c'), 3)
  assert.equal(r.length, 2)
})

test('a preview is the same when engine, source, chain and limit are', () => {
  const body = { bucket: 'b', input_key: 'k', operations: [{ type: 'filter', params: { column: 'x' } }], limit: 100 }
  assert.equal(previewKey({ ...body, engine: 'duckdb' }), previewKey({ ...body, engine: 'duckdb' }))
  assert.notEqual(previewKey({ ...body, engine: 'duckdb' }), previewKey({ ...body, engine: 'polars' }))
  assert.notEqual(previewKey({ ...body, engine: 'duckdb' }), previewKey({ ...body, engine: 'duckdb', limit: 500 }))
  assert.notEqual(previewKey(body), previewKey({ ...body, operations: [] }))
})

// ── round 4: the keyboard and the column profile ────────────────────────────────
test('the arrows, Home, End and the page keys move one active cell, the header one row up', async () => {
  const { moveCell } = await import('../composables/previewGrid.ts')
  const at = (r, c) => ({ r, c })
  // 100 rows, 5 columns, 10 rows a page
  const m = (pos, key, ctrl = false) => moveCell(pos, key, ctrl, 100, 5, 10)
  assert.deepEqual(m(at(0, 0), 'ArrowRight'), at(0, 1))
  assert.deepEqual(m(at(0, 4), 'ArrowRight'), at(0, 4)) // the last column stays
  assert.deepEqual(m(at(0, 0), 'ArrowLeft'), at(0, 0))
  assert.deepEqual(m(at(0, 2), 'ArrowUp'), at(-1, 2)) // to the column's header
  assert.deepEqual(m(at(-1, 2), 'ArrowUp'), at(-1, 2))
  assert.deepEqual(m(at(-1, 2), 'ArrowDown'), at(0, 2))
  assert.deepEqual(m(at(99, 1), 'ArrowDown'), at(99, 1))
  assert.deepEqual(m(at(7, 3), 'Home'), at(7, 0))
  assert.deepEqual(m(at(7, 1), 'End'), at(7, 4))
  assert.deepEqual(m(at(7, 3), 'Home', true), at(0, 0)) // the first CELL, not the header
  assert.deepEqual(m(at(7, 1), 'End', true), at(99, 4))
  // Ctrl or ⌘ with ↑ ↓: the column's ends, the way of a keyboard with no Home nor End
  assert.deepEqual(m(at(7, 3), 'ArrowUp', true), at(0, 3))
  assert.deepEqual(m(at(7, 3), 'ArrowDown', true), at(99, 3))
  assert.deepEqual(m(at(5, 1), 'PageDown'), at(15, 1))
  assert.deepEqual(m(at(95, 1), 'PageDown'), at(99, 1))
  assert.deepEqual(m(at(-1, 1), 'PageDown'), at(9, 1))
  assert.deepEqual(m(at(5, 1), 'PageUp'), at(0, 1)) // stops at the first row
  assert.deepEqual(m(at(0, 1), 'PageUp'), at(0, 1))
  assert.equal(m(at(0, 0), 'Enter'), null)
  assert.equal(m(at(0, 0), 'a'), null)
  // a result with no rows: only the header
  assert.deepEqual(moveCell(at(-1, 1), 'ArrowDown', false, 0, 3, 10), at(-1, 1))
  assert.deepEqual(moveCell(at(-1, 1), 'End', true, 0, 3, 10), at(-1, 2))
  assert.deepEqual(moveCell(at(-1, 1), 'Home', true, 0, 3, 10), at(-1, 0))
  assert.equal(moveCell(at(0, 0), 'ArrowDown', false, 10, 0, 10), null)
})

test('a share of nulls is never rounded into none or all', async () => {
  const { shareText } = await import('../composables/previewGrid.ts')
  assert.equal(shareText(0, 1000, 'en'), '0%')
  assert.equal(shareText(1, 42_007_616, 'en'), '<0.1%')
  assert.equal(shareText(24, 1000, 'en'), '2.4%')
  assert.equal(shareText(120, 1000, 'en'), '12%')
  assert.equal(shareText(995, 1000, 'en'), '99.5%')
  assert.equal(shareText(42_007_615, 42_007_616, 'en'), '>99.9%')
  assert.equal(shareText(1000, 1000, 'en'), '100%')
  assert.equal(shareText(0, 0, 'en'), '0%')
  // in the interface's language
  assert.equal(shareText(24, 1000, 'it'), '2,4%')
  assert.match(shareText(120, 1000, 'de'), /^12\s%$/)
})

test('the null bar is empty for none, a pixel at least for a few, full for all', async () => {
  const { nullBarWidth } = await import('../composables/previewGrid.ts')
  assert.equal(nullBarWidth(0, 100), '0%')
  assert.equal(nullBarWidth(5, 0), '0%')
  assert.equal(nullBarWidth(1, 42_007_616), 'max(1px, 0.00%)')
  assert.equal(nullBarWidth(12, 100), 'max(1px, 12.00%)')
  assert.equal(nullBarWidth(100, 100), '100%')
})

// ── round 5: an honest profile, a profile to act on ─────────────────────────────
const EN5 = {
  ...EN,
  'dataGrid.profileAllRows': 'The one row | All {n} rows',
}
const t5 = (k, p = {}, plural) => {
  const forms = EN5[k].split(' | ')
  const msg = forms.length > 1 ? forms[plural === 1 ? 0 : 1] : forms[0]
  return msg.replace(/\{(\w+)\}/g, (_, x) => String(p[x]))
}

test('a profile says once which rows its figures are of, the total from the count first', async () => {
  const { profileBasisText } = await import('../composables/previewGrid.ts')
  const fmt = (n) => n.toLocaleString('en')
  const none = { state: 'none' }
  assert.equal(profileBasisText({ rows: 764, sampled: false, total: 764 }, none, t5, fmt), 'All 764 rows')
  assert.equal(profileBasisText({ rows: 1, sampled: false, total: 1 }, none, t5, fmt), 'The one row')
  // the first 100,000: of the count when the page has it, of the footer's total otherwise
  assert.equal(profileBasisText({ rows: 100_000, sampled: true, total: null }, { state: 'done', rows: 42_007_616, over: false }, t5, fmt),
    'First 100,000 of 42,007,616 rows')
  assert.equal(profileBasisText({ rows: 100_000, sampled: true, total: 60_000_000 }, none, t5, fmt), 'First 100,000 of 60,000,000 rows')
  assert.equal(profileBasisText({ rows: 100_000, sampled: true, total: null }, { state: 'done', rows: 10_000_000, over: true }, t5, fmt),
    'First 100,000 of more than 10,000,000 rows')
  assert.equal(profileBasisText({ rows: 100_000, sampled: true, total: null }, { state: 'failed', slow: true }, t5, fmt), 'First 100,000 rows')
})

test('a column (almost) all different is likely a key, and asks nothing more', async () => {
  const { columnDetail, likelyKey } = await import('../composables/previewGrid.ts')
  const st = (o) => ({ kind: 'number', nulls: 0, of: 100_000, distinct: 100_000, min: 1, max: 100_000, ...o })
  // riga_id: 100,000 distinct of the first 100,000 rows
  assert.equal(columnDetail(st({}), 'Int64', 100_000), 'key')
  // 99% of the rows that hold a value (the nulls left out)
  assert.equal(likelyKey(st({ nulls: 50_000, distinct: 49_600 }), 'Int64', 100_000), true)
  assert.equal(likelyKey(st({ distinct: 98_000 }), 'Int64', 100_000), false)
  // a measure in decimals is all different too, and no key: its histogram
  assert.equal(columnDetail(st({ kind: 'number', distinct: 99_997, min: 0.5, max: 99.7 }), 'Float64', 100_000), 'histogram')
  // a text key: an identifier; a handful of rows tells nothing
  assert.equal(columnDetail({ kind: 'text', nulls: 0, of: 1000, distinct: 1000 }, 'String', 1000), 'key')
  assert.equal(columnDetail({ kind: 'text', nulls: 0, of: 5, distinct: 5 }, 'String', 5), 'top')
  // a category: the most frequent values; a number with few values too
  assert.equal(columnDetail({ kind: 'text', nulls: 0, of: 100_000, distinct: 4 }, 'String', 100_000), 'top')
  assert.equal(columnDetail(st({ distinct: 12, min: 1, max: 12 }), 'Int64', 100_000), 'top')
  assert.equal(columnDetail(st({ distinct: 30, min: 1, max: 30 }), 'Int32', 100_000), 'top')
  // a number with one value only, a boolean, a list
  assert.equal(columnDetail(st({ distinct: 1, min: 3, max: 3 }), 'Int64', 100_000), 'top')
  assert.equal(columnDetail({ kind: 'bool', nulls: 0, of: 10, distinct: 2 }, 'Boolean', 10), 'top')
  assert.equal(columnDetail({ kind: 'other', nulls: 0, of: 10 }, 'List(String)', 10), 'none')
})

test('a histogram has 20 equal intervals, fewer for a short range of whole numbers', async () => {
  const { histogramBins, histogramEdges, histogramHeights } = await import('../composables/previewGrid.ts')
  assert.equal(histogramBins(0, 1000.5, false), 20)
  assert.equal(histogramBins(1, 7, true), 7) // one bar a value
  assert.equal(histogramBins(1, 1000, true), 20)
  assert.equal(histogramBins(5, 5, true), 1)
  // the whole numbers each bar holds, as the server's floor((x - lo) / width) puts them
  assert.deepEqual(histogramEdges(1, 7, 7, true).map((e) => [e.from, e.to]), [[1, 1], [2, 2], [3, 3], [4, 4], [5, 5], [6, 6], [7, 7]])
  assert.deepEqual(histogramEdges(0, 100, 20, true).slice(0, 2).map((e) => [e.from, e.to]), [[0, 4], [5, 9]])
  assert.deepEqual(histogramEdges(0, 100, 20, true).at(-1), { from: 95, to: 100 })
  assert.deepEqual(histogramEdges(0, 1, 4).map((e) => e.to), [0.25, 0.5, 0.75, 1])
  // heights in percent of the tallest, a bar with rows never drawn as none
  assert.deepEqual(histogramHeights([0, 1, 500, 1000]), [0, 2, 50, 100])
  assert.deepEqual(histogramHeights([0, 0]), [0, 0])
})

test('the arrows move a column profile to the column beside, and only there', async () => {
  const { profileStep } = await import('../composables/previewGrid.ts')
  assert.equal(profileStep('ArrowRight', {}, 2, 5), 3)
  assert.equal(profileStep('ArrowLeft', {}, 2, 5), 1)
  assert.equal(profileStep('ArrowLeft', {}, 0, 5), null) // no column before the first
  assert.equal(profileStep('ArrowRight', {}, 4, 5), null)
  assert.equal(profileStep('ArrowDown', {}, 2, 5), null)
  assert.equal(profileStep('ArrowRight', { ctrl: true }, 2, 5), null)
  assert.equal(profileStep('ArrowRight', { shift: true }, 2, 5), null)
})

test('the column profile takes the focus itself, describes its figures, and closes on a click outside', async () => {
  const { readFileSync } = await import('node:fs')
  const grid = readFileSync(new URL('../components/ui/DataGrid.vue', import.meta.url), 'utf8')
  const pop = grid.slice(grid.indexOf('class="dg-pop"') - 200, grid.indexOf('class="dg-pop"') + 600)
  // the panel itself (not «Copy name»: a second Enter would copy) and its figures described
  assert.match(pop, /tabindex="-1"/)
  assert.match(pop, /:aria-describedby=/)
  assert.match(pop, /@keydown="onPopKey"/)
  assert.match(grid, /usePopover\(popOpen, popEl,[\s\S]*?focus: 'panel'/)
  const popover = readFileSync(new URL('../composables/usePopover.ts', import.meta.url), 'utf8')
  // a press anywhere outside closes, even where nothing takes the focus
  assert.match(popover, /addEventListener\('pointerdown', onPointerDown, true\)/)
  assert.match(popover, /removeEventListener\('pointerdown', onPointerDown, true\)/)
})

test('the summary bar gives way in order: the sample into a menu, the cache to its icon, then «−N»', async () => {
  const { readFileSync } = await import('node:fs')
  const editor = readFileSync(new URL('../components/FlowEditor.vue', import.meta.url), 'utf8')
  const queries = [...editor.matchAll(/@container pvbar \(max-width: (\d+)px\) \{([\s\S]*?)\n\}/g)].map((m) => [Number(m[1]), m[2]])
  const at = (what) => queries.find(([, body]) => body.includes(what))?.[0]
  const sample = at('.pv-sample-btn { display: none; }')
  const cache = at('.pv-cache .pv-long { display: none; }')
  const removed = at('.pv-removed .pv-short { display: inline; }')
  assert.ok(sample && cache && removed, 'the three steps are there')
  // the wider the threshold, the sooner the part gives way
  assert.ok(sample > cache && cache > removed, `${sample} > ${cache} > ${removed}`)
  // last, in a 1024px window: the tabs keep their icons, the count its short form
  const narrow = at('.pv-rows .pv-short { display: inline; }')
  assert.ok(narrow && narrow < removed && at('.viewtabs .vt-label { display: none; }') === narrow)
  // no part is ever cut: a part is whole or on the hidden second line; the node keeps 14ch
  assert.match(editor, /\.pv-part \{ flex: none; \}/)
  assert.match(editor, /\.pv-summary \{[\s\S]*?flex-wrap: wrap;[\s\S]*?overflow: hidden;/)
  assert.match(editor, /\.pv-node \{[^}]*min-width: 14ch;/)
  // and no place kept for «N removed» when there is none
  assert.doesNotMatch(editor, /pv-reserve/)
})


test('a Fill nulls step added from a profile, its value not written yet, asks to be completed', async () => {
  const { resolveChain } = await import('../composables/useFlowModel.ts')
  const src = { id: 's', type: 'source', data: { bucket: 'b', parquetKey: 'k' } }
  const fill = (columns) => ({ id: 'f', type: 'operation', data: { opType: 'fill_null', params: { columns } } })
  const edges = [{ id: 'e', source: 's', target: 'f', targetHandle: 'left' }]
  const ops = (columns) => resolveChain([src, fill(columns)], edges, 'f').operations.at(-1)
  // no value yet: no `columns`, so the engine says a parameter is missing («complete the node»)
  assert.deepEqual(ops({ citta: null }), { type: 'fill_null', params: {} })
  // the values written are kept, the others left out
  assert.deepEqual(ops({ citta: 'n/a', cap: null }), { type: 'fill_null', params: { columns: { citta: 'n/a' } } })
  assert.deepEqual(ops({ citta: '' }), { type: 'fill_null', params: { columns: { citta: '' } } })
})

test('the Viewer offers a value as a filter only on the datasource\'s own columns', async () => {
  const { readFileSync } = await import('node:fs')
  const viewer = readFileSync(new URL('../pages/viewer.vue', import.meta.url), 'utf8')
  const grid = readFileSync(new URL('../components/ui/DataGrid.vue', import.meta.url), 'utf8')
  // the filters come before the computed fields: a computed field cannot be filtered on
  // (the same columns the filters' own pickers offer, and a field dragged onto the filters)
  assert.match(viewer, /const filterCols = computed\(\(\) => fields\.value\.filter\(\(f\) => !f\.computed\)/)
  assert.match(viewer, /const valueActions = computed\(\(\) => \{[\s\S]*?columns: filterCols\.value/)
  assert.match(grid, /!a\.columns \|\| a\.columns\.includes\(c\.name\)/)
  assert.doesNotMatch(grid, /v-if="valueActions"/)
})

test('a column\'s values are asked again when the profile moves to another column of the same kind', async () => {
  const { readFileSync } = await import('node:fs')
  const grid = readFileSync(new URL('../components/ui/DataGrid.vue', import.meta.url), 'utf8')
  // the watch that asks a column's values is keyed by the column, not by the kind alone
  assert.match(grid, /watch\(\(\) => \(popColumn\.value && popKind\.value \? `\$\{popColumn\.value\.name\}/)
  assert.doesNotMatch(grid, /watch\(popKind,/)
})

test('a popover opened without the focus on its button still closes at a press outside', async () => {
  const { readFileSync } = await import('node:fs')
  const popover = readFileSync(new URL('../composables/usePopover.ts', import.meta.url), 'utf8')
  // the page's body is never taken for the opener (it contains every press)
  assert.match(popover, /origin !== document\.body && origin !== document\.documentElement/)
})
