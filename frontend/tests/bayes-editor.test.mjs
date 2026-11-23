// Drawing a Bayesian model without a browser (composables/bayesGraphEdits.ts, useBayesEditor.ts): a
// node added from the palette lands where asked with a proposed name, a box nests and takes what it
// holds along when moved, a drop changes a node's box and the mirror's findings follow, an edge drawn
// by hand writes the slot the mirror accepts and an edge removed clears only a slot that held the
// reference alone; every gesture is one step of the history, typing one step when it stops, and
// undo and redo walk them.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { register } from 'node:module'

register('./alias-hooks.mjs', import.meta.url)
const E = await import('~/composables/bayesGraphEdits.ts')
const { bayesCanvasNodes, bayesCanvasLayout, bayesBoxes, BAYES_NODE_W, BAYES_NODE_H } = await import('~/composables/bayesCanvasModel.ts')
const { bayesOpenTemplate, bayesTemplates } = await import('~/composables/bayesTemplates.ts')
const { bayesGraphDecode, bayesGraphModel } = await import('~/composables/bayesGraph.ts')
const { useBayesEditor } = await import('~/composables/useBayesEditor.ts')
const { nextTick } = await import('vue')

const model = (g) => { const d = bayesGraphDecode(JSON.stringify(g)); assert.ok(d.graph, JSON.stringify(d.findings)); return bayesGraphModel(d.graph) }
const errorsOf = (g, id) => model(g).findings.filter(f => f.severity === 'error' && f.node.split('.')[0] === id)
const empty = () => ({ format_version: 1, plates: [{ id: 'rows', kind: 'rows', label: 'Rows' }], nodes: [], layout: { rows: { x: 0, y: 0, w: 600, h: 300 } } })

test('every kind of the palette adds a node the document can hold, its name proposed and unique', () => {
  const g = empty()
  const kinds = E.BAYES_PALETTE.flatMap(x => x.items)
  assert.equal(kinds.length, 12)
  for (const k of kinds) {
    const id = E.bayesAddItem(g, k, { x: 700, y: 10 * g.nodes.length }, k === 'parameter' ? null : 'rows', { label: `New ${k}`, components: ['Level', 'Slope'], correlation: 'Correlation' })
    assert.ok(g.layout[id], k)
  }
  // the document still decodes: every key is one the kind owns
  const d = bayesGraphDecode(JSON.stringify(g))
  assert.ok(d.graph, JSON.stringify(d.findings))
  const names = g.nodes.flatMap(n => [n.name, ...(n.components ?? []).map(c => c.name), n.correlation?.name]).filter(Boolean)
  assert.equal(new Set(names).size, names.length)
  assert.equal(g.nodes.find(n => n.type === 'real').name, 'new_real')
  assert.equal(g.nodes.find(n => n.type === 'real').standardize, true)
  assert.equal(g.plates.filter(p => p.kind === 'group').length, 1)
  assert.equal(model(g).findings.filter(f => f.code.startsWith('graph.name')).length, 0)
  // a new parameter's prior is written in the open, and checks
  const p = g.nodes.find(n => n.kind === 'parameter')
  assert.deepEqual(errorsOf(g, p.id), [])
})

test('a free spot: inside a box under its header, never on a node', () => {
  const g = bayesOpenTemplate('hier_intercepts')
  const grp = bayesBoxes(g).find(b => b.id === 'grp')
  const at = E.bayesFreeSpot(g, { x: 0, y: 0 }, grp)
  const r = { ...at, w: BAYES_NODE_W, h: BAYES_NODE_H }
  for (const n of g.nodes) {
    const o = { ...g.layout[n.id], w: BAYES_NODE_W, h: BAYES_NODE_H }
    assert.ok(!(r.x < o.x + o.w && o.x < r.x + r.w && r.y < o.y + o.h && o.y < r.y + r.h), `${n.id} overlaps`)
  }
  assert.ok(at.x >= grp.x && at.y >= grp.y + 30)
  // outside every box: wholly in a box or wholly out of it, never across an edge
  const rows = bayesBoxes(g).find(b => b.id === 'rows')
  const out = E.bayesFreeSpot(g, { x: rows.x + rows.w - 60, y: rows.y + 40 }, null)
  const o = { ...out, w: BAYES_NODE_W, h: BAYES_NODE_H }
  for (const b of bayesBoxes(g)) {
    const meets = o.x < b.x + b.w && b.x < o.x + o.w && o.y < b.y + b.h && b.y < o.y + o.h
    const inside = o.x >= b.x && o.y >= b.y && o.x + o.w <= b.x + b.w && o.y + o.h <= b.y + b.h
    assert.ok(!meets || inside, `${b.id}: across its edge`)
  }
})

