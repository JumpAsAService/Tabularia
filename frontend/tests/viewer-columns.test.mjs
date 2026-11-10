// The Viewer's table (composables/viewerColumns.ts): it starts empty and is built on three
// areas, Rows, Columns and Measures. The list of fields, what the areas make (the rows as
// they are, a group by, totals, a cross table), where a dropped field lands, which request
// a change needs (none at all while the table is empty), and how a saved view of before
// opens. Run: npm test.
import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  baseKeyOf, chartSlotOf, countNeeded, insertionSlot, keepKnown,
  profileMissing, rowsPlan, sameSlot, selectStep, shiftColumn, shownColumns, viewerFields,
  defaultFunc, keepKnownShelves, measureAliases, placeOnShelf, fieldOffShelves, removeFromShelf, shelfOps, shelvesOfSpec,
  specOfShelves, tableMode,
} from '../composables/viewerColumns.ts'

const COLS = [{ name: 'id', dtype: 'Int64' }, { name: 'canale', dtype: 'String', description: 'from the source' }, { name: 'data', dtype: 'Date' }]

test('the fields: the columns with their descriptions, then the computed fields', () => {
  const f = viewerFields(COLS, { canale: 'Sales channel', id: '' }, [{ name: 'margin', expr: 'a - b' }, { name: ' ', expr: '1' }, { name: 'x', expr: '' }], { margin: 'Float64' })
  assert.deepEqual(f.map((x) => x.name), ['id', 'canale', 'data', 'margin'])
  assert.equal(f[1].description, 'Sales channel')
  assert.equal(f[0].description, '')
  assert.deepEqual(f[3], { name: 'margin', dtype: 'Float64', description: 'a - b', computed: true })
  // the source's own description when nobody curated one
  assert.equal(viewerFields(COLS, {}, [])[1].description, 'from the source')
})

test('a computed field named like a column replaces it in its place', () => {
  const f = viewerFields(COLS, {}, [{ name: 'canale', expr: 'UPPER(canale)' }])
  assert.deepEqual(f.map((x) => [x.name, x.computed, x.dtype]), [['id', false, 'Int64'], ['canale', true, ''], ['data', false, 'Date']])
})

test('the keyboard moves a column one place, stopping at the ends; a drop on its own slot moves nothing', () => {
  const t = ['a', 'b', 'c']
  assert.equal(sameSlot(t, 'b', 1), true)
  assert.equal(sameSlot(t, 'b', 2), true)
  assert.equal(sameSlot(t, 'b', 3), false)
  assert.equal(sameSlot(t, 'z', 0), false)
  assert.deepEqual(keepKnown(['gone', 'a', 'a', 7, 'c'], t), ['a', 'c'])
  assert.deepEqual(keepKnown('a', t), [])
  assert.deepEqual(shiftColumn(t, 'a', 1), ['b', 'a', 'c'])
  assert.deepEqual(shiftColumn(t, 'c', -1), ['a', 'c', 'b'])
  assert.equal(shiftColumn(t, 'a', -1), t)
  assert.equal(shiftColumn(t, 'c', 1), t)
  assert.equal(shiftColumn(t, 'z', 1), t)
})

test('the slot under the pointer: before the header whose middle is past it', () => {
  const edges = [{ left: 40, right: 140 }, { left: 140, right: 200 }, { left: 200, right: 400 }]
  assert.equal(insertionSlot(edges, 0), 0)
  assert.equal(insertionSlot(edges, 89), 0)
  assert.equal(insertionSlot(edges, 91), 1)
  assert.equal(insertionSlot(edges, 171), 2)
  assert.equal(insertionSlot(edges, 301), 3)
  assert.equal(insertionSlot(edges, 9999), 3)
  assert.equal(insertionSlot([], 10), 0)
})

test('the engine is asked the shown columns only, in their order', () => {
  assert.deepEqual(selectStep(['b', 'a']), { type: 'select', params: { columns: ['b', 'a'] } })
})

const KEY = baseKeyOf({ id: 1, key: 'k.parquet' }, 'duckdb', [])
const state = (over) => ({ chosen: [], held: [], heldKey: null, baseKey: KEY, typeless: [], ...over })

test('an empty table asks nothing, whatever happens', () => {
  assert.equal(rowsPlan(state({}), 'columns').columns, null)
  assert.equal(rowsPlan(state({}), 'apply').columns, null)
  // the rows of the columns removed are still there: still nothing
  assert.equal(rowsPlan(state({ held: ['a'], heldKey: KEY }), 'columns').columns, null)
})

