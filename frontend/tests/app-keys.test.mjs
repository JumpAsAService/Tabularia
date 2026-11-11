// The keys of every page: where «g» and a letter lead and for whom, what a key means away
// from a field, when a key is typing, the walk along a row of tabs, and how keys are
// written on a Mac. Without a browser: the pure rules of appKeys.ts.
// Run: npm test
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { register } from 'node:module'

register('./alias-hooks.mjs', import.meta.url)
const K = await import('../composables/appKeys.ts')
const en = (await import('../i18n/locales/en.ts')).default

const all = K.visibleSections({ scripting: true, observe: true })

test('every place of the bar has its own letter, taken from its English name', () => {
  const letters = K.NAV_SECTIONS.map((s) => s.key)
  assert.equal(new Set(letters).size, letters.length)
  for (const s of K.NAV_SECTIONS) {
    assert.match(s.key, /^[a-z]$/, s.id)
    assert.notEqual(s.key, 'g', s.id)
    const name = s.label.split('.').reduce((o, k) => o[k], en)
    assert.equal(typeof name, 'string', s.label)
    assert.ok(name.toLowerCase().includes(s.key), `${s.key} is not in «${name}»`)
  }
  assert.equal(new Set(K.NAV_SECTIONS.map((s) => s.to)).size, K.NAV_SECTIONS.length)
})

test('«g» leads only where this person has a link', () => {
  const plain = K.visibleSections({ scripting: false, observe: false })
  assert.deepEqual(plain.map((s) => s.id), ['explore', 'flows', 'datasources', 'lineage', 'viewer', 'reports', 'dashboards', 'assistant', 'connections', 'runs'])
  assert.ok(K.visibleSections({ scripting: true, observe: false }).some((s) => s.id === 'scripting'))
  assert.ok(all.some((s) => s.to === '/admin'))
  // with Administration hidden its letter leads nowhere, and is dropped
  const t0 = 1000
  assert.deepEqual(K.appKey({ key: 'm' }, t0, t0 + 100, plain), { act: 'swallow', pending: false })
  assert.deepEqual(K.appKey({ key: 'm' }, t0, t0 + 100, all), { act: 'goto', to: '/admin', pending: false })
})

test('«g» then a letter, within its time; anything else lets go', () => {
  const t0 = 5000
  assert.deepEqual(K.appKey({ key: 'g' }, null, t0, all), { act: 'pending', pending: true })
  assert.deepEqual(K.appKey({ key: 'f' }, t0, t0 + 400, all), { act: 'goto', to: '/flows', pending: false })
  assert.deepEqual(K.appKey({ key: 'e' }, t0, t0 + K.SEQUENCE_MS, all), { act: 'goto', to: '/', pending: false })
  // too late: the letter is the page's
  assert.deepEqual(K.appKey({ key: 'f' }, t0, t0 + K.SEQUENCE_MS + 1, all), { act: null, pending: false })
  // a letter alone is nothing here
  assert.deepEqual(K.appKey({ key: 'f' }, null, t0, all), { act: null, pending: false })
  // Shift going down while «g» waits is not the letter
  assert.deepEqual(K.appKey({ key: 'Shift' }, t0, t0 + 100, all), { act: null, pending: true })
  // a character that is no place is dropped; a key that is no character goes to the page
  assert.deepEqual(K.appKey({ key: 'z' }, t0, t0 + 100, all), { act: 'swallow', pending: false })
  assert.deepEqual(K.appKey({ key: 'g' }, t0, t0 + 100, all), { act: 'swallow', pending: false })
  assert.deepEqual(K.appKey({ key: 'ArrowDown' }, t0, t0 + 100, all), { act: null, pending: false })
  assert.deepEqual(K.appKey({ key: 'Escape' }, t0, t0 + 100, all), { act: null, pending: false })
  // a capital G, or «g» held down, does not start it
  assert.equal(K.appKey({ key: 'G' }, null, t0, all).act, null)
  assert.equal(K.appKey({ key: 'g', repeat: true }, null, t0, all).act, null)
})

test('nothing with Ctrl, ⌘ or Alt: those are the browser\'s and the system\'s', () => {
  for (const mod of ['ctrlKey', 'metaKey', 'altKey']) {
    for (const key of ['g', ',', '.', '?', '/', 'k']) {
      assert.deepEqual(K.appKey({ key, [mod]: true }, null, 0, all), { act: null, pending: false }, `${mod}+${key}`)
    }
    // and a chord ends a «g» that was waiting
    assert.deepEqual(K.appKey({ key: 'f', [mod]: true }, 10, 20, all), { act: null, pending: false })
  }
})