test('a node dropped in a box takes its plate, and the findings follow; dropped outside it is a scalar', () => {
  const g = bayesOpenTemplate('hier_intercepts')
  const grp = g.layout.grp
  // the effect of the predictor dropped into the group box: one per group now
  assert.equal(E.bayesDropItem(g, 'bx', { x: grp.x + 10, y: grp.y + 50, w: BAYES_NODE_W, h: 56 }), true)
  assert.equal(g.nodes.find(n => n.id === 'bx').plate, 'grp')
  assert.ok(errorsOf(g, 'eta').length > 0, 'eta now reads a value per group without an index')
  assert.deepEqual(g.layout.bx, { x: grp.x + 10, y: grp.y + 50 })
  // out again, on the empty canvas
  E.bayesDropItem(g, 'bx', { x: -400, y: -400, w: BAYES_NODE_W, h: 56 })
  assert.equal('plate' in g.nodes.find(n => n.id === 'bx'), false)
  assert.deepEqual(errorsOf(g, 'eta'), [])
  // a group index dropped on a group box: the mirror says it may not live there, nothing is moved back
  E.bayesDropItem(g, 'g', { x: grp.x + 10, y: grp.y + 50, w: BAYES_NODE_W, h: 56 })
  assert.equal(g.nodes.find(n => n.id === 'g').plate, 'grp')
  assert.ok(model(g).findings.some(f => f.node === 'g' && f.code === 'graph.data_type' && f.args.reason === 'not_on_rows'))
  // a likelihood stays in its observed data's box
  assert.equal(E.bayesDropItem(g, 'lik', { x: -400, y: -400, w: BAYES_NODE_W, h: 56 }), false)
  assert.equal('plate' in g.nodes.find(n => n.id === 'lik'), false)
})

test('a box dropped in a box nests, moves what it holds, never goes into itself', () => {
  const g = bayesOpenTemplate('hier_nested')
  const before = JSON.parse(JSON.stringify(g.layout))
  // the district's box moved by (+100, +40): the school's box and every node inside follow
  E.bayesMoveItem(g, 'dst', { x: before.dst.x + 100, y: before.dst.y + 40 })
  assert.deepEqual(g.layout.sch, { ...before.sch, x: before.sch.x + 100, y: before.sch.y + 40 })
  assert.deepEqual(g.layout.us, { x: before.us.x + 100, y: before.us.y + 40 })
  assert.deepEqual(g.layout.mu, before.mu, 'a node outside stays')
  // the school's box dropped out of the district's: no parent; back in: nested again
  const sch = g.layout.sch
  E.bayesDropItem(g, 'sch', { x: 3000, y: 3000, w: sch.w, h: sch.h })
  assert.equal(g.plates.find(p => p.id === 'sch').parent, null)
  const dst = g.layout.dst
  E.bayesDropItem(g, 'sch', { x: dst.x + 20, y: dst.y + 50, w: sch.w, h: sch.h })
  assert.equal(g.plates.find(p => p.id === 'sch').parent, 'dst')
  // the district dropped on the school (its own child): nothing nests in itself
  assert.equal(E.bayesSetParent(g, 'dst', 'sch'), false)
  assert.equal(E.bayesDropTarget(g, 'dst', { x: g.layout.sch.x, y: g.layout.sch.y, w: 100, h: 100 }), null)
  // a group box over the rows box does not nest in it
  const rows = g.layout.rows
  assert.equal(E.bayesDropTarget(g, 'sch', { x: rows.x + 20, y: rows.y + 20, w: 200, h: 100 }), null)
  // the canvas draws the moved boxes and reads the same layout back
  assert.deepEqual(bayesCanvasLayout(bayesCanvasNodes(g)).us, g.layout.us)
})