test('the first field is one request for that column; the next ones ask the shown ones', () => {
  assert.deepEqual(rowsPlan(state({ chosen: ['a'] }), 'columns'), { columns: ['a'], onlyTypes: false, replaces: false })
  assert.deepEqual(rowsPlan(state({ chosen: ['b', 'a'], held: ['a'], heldKey: KEY }), 'columns'),
    { columns: ['b', 'a'], onlyTypes: false, replaces: false })
})

test('moving, removing and putting back a column the rows hold: no request', () => {
  const held = { held: ['a', 'b', 'c'], heldKey: KEY }
  assert.equal(rowsPlan(state({ chosen: ['c', 'a', 'b'], ...held }), 'columns').columns, null)
  assert.equal(rowsPlan(state({ chosen: ['a'], ...held }), 'columns').columns, null)
  assert.equal(rowsPlan(state({ chosen: ['a', 'c'], ...held }), 'columns').columns, null)
  // also while a filter is being written and not applied: the rows on screen hold the column
  assert.equal(rowsPlan(state({ chosen: ['b'], ...held, baseKey: 'other' }), 'columns').columns, null)
})

test('Apply always computes, and replaces the rows on screen', () => {
  assert.deepEqual(rowsPlan(state({ chosen: ['a'], held: ['a'], heldKey: KEY }), 'apply'),
    { columns: ['a'], onlyTypes: false, replaces: true })
  // a column added after the filters changed: the rows are of the old filters, all replaced
  assert.equal(rowsPlan(state({ chosen: ['a', 'b'], held: ['a'], heldKey: 'old' }), 'columns').replaces, true)
})

test('a computed field of unknown type is asked with the rows, and alone (one row) on Apply', () => {
  assert.deepEqual(rowsPlan(state({ chosen: ['a'], typeless: ['m'] }), 'columns').columns, ['a', 'm'])
  assert.deepEqual(rowsPlan(state({ chosen: ['m', 'a'], typeless: ['m'] }), 'columns').columns, ['m', 'a'])
  assert.deepEqual(rowsPlan(state({ typeless: ['m'] }), 'apply'), { columns: ['m'], onlyTypes: true, replaces: false })
  assert.equal(rowsPlan(state({ typeless: ['m'] }), 'columns').columns, null)
})

test('the rows are counted once per filters, not once per column', () => {
  assert.equal(countNeeded(null, KEY, 'columns'), true)
  assert.equal(countNeeded(KEY, KEY, 'columns'), false)
  assert.equal(countNeeded('old', KEY, 'columns'), true)
  assert.equal(countNeeded(KEY, KEY, 'apply'), true)
  assert.notEqual(baseKeyOf({ id: 1, key: 'k.parquet' }, 'duckdb', [{ type: 'filter', params: { column: 'a', operator: 'eq', value: 1 } }]), KEY)
  assert.notEqual(baseKeyOf({ id: 1, key: 'k.parquet' }, 'polars', []), KEY)
})

test('the profile is asked for the columns not profiled yet on these rows', () => {
  assert.deepEqual(profileMissing(['a', 'b', 'c'], ['a'], true), ['b', 'c'])
  assert.deepEqual(profileMissing(['a'], ['a', 'b'], true), [])
  assert.deepEqual(profileMissing(['a', 'b'], ['a'], false), ['a', 'b'])
})

test('the shown columns carry the type the result said, else the list\'s', () => {
  const fields = viewerFields(COLS, {}, [{ name: 'm', expr: '1' }])
  assert.deepEqual(shownColumns(['m', 'id', 'gone'], fields, { m: 'Int32' }),
    [{ name: 'm', dtype: 'Int32' }, { name: 'id', dtype: 'Int64' }, { name: 'gone', dtype: '' }])
})

test('a field sent to the chart: a number is a value, the rest the axis', () => {
  assert.equal(chartSlotOf('Decimal(38, 2)'), 'value')
  assert.equal(chartSlotOf('Int32'), 'value')
  assert.equal(chartSlotOf('String'), 'axis')
  assert.equal(chartSlotOf('Date'), 'axis')
  assert.equal(chartSlotOf(''), 'axis')
})

