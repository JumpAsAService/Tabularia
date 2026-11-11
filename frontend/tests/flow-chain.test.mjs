// The flow as a chain, without a browser (composables/flowChain.ts): where a new step goes
// and what makes room for it, where the keyboard walks, which edges lead to a node, where a
// long flow opens.
// Run: npm test
import { test } from 'node:test'
import assert from 'node:assert/strict'

import { planStep, chainNeighbour, upstreamEdgeIds, flowEndIds, openingViewport, easeOutExpo, FLOW_STEP_X } from '../composables/flowChain.ts'

const node = (id, x, y, more = {}) => ({ id, type: 'operation', position: { x, y }, ...more })
const edge = (source, target, targetHandle = 'left', more = {}) => ({ id: `e-${source}-${targetHandle}-${target}`, source, target, targetHandle, ...more })

// source → a → b → out, on the editor's own grid
const CHAIN = [node('source', 60, 140, { type: 'source' }), node('a', 290, 140), node('b', 520, 140), node('out', 750, 140, { type: 'output' })]
const CHAIN_EDGES = [edge('source', 'a'), edge('a', 'b'), edge('b', 'out')]

test('a step added to a node that feeds one goes between the two, and the rest moves on', () => {
  const p = planStep(CHAIN, CHAIN_EDGES, 'a', 'n')
  assert.equal(p.inserted, true)
  assert.deepEqual(p.position, { x: 290 + FLOW_STEP_X, y: 140 })
  assert.deepEqual(p.removeEdgeIds, ['e-a-left-b'])
  assert.deepEqual(p.addEdges.map((e) => [e.source, e.target, e.targetHandle]), [['a', 'n', 'left'], ['n', 'b', 'left']])
  assert.deepEqual(p.moves, [{ id: 'b', x: 750, y: 140 }, { id: 'out', x: 980, y: 140 }])
})

test('after a wide node the step stands clear of it, and what follows one step after the step', () => {
  const nodes = [node('a', 820, 260, { width: 240 }), node('b', 1080, 260), node('c', 1340, 260)]
  const p = planStep(nodes, [edge('a', 'b'), edge('b', 'c')], 'a', 'n')
  assert.deepEqual(p.position, { x: 820 + 240 + 60, y: 260 })
  assert.deepEqual(p.moves, [{ id: 'b', x: 1120 + FLOW_STEP_X, y: 260 }, { id: 'c', x: 1340 + 270, y: 260 }])
})

test('at the end of a chain the step is appended, and nothing moves', () => {
  const p = planStep(CHAIN, CHAIN_EDGES.slice(0, 2), 'b', 'n')
  assert.equal(p.inserted, false)
  assert.deepEqual(p.removeEdgeIds, [])
  assert.deepEqual(p.moves, [])
  // the place beside is taken by the unlinked Output: the step stands under it
  assert.deepEqual(p.position, { x: 750, y: 230 })
  assert.deepEqual(p.addEdges.map((e) => [e.source, e.target, e.targetHandle]), [['b', 'n', 'left']])
})

test('Shift starts a branch even where the step could go in', () => {
  const p = planStep(CHAIN, CHAIN_EDGES, 'a', 'n', { branch: true })
  assert.equal(p.inserted, false)
  assert.deepEqual(p.removeEdgeIds, [])
  assert.deepEqual(p.position, { x: 520, y: 230 }) // under b, which keeps its place
  assert.equal(p.addEdges.length, 1)
})

test('a node that forks takes one more branch; a drop on one of its edges says which', () => {
  const nodes = [...CHAIN, node('c', 520, 300)]
  const edges = [...CHAIN_EDGES, edge('a', 'c')]
  const more = planStep(nodes, edges, 'a', 'n')
  assert.equal(more.inserted, false)
  assert.deepEqual(more.position, { x: 520, y: 410 }) // under b and c
  const onEdge = planStep(nodes, edges, 'a', 'n', { edgeId: 'e-a-left-c', at: { x: 400, y: 250 }, snap: true })
  assert.equal(onEdge.inserted, true)
  assert.deepEqual(onEdge.removeEdgeIds, ['e-a-left-c'])
  assert.deepEqual(onEdge.position, { x: 520, y: 300 }) // on that branch's row, not on b
  assert.deepEqual(onEdge.moves, [{ id: 'c', x: 750, y: 300 }]) // b's branch stays where it is
})

test('the child keeps the input it was reached on: the right side of a join', () => {
  const nodes = [node('s1', 60, 140, { type: 'source' }), node('s2', 60, 300, { type: 'source' }), node('j', 290, 140)]
  const edges = [edge('s1', 'j'), edge('s2', 'j', 'right')]
  const p = planStep(nodes, edges, 's2', 'n')
  assert.equal(p.inserted, true)
  assert.deepEqual(p.addEdges.map((e) => [e.source, e.target, e.targetHandle]), [['s2', 'n', 'left'], ['n', 'j', 'right']])
  assert.equal(p.addEdges[1].id, 'e-n-right-j')
  assert.deepEqual(p.position, { x: 290, y: 300 })
  assert.deepEqual(p.moves, [{ id: 'j', x: 520, y: 140 }])
})

