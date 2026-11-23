// The Bayesian model node's type is in every list of step nodes (docs/specs/bayes-flow-node.md, lot F0).
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = join(dirname(fileURLToPath(import.meta.url)), '..')
const read = (p) => readFileSync(join(root, p), 'utf8')

test('bayes_model is in the editor\'s sets of step nodes', () => {
  const editor = read('components/FlowEditor.vue')
  for (const name of ['STEP_TYPES', 'CONTROL_TYPES', 'DATA_PARENTS']) {
    assert.match(editor, new RegExp(`const ${name} = new Set\\(\\[[^\\]]*'bayes_model'`), name)
  }
  assert.match(editor, /bayes_model: MODEL_COLOR/)
  assert.ok(editor.includes("type === 'bayes_model' ? 'models.node'"))
  assert.match(editor, /modelReportNodeId = computed[\s\S]{0,300}type === 'bayes_model'/)
})

test('bayes_model is in the chain readers', () => {
  assert.match(read('composables/flowChain.ts'), /READABLE = new Set\(\[[^\]]*'bayes_model'/)
  assert.ok(read('composables/flowChain.ts').includes("parent.type === 'bayes_model' ? 'out'"))
  assert.ok(read('composables/useFlowModel.ts').includes("node.type === 'bayes_model'"))
})