// ── the three areas: Rows, Columns, Measures ──
const S = (rows = [], cols = [], measures = []) => ({ rows, cols, measures })
const sum = (column) => ({ column, func: 'sum' })

test('what the areas make: the rows as they are, a group by, totals, a cross table', () => {
  assert.equal(tableMode(S()), 'empty')
  assert.equal(tableMode(S(['a'])), 'detail')
  assert.equal(tableMode(S(['a'], [], [sum('x')])), 'grouped')
  assert.equal(tableMode(S([], [], [sum('x')])), 'totals')
  assert.equal(tableMode(S(['a'], ['b'], [sum('x')])), 'cross')
  assert.equal(tableMode(S([], ['b'], [sum('x')])), 'cross')
  // Columns without a measure: nothing to spread yet, and nothing is asked
  assert.equal(tableMode(S(['a'], ['b'])), 'needs-measure')
})

test('the engine aggregates: a group by with every measure, sorted by its rows', () => {
  assert.deepEqual(shelfOps(S(['a', 'b'], [], [sum('x'), { column: 'y', func: 'n_unique' }])), [
    { type: 'group_by', params: { by: ['a', 'b'], aggregations: [{ column: 'x', func: 'sum', alias: 'sum_x' }, { column: 'y', func: 'n_unique', alias: 'n_unique_y' }] } },
    { type: 'sort', params: { by: ['a', 'b'], descending: false } },
  ])
  assert.deepEqual(shelfOps(S(['a'])), [])
  assert.deepEqual(shelfOps(S()), [])
  assert.deepEqual(shelfOps(S(['a'], ['b'])), [])
})

test('measures alone are one row of totals, over one constant group', () => {
  assert.deepEqual(shelfOps(S([], [], [sum('x'), { column: 'x', func: 'max' }])), [
    { type: 'compute', params: { columns: [{ name: '__all', expr: '1' }] } },
    { type: 'group_by', params: { by: ['__all'], aggregations: [{ column: 'x', func: 'sum', alias: 'sum_x' }, { column: 'x', func: 'max', alias: 'max_x' }] } },
    { type: 'select', params: { columns: ['sum_x', 'max_x'] } },
  ])
})

test('a cross table is the engine\'s pivot, with its one measure: the first', () => {
  assert.deepEqual(shelfOps(S(['a'], ['m'], [{ column: 'x', func: 'mean' }, sum('y')])),
    [{ type: 'pivot', params: { index: ['a'], on: ['m'], values: 'x', func: 'mean' } }])
  assert.deepEqual(shelfOps(S([], ['m'], [sum('x')])), [{ type: 'pivot', params: { index: [], on: ['m'], values: 'x', func: 'sum' } }])
})

test('a measure\'s column never takes the name of a row field or of another measure', () => {
  assert.deepEqual(measureAliases([sum('x'), sum('x')], ['sum_x']), ['sum_x_2', 'sum_x_3'])
  assert.equal(defaultFunc('Decimal(14, 2)'), 'sum')
  assert.equal(defaultFunc('String'), 'count')
  assert.equal(defaultFunc(''), 'count')
})

test('a field dropped on an area: at its slot, a number summed, a text counted', () => {
  assert.deepEqual(placeOnShelf(S(['a']), 'rows', 'b', 'String', 0), S(['b', 'a']))
  assert.deepEqual(placeOnShelf(S(['a']), 'rows', 'b', 'String'), S(['a', 'b']))
  assert.deepEqual(placeOnShelf(S(), 'measures', 'x', 'Int64'), S([], [], [sum('x')]))
  assert.deepEqual(placeOnShelf(S(), 'measures', 'c', 'String'), S([], [], [{ column: 'c', func: 'count' }]))
  // already there, added again: nothing changes (the same object)
  const s = S(['a'], [], [sum('x')])
  assert.equal(placeOnShelf(s, 'rows', 'a', 'String'), s)
  assert.equal(placeOnShelf(s, 'measures', 'x', 'Int64'), s)
})

