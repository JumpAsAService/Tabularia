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
