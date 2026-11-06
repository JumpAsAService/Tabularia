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