test('room between the two nodes: the step goes in and nothing moves', () => {
  const nodes = [node('source', 60, 140, { type: 'source' }), node('a', 600, 140)]
  const p = planStep(nodes, [edge('source', 'a')], 'source', 'n')
  assert.equal(p.inserted, true)
  assert.deepEqual(p.moves, [])
  assert.deepEqual(p.position, { x: 290, y: 140 })
  // a drop on that edge stays where it was dropped
  const d = planStep(nodes, [edge('source', 'a')], 'source', 'n', { edgeId: 'e-source-left-a', at: { x: 333, y: 150 }, snap: true })
  assert.deepEqual(d.position, { x: 333, y: 150 })
})

test('a place the person chose on the empty canvas is kept, and nothing is pushed', () => {
  const p = planStep(CHAIN, CHAIN_EDGES, 'a', 'n', { at: { x: 400, y: 400 } })
  assert.equal(p.inserted, true)
  assert.deepEqual(p.position, { x: 400, y: 400 })
  assert.deepEqual(p.moves, [])
})

test('a Script node hands its output from its «out» handle; a node with no data output takes no step', () => {
  const nodes = [node('ctl-1', 60, 140, { type: 'script' }), node('a', 290, 140), node('out', 520, 140, { type: 'output' })]
  const edges = [edge('ctl-1', 'a', 'left', { sourceHandle: 'out' })]
  assert.equal(planStep(nodes, edges, 'ctl-1', 'n').addEdges[0].sourceHandle, 'out')
  assert.equal(planStep(nodes, [], 'ctl-1', 'n').addEdges[0].sourceHandle, 'out')
  assert.equal(planStep(nodes, edges, 'out', 'n'), null)
  assert.equal(planStep(nodes, edges, 'gone', 'n'), null)
})

test('a sequence edge is not a successor', () => {
  const edges = [edge('a', 'b', 'seq-in', { sourceHandle: 'seq-out' })]
  const p = planStep(CHAIN, edges, 'a', 'n')
  assert.equal(p.inserted, false)
  assert.deepEqual(p.removeEdgeIds, [])
})

test('inside a foreach the body moves in the container, which widens, and what follows it moves by as much', () => {
  const nodes = [
    node('source', 60, 140, { type: 'source' }),
    node('fe', 290, 100, { type: 'foreach', width: 460, height: 280 }),
    node('x', 20, 60, { parentNode: 'fe', width: 170 }), node('y', 250, 60, { parentNode: 'fe', width: 170 }),
    node('after', 800, 140),
  ]
  const edges = [edge('source', 'fe'), edge('x', 'y'), edge('fe', 'after')]
  const p = planStep(nodes, edges, 'x', 'n')
  assert.equal(p.parentNode, 'fe')
  assert.deepEqual(p.position, { x: 250, y: 60 })
  assert.deepEqual(p.resize, { id: 'fe', width: 480 + 170 + 24 })
  assert.deepEqual(p.moves, [{ id: 'y', x: 480, y: 60 }, { id: 'after', x: 800 + 674 - 460, y: 140 }])
  // a step after the container itself is outside it
  const out = planStep(nodes, edges, 'fe', 'n')
  assert.equal(out.parentNode, undefined)
  assert.equal(out.inserted, true)
})

test('the keyboard walks the chain, and between siblings', () => {
  const nodes = [
    node('s1', 60, 140, { type: 'source' }), node('s2', 60, 300, { type: 'source' }),
    node('j', 290, 140), node('up', 520, 60), node('down', 520, 260), node('note', 0, 0, { type: 'comment' }),
  ]
  const edges = [edge('s1', 'j'), edge('s2', 'j', 'right'), edge('j', 'up'), edge('j', 'down')]
  assert.equal(chainNeighbour(nodes, edges, 'j', 'upstream'), 's1') // the left input first
  assert.equal(chainNeighbour(nodes, edges, 's1', 'upstream'), null)
  assert.equal(chainNeighbour(nodes, edges, 's1', 'downstream'), 'j')
  assert.equal(chainNeighbour(nodes, edges, 'j', 'downstream'), 'up') // the nearest in height
  assert.equal(chainNeighbour(nodes, edges, 'up', 'below'), 'down')
  assert.equal(chainNeighbour(nodes, edges, 'down', 'above'), 'up')
  assert.equal(chainNeighbour(nodes, edges, 'down', 'below'), null)
  // the flow's starts among themselves: down and up again is the same node
  assert.equal(chainNeighbour(nodes, edges, 's1', 'below'), 's2')
  assert.equal(chainNeighbour(nodes, edges, 's2', 'above'), 's1')
  // the two sides of a join, when they are steps
  const more = [...nodes, node('t', 290, 300)]
  const wired = [edge('s1', 'j'), edge('s2', 't'), edge('t', 'j', 'right'), edge('s1', 'k')]
  assert.equal(chainNeighbour([...more, node('k', 290, 0)], wired, 'j', 'below'), null)
  assert.equal(chainNeighbour([...more, node('k', 290, 0)], wired, 'j', 'above'), 'k') // fed by the same node
  assert.equal(chainNeighbour(more, [edge('s1', 'up'), edge('up', 'j'), edge('s2', 't'), edge('t', 'j', 'right')], 't', 'above'), 'up')
  assert.equal(chainNeighbour(nodes, edges, 'gone', 'below'), null)
})

