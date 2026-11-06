// The runs' Gantt in Flows draws one bar per execution, not one per step
// (composables/runExecutions.ts). Run: npm test.
import { test } from 'node:test'
import assert from 'node:assert/strict'

import { executionRuns } from '../composables/runExecutions.ts'

test('the steps of an orchestration are not further runs', () => {
  // GET /flows/1/runs as the gateway answers it: each launch, an orchestration and its step
  const runs = [
    { id: 37, kind: 'flow', parent_run_id: 34 },
    { id: 34, kind: 'orchestration', parent_run_id: null },
    { id: 14, kind: 'flow', parent_run_id: 12 },
    { id: 13, kind: 'flow', parent_run_id: 12 },
    { id: 12, kind: 'orchestration', parent_run_id: null },
  ]
  assert.deepEqual(executionRuns(runs).map((r) => r.id), [34, 12])
})

test('a run with no parent, or whose parent is not the flow\'s, is one execution', () => {
  const runs = [
    { id: 9, parent_run_id: 5 }, // a cell of a flow group's run
    { id: 8 },
    { id: 7, parent_run_id: null },
  ]
  assert.deepEqual(executionRuns(runs).map((r) => r.id), [9, 8, 7])
  assert.deepEqual(executionRuns([]), [])
})