test('a third level of boxes is drawn, and one past max_plate_depth is the mirror\'s finding', () => {
  const g = bayesOpenTemplate('hier_nested')
  const ids = []
  for (let i = 0; i < 3; i++) ids.push(E.bayesAddItem(g, 'group', { x: 2000 + i * 40, y: 2000 + i * 60 }, i ? ids[i - 1] : 'sch', { label: `Level ${i}` }))
  assert.equal(g.plates.find(p => p.id === ids[0]).parent, 'sch')
  assert.equal(bayesBoxes(g).find(b => b.id === ids[2]).depth, 4)
  assert.ok(model(g).findings.some(f => f.code === 'graph.plate_parent' && f.args?.reason === 'too_deep'))
  const back = bayesCanvasLayout(bayesCanvasNodes(g))
  for (const id of ids) assert.deepEqual(back[id], g.layout[id])
})

test('removing: a box gives what it holds to its parent, the rows box stays', () => {
  const g = bayesOpenTemplate('hier_nested')
  assert.deepEqual(E.bayesRemoveItem(g, 'rows'), { ok: false, reason: 'rows' })
  assert.deepEqual(E.bayesRemoveItem(g, 'sch'), { ok: true })
  assert.equal(g.nodes.find(n => n.id === 'us').plate, 'dst')
  assert.equal(g.layout.sch, undefined)
  E.bayesRemoveItem(g, 'dst')
  assert.equal('plate' in g.nodes.find(n => n.id === 'us'), false)
  assert.deepEqual(E.bayesRemoveItem(g, 'mu'), { ok: true })
  assert.ok(model(g).findings.some(f => f.severity === 'error'), 'the references to mu are findings now')
  assert.deepEqual(E.bayesRemoveItem(g, 'nope'), { ok: false, reason: 'unknown' })
})

test('duplicate: a copy beside, its names unique', () => {
  const g = bayesOpenTemplate('hier_slopes')
  const id = E.bayesDuplicateNode(g, 'eff')
  const copy = g.nodes.find(n => n.id === id)
  assert.notEqual(copy.components[0].name, g.nodes.find(n => n.id === 'eff').components[0].name)
  assert.equal(model(g).findings.filter(f => f.code === 'graph.name_taken').length, 0)
  assert.deepEqual(g.layout[id], { x: g.layout.eff.x + 32, y: g.layout.eff.y + 32 })
  assert.equal(E.bayesDuplicateNode(g, 'grp'), null)
})