test('a field is a row heading or a column heading, never both; chips move between areas', () => {
  assert.deepEqual(placeOnShelf(S(['a', 'b']), 'cols', 'a', 'String'), S(['b'], ['a']))
  assert.deepEqual(placeOnShelf(S(['a', 'b'], ['c']), 'rows', 'c', 'String', 1, { area: 'cols', index: 0 }), S(['a', 'c', 'b'], []))
  // within its own area the slot is counted with the chip still there
  assert.deepEqual(placeOnShelf(S(['a', 'b', 'c']), 'rows', 'a', 'String', 3, { area: 'rows', index: 0 }), S(['b', 'c', 'a']))
  assert.deepEqual(placeOnShelf(S(['a', 'b', 'c']), 'rows', 'c', 'String', 0, { area: 'rows', index: 2 }), S(['c', 'a', 'b']))
  // a row field made a measure leaves the rows; a measure keeps its function when it moves
  assert.deepEqual(placeOnShelf(S(['x']), 'measures', 'x', 'Int64', undefined, { area: 'rows', index: 0 }), S([], [], [sum('x')]))
  const two = S([], [], [{ column: 'x', func: 'max' }, sum('y')])
  assert.deepEqual(placeOnShelf(two, 'measures', 'x', 'Int64', 2, { area: 'measures', index: 0 }), S([], [], [sum('y'), { column: 'x', func: 'max' }]))
  assert.deepEqual(placeOnShelf(two, 'rows', 'x', 'Int64', undefined, { area: 'measures', index: 0 }), S(['x'], [], [sum('y')]))
  // dropped from the list onto Measures while it is a row: it is both (the sum of x by x is a question)
  assert.deepEqual(placeOnShelf(S(['x']), 'measures', 'x', 'Int64'), S(['x'], [], [sum('x')]))
})

test('chips leave an area, a field leaves them all, lost fields leave by themselves', () => {
  const s = S(['a', 'b'], ['c'], [sum('x'), sum('a')])
  assert.deepEqual(removeFromShelf(s, 'rows', 0), S(['b'], ['c'], [sum('x'), sum('a')]))
  assert.deepEqual(removeFromShelf(s, 'measures', 1), S(['a', 'b'], ['c'], [sum('x')]))
  assert.deepEqual(fieldOffShelves(s, 'a'), S(['b'], ['c'], [sum('x')]))
  assert.deepEqual(keepKnownShelves(s, ['a', 'x']), S(['a'], [], [sum('x'), sum('a')]))
})

test('a view saved with the areas opens with them', () => {
  const all = ['a', 'b', 'x']
  assert.deepEqual(shelvesOfSpec({ rows: ['b', 'gone'], cols: ['a'], measures: [sum('x'), { column: 'x', func: 'evil' }, sum('gone')] }, all),
    S(['b'], ['a'], [sum('x')]))
  assert.deepEqual(shelvesOfSpec({ rows: [], cols: [], measures: [] }, all), S())
})

test('a view of before opens as before: its pivot, or every field', () => {
  const all = ['a', 'b', 'x']
  // no pivot: the whole table
  assert.deepEqual(shelvesOfSpec({ pivotOn: false, pivot: { index: [''], on: [''], values: '', func: 'sum' } }, all), S(all))
  assert.deepEqual(shelvesOfSpec({}, all), S(all))
  assert.deepEqual(shelvesOfSpec(null, all), S(all))
  // its pivot: the same request as before
  const old = shelvesOfSpec({ pivotOn: true, pivot: { index: ['a', ''], on: ['b'], values: 'x', func: 'mean' } }, all)
  assert.deepEqual(old, S(['a'], ['b'], [{ column: 'x', func: 'mean' }]))
  assert.deepEqual(shelfOps(old), [{ type: 'pivot', params: { index: ['a'], on: ['b'], values: 'x', func: 'mean' } }])
  // a pivot switched on and never filled in was no pivot
  assert.deepEqual(shelvesOfSpec({ pivotOn: true, pivot: { index: ['a'], on: [''], values: '', func: 'sum' } }, all), S(all))
})

test('a view keeps the areas, and the pivot they are the face of for who reads it elsewhere', () => {
  assert.deepEqual(specOfShelves(S(['a'], ['b'], [sum('x'), sum('y')])), {
    rows: ['a'], cols: ['b'], measures: [sum('x'), sum('y')], pivotOn: true, pivot: { index: ['a'], on: ['b'], values: 'x', func: 'sum' } })
  assert.equal(specOfShelves(S(['a'])).pivotOn, false)
  assert.equal(specOfShelves(S([], [], [sum('x')])).pivotOn, false)
  const back = shelvesOfSpec(specOfShelves(S(['a'], [], [sum('x')])), ['a', 'x'])
  assert.deepEqual(back, S(['a'], [], [sum('x')]))
})
