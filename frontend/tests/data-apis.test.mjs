// Data APIs without a browser (docs/specs/data-apis.md): what the page offers for a column, the
// names it proposes, what it cleans before saving, and where Explore offers «New → Data API».
// The rules themselves are the gateway's (gateway-go/internal/dataapis), tested there.
// Run: npm test
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { register } from 'node:module'
import { readFileSync } from 'node:fs'

register('./alias-hooks.mjs', import.meta.url)
const m = await import('../composables/useDataApis.ts')

test('the operators follow the column\'s type, as the gateway decides', () => {
  assert.deepEqual(m.operatorsFor('Int64'), m.DATA_API_OPERATORS)
  assert.deepEqual(m.operatorsFor('Datetime(time_unit=\'us\', time_zone=None)'), m.DATA_API_OPERATORS)
  assert.deepEqual(m.operatorsFor('Decimal(precision=10, scale=2)'), m.DATA_API_OPERATORS)
  assert.deepEqual(m.operatorsFor('String'), ['eq', 'in', 'null'])
  assert.deepEqual(m.operatorsFor('Boolean'), ['eq', 'in', 'null'])
  assert.deepEqual(m.operatorsFor('List(String)'), [])
  assert.deepEqual(m.operatorsFor('Struct({\'a\': Int64})'), [])
})

test('a name from a datasource\'s is a valid label', () => {
  assert.equal(m.resourceName('Clienti attivi 2026'), 'clienti-attivi-2026')
  assert.equal(m.resourceName('  Città / Région  '), 'citta-region')
  assert.equal(m.resourceName('***'), 'resource')
  assert.ok(m.validLabel(m.resourceName('x'.repeat(80))))
  for (const ok of ['crm', 'crm-customers', 'a1']) assert.ok(m.validLabel(ok), ok)
  for (const bad of ['', '-crm', 'crm-', 'CRM', 'crm_x', 'a'.repeat(64)]) assert.ok(!m.validLabel(bad), bad)
})

test('a new resource sends every column, filters and sorts on the first one that can be', () => {
  const ds = { id: 7, name: 'Customers', columns: [{ name: 'tags', type: 'List(String)' }, { name: 'id', type: 'Int64' }, { name: 'name', type: 'String' }] }
  const r = m.newResource(ds, ['customers'])
  assert.equal(r.name, 'customers-2')
  assert.deepEqual(r.source, { kind: 'datasource', id: 7 })
  assert.deepEqual(r.columns, ['tags', 'id', 'name'])
  assert.deepEqual(r.filters, [{ column: 'id', operators: ['eq', 'in'] }])
  assert.deepEqual(r.default_sort, ['id'])
  assert.equal(r.max_page, 1000)
})

test('what no longer goes out is cleaned from the filters and the order', () => {
  const r = {
    name: 'c', source: { kind: 'datasource', id: 1 }, columns: ['id'], max_page: 10,
    filters: [{ column: 'id', operators: ['eq'] }, { column: 'name', operators: ['eq'] }, { column: 'score', operators: [] }],
    default_sort: ['-name', 'id'],
  }
  const t = m.tidyResource(r)
  assert.deepEqual(t.filters, [{ column: 'id', operators: ['eq'] }])
  assert.deepEqual(t.default_sort, ['id'])
})

test('keys: which answer, and the addresses as typed', () => {
  assert.ok(m.keyAnswers('active') && m.keyAnswers('grace'))
  assert.ok(!m.keyAnswers('revoked') && !m.keyAnswers('expired') && !m.keyAnswers('replaced'))
  assert.deepEqual(m.cidrsOf(' 203.0.113.0/24,\n198.51.100.7  \n'), ['203.0.113.0/24', '198.51.100.7'])
})

test('Explore offers «New → Data API» only where the installation has them, and where one may create it', () => {
  const vue = readFileSync(new URL('../components/ProjectBrowser.vue', import.meta.url), 'utf8')
  assert.match(vue, /dataApisApi\.limits\(\)\.then\(\(l\) => \(dataApisOn\.value = l\.enabled\)/)
  assert.match(vue, /const dataApiHere = computed\(\(\) => dataApisOn\.value && \(isSuper\.value \|\| \(currentId\.value !== null && canManage\.value\)\)\)/)
  assert.match(vue, /<button v-if="dataApiHere" class="menu-item" @click="createDataApi">/)
  assert.ok(vue.includes('navigateTo(`/data-api?project=${target.value}`)'))
})
