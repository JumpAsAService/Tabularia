// The saved Bayesian models in Explore, the search, the recent objects and the audit page:
// where a model opens, what its row offers, the labels of its family and compile state, and
// that the audit page names every action the gateway writes (api/bayes_models.go).
// Run: npm test
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { register } from 'node:module'

register('./alias-hooks.mjs', import.meta.url)
const L = await import('../composables/exploreList.ts')
const S = await import('../composables/useSearch.ts')
const B = await import('../composables/useBayesModels.ts')
const en = (await import('../i18n/locales/en.gen.ts')).default
const langs = {
  en, it: (await import('../i18n/locales/it.gen.ts')).default, fr: (await import('../i18n/locales/fr.gen.ts')).default,
  de: (await import('../i18n/locales/de.gen.ts')).default, es: (await import('../i18n/locales/es.gen.ts')).default,
}

test('a model opens its editor from the recent objects and from a search hit', () => {
  assert.equal(L.objectTarget('bayes', 7), '/bayes-model?id=7')
  assert.equal(S.searchHitTarget({ kind: 'bayes_model', id: 7, name: 'm', project_id: 1, project_name: 'f', detail: '' }), '/bayes-model?id=7')
})

test('a model\'s row: rename, duplicate, move, delete; no favourite, no run history', () => {
  const ids = L.rowActions({ kind: 'bayes' }).map((a) => a.id)
  assert.deepEqual(ids, ['rename', 'duplicate', 'move', 'delete'])
  assert.equal(L.rowActions({ kind: 'bayes' }).at(-1).sep, true)
  assert.equal(L.primaryAction({ kind: 'bayes' }), null)
  assert.equal(L.lineageType('bayes'), null)
})

test('the family is read as the gateway derives it; anything else is free-form', () => {
  for (const f of ['hierarchical', 'series', 'glm', 'free']) assert.equal(B.bayesFamilyKey(f), `bayesModels.family_${f}`)
  for (const f of ['', null, undefined, 'weird']) assert.equal(B.bayesFamilyKey(f), 'bayesModels.family_free')
})

test('every language names the families, the compile states, the kind and the audit actions', () => {
  const keys = [
    ['bayesModels', ['family_glm', 'family_hierarchical', 'family_series', 'family_free',
      'compile_none', 'compile_queued', 'compile_running', 'compile_ready', 'compile_failed', 'compile_timeout']],
    ['projectBrowser', ['kind_bayes', 'confirmDeleteBayesModel', 'bayesModelDeleted']],
    ['globalSearch', ['kind_bayes_model']],
    ['audit', ['bayesModelCreateAction', 'bayesModelUpdateAction', 'bayesModelRestoreAction', 'bayesModelDeleteAction', 'bayesModelCompileAction']],
  ]
  for (const [lang, cat] of Object.entries(langs)) {
    for (const [ns, names] of keys) for (const k of names) {
      assert.equal(typeof cat[ns]?.[k], 'string', `${lang}: ${ns}.${k}`)
      assert.notEqual(cat[ns][k].trim(), '', `${lang}: ${ns}.${k}`)
    }
    // there is no «with errors» state: a model with errors is never stored
    assert.equal(cat.bayesModels.compile_error, undefined)
  }
  // a compile state or a family differs from the others in every language
  for (const [lang, cat] of Object.entries(langs)) {
    const states = ['none', 'queued', 'running', 'ready', 'failed', 'timeout'].map((s) => cat.bayesModels[`compile_${s}`])
    assert.equal(new Set(states).size, 6, `${lang}: the compile states read apart`)
  }
})

test('the audit page labels every action the gateway writes for a model', () => {
  const page = readFileSync(new URL('../pages/audit.vue', import.meta.url), 'utf8')
  for (const a of ['create', 'update', 'restore', 'delete', 'compile']) {
    assert.match(page, new RegExp(`'bayes_model\\.${a}': \\{ icon: Sigma, color: u\\.\\w+, label: t\\('audit\\.bayesModel${a[0].toUpperCase()}${a.slice(1)}Action'\\) \\}`), a)
  }
})

test('Explore lists the models of a folder through the client, one request for the list', () => {
  const page = readFileSync(new URL('../components/ProjectBrowser.vue', import.meta.url), 'utf8')
  assert.match(page, /settle\(bayesApi\.listByProject\(id\)\)/)
  assert.match(page, /kind: 'bayes', id: b\.id/)
  const client = readFileSync(new URL('../composables/useBayesModels.ts', import.meta.url), 'utf8')
  assert.match(client, /`\/projects\/\$\{projectId\}\/bayes-models`/)
})

test('the client knows the state timeout and the 409 of «Compile now»', () => {
  const client = readFileSync(new URL('../composables/useBayesModels.ts', import.meta.url), 'utf8')
  assert.match(client, /export type BayesCompileState = 'none' \| 'queued' \| 'running' \| 'ready' \| 'failed' \| 'timeout'/)
  assert.match(client, /409 with\s+\* the state's sentence/)
})
