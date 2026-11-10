// The Runs page's groups and links, without a browser (composables/runGroups.ts): which
// failures are one row, and which object a run leads to.
// Run: npm test
import { test } from 'node:test'
import assert from 'node:assert/strict'

import { groupRuns, runOpens } from '../composables/runGroups.ts'

const run = (id, more = {}) => ({ id, kind: 'flow', status: 'FAILURE', error: 'boom', flow_id: 7, datasource_id: null, flow_name: 'orders', ...more })

test('runs that follow one another with the same source and error are one group', () => {
  const rows = [run(9), run(8), run(7), run(6, { error: 'other' }), run(5), run(4, { status: 'SUCCESS', error: null }), run(3), run(2, { flow_id: 8, flow_name: 'stock' })]
  const g = groupRuns(rows)
  assert.deepEqual(g.map((x) => x.runs.map((r) => r.id)), [[9, 8, 7], [6], [5], [4], [3], [2]])
  assert.equal(g[0].head.id, 9) // the newest: the list comes newest first
})

test('successes, and failures that say nothing, are never grouped', () => {
  const ok = [run(3, { status: 'SUCCESS', error: null }), run(2, { status: 'SUCCESS', error: null })]
  assert.equal(groupRuns(ok).length, 2)
  assert.equal(groupRuns([run(3, { error: '' }), run(2, { error: '' })]).length, 2)
  assert.deepEqual(groupRuns([]), [])
})

test('two Script nodes of the same flow are two sources', () => {
  const s = (id, label) => run(id, { kind: 'script', script: JSON.stringify({ label }) })
  assert.equal(groupRuns([s(3, 'clean'), s(2, 'clean'), s(1, 'score')]).length, 2)
  assert.equal(groupRuns([s(3, 'clean'), run(2, { kind: 'script', script: 'not json' })]).length, 2)
})

test('a run leads to the object the row names, when it still exists', () => {
  assert.deepEqual(runOpens(run(1)), { what: 'flow', to: '/editor?flow=7' })
  assert.equal(runOpens(run(1, { flow_name: null })), null) // the flow was deleted
  assert.equal(runOpens(run(1, { flow_id: null, flow_name: null })), null)
  assert.deepEqual(runOpens(run(1, { kind: 'ingest', flow_id: null, flow_name: null, datasource_id: 4, source_name: 'crm' })), { what: 'datasource', to: '/datasources?ds=4' })
  assert.deepEqual(runOpens(run(1, { kind: 'sql', flow_id: null, sql_script_id: 3, sql_script_name: 'q' })), { what: 'sqlScript', to: '/sql-script?id=3' })
  assert.deepEqual(runOpens(run(1, { flow_id: null, flow_name: null, flow_group_id: 2, group_name: 'nightly' })), { what: 'group', to: '/flow-group?id=2' })
  assert.deepEqual(runOpens(run(1, { kind: 'script', flow_id: null, flow_name: null, saved_script_id: 5, script_name: 's' })), { what: 'script', to: '/script?id=5' })
  // a flow that published a datasource is still its flow
  assert.deepEqual(runOpens(run(1, { datasource_id: 4, source_name: 'crm' })).what, 'flow')
})
