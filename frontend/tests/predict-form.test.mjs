// The Predict form's lists (components/ui/PredictForm.vue) without a browser. Found on kind: a node
// that already had its connection and its experiment showed the experiment's skeleton for ever,
// because the experiments' and the versions' watchers (both immediate) shared one counter, so the
// second cancelled the answer of the first. Each list has a counter of its own.
// Run: npm test
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { join } from 'node:path'

const FRONTEND = fileURLToPath(new URL('..', import.meta.url))
const source = readFileSync(join(FRONTEND, 'components/ui/PredictForm.vue'), 'utf8')

test('the experiments and the versions are asked with a counter each', () => {
  assert.doesNotMatch(source, /\+\+asked\b/, 'a counter shared by both lists cancels the first answer')
  assert.match(source, /const seq = \+\+askedExperiments[\s\S]*?seq === askedExperiments/)
  assert.match(source, /const seq = \+\+askedVersions[\s\S]*?seq === askedVersions/)
  // every answer is written only if it is still the last question of its own list
  assert.equal([...source.matchAll(/seq === askedExperiments/g)].length, 2)
  assert.equal([...source.matchAll(/seq === askedVersions/g)].length, 2)
})
