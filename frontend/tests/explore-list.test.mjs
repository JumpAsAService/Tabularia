// Explore's list: what it shows while loading, which actions a row offers and in what
// order, and what the keyboard does. Without a browser: the pure rules of exploreList.ts.
// Run: npm test
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { register } from 'node:module'

register('./alias-hooks.mjs', import.meta.url)
const L = await import('../composables/exploreList.ts')

test('the list is loading until both the tree and the folder are in: no false «empty»', () => {
  const s = (treeLoaded, folderLoading, total, visible) => L.listState({ treeLoaded, folderLoading, total, visible })
  assert.equal(s(false, false, 0, 0), 'loading') // the top level before the tree arrives
  assert.equal(s(true, true, 0, 0), 'loading')   // a folder whose lists are on their way
  assert.equal(s(false, false, 3, 3), 'loading') // a folder's contents in, its subfolders not yet
  assert.equal(s(true, false, 0, 0), 'empty')
  assert.equal(s(true, false, 4, 0), 'nomatch')
  assert.equal(s(true, false, 4, 2), 'rows')
})

test('every kind lists its commands in the same order, Delete last and apart', () => {
  const ids = (r) => L.rowActions(r).map((a) => a.id)
  const order = L.ROW_ACTION_ORDER
  for (const r of [
    { kind: 'flow' }, { kind: 'datasource', dbDatasource: true }, { kind: 'datasource' }, { kind: 'connection' },
    { kind: 'folder' }, { kind: 'view' }, { kind: 'report' }, { kind: 'group' }, { kind: 'sql' },
  ]) {
    const got = ids(r)
    assert.deepEqual(got, [...got].sort((a, b) => order.indexOf(a) - order.indexOf(b)), r.kind)
    assert.equal(got[got.length - 1], 'delete', r.kind)
    assert.equal(L.rowActions(r).at(-1).sep, true, `${r.kind}: Delete has its own group`)
  }
  assert.deepEqual(ids({ kind: 'flow' }), ['history', 'rename', 'duplicate', 'move', 'delete'])
  assert.deepEqual(ids({ kind: 'datasource', dbDatasource: true }), ['refresh', 'contract', 'rename', 'duplicate', 'move', 'delete'])
  assert.deepEqual(ids({ kind: 'connection' }), ['editConnection', 'duplicate', 'move', 'delete'])
  assert.deepEqual(ids({ kind: 'folder' }), ['rename', 'move', 'delete'])
  assert.deepEqual(ids({ kind: 'view' }), ['rename', 'duplicate', 'move', 'delete'])
  // the separators fall between groups only
  assert.deepEqual(L.rowActions({ kind: 'flow' }).map((a) => a.sep), [false, true, false, false, true])
  assert.deepEqual(L.rowActions({ kind: 'view' }).map((a) => a.sep), [false, false, false, true])
})

test('a personal folder keeps only Rename; a locked flow has no command; the row keeps its main one', () => {
  assert.deepEqual(L.rowActions({ kind: 'folder', fixedFolder: true }).map((a) => a.id), ['rename'])
  assert.deepEqual(L.rowActions({ kind: 'flow', locked: true }), [])
  assert.equal(L.primaryAction({ kind: 'flow' }), 'history')
  assert.equal(L.primaryAction({ kind: 'datasource', dbDatasource: true }), 'refresh')
  assert.equal(L.primaryAction({ kind: 'datasource' }), null)
  assert.equal(L.primaryAction({ kind: 'folder' }), null)
  assert.equal(L.primaryAction({ kind: 'flow', locked: true }), null)
})

test('the keyboard: the shortcuts outside the fields, the browser and the controls keep theirs', () => {
  const k = (key, extra = {}) => L.keyCommand({ key, ...extra })
  assert.equal(k('/'), 'filter')
  assert.equal(k('ArrowDown'), 'next')
  assert.equal(k('ArrowUp'), 'prev')
  assert.equal(k('ArrowUp', { altKey: true }), 'up')
  assert.equal(k('Backspace'), 'up')
  assert.equal(k('Enter'), 'open')
  assert.equal(k('Enter', { onControl: true }), null) // the focused link or button opens it itself
  assert.equal(k('F2'), 'rename')
  assert.equal(k('F10', { shiftKey: true }), 'menu')
  assert.equal(k('F10'), null)
  assert.equal(k('ContextMenu'), 'menu')
  // typing in a field: nothing is taken from it
  for (const key of ['/', 'Backspace', 'ArrowDown', 'Enter', 'F2']) assert.equal(k(key, { inField: true }), null, key)
  // Ctrl/⌘ combinations are the browser's; Alt+← is history's
  assert.equal(k('ArrowDown', { ctrlKey: true }), null)
  assert.equal(k('Backspace', { metaKey: true }), null)
  assert.equal(k('ArrowLeft', { altKey: true }), null)
  assert.equal(k('ArrowDown', { altKey: true }), null)
})

