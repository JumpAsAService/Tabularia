// The canvas of a Bayesian model without a browser (composables/bayesCanvasModel.ts): every template
// drawn as Vue Flow nodes and read back gives its layout again (canvas coordinates through nested
// boxes), parents come before their children, a node sits in its own plate's box, the mirror's
// edges become one line per pair going rightwards, the drop target is the deepest box, and Alt with
// an arrow walks to the nearest node in that direction.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { register } from 'node:module'

register('./alias-hooks.mjs', import.meta.url)
const {
  bayesBoxes, bayesCanvasNodes, bayesCanvasLayout, bayesCanvasEdges, bayesDropPlate, bayesInside, bayesNearestNode, bayesDrawnPlate,
  BAYES_NODE_W, BAYES_NODE_H,
} = await import('~/composables/bayesCanvasModel.ts')
const { bayesTemplates, bayesOpenTemplate } = await import('~/composables/bayesTemplates.ts')
const { bayesGraphDecode, bayesGraphModel } = await import('~/composables/bayesGraph.ts')

const opened = () => bayesTemplates().map(t => [t.id, bayesOpenTemplate(t.id, t.order ? { order: 5 } : {})])

test('every template to Vue Flow and back gives the same layout', () => {
  for (const [id, g] of opened()) {
    const nodes = bayesCanvasNodes(g)
    const back = bayesCanvasLayout(nodes)
    for (const [key, at] of Object.entries(g.layout)) {
      assert.deepEqual(back[key], at, `${id}: ${key}`)
    }
    assert.equal(nodes.length, g.nodes.length + g.plates.filter(p => p.kind === 'rows' || p.kind === 'group').length, id)
  }
})

test('a box comes before what it holds, and a node is placed in its own plate\'s box', () => {
  for (const [id, g] of opened()) {
    const nodes = bayesCanvasNodes(g)
    const index = new Map(nodes.map((n, i) => [n.id, i]))
    for (const n of nodes) {
      if (n.parentNode) assert.ok(index.get(n.parentNode) < index.get(n.id), `${id}: ${n.parentNode} before ${n.id}`)
    }
    for (const n of g.nodes) {
      const drawn = nodes.find(x => x.id === n.id)
      const plate = bayesDrawnPlate(n, g)
      assert.equal(drawn.parentNode ?? null, plate, `${id}: ${n.id} in ${plate}`)
    }
  }
  // the school's box sits inside the district's, the likelihood in the rows' box
  const g = bayesOpenTemplate('hier_nested')
  const nodes = bayesCanvasNodes(g)
  assert.equal(nodes.find(n => n.id === 'sch').parentNode, 'dst')
  assert.equal(nodes.find(n => n.id === 'lik').parentNode, 'rows')
  assert.deepEqual(nodes.find(n => n.id === 'us').position, { x: g.layout.us.x - g.layout.sch.x, y: g.layout.us.y - g.layout.sch.y })
})

test('canvas and relative coordinates through three levels of boxes', () => {
  const doc = {
    plates: [{ id: 'rows', kind: 'rows' }, { id: 'a', kind: 'group', parent: null }, { id: 'b', kind: 'group', parent: 'a' }, { id: 'c', kind: 'group', parent: 'b' }],
    nodes: [{ id: 'n', kind: 'parameter', plate: 'c' }, { id: 'm', kind: 'parameter', plate: null }],
    layout: { a: { x: 100, y: 50, w: 600, h: 400 }, b: { x: 130, y: 90, w: 500, h: 300 }, c: { x: 170, y: 140, w: 300, h: 200 }, n: { x: 200, y: 190 }, m: { x: -40, y: 10 } },
  }
  const nodes = bayesCanvasNodes(doc)
  assert.deepEqual(nodes.map(n => n.id).slice(0, 4), ['rows', 'a', 'b', 'c'])
  assert.deepEqual(nodes.find(n => n.id === 'c').position, { x: 40, y: 50 })
  assert.deepEqual(nodes.find(n => n.id === 'n').position, { x: 30, y: 50 })
  assert.deepEqual(nodes.find(n => n.id === 'm').position, { x: -40, y: 10 })
  const back = bayesCanvasLayout(nodes)
  assert.deepEqual(back.n, { x: 200, y: 190 })
  assert.deepEqual(back.c, { x: 170, y: 140, w: 300, h: 200 })
  // the rows plate has no place: its box is the smallest around what it holds (none here)
  assert.equal(bayesBoxes(doc).find(b => b.id === 'rows').depth, 0)
  assert.equal(bayesBoxes(doc).find(b => b.id === 'c').depth, 2)
})

