// Saving a Bayesian model from its editor without a browser (composables/bayesModelSave.ts with the
// editor of useBayesEditor.ts and a fake client of the store's routes): a new model is created in its
// folder with the template it was opened from; a saved one sends `base_version` and only the name or
// folder that changed; a 422 puts the gateway's findings on the nodes in place of the mirror's until
// the next change; a 409 stale names the current version and «Reload» keeps the person's graph one
// Undo away; «unsaved changes» compares with the document as saved; a version looked at takes no edit.
// The list's rows (composables/bayesModelsList.ts) and the versions' words are here too.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { register } from 'node:module'

register('./alias-hooks.mjs', import.meta.url)
const S = await import('~/composables/bayesModelSave.ts')
const L = await import('~/composables/bayesModelsList.ts')
const E = await import('~/composables/bayesGraphEdits.ts')
const { bayesOpenTemplate } = await import('~/composables/bayesTemplates.ts')
const { useBayesEditor } = await import('~/composables/useBayesEditor.ts')
const { nextTick } = await import('vue')

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
const fetchError = (status, data) => Object.assign(new Error(`HTTP ${status}`), { response: { status }, data })
const detail = (over = {}) => ({
  id: 7, name: 'Scores', description: '', project_id: 3, owner_id: 1, current_version: 5, template_id: null, template_version: null,
  family: 'hierarchical', created_at: '', updated_at: '', editable: true, runnable: true, uses: 0,
  compile: { state: 'none', detail: '', at: null },
  version: { version: 5, note: 'edited', created_at: '', created_by: 1, graph: {}, findings: [{ code: 'graph.unused_data', severity: 'warning', node: 'cls' }] },
  ...over,
})
function client (answer) {
  const calls = []
  return {
    calls,
    create: async (projectId, body) => { calls.push(['create', projectId, body]); return answer('create', body) },
    update: async (id, body) => { calls.push(['update', id, body]); return answer('update', body) },
  }
}
function editor () {
  const clock = fakeClock()
  const ed = useBayesEditor({ settleMs: 400, schedule: clock.schedule, cancel: clock.cancel })
  return { ed, clock }
}
const target = (over = {}) => ({ id: 7, projectId: 3, baseVersion: 5, name: 'Scores', savedName: 'Scores', savedProjectId: 3, ...over })

test('a saved model: the graph with base_version, only what changed besides it, the version\'s findings on the nodes', async () => {
  const { ed } = editor()
  ed.load(bayesOpenTemplate('hier_intercepts'))
  assert.equal(ed.dirty.value, false)
  const id = ed.edit(d => E.bayesAddItem(d, 'parameter', { x: -300, y: 0 }, null, { label: 'Trend' }))
  assert.equal(ed.dirty.value, true, 'an edit is an unsaved change')
  const c = client(() => detail({ current_version: 6, version: { ...detail().version, version: 6 } }))
  const out = await S.bayesSaveModel(ed, c, target())
  assert.equal(out.kind, 'saved')
  assert.equal(out.created, false)
  const [kind, mid, body] = c.calls[0]
  assert.equal(kind, 'update')
  assert.equal(mid, 7)
  assert.equal(body.base_version, 5)
  assert.ok(body.graph.nodes.some(n => n.id === id), 'the graph as drawn')
  assert.equal('name' in body, false)
  assert.equal('project_id' in body, false)
  assert.equal(ed.dirty.value, false, 'the document sent is the saved one')
  assert.equal(ed.fromGateway.value, true)
  assert.deepEqual(ed.findings.value, [{ code: 'graph.unused_data', severity: 'warning', node: 'cls', slot: '' }])
  // the first change gives the word back to the mirror
  ed.edit(d => E.bayesSetLabel(d, id, 'Trend 2'))
  assert.equal(ed.fromGateway.value, false)
  assert.equal(ed.dirty.value, true)
  // a rename and a move go with the save
  const c2 = client(() => detail())
  await S.bayesSaveModel(ed, c2, target({ name: '  New name ', projectId: 4 }))
  assert.equal(c2.calls[0][2].name, 'New name')
  assert.equal(c2.calls[0][2].project_id, 4)
})

