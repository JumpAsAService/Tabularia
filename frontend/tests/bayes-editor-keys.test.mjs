// The keys of the editor of Bayesian models (composables/bayesEditorKeys.ts), on a PC's keyboard and on a
// Mac's: every row of the spec's «Keyboard» table has a way on each, none needs an F key, Home, End or the
// Menu key, none is Alt with a letter or three modifiers, and each way asks for the action of its row.
// Run: npm test
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { register } from 'node:module'

register('./alias-hooks.mjs', import.meta.url)
const K = await import('~/composables/bayesEditorKeys.ts')

const kinds = ['walk', 'move', 'add', 'menu', 'remove', 'duplicate', 'undo', 'redo', 'zoomIn', 'zoomOut', 'fit']

test('the table lists every action of the spec, each with a way on both keyboards', () => {
  assert.deepEqual(K.BAYES_KEY_TABLE.map(r => r.id), ['into', 'walk', 'move', 'add', 'menu', 'remove', 'duplicate', 'undo', 'redo', 'save', 'zoomIn', 'zoomOut', 'fit', 'tabs', 'escape', 'help'])
  for (const k of kinds) assert.ok(K.BAYES_KEY_TABLE.some(r => r.action === k), k)
  for (const r of K.BAYES_KEY_TABLE) {
    for (const [name, ways] of [['pc', r.pc], ['mac', r.mac]]) {
      assert.ok(ways.length > 0, `${r.id} has no way on ${name}`)
      assert.ok(ways.some(w => !K.bayesKeyMissingOnLaptops(w)), `${r.id} on ${name} needs a key that a laptop may lack`)
      for (const w of ways) assert.ok(!K.bayesKeyForbidden(w), `${r.id} on ${name}: ${JSON.stringify(w)}`)
    }
    if (!r.action) assert.ok(r.elsewhere, `${r.id} says where it lives`)
  }
})

for (const mac of [false, true]) {
  test(`every way of every row asks for its action on a ${mac ? 'Mac' : 'PC'}`, () => {
    for (const r of K.BAYES_KEY_TABLE) {
      for (const w of mac ? r.mac : r.pc) {
        const got = K.bayesKeyAction(K.bayesKeyEventOf(w, mac), mac)
        if (!r.action) { assert.equal(got, null, `${r.id} is not this module's: ${JSON.stringify(w)}`); continue }
        assert.equal(got?.kind, r.action, `${r.id}: ${JSON.stringify(w)}`)
        if (r.action === 'walk' || r.action === 'move') assert.equal(got.dir, { ArrowLeft: 'left', ArrowRight: 'right', ArrowUp: 'up', ArrowDown: 'down' }[w.key])
      }
    }
  })
}

test('redo: Ctrl+Y only off a Mac, where ⌘Y is the browser’s history', () => {
  assert.equal(K.bayesKeyAction({ key: 'y', ctrlKey: true }, false)?.kind, 'redo')
  assert.equal(K.bayesKeyAction({ key: 'y', metaKey: true }, true), null)
  assert.equal(K.bayesKeyAction({ key: 'y', ctrlKey: true }, true), null)
  assert.equal(K.bayesKeyAction({ key: 'Z', metaKey: true, shiftKey: true }, true)?.kind, 'redo')
  assert.equal(K.bayesKeyAction({ key: 'z', ctrlKey: true, shiftKey: true }, false)?.kind, 'redo')
})

test('a bare arrow moves nothing, Alt walks, Shift moves, both together do neither', () => {
  assert.equal(K.bayesKeyAction({ key: 'ArrowUp' }, false), null)
  assert.deepEqual(K.bayesKeyAction({ key: 'ArrowUp', altKey: true }, true), { kind: 'walk', dir: 'up' })
  assert.deepEqual(K.bayesKeyAction({ key: 'ArrowUp', shiftKey: true }, true), { kind: 'move', dir: 'up' })
  assert.equal(K.bayesKeyAction({ key: 'ArrowUp', altKey: true, shiftKey: true }, false), null)
  assert.equal(K.bayesKeyAction({ key: 'ArrowUp', ctrlKey: true }, false), null)
  assert.deepEqual(K.bayesMoveStep('left'), [-10, 0])
  assert.deepEqual(K.bayesMoveStep('down'), [0, 10])
  assert.equal(K.bayesArrowDirection('ArrowRight'), 'right')
  assert.equal(K.bayesArrowDirection('a'), null)
})

test('Alt with a letter does nothing, nor does a letter with Control (the browser’s keys stay)', () => {
  for (const key of 'nmd0') assert.equal(K.bayesKeyAction({ key, altKey: true }, false), null, key)
  for (const mac of [false, true]) {
    for (const key of ['n', 'm', '0', '+', '-', 'Delete', 'Backspace']) {
      assert.equal(K.bayesKeyAction({ key, ctrlKey: true }, mac), null, key)
      assert.equal(K.bayesKeyAction({ key, metaKey: true }, mac), null, key)
    }
    assert.equal(K.bayesKeyAction({ key: 'd', metaKey: true, shiftKey: true }, mac), null)
    assert.equal(K.bayesKeyAction({ key: 'z', ctrlKey: true, altKey: true }, mac), null)
  }
})

test('the menu: m, the Menu key and Shift+F10, but not a bare F10 nor Shift+m', () => {
  for (const e of [{ key: 'm' }, { key: 'ContextMenu' }, { key: 'F10', shiftKey: true }]) assert.equal(K.bayesKeyAction(e, false)?.kind, 'menu')
  assert.equal(K.bayesKeyAction({ key: 'F10' }, false), null)
  assert.equal(K.bayesKeyAction({ key: 'M', shiftKey: true }, false), null)
  assert.equal(K.bayesKeyAction({ key: 'N', shiftKey: true }, false), null)
})

test('zoom: + or = in, - out, 0 fits; the scopes of the actions', () => {
  assert.equal(K.bayesKeyAction({ key: '=' }, true)?.kind, 'zoomIn')
  assert.equal(K.bayesKeyAction({ key: '+', shiftKey: true }, false)?.kind, 'zoomIn')
  for (const k of ['undo', 'redo', 'duplicate']) assert.equal(K.bayesKeyScope(k), 'page')
  for (const k of ['walk', 'move', 'add', 'menu', 'remove', 'zoomIn', 'zoomOut', 'fit']) assert.equal(K.bayesKeyScope(k), 'canvas')
})

test('the checks catch what they are meant to', () => {
  assert.ok(K.bayesKeyMissingOnLaptops({ key: 'F2' }) && K.bayesKeyMissingOnLaptops({ key: 'Home' }) && K.bayesKeyMissingOnLaptops({ key: 'ContextMenu' }))
  assert.ok(!K.bayesKeyMissingOnLaptops({ key: 'm' }))
  assert.ok(K.bayesKeyForbidden({ key: 'n', alt: true }) && K.bayesKeyForbidden({ key: 'ArrowUp', alt: true, shift: true, mod: true }))
  assert.ok(!K.bayesKeyForbidden({ key: 'ArrowUp', alt: true }))
})
