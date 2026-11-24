// The history of the editors (composables/useFlowHistory.ts) without a browser, on a fake clock: after an undo the state
// settles (what the canvas adjusts is adopted, not recorded), but a change the PERSON makes inside that window is a step
// of their own: it can be undone and a redo does not overwrite it (review of the Bayesian editor, defect 3).
// Run: npm test
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { register } from 'node:module'

register('./alias-hooks.mjs', import.meta.url)
const { createFlowHistory } = await import('~/composables/useFlowHistory.ts')

function rig() {
  const timers = new Map()
  let id = 0
  const state = { v: 'a' }
  const h = createFlowHistory({
    serialize: () => state.v,
    restore: (s) => { state.v = s },
    schedule: (fn) => { timers.set(++id, fn); return id },
    cancel: (k) => timers.delete(k),
  })
  const tick = () => { const fns = [...timers.values()]; timers.clear(); fns.forEach((f) => f()) }
  const change = (v) => { state.v = v; h.touch() }
  return { h, state, tick, change }
}

test('a change made inside the settle window after an undo is a step: it undoes, and a redo does not overwrite it', () => {
  const { h, state, tick, change } = rig()
  h.touch(); tick() // the first still state
  change('b'); tick()
  assert.ok(h.undo())
  assert.equal(state.v, 'a')
  // the person types BEFORE the window closes: the editor tells the history a person acts
  h.adoptSettled()
  change('a typed'); tick()
  assert.equal(h.canRedo(), false, 'a new change drops the redo branch')
  assert.ok(h.undo())
  assert.equal(state.v, 'a', 'the typing is a step of its own')
  assert.ok(h.redo())
  assert.equal(state.v, 'a typed')
})

test('what the canvas adjusts after an undo (no person acts) is still adopted, not recorded', () => {
  const { h, state, tick, change } = rig()
  h.touch(); tick()
  change('b'); tick()
  h.undo()
  change('a measured') // the canvas measures its nodes
  tick()
  assert.equal(h.canUndo(), false)
  assert.equal(h.canRedo(), true)
})

test('adoptSettled does not close a step the person is still typing', () => {
  const { h, state, tick, change } = rig()
  h.touch(); tick()
  change('ab') // hot
  h.adoptSettled()
  change('abc'); tick()
  h.undo()
  assert.equal(state.v, 'a', 'one step for the whole typing')
})

test('the flow editor tells the history that a person acts (a key, a click) inside the settle window', async () => {
  const { readFileSync } = await import('node:fs')
  const src = readFileSync(new URL('../components/FlowEditor.vue', import.meta.url), 'utf8')
  assert.match(src, /addEventListener\('keydown', adoptSettled, true\)/)
  assert.match(src, /addEventListener\('pointerdown', adoptSettled, true\)/)
  assert.match(src, /removeEventListener\('keydown', adoptSettled, true\)/)
})