test('a new model is created in its folder, with the template it was opened from', async () => {
  const { ed } = editor()
  ed.load(bayesOpenTemplate('glm_logistic'))
  const c = client(() => detail({ id: 9, current_version: 1 }))
  const out = await S.bayesSaveModel(ed, c, { id: null, projectId: 12, baseVersion: null, name: 'Churn', savedName: null, savedProjectId: null, templateId: 'glm_logistic', templateVersion: 1 })
  assert.equal(out.kind, 'saved')
  assert.equal(out.created, true)
  const [kind, project, body] = c.calls[0]
  assert.equal(kind, 'create')
  assert.equal(project, 12)
  assert.equal(body.name, 'Churn')
  assert.equal(body.template_id, 'glm_logistic')
  assert.equal(body.template_version, 1)
  // the free model sends no template
  const c2 = client(() => detail())
  await S.bayesSaveModel(ed, c2, { id: null, projectId: 12, baseVersion: null, name: 'Free', savedName: null, savedProjectId: null })
  assert.equal('template_id' in c2.calls[0][2], false)
})

test('a graph with an error: the 422\'s findings replace the mirror\'s until the graph changes, nothing is saved', async () => {
  const { ed } = editor()
  ed.load(bayesOpenTemplate('hier_intercepts'))
  ed.edit(d => E.bayesAddItem(d, 'parameter', { x: -300, y: 0 }, null, { label: 'Trend' }))
  const mirror = ed.findings.value
  const gateway = [
    { code: 'graph.slot', severity: 'error', node: 'ag', slot: 'prior.mu', args: { reason: 'x' } },
    { code: 'graph.unused_parameter', severity: 'warning', node: 'p1' },
  ]
  const c = client(() => { throw fetchError(422, { detail: 'the graph has errors', findings: gateway }) })
  const out = await S.bayesSaveModel(ed, c, target())
  assert.deepEqual(out, { kind: 'refused', errors: 1, warnings: 1 })
  assert.equal(ed.dirty.value, true, 'nothing was saved')
  assert.equal(ed.fromGateway.value, true)
  assert.deepEqual(ed.findings.value.map(f => [f.code, f.node, f.slot]), [['graph.slot', 'ag', 'prior.mu'], ['graph.unused_parameter', 'p1', '']])
  assert.deepEqual(ed.findings.value[0].args, { reason: 'x' })
  ed.edit(d => E.bayesSetLabel(d, 'ag', 'Level of a school'))
  assert.equal(ed.fromGateway.value, false)
  // the mirror's own findings again: the gateway's made-up one is gone
  assert.equal(ed.findings.value.some(f => f.args?.reason === 'x'), false)
  assert.ok(Array.isArray(mirror))
  // a 422 that is not a graph's (an unknown key) is a failure, the mirror keeps the word
  const c2 = client(() => { throw fetchError(422, { detail: 'unknown field "x"' }) })
  const out2 = await S.bayesSaveModel(ed, c2, target())
  assert.equal(out2.kind, 'failed')
  assert.equal(out2.detail, 'unknown field "x"')
  assert.equal(ed.fromGateway.value, false)
})

test('a stale save names the current version; «Reload» is one step, the person\'s graph one Undo away', async () => {
  const { ed, clock } = editor()
  ed.load(bayesOpenTemplate('hier_intercepts'))
  await nextTick(); clock.advance(500)
  const mine = ed.edit(d => E.bayesAddItem(d, 'parameter', { x: -300, y: 0 }, null, { label: 'Mine' }))
  const c = client(() => { throw fetchError(409, { detail: 'version 6 was saved by someone else meanwhile: reload to see it', current_version: 6 }) })
  const out = await S.bayesSaveModel(ed, c, target())
  assert.deepEqual(out, { kind: 'stale', current: 6 })
  // the page reloads: the version saved meanwhile as one step of the history
  const theirs = bayesOpenTemplate('hier_intercepts')
  theirs.nodes[0].label = 'Theirs'
  ed.adopt(theirs)
  ed.markSaved(ed.text.value)
  assert.equal(ed.dirty.value, false)
  assert.equal(ed.doc.value.nodes.some(n => n.id === mine), false)
  assert.equal(ed.undo(), true)
  assert.ok(ed.doc.value.nodes.some(n => n.id === mine), 'Undo brings the person\'s graph back')
  assert.equal(ed.dirty.value, true)
  // a 409 without a version is the name taken in that folder
  const c2 = client(() => { throw fetchError(409, { detail: 'There is already a Bayesian model with that name in this folder' }) })
  assert.deepEqual(await S.bayesSaveModel(ed, c2, target({ name: 'Taken' })), { kind: 'name' })
})

