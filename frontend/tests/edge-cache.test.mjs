// A data edge leaving a cached node shows dots passing (`edge-cached`); the working dashes win.
// Run: npm test
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { edgeCacheClass, edgeCacheKey, hasEdgeCacheClass, withEdgeCacheClass } from '../composables/edgeCache.ts'

const css = readFileSync(new URL('../assets/main.css', import.meta.url), 'utf8')
const cached = new Set(['a'])

test('an edge whose source is cached gets the class, one whose source is not does not', () => {
  assert.equal(edgeCacheClass('a', cached), 'edge-cached')
  assert.equal(edgeCacheClass('b', cached), '')
})

test('sequence edges and edges the engine works along get none', () => {
  assert.equal(edgeCacheClass('a', cached, { seq: true }), '')
  assert.equal(edgeCacheClass('a', cached, { animated: true }), '')
})

test('the class is added and removed without touching the others', () => {
  assert.equal(withEdgeCacheClass(undefined, true), 'edge-cached')
  assert.equal(withEdgeCacheClass('x', true), 'x edge-cached')
  assert.equal(withEdgeCacheClass('x edge-cached', true), 'x edge-cached')
  assert.equal(withEdgeCacheClass('x edge-cached', false), 'x')
  assert.equal(withEdgeCacheClass('edge-cached', false), undefined)
})

test('main.css animates the cached edge in the cache colour, and stops it under reduced motion', () => {
  const rule = css.match(/\.vue-flow__edge\.edge-cached[^{]*\{([^}]*)\}/)
  assert.ok(rule, 'the .edge-cached rule is missing')
  assert.match(rule[1], /stroke:\s*var\(--cache-ring\)/)
  assert.match(rule[1], /animation:\s*cachedflow/)
  assert.match(css, /:not\(\.animated\)[^{]*\.vue-flow__edge-path/)
  const reduced = css.match(/@media \(prefers-reduced-motion: reduce\) \{\s*\.canvas[^{]*edge-cached[^{]*\{([^}]*)\}/)
  assert.ok(reduced, 'no reduced-motion rule for .edge-cached')
  assert.match(reduced[1], /animation:\s*none/)
})

test('the dots travel for a few turns and stop: an animation of a stroke that never ends keeps a core busy at rest', () => {
  const rule = css.match(/\.vue-flow__edge\.edge-cached[^{]*\{([^}]*)\}/)
  const turns = rule[1].match(/animation:\s*cachedflow\s+[\d.]+s\s+linear\s+(\S+);/)
  assert.ok(turns, 'the animation is not the one this test knows')
  assert.notEqual(turns[1], 'infinite')
  assert.ok(Number(turns[1]) >= 1 && Number(turns[1]) <= 8, `turns: ${turns[1]}`)
})

test('the marks are set again when edges are rebuilt without them, and when the cached nodes change', () => {
  const marked = [{ id: 'e1', source: 'a', class: 'edge-seq edge-cached' }, { id: 'e2', source: 'b' }]
  // an undo: the same edges, the same ids, no class
  const rebuilt = [{ id: 'e1', source: 'a' }, { id: 'e2', source: 'b' }]
  const cached = new Set(['a'])
  assert.notEqual(edgeCacheKey(marked, cached), edgeCacheKey(rebuilt, cached))
  // the cache emptied with the edges untouched (the source lost its table)
  assert.notEqual(edgeCacheKey(marked, cached), edgeCacheKey(marked, new Set()))
  assert.notEqual(edgeCacheKey(rebuilt, new Set(['a'])), edgeCacheKey(rebuilt, new Set(['b'])))
  // nothing changed: the same key, whatever the order the cached nodes came in (no loop)
  assert.equal(edgeCacheKey(marked, new Set(['a', 'b'])), edgeCacheKey(marked, new Set(['b', 'a'])))
  // an edge worked along is another state
  assert.notEqual(edgeCacheKey(rebuilt, cached), edgeCacheKey([{ ...rebuilt[0], animated: true }, rebuilt[1]], cached))
  assert.equal(hasEdgeCacheClass('edge-seq edge-cached'), true)
  assert.equal(hasEdgeCacheClass('edge-cachedish'), false)
  assert.equal(hasEdgeCacheClass(undefined), false)
})

test('the editor sets the marks from that key', () => {
  const code = readFileSync(new URL('../components/FlowEditor.vue', import.meta.url), 'utf8')
  assert.match(code, /watch\(\(\) => edgeCacheKey\(getEdges\.value, cachedNodes\), syncCachedEdges\)/)
})
