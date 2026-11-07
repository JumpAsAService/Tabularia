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

test('a column\'s kind is the gateway\'s (dataapiwire.KindOfPolars), which the pods agree with', () => {
  const cases = {
    Int64: 'integer', UInt8: 'integer', 'Duration(time_unit=\'us\')': 'integer', Int128: 'other', Float32: 'float',
    'Decimal(precision=12, scale=2)': 'decimal', Boolean: 'boolean', String: 'string', Categorical: 'string',
    'Enum(categories=[\'x\'])': 'string', Date: 'date', 'Datetime(time_unit=\'us\', time_zone=None)': 'timestamp',
    'Datetime(time_unit=\'us\', time_zone=\'Europe/Rome\')': 'timestamptz', Time: 'time', Binary: 'other', 'List(Int64)': 'other',
  }
  for (const [type, kind] of Object.entries(cases)) assert.equal(m.kindOfPolars(type), kind, type)
  assert.deepEqual(m.operatorsFor('Time'), m.DATA_API_OPERATORS)
  assert.deepEqual(m.operatorsFor('Int128'), [])
})

test('the documentation writes the address the other systems call, and curl lines with no key in them', () => {
  const here = { protocol: 'http:', origin: 'http://localhost:8080' }
  assert.equal(m.dataApiOrigin('', '', here), 'http://localhost:8080')
  assert.equal(m.dataApiOrigin('', 'data.example.com', here), 'http://data.example.com')
  assert.equal(m.dataApiOrigin('https://data.example.com/', 'data.example.com', here), 'https://data.example.com')
  assert.equal(m.filterParam('id', 'eq'), 'id')
  assert.equal(m.filterParam('id', 'in'), 'id__in')
  const line = m.curlLine('https://d.example.com/data/crm/customers', { id__in: '1,2', name: 'a b+c' })
  assert.equal(line, 'curl -H "Authorization: Bearer $TABULARIA_KEY" "https://d.example.com/data/crm/customers?id__in=1,2&name=a%20b%2Bc"')
  assert.ok(m.curlLine('https://x/data/a/b', {}, 'apikey').includes('-H "X-API-Key: $TABULARIA_KEY"'))
  const doc = { base_url: '', openapi: { openapi: '3.1.0', servers: [{ url: '/' }] } }
  assert.deepEqual(m.openapiForDownload(doc, 'http://localhost:8080').servers, [{ url: 'http://localhost:8080' }])
  assert.deepEqual(doc.openapi.servers, [{ url: '/' }], 'the gateway\'s document is not changed')
  const pub = { base_url: 'https://d.example.com', openapi: { servers: [{ url: 'https://d.example.com' }] } }
  assert.deepEqual(m.openapiForDownload(pub, 'http://localhost:8080').servers, [{ url: 'https://d.example.com' }])
  for (const k of ['integer', 'date', 'timestamp', 'time', 'uuid', 'string']) assert.ok(m.sampleValue(k).length)
})

test('the runtime: quantities read as the gateway reads them, the same runtime whatever its spelling', () => {
  assert.equal(m.millicores('500m'), 500)
  assert.equal(m.millicores(' 1.5 '), 1500)
  assert.equal(m.millicores('2'), 2000)
  assert.equal(m.millicores('a lot'), null)
  assert.equal(m.memoryBytes('256Mi'), 256 * 2 ** 20)
  assert.equal(m.memoryBytes('1G'), 1e9)
  assert.equal(m.memoryBytes('512MB'), null)
  const rt = { autoscale: false, replicas: 2, min_replicas: 2, max_replicas: 2, cpu: '1', memory: '1Gi' }
  assert.ok(m.sameRuntime(rt, { ...rt, min_replicas: 9, cpu: '1000m', memory: '1024Mi' }))
  assert.ok(!m.sameRuntime(rt, { ...rt, replicas: 3 }))
  assert.ok(!m.sameRuntime(rt, { ...rt, autoscale: true }))
  assert.deepEqual(m.tidyRuntime({ ...rt, autoscale: true, min_replicas: 1, max_replicas: 3, cpu: ' 250m ' }),
    { autoscale: true, replicas: 1, min_replicas: 1, max_replicas: 3, cpu: '250m', memory: '1Gi' })
  assert.equal(m.runtimePods({ ...rt, autoscale: true, max_replicas: 4 }), 4)
})

test('the runtime\'s bounds, as the gateway checks them, past the chart\'s old caps', () => {
  const l = { min_cpu: '100m', max_cpu: '16', min_memory: '256Mi', max_memory: '64Gi', max_replicas: 10 }
  const ok = { autoscale: false, replicas: 6, min_replicas: 6, max_replicas: 6, cpu: '4', memory: '8Gi' }
  assert.equal(m.runtimeProblem(ok, l), null)
  const key = (rt) => m.runtimeProblem({ ...ok, ...rt }, l)?.key
  assert.equal(key({ replicas: 0 }), 'rtBadReplicas')
  assert.equal(key({ replicas: 1.5 }), 'rtBadReplicas')
  assert.equal(key({ replicas: 11 }), 'rtTooManyPods')
  assert.equal(key({ autoscale: true, min_replicas: 3, max_replicas: 2 }), 'rtBadRange')
  assert.equal(key({ autoscale: true, min_replicas: 1, max_replicas: 11 }), 'rtTooManyPods')
  assert.equal(key({ cpu: 'lots' }), 'rtBadCpu')
  assert.equal(key({ cpu: '50m' }), 'rtCpuRange')
  assert.equal(key({ cpu: '17' }), 'rtCpuRange')
  assert.equal(key({ memory: '1 GB' }), 'rtBadMemory')
  assert.equal(key({ memory: '128Mi' }), 'rtMemoryRange')
  assert.equal(key({ memory: '65Gi' }), 'rtMemoryRange')
})

test('the page never sends the runtime with an edit: an administrator\'s, saved on its own route', () => {
  const page = readFileSync(new URL('../pages/data-api.vue', import.meta.url), 'utf8')
  const save = page.slice(page.indexOf('async function save()'), page.indexOf('// ── the runtime, an administrator'))
  assert.ok(!/runtime/.test(save.replace(/\/\/.*$/gm, '')), 'save() sends a runtime')
  assert.ok(page.includes('api.adminSetRuntime('))
  const comp = readFileSync(new URL('../composables/useDataApis.ts', import.meta.url), 'utf8')
  assert.ok(/adminSetRuntime: .*\n.*`\/admin\/data-apis\/\$\{id\}\/runtime`, \{ method: 'PUT'/.test(comp))
})
