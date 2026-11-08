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

test('a sample is suggested past a million rows or a slow preview, never for a small step', () => {
  assert.equal(suggestSample({ state: 'done', rows: 764, over: false }, 300), false)
  assert.equal(suggestSample({ state: 'done', rows: 2_000_000, over: false }, 300), true)
  assert.equal(suggestSample({ state: 'done', rows: 10_000_000, over: true }, 300), true)
  assert.equal(suggestSample({ state: 'none' }, 6_000), true)
  assert.equal(suggestSample({ state: 'failed', slow: true }, 300), true)
  assert.equal(suggestSample({ state: 'failed', slow: false }, 300), false)
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
  assert.deepEqual(m(at(7, 3), 'Home', true), at(-1, 0))
  assert.deepEqual(m(at(7, 1), 'End', true), at(99, 4))
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

test('a profile says which of its figures are of every row', async () => {
  const { profileBasis } = await import('../composables/previewGrid.ts')
  assert.equal(profileBasis(false, []), 'all')
  assert.equal(profileBasis(true, []), 'sample')
  assert.equal(profileBasis(true, ['nulls']), 'nulls')
  assert.equal(profileBasis(true, ['nulls', 'min', 'max']), 'range')
})