test('the symbols are read from the character, wherever the keyboard has it', () => {
  assert.deepEqual(K.appKey({ key: ',' }, null, 0, all), { act: 'tab', dir: -1, pending: false })
  assert.deepEqual(K.appKey({ key: '.' }, null, 0, all), { act: 'tab', dir: 1, pending: false })
  assert.equal(K.appKey({ key: '?' }, null, 0, all).act, 'help')   // Shift+' on an Italian keyboard
  assert.equal(K.appKey({ key: '/' }, null, 0, all).act, 'search') // Shift+7
  assert.equal(K.appKey({ key: ';' }, null, 0, all).act, null)
})

test('a character typed into a field is never a key of the app', () => {
  const typing = (tag, more = {}) => K.keysAreTyping({ tag, ...more })
  assert.ok(typing('INPUT'))
  assert.ok(typing('INPUT', { type: 'search' }))
  assert.ok(typing('INPUT', { type: 'number' }))
  assert.ok(typing('TEXTAREA'))
  assert.ok(typing('SELECT'))
  assert.ok(typing('DIV', { editable: true }))
  assert.ok(typing('DIV', { inEditor: true })) // Monaco types through a div
  assert.ok(!typing('BODY'))
  assert.ok(!typing('BUTTON'))
  assert.ok(!typing('TD'))
  assert.ok(!typing('INPUT', { type: 'checkbox' }))
  assert.ok(!typing('INPUT', { type: 'range' }))
})

test('«,» and «.» go round the row of tabs, past the ones that are off', () => {
  const row = (s) => [...s].map((c) => ({ selected: c === 'X', disabled: c === '-' }))
  assert.equal(K.stepTab(row('Xoo'), 1), 1)
  assert.equal(K.stepTab(row('ooX'), 1), 0)
  assert.equal(K.stepTab(row('Xoo'), -1), 2)
  assert.equal(K.stepTab(row('X-o'), 1), 2)
  assert.equal(K.stepTab(row('o-X'), -1), 0)
  assert.equal(K.stepTab(row('X--'), 1), -1) // nowhere to go
  assert.equal(K.stepTab(row('X'), 1), -1)
  assert.equal(K.stepTab(row('ooo'), 1), 0)  // none chosen yet
  assert.equal(K.stepTab(row('ooo'), -1), 2)
  assert.equal(K.stepTab([], 1), -1)
})

test('the arrows on a row of tabs: ← → on a row, ↑ ↓ too on a column, going round', () => {
  assert.equal(K.rovingKey('ArrowRight', 0, 3), 1)
  assert.equal(K.rovingKey('ArrowRight', 2, 3), 0)
  assert.equal(K.rovingKey('ArrowLeft', 0, 3), 2)
  assert.equal(K.rovingKey('Home', 2, 3), 0)
  assert.equal(K.rovingKey('End', 0, 3), 2)
  assert.equal(K.rovingKey('ArrowDown', 0, 3), -1) // a row: ↓ is the page's
  assert.equal(K.rovingKey('ArrowDown', 0, 3, true), 1)
  assert.equal(K.rovingKey('ArrowUp', 0, 3, true), 2)
  assert.equal(K.rovingKey('Enter', 0, 3), -1)
  assert.equal(K.rovingKey('ArrowRight', 0, 0), -1)
})

test('a key is written as its keyboard writes it', () => {
  assert.ok(K.platformIsMac({ platform: 'MacIntel' }))
  assert.ok(K.platformIsMac({ platform: '', userAgentData: { platform: 'macOS' } }))
  assert.ok(!K.platformIsMac({ platform: 'Linux x86_64' }))
  assert.ok(!K.platformIsMac({ platform: 'Win32', userAgentData: { platform: 'Windows' } }))
  const pc = K.keyNames(false)
  const mac = K.keyNames(true)
  assert.equal(pc.chord(pc.mod, pc.enter), 'Ctrl+Enter')
  assert.equal(mac.chord(mac.mod, mac.enter), '⌘↩')
  assert.equal(mac.chord(mac.alt, '←'), '⌥←')
  assert.equal(mac.chord(mac.mod, mac.shift, 'Z'), '⌘⇧Z')
  // a Mac is shown only what it has: no F2, no Shift+F10, no Home, no Del
  assert.equal(pc.ways({ pc: ['F2'], any: ['r'] }), 'F2 · r')
  assert.equal(mac.ways({ pc: ['F2'], any: ['r'] }), 'r')
  assert.equal(pc.ways({ pc: ['Del'], mac: ['⌘⌫'] }), 'Del')
  assert.equal(mac.ways({ pc: ['Del'], mac: ['⌘⌫'] }), '⌘⌫')
})