test('an edge drawn by hand: the mirror chooses the slots, the slot gets the name or a ref', () => {
  const g = bayesOpenTemplate('hier_intercepts')
  // a new parameter towards the group's level: μ and σ can take it (σ with the mirror's warning that
  // it may not be positive: a warning never blocks)
  const tau = E.bayesAddItem(g, 'parameter', { x: -300, y: 0 }, null, { label: 'Other spread' })
  assert.deepEqual(E.bayesEdgeChoices(g, tau, 'ag').map(c => c.slot.path), ['prior.mu', 'prior.sigma'])
  // a value per group cannot be the scale of a scalar: the predictor's effect takes none of a_g
  assert.ok(!E.bayesEdgeChoices(g, 'ag', 'bx').length)
  g.nodes.find(n => n.id === tau).bounds = { lower: 0 }
  const c = E.bayesEdgeChoices(g, tau, 'ag').find(x => x.slot.path === 'prior.sigma')
  assert.equal(E.bayesWriteSlot(g, 'ag', c.slot.path, c.value), true)
  assert.deepEqual(g.nodes.find(n => n.id === 'ag').prior.args.sigma, { expr: 'other_spread' })
  assert.deepEqual(errorsOf(g, 'ag'), [])
  // a slot already holding the name is not offered again
  assert.ok(!E.bayesEdgeChoices(g, tau, 'ag').some(x => x.slot.path === 'prior.sigma'))
  // a data node towards a linear predictor: a new term
  const z = E.bayesAddItem(g, 'real', { x: 300, y: 400 }, 'rows', { label: 'Second predictor' })
  const terms = E.bayesEdgeChoices(g, z, 'eta').map(x => x.slot.path)
  assert.ok(terms.includes('terms.new'), terms.join())
  E.bayesWriteSlot(g, 'eta', 'terms.new', z)
  assert.equal(g.nodes.find(n => n.id === 'eta').terms.at(-1).data, z)
  // a source with no name is written as a ref
  const n = g.nodes.find(x => x.id === tau)
  delete n.name
  const refs = E.bayesEdgeChoices(g, tau, 'ag')
  assert.ok(refs.every(x => x.value.ref === tau), JSON.stringify(refs.map(x => x.value)))
  // nothing from a node to itself, nothing that makes a cycle
  assert.deepEqual(E.bayesEdgeChoices(g, 'ag', 'ag'), [])
  assert.ok(!E.bayesEdgeChoices(g, 'ag', 'mua').length, 'mu_a feeds a_g: a_g into mu_a is a cycle')
})

test('removing an edge clears a slot that held the reference alone, refuses one that holds more', () => {
  const g = bayesOpenTemplate('hier_intercepts')
  const edges = () => model(g).edges
  // tau_a → a_g: σ held only `tau_a`
  assert.deepEqual(E.bayesDisconnect(g, edges(), 'taua', 'ag'), { ok: true })
  assert.deepEqual(g.nodes.find(n => n.id === 'ag').prior.args.sigma, {})
  assert.ok(errorsOf(g, 'ag').some(f => f.slot === 'prior.sigma'))
  // a_g → eta: the intercept is `a_g[g]`, more than the name: refused, nothing changes
  const r = E.bayesDisconnect(g, edges(), 'ag', 'eta')
  assert.equal(r.ok, false)
  assert.equal(r.slot.path, 'intercept')
  assert.equal(r.name, 'a_g')
  assert.deepEqual(g.nodes.find(n => n.id === 'eta').intercept, { expr: 'a_g[g]' })
  // x → eta: the term goes, its coefficient with it
  assert.deepEqual(E.bayesDisconnect(g, edges(), 'x', 'eta'), { ok: true })
  assert.deepEqual(g.nodes.find(n => n.id === 'eta').terms, [])
  // y → the likelihood: the observed node is left empty
  assert.deepEqual(E.bayesDisconnect(g, edges(), 'y', 'lik'), { ok: true })
  assert.equal(g.nodes.find(n => n.id === 'lik').observed, '')
})

test('the rows box\'s series, a box\'s key and form, a label the name follows until the person writes one', () => {
  const g = bayesOpenTemplate('glm_linear')
  E.bayesSetSeries(g, true)
  assert.deepEqual(g.plates[0].series, { time: '', by: null })
  assert.ok(model(g).findings.some(f => f.code === 'graph.series'))
  const t = E.bayesAddItem(g, 'time', { x: 0, y: 0 }, 'rows', { label: 'Week' })
  E.bayesSetSeriesPart(g, 'time', t)
  assert.ok(!model(g).findings.some(f => f.code === 'graph.series'))
  E.bayesSetSeries(g, false)
  assert.equal(g.plates[0].series, undefined)

  const p = E.bayesAddItem(g, 'parameter', { x: 0, y: 0 }, null, { label: 'Parameter' })
  E.bayesSetLabel(g, p, 'Weekly trend')
  assert.equal(g.nodes.find(n => n.id === p).name, 'weekly_trend')
  E.bayesSetName(g, p, 'trend')
  E.bayesSetLabel(g, p, 'Trend of the week')
  assert.equal(g.nodes.find(n => n.id === p).name, 'trend', 'a name the person wrote stays')
  E.bayesSetName(g, p, '')
  assert.equal('name' in g.nodes.find(n => n.id === p), false)

  const h = bayesOpenTemplate('hier_intercepts')
  E.bayesSetForm(h, 'grp', false)
  assert.equal(h.plates[1].form, 'centred')
  E.bayesSetKey(h, 'grp', 'x')
  assert.ok(model(h).findings.some(f => f.code === 'graph.plate_key'))
})