test('a plate with no size gets the box around its nodes', () => {
  const doc = {
    plates: [{ id: 'rows', kind: 'rows' }],
    nodes: [{ id: 'y', kind: 'data', plate: 'rows' }, { id: 'lik', kind: 'likelihood', observed: 'y' }],
    layout: { y: { x: 100, y: 100 }, lik: { x: 340, y: 100 } },
  }
  const [rows] = bayesBoxes(doc)
  assert.deepEqual({ x: rows.x, y: rows.y, w: rows.w, h: rows.h }, { x: 80, y: 60, w: 340 + BAYES_NODE_W + 20 - 80, h: BAYES_NODE_H + 60 })
})

test('the drop target: inside, on an edge, in two boxes, outside', () => {
  const g = bayesOpenTemplate('hier_nested')
  const boxes = bayesBoxes(g)
  const sch = g.layout.sch
  const dst = g.layout.dst
  assert.equal(bayesDropPlate({ x: sch.x + 10, y: sch.y + 60 }, boxes), 'sch', 'in two boxes: the deeper')
  assert.equal(bayesDropPlate({ x: dst.x + 5, y: dst.y + 100 }, boxes), 'dst')
  assert.equal(bayesDropPlate({ x: dst.x, y: dst.y }, boxes), 'dst', 'on the edge')
  assert.equal(bayesDropPlate({ x: -500, y: -500 }, boxes), null, 'outside every box')
  assert.equal(bayesDropPlate({ x: g.layout.rows.x + 30, y: g.layout.rows.y + 60 }, boxes), 'rows')
  assert.ok(bayesInside({ x: 0, y: 0 }, { x: 0, y: 0, w: 1, h: 1 }))
})

test('edges: one line per pair, a component\'s on its node, every one rightwards in the templates', () => {
  for (const [id, g] of opened()) {
    const model = bayesGraphModel(bayesGraphDecode(JSON.stringify(g)).graph)
    const lines = bayesCanvasEdges(model.edges)
    const ids = lines.map(l => l.id)
    assert.equal(new Set(ids).size, ids.length, `${id}: one line per pair`)
    for (const l of lines) {
      assert.notEqual(l.source, l.target)
      assert.ok(g.layout[l.source].x + BAYES_NODE_W <= g.layout[l.target].x, `${id}: ${l.id} goes rightwards`)
      assert.equal(l.sourceHandle, 'r')
      assert.equal(l.targetHandle, 'l')
    }
  }
  const lines = bayesCanvasEdges([
    { from: 'eff.ca', to: 'lik', node: 'lik', slot: 'args.mu', kind: 'read' },
    { from: 'eff.cb', to: 'lik', node: 'lik', slot: 'args.mu', kind: 'read' },
    { from: 'g', to: 'lik', node: 'lik', slot: 'args.mu', kind: 'index' },
    { from: 'y', to: 'lik', node: 'lik', slot: 'observed', kind: 'observed' },
    { from: 'y', to: 'lik', node: 'lik', slot: 'args.mu', kind: 'lag' },
    { from: 'eff.ca', to: 'eff', node: 'eff', slot: 'x', kind: 'read' },
  ])
  assert.deepEqual(lines.map(l => [l.id, l.kinds]), [['eff>lik', ['read']], ['g>lik', ['index']], ['y>lik', ['observed', 'lag']]])
})

test('Alt with an arrow: the nearest node in that direction, none where there is none', () => {
  const r = (x, y) => ({ x, y, w: 180, h: 80 })
  const others = [{ id: 'right', rect: r(300, 0) }, { id: 'farRight', rect: r(600, 0) }, { id: 'down', rect: r(0, 200) }, { id: 'diag', rect: r(300, 250) }]
  assert.equal(bayesNearestNode(r(0, 0), others, 'right'), 'right')
  assert.equal(bayesNearestNode(r(0, 0), others, 'down'), 'down')
  assert.equal(bayesNearestNode(r(0, 0), others, 'left'), null)
  assert.equal(bayesNearestNode(r(0, 0), others, 'up'), null)
  assert.equal(bayesNearestNode(r(300, 0), [{ id: 'below', rect: r(250, 300) }], 'left'), null, 'the cone leaves out what is mostly below')
  assert.equal(bayesNearestNode(r(300, 0), [{ id: 'below', rect: r(250, 300) }], 'down'), 'below')
})
