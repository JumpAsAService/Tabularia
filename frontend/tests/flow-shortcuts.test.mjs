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

// the engine's operations, as GET /tasks/operations lists them: the transformations written
// by hand, and the hypothesis tests of the catalogue, read from the engine's snapshot
const TRANSFORMS = ['cast', 'compute', 'drop', 'drop_nulls', 'fill_null', 'filter', 'foreach', 'group_by', 'join', 'limit',
  'pivot', 'rename', 'reorder', 'sample', 'select', 'sort', 'sql', 'union', 'unique', 'unpivot']
const STATS = JSON.parse(readFileSync(new URL('../../backend/app/engine/stats/catalog.json', import.meta.url), 'utf8'))
  .tests.map((t) => t.operation)
const ENGINE = [...TRANSFORMS, ...STATS].sort()

test('the defaults are eight real transformations, each once', () => {
  assert.equal(m.FLOW_SHORTCUTS_DEFAULT.length, m.FLOW_SHORTCUTS_MAX)
  assert.equal(new Set(m.FLOW_SHORTCUTS_DEFAULT).size, 8)
  for (const op of m.FLOW_SHORTCUTS_DEFAULT) assert.ok(ENGINE.includes(op) && op !== 'foreach', op)
  assert.deepEqual(m.flowShortcutsShown(null, ENGINE), [...m.FLOW_SHORTCUTS_DEFAULT])
})

test('the gateway knows the same transformations the editor offers', () => {
  const go = readFileSync(new URL('../../gateway-go/internal/api/flow_shortcuts.go', import.meta.url), 'utf8')
  const list = go.match(/var transformOps = \[\]any\{([^}]*)\}/)[1].match(/"([a-z_]+)"/g).map((s) => s.slice(1, -1))
  assert.deepEqual([...list].sort(), TRANSFORMS.filter((o) => o !== 'foreach'))
  // and the hypothesis tests from the catalogue's copy it embeds: none is written by hand
  const made = go.match(/var FlowShortcutOps = func\(\) \[\]any \{([\s\S]*?)\n\}\(\)/)[1]
  assert.ok(made.includes('transformOps...') && made.includes('statcatalog.Operations()'), made)
  assert.ok(STATS.length > 0 && !list.some((o) => o.startsWith('stat_')))
  const embedded = readFileSync(new URL('../../gateway-go/internal/statcatalog/catalog.json', import.meta.url), 'utf8')
  assert.deepEqual(JSON.parse(embedded).tests.map((t) => t.operation), STATS)
})

test('a hypothesis test may be one of the menu\'s eight', () => {
  assert.ok(STATS.includes('stat_t_two_sample'))
  assert.deepEqual(m.flowShortcutsShown(['filter', 'stat_t_two_sample'], ENGINE), ['filter', 'stat_t_two_sample'])
  assert.equal(m.flowShortcutsProblem(['stat_t_two_sample', 'join'], ENGINE), '')
  assert.equal(m.flowShortcutsProblem(['stat_nope'], ENGINE), 'unknown')
  // an engine of before, without the tests: a saved one is not offered
  assert.deepEqual(m.flowShortcutsShown(['filter', 'stat_t_two_sample'], TRANSFORMS), ['filter'])
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

// ── Delete and Backspace belong to the canvas ───────────────────────────────────────────────────────────────────────

test('the keys that remove nodes act only with the focus on the canvas or on nothing', async () => {
  const k = await import('../composables/nodeKeys.ts')
  const el = (tagName, parent = null) => ({ tagName, contains(x) { for (let n = x; n; n = n.parent) if (n === this) return true; return false }, parent })
  const body = el('BODY')
  const canvas = el('DIV')
  const node = el('DIV', canvas)
  const handle = el('BUTTON', node)
  const panel = el('DIV')
  const combobox = el('BUTTON', panel) // the trigger of a Select in the node's panel
  const toolbar = el('BUTTON')
  assert.equal(k.focusOnCanvas(null, canvas, body), true)
  assert.equal(k.focusOnCanvas(undefined, canvas, body), true)
  assert.equal(k.focusOnCanvas(body, canvas, body), true)
  assert.equal(k.focusOnCanvas(canvas, canvas, body), true)
  assert.equal(k.focusOnCanvas(node, canvas, body), true)
  assert.equal(k.focusOnCanvas(handle, canvas, body), true)
  assert.equal(k.focusOnCanvas(combobox, canvas, body), false)
  assert.equal(k.focusOnCanvas(toolbar, canvas, body), false)
  assert.equal(k.focusOnCanvas(combobox, null, body), false)
  // and the editor asks before it deletes: the key for the nodes, the cut, never the edges' own path
  const editor = readFileSync(new URL('../components/FlowEditor.vue', import.meta.url), 'utf8')
  assert.match(editor, /if \(inGrid \|\| !onCanvas\(\)\) return/)
  assert.match(editor, /key_ === 'x' && !onCanvas\(\)/)
  assert.match(editor, /focusOnCanvas\(document\.activeElement, canvasEl\.value, document\.body\)/)
})