test('every template still opens, draws and reads back after a drop on its own place', () => {
  for (const t of bayesTemplates()) {
    const g = bayesOpenTemplate(t.id, t.order ? { order: 3 } : {})
    const before = JSON.stringify(g)
    for (const n of g.nodes) {
      const at = g.layout[n.id]
      E.bayesDropItem(g, n.id, { ...at, w: BAYES_NODE_W, h: 56 })
    }
    assert.equal(JSON.stringify(g), before, `${t.id}: dropping every node where it is changes nothing`)
  }
})

// ---------------------------------------------------------------- the history

function fakeClock () {
  let now = 0
  const timers = new Map()
  let next = 1
  return {
    schedule: (fn, ms) => { const id = next++; timers.set(id, { at: now + ms, fn }); return id },
    cancel: (id) => timers.delete(id),
    advance (ms) {
      now += ms
      for (const [id, t] of [...timers].sort((a, b) => a[1].at - b[1].at)) if (t.at <= now) { timers.delete(id); t.fn() }
    },
  }
}

test('the history: a gesture is one step, typing one step when it stops, undo and redo walk them', async () => {
  const clock = fakeClock()
  const ed = useBayesEditor({ settleMs: 400, schedule: clock.schedule, cancel: clock.cancel })
  ed.load(bayesOpenTemplate('hier_intercepts'))
  await nextTick(); clock.advance(500)
  assert.equal(ed.canUndo.value, false)
  const id = ed.edit(d => E.bayesAddItem(d, 'parameter', { x: -300, y: 0 }, null, { label: 'Parameter' }))
  assert.equal(ed.canUndo.value, true)
  ed.edit(d => E.bayesMoveItem(d, id, { x: -320, y: 40 }))
  // typing a label letter by letter: one step when it stops
  for (const s of ['T', 'Tr', 'Tre', 'Trend']) { ed.edit(d => E.bayesSetLabel(d, id, s), false); await nextTick(); clock.advance(100) }
  clock.advance(500)
  assert.equal(ed.doc.value.nodes.find(n => n.id === id).name, 'trend')
  assert.ok(ed.model.value, 'the mirror reads the document')
  assert.equal(ed.undo(), true)
  assert.equal(ed.doc.value.nodes.find(n => n.id === id).label, 'Parameter', 'the whole word is one step')
  assert.equal(ed.undo(), true)
  assert.deepEqual(ed.doc.value.layout[id], { x: -300, y: 0 })
  ed.selected.value = id
  assert.equal(ed.undo(), true)
  assert.equal(ed.doc.value.nodes.some(n => n.id === id), false)
  assert.equal(ed.selected.value, null, 'a selection that no longer exists is dropped')
  assert.equal(ed.canUndo.value, false)
  assert.equal(ed.redo(), true)
  assert.equal(ed.redo(), true)
  assert.deepEqual(ed.doc.value.layout[id], { x: -320, y: 40 })
  // a new gesture after an undo drops the redo road
  ed.undo()
  ed.edit(d => E.bayesRemoveItem(d, 'mua'))
  assert.equal(ed.canRedo.value, false)
  // the findings follow the document at once
  assert.ok(ed.findings.value.some(f => f.severity === 'error'))
  ed.dispose()
})