test('a version looked at takes no edit, no undo, no redo', async () => {
  const { ed } = editor()
  ed.load(bayesOpenTemplate('glm_linear'))
  ed.readonly.value = true
  const before = ed.text.value
  assert.equal(ed.edit(d => E.bayesAddItem(d, 'parameter', { x: 0, y: 0 }, null, { label: 'X' })), undefined)
  assert.equal(ed.text.value, before)
  assert.equal(ed.undo(), false)
  assert.equal(ed.dirty.value, false)
})

test('the gateway\'s findings in the mirror\'s shape; a fetch error\'s status and body', () => {
  assert.deepEqual(S.bayesGatewayFindings([{ code: 'graph.cycle', severity: 'error' }, { severity: 'warning' }, null]), [{ code: 'graph.cycle', severity: 'error', node: '', slot: '' }])
  assert.deepEqual(S.bayesFetchError(fetchError(404, { detail: 'Bayesian model not found' })), { status: 404, data: { detail: 'Bayesian model not found' } })
  assert.deepEqual(S.bayesFetchError(new Error('network')), { status: 0, data: {} })
})

test('the versions: their notes in words, and whether a newer one changes the model or only its drawing', () => {
  assert.deepEqual(S.bayesVersionNote('restored from v3'), { key: 'restored', params: { n: 3 } })
  assert.deepEqual(S.bayesVersionNote('duplicated from «Scores» v2'), { key: 'duplicated', params: { name: 'Scores', n: 2 } })
  for (const n of ['created', 'edited', 'layout']) assert.deepEqual(S.bayesVersionNote(n), { key: n, params: {} })
  assert.deepEqual(S.bayesVersionNote('something new'), { key: 'other', params: { note: 'something new' } })
  const vs = [
    { version: 1, semantic_hash: 'a', current: false },
    { version: 2, semantic_hash: 'b', current: false },
    { version: 3, semantic_hash: 'b', current: true },
  ]
  assert.deepEqual(S.bayesNewerVersion(vs, 1), { current: 3, layoutOnly: false })
  assert.deepEqual(S.bayesNewerVersion(vs, 2), { current: 3, layoutOnly: true })
  assert.equal(S.bayesNewerVersion(vs, 3), null)
  assert.equal(S.bayesNewerVersion([], 1), null)
})

test('the list: the filter reads name, description, folder and template; the compile cell', () => {
  const rows = [
    { name: 'Punteggi per scuola', description: 'Alunni', project_id: 1, template_id: 'hier_nested' },
    { name: 'Resi', description: 'Conteggio dei resi', project_id: 2, template_id: null },
  ]
  const path = (id) => (id === 1 ? 'Didattica / Valutazioni' : 'Logistica')
  const title = (id) => (id === 'hier_nested' ? 'Gruppi dentro gruppi' : id)
  assert.deepEqual(L.bayesFilterModels(rows, '', path).length, 2)
  assert.deepEqual(L.bayesFilterModels(rows, 'PUNTEGGI', path).map(r => r.name), ['Punteggi per scuola'])
  assert.deepEqual(L.bayesFilterModels(rows, 'logistica', path).map(r => r.name), ['Resi'])
  assert.deepEqual(L.bayesFilterModels(rows, 'gruppi dentro', path, title).map(r => r.name), ['Punteggi per scuola'])
  assert.deepEqual(L.bayesFilterModels(rows, 'conteggio', path).map(r => r.name), ['Resi'])
  assert.deepEqual(L.bayesCompileCell({ state: 'ready', detail: '', at: null }), { key: 'bayesModels.compile_ready', sub: '', tone: 'ok' })
  assert.deepEqual(L.bayesCompileCell({ state: 'running', detail: '', at: '2026-11-24T15:02:00Z' }), { key: 'bayesModels.compile_running', sub: 'at', tone: 'run' })
  assert.deepEqual(L.bayesCompileCell({ state: 'failed', detail: 'timeout', at: null }), { key: 'bayesModels.compile_failed', sub: 'detail', tone: 'ko' })
  assert.deepEqual(L.bayesCompileCell({ state: 'none', detail: '', at: null }), { key: 'bayesModels.list.toCompile', sub: 'firstRun', tone: 'none' })
  assert.deepEqual(L.bayesCompileCell(undefined).tone, 'none')
})