test('a foreach body\'s first node goes back to its container', () => {
  const nodes = [node('fe', 290, 100, { type: 'foreach' }), node('x', 20, 60, { parentNode: 'fe' }), node('y', 250, 60, { parentNode: 'fe' })]
  assert.equal(chainNeighbour(nodes, [edge('x', 'y')], 'x', 'upstream'), 'fe')
  assert.equal(chainNeighbour(nodes, [edge('x', 'y')], 'y', 'upstream'), 'x')
})

test('the edges that lead to a node: every input upstream, never what follows or the sequence', () => {
  const nodes = [
    node('s1', 0, 0, { type: 'source' }), node('s2', 0, 0, { type: 'source' }), node('j', 0, 0), node('g', 0, 0), node('z', 0, 0),
    node('fe', 0, 0, { type: 'foreach' }), node('x', 0, 0, { parentNode: 'fe' }),
  ]
  const edges = [edge('s1', 'j'), edge('s2', 'j', 'right'), edge('j', 'g'), edge('g', 'z'), edge('g', 'fe'),
    edge('s1', 'g', 'seq-in', { sourceHandle: 'seq-out' })]
  assert.deepEqual([...upstreamEdgeIds(nodes, edges, 'g')].sort(), ['e-j-left-g', 'e-s1-left-j', 'e-s2-right-j'])
  assert.deepEqual([...upstreamEdgeIds(nodes, edges, 's1')], [])
  // a node of a foreach body: what its container reads
  assert.ok(upstreamEdgeIds(nodes, edges, 'x').has('e-g-left-fe'))
  assert.ok(upstreamEdgeIds(nodes, edges, 'x').has('e-s1-left-j'))
})

test('a flow ends at its outputs, or where nothing reads on', () => {
  assert.deepEqual(flowEndIds(CHAIN, CHAIN_EDGES), ['out'])
  assert.deepEqual(flowEndIds(CHAIN.slice(0, 3), CHAIN_EDGES.slice(0, 2)), ['b'])
  assert.deepEqual(flowEndIds([...CHAIN.slice(0, 3), node('note', 0, 0, { type: 'comment' })], CHAIN_EDGES.slice(0, 2)), ['b'])
})

test('a flow that fits at a legible zoom opens whole', () => {
  const rects = [{ x: 0, y: 0, width: 170, height: 70 }, { x: 460, y: 0, width: 170, height: 70 }]
  assert.equal(openingViewport(rects, [rects[1]], { width: 1100, height: 420 }), null)
  assert.equal(openingViewport([], [], { width: 1100, height: 420 }), null)
})

test('a long flow opens at the legible zoom, on its end', () => {
  // twelve steps in a row: the fit would be 1100 / (2700 × 1.15) = 0.35
  const rects = Array.from({ length: 12 }, (_, i) => ({ x: 60 + i * 230, y: 140, width: 170, height: 70 }))
  const pane = { width: 1100, height: 420 }
  const v = openingViewport(rects, [rects[11]], pane, { marginRight: 220 })
  assert.equal(v.zoom, 0.6)
  // the last node ends at the minimap's left, and is on screen whole
  const right = (rects[11].x + 170) * v.zoom + v.x
  assert.equal(Math.round(right), 1100 - 220)
  // one row fits in height: it is centred
  assert.equal(Math.round((140 + 35) * v.zoom + v.y), 210)
  // the start is off screen, in the minimap
  assert.ok(60 * v.zoom + v.x < 0)
})

test('a tall flow keeps its end in view without showing the emptiness under it', () => {
  const rects = Array.from({ length: 10 }, (_, i) => ({ x: 60, y: i * 150, width: 170, height: 70 }))
  const v = openingViewport(rects, [rects[9]], { width: 1100, height: 420 })
  assert.equal(v.zoom, 0.6)
  const bottom = (rects[9].y + 70) * v.zoom + v.y
  assert.ok(bottom <= 420 - 48 + 1 && bottom > 300, String(bottom))
  // it fits in width: centred
  assert.equal(Math.round((60 + 85) * v.zoom + v.x), 550)
})

test('the easing ends where it must', () => {
  assert.equal(easeOutExpo(0), 0)
  assert.equal(easeOutExpo(1), 1)
  assert.ok(easeOutExpo(0.3) > 0.8) // most of the way early, as --ease-out
})
