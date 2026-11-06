// The flow editor's right-click menu without a browser: which transformations it offers
// (the person's, or the defaults), the checks before saving (the gateway's own rules), and
// where the ring puts its items and how far it moves inwards near the canvas' edge.
// Run: npm test
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { register } from 'node:module'
import { readFileSync } from 'node:fs'

register('./alias-hooks.mjs', import.meta.url)
const m = await import('../composables/flowShortcuts.ts')

// the engine's operations, as GET /tasks/operations lists them
const ENGINE = ['cast', 'compute', 'drop', 'drop_nulls', 'fill_null', 'filter', 'foreach', 'group_by', 'join', 'limit',
  'pivot', 'rename', 'reorder', 'sample', 'select', 'sort', 'sql', 'union', 'unique', 'unpivot']

test('the defaults are eight real transformations, each once', () => {
  assert.equal(m.FLOW_SHORTCUTS_DEFAULT.length, m.FLOW_SHORTCUTS_MAX)
  assert.equal(new Set(m.FLOW_SHORTCUTS_DEFAULT).size, 8)
  for (const op of m.FLOW_SHORTCUTS_DEFAULT) assert.ok(ENGINE.includes(op) && op !== 'foreach', op)
  assert.deepEqual(m.flowShortcutsShown(null, ENGINE), [...m.FLOW_SHORTCUTS_DEFAULT])
})

test('the gateway knows the same transformations the editor offers', () => {
  const go = readFileSync(new URL('../../gateway-go/internal/api/flow_shortcuts.go', import.meta.url), 'utf8')
  const list = go.match(/var FlowShortcutOps = \[\]any\{([^}]*)\}/)[1].match(/"([a-z_]+)"/g).map((s) => s.slice(1, -1))
  assert.deepEqual([...list].sort(), ENGINE.filter((o) => o !== 'foreach'))
})

test('the menu shows the saved choice, in its order, only what is offered', () => {
  assert.deepEqual(m.flowShortcutsShown(['join', 'sql', 'filter'], ENGINE), ['join', 'sql', 'filter'])
  assert.deepEqual(m.flowShortcutsShown([], ENGINE), [])
  assert.deepEqual(m.flowShortcutsShown(['join', 'gone', 'foreach', 'join'], ENGINE), ['join'])
  assert.deepEqual(m.flowShortcutsShown(null, ['filter', 'sort']), ['filter', 'sort'])
  assert.equal(m.flowShortcutsShown(ENGINE, ENGINE).length, 8)
})

test('a choice is checked as the gateway checks it', () => {
  assert.equal(m.flowShortcutsProblem(['filter', 'join'], ENGINE), '')
  assert.equal(m.flowShortcutsProblem([], ENGINE), '')
  assert.equal(m.flowShortcutsProblem(['filter', 'filter'], ENGINE), 'twice')
  assert.equal(m.flowShortcutsProblem(['foreach'], ENGINE), 'unknown')
  assert.equal(m.flowShortcutsProblem(['nope'], ENGINE), 'unknown')
  assert.equal(m.flowShortcutsProblem(ENGINE.filter((o) => o !== 'foreach').slice(0, 9), ENGINE), 'tooMany')
})

test('the first item sits at 12 o\'clock, the others clockwise, evenly', () => {
  const at = (i, n) => { const { x, y } = m.radialItemOffset(i, n); return [x, y] }
  assert.deepEqual(at(0, 8), [0, -76])
  assert.deepEqual(at(2, 8), [76, 0]) // 3 o'clock: clockwise
  assert.deepEqual(at(4, 8), [0, 76])
  assert.deepEqual(at(6, 8), [-76, 0])
  assert.equal(m.radialItemOffset(1, 8).angle, 45)
  assert.equal(m.radialItemOffset(1, 3).angle, 120)
  assert.deepEqual(at(0, 1), [0, -76])
})

test('a label goes outwards along its item\'s radius', () => {
  assert.equal(m.radialLabelSide(0), 'top')
  assert.equal(m.radialLabelSide(45), 'right')
  assert.equal(m.radialLabelSide(90), 'right')
  assert.equal(m.radialLabelSide(180), 'bottom')
  assert.equal(m.radialLabelSide(225), 'left')
  assert.equal(m.radialLabelSide(315), 'left')
})

test('near the canvas\' edge the ring moves inwards just enough, in the middle not at all', () => {
  const labels = Array.from({ length: 8 }, () => ({ width: 100, height: 22 }))
  const extent = m.radialMenuExtent(8, labels)
  // the 3 o'clock label reaches 76 + 20 + 8 + 100 to the right
  assert.equal(extent.right, 204)
  assert.equal(extent.left, -204)
  assert.equal(extent.top, -76 - 20 - 8 - 22)
  const bounds = { left: 0, top: 0, right: 1000, bottom: 700 }
  assert.deepEqual(m.radialMenuCenter({ x: 500, y: 350 }, extent, bounds), { x: 500, y: 350 })
  assert.deepEqual(m.radialMenuCenter({ x: 20, y: 10 }, extent, bounds), { x: 212, y: 134 })
  assert.deepEqual(m.radialMenuCenter({ x: 990, y: 690 }, extent, bounds), { x: 1000 - 8 - 204, y: 700 - 8 - 126 })
  // a canvas too small on an axis: centred on it
  assert.equal(m.radialMenuCenter({ x: 10, y: 350 }, extent, { left: 0, top: 0, right: 300, bottom: 700 }).x, 150)
})