test('↑ ↓ start from the first or the last row and hold at the ends', () => {
  assert.equal(L.stepRow(-1, 5, 1), 0)
  assert.equal(L.stepRow(-1, 5, -1), 4)
  assert.equal(L.stepRow(2, 5, 1), 3)
  assert.equal(L.stepRow(4, 5, 1), 4)
  assert.equal(L.stepRow(0, 5, -1), 0)
  assert.equal(L.stepRow(3, 0, 1), -1)
})

test('what uses an object: its readers and its runners, not what it publishes', () => {
  const nodes = [
    { id: 'ds:1', type: 'datasource' }, { id: 'flow:2', type: 'flow' }, { id: 'flow:3', type: 'flow' },
    { id: 'group:4', type: 'flow_group' }, { id: 'sql:5', type: 'sql_script' }, { id: 'ds:6', type: 'datasource' },
    { id: 'conn:7', type: 'connection' }, { id: 'sink:8', type: 'db_sink' },
  ]
  const edges = [
    { source: 'ds:1', target: 'flow:2', kind: 'read' },
    { source: 'ds:1', target: 'flow:3', kind: 'read' },
    { source: 'ds:1', target: 'flow:3', kind: 'read' }, // two sources of the same flow: one flow
    { source: 'ds:1', target: 'sql:5', kind: 'read' },
    { source: 'flow:2', target: 'ds:1', kind: 'refresh' }, // flow 2 refreshes it before running
    { source: 'flow:9', target: 'ds:1', kind: 'publish' }, // who makes it is not a user of it
    { source: 'conn:7', target: 'ds:1', kind: 'ingest' },
    { source: 'group:4', target: 'flow:2', kind: 'orchestrate' },
  ]
  assert.deepEqual(L.dependentsOf({ nodes, edges }, 'ds:1'), { flow: 2, sql_script: 1 })
  assert.deepEqual(L.dependentsOf({ nodes, edges }, 'flow:2'), { flow_group: 1 })
  assert.deepEqual(L.dependentsOf({ nodes, edges }, 'conn:7'), { datasource: 1 })
  assert.deepEqual(L.dependentsOf({ nodes, edges: [{ source: 'flow:2', target: 'sink:8', kind: 'write' }] }, 'flow:2'), {})
  assert.equal(L.lineageType('sql'), 'sql')
  assert.equal(L.lineageType('report'), null)
  assert.equal(L.lineageType('folder'), null)
})

test('the start band: failures of the last 24 hours, and where each item leads', () => {
  const now = Date.parse('2026-11-06T12:00:00Z')
  const run = (id, status, finished, extra = {}) => ({ id, status, started_at: finished, finished_at: finished, flow_id: 1, ...extra })
  const runs = [
    run(1, 'FAILURE', '2026-11-06T11:00:00'),          // naive = UTC, an hour ago
    run(2, 'SUCCESS', '2026-11-06T11:30:00'),
    run(3, 'FAILURE', '2026-11-05T13:00:00Z'),         // 23 hours ago
    run(4, 'FAILURE', '2026-11-05T11:00:00+00:00'),    // 25 hours ago
    run(5, 'FAILURE', null, { started_at: '2026-11-06T10:00:00' }), // still without an end
  ]
  assert.deepEqual(L.failedWithin(runs, now).map((r) => r.id), [1, 3, 5])
  assert.equal(L.runTarget({ flow_id: 4 }), '/editor?flow=4')
  assert.equal(L.runTarget({ flow_id: null, flow_group_id: 2 }), '/flow-group?id=2')
  assert.equal(L.runTarget({ flow_id: null, sql_script_id: 7 }), '/sql-script?id=7')
  assert.equal(L.runTarget({ flow_id: null }), '/runs')
  assert.equal(L.objectTarget('datasource', 3), '/viewer?ds=3')
  assert.equal(L.objectTarget('sql', 3), '/sql-script?id=3')
  assert.equal(L.objectTarget('report', 9), '/report?id=9')
})
