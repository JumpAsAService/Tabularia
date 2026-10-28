// The rows of a report's dataset as «Refresh fields» counted them, and what the designer
// says about them (composables/reportRowCount.ts): the number; past SLOW_ROWS a warning
// with the time a render takes; past the cap, that the report will not render; a count
// kept only while the source is the one counted.
// Run: npm test (Node strips the types of the modules it imports).
import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  SECONDS_PER_MILLION, SLOW_ROWS, aboutRows, aboutSeconds, countFor, rowCountNote, rowCountOf, sourceKey,
} from '../composables/reportRowCount.ts'

const CAP = 1_000_000

test('a fields answer gives its count, or none when it could not be made', () => {
  assert.deepEqual(rowCountOf({ dataset: 'd', fields: [], rows: 12, over: false, max_rows: CAP }), { rows: 12, over: false, max_rows: CAP })
  assert.equal(rowCountOf({ dataset: 'd', fields: [] }), null) // the gateway leaves it out
  assert.equal(rowCountOf({ rows: '12', over: false, max_rows: CAP }), null)
  assert.equal(rowCountOf({ rows: 12, over: 'no', max_rows: CAP }), null)
  assert.equal(rowCountOf({ rows: -1, over: false, max_rows: CAP }), null)
  assert.equal(rowCountOf(null), null)
})

test('under the threshold: the number, as it is', () => {
  assert.deepEqual(rowCountNote({ rows: 0, over: false, max_rows: CAP }), { level: 'plain', rows: 0, seconds: 1 })
  assert.equal(rowCountNote({ rows: 1_234, over: false, max_rows: CAP }).rows, 1_234)
  assert.equal(rowCountNote({ rows: SLOW_ROWS, over: false, max_rows: CAP }).level, 'plain')
  assert.equal(rowCountNote(null), null)
})

test('past the threshold: a warning, with the rows rounded and the time a render takes', () => {
  assert.equal(SLOW_ROWS, 500_000)
  assert.deepEqual(rowCountNote({ rows: 105_035, over: false, max_rows: CAP }), { level: 'plain', rows: 105_035, seconds: 1 })
  // the owner's example: some 840,000 rows, now some 2 seconds (in columns, with DuckDB)
  assert.deepEqual(rowCountNote({ rows: 842_345, over: false, max_rows: CAP }), { level: 'slow', rows: 842_000, seconds: 2 })
  assert.equal(rowCountNote({ rows: CAP, over: false, max_rows: CAP }).level, 'slow') // exactly the cap still renders
})

test('past the cap: the report will not render, said with the cap', () => {
  assert.deepEqual(rowCountNote({ rows: CAP, over: true, max_rows: CAP }).level, 'over')
  assert.equal(rowCountNote({ rows: CAP, over: true, max_rows: CAP }).rows, CAP)
  // a lower cap (REPORTS__MAX_ROWS) is the one said, even under the warning's threshold
  assert.deepEqual(rowCountNote({ rows: 50, over: true, max_rows: 50 }), { level: 'over', rows: 50, seconds: 1 })
})

test('rounding: three significant figures, the seconds whole and then by fives', () => {
  assert.equal(aboutRows(999), 999)
  assert.equal(aboutRows(1_234), 1_230)
  assert.equal(aboutRows(105_035), 105_000)
  assert.equal(aboutRows(999_999), 1_000_000)
  assert.equal(SECONDS_PER_MILLION, 2)
  assert.equal(aboutSeconds(10), 1) // never «about 0 seconds»
  assert.equal(aboutSeconds(2_000_000), 4)
  assert.equal(aboutSeconds(7_000_000), 15) // 14 → by fives
  assert.equal(aboutSeconds(10_000_000), 20)
})

test('a count belongs to the source it counted, whatever the order of its properties', () => {
  const source = { kind: 'sql', sql: 'SELECT * FROM orders', connection_id: null, engine: 'duckdb' }
  const kept = { rows: 7, over: false, max_rows: CAP, source: sourceKey(source) }
  assert.equal(countFor(kept, { engine: 'duckdb', connection_id: null, sql: 'SELECT * FROM orders', kind: 'sql' }), kept)
  assert.equal(countFor(kept, { ...source, sql: 'SELECT region, SUM(amount) FROM orders GROUP BY region' }), null)
  assert.equal(countFor(kept, { ...source, engine: 'polars' }), null)
  assert.equal(countFor(undefined, source), null)
  // filters are a list: their order is the source's
  const a = { kind: 'datasource', datasource_id: 3, filters: [{ column: 'r', op: '=', value: 'N' }, { column: 'a', op: '>', value: 0 }] }
  assert.notEqual(sourceKey(a), sourceKey({ ...a, filters: [...a.filters].reverse() }))
})
