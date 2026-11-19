// The hypothesis tests in the editor without a browser (composables/statTests.ts): the
// catalogue's copy is the engine's snapshot, every test has a form the editor can draw and
// its words in the five languages, the palette groups and finds the tests, a node starts
// with the catalogue's defaults and says what it reads, and what a test forbids is known
// to the form. A second test is tried on a catalogue made here: nothing in the editor
// names a test.
// Run: npm test
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { register } from 'node:module'
import { readFileSync } from 'node:fs'

register('./alias-hooks.mjs', import.meta.url)
const s = await import('../composables/statTests.ts')
const { defaultParams } = await import('../composables/useFlowModel.ts')
const { opSummary, opSubject } = await import('../composables/opSummary.ts')
const { consumedColumns, columnChanges } = await import('../composables/previewGrid.ts')

const read = (path) => readFileSync(new URL(path, import.meta.url), 'utf8')
const LANGS = ['en', 'it', 'de', 'es', 'fr']
const flatten = (tree, prefix = '', out = {}) => {
  for (const [k, v] of Object.entries(tree)) {
    if (typeof v === 'string') out[prefix + k] = v
    else flatten(v, `${prefix}${k}.`, out)
  }
  return out
}
const WORDS = {}
for (const lang of LANGS) WORDS[lang] = flatten((await import(`../i18n/locales/${lang}.gen.ts`)).default)
const en = (key) => WORDS.en[key] ?? key

const OP = 'stat_t_two_sample'
const T = s.statTestOf(OP)

// a catalogue made here: the t test and two tests nobody wrote words for, one with a choice
// that excludes another (ids no real test will take, so that this file holds as tests are added)
const TWO = {
  ...s.STAT_CATALOG,
  kinds: { ...s.STAT_CATALOG.kinds, zz_posthoc: { control: 'choice', choices: ['none', 'tukey', 'games_howell'] } },
  tests: [
    T,
    {
      id: 'zz_anova', operation: 'stat_zz_anova', family: 'means',
      needs: { value: 'number_column', group: 'column' },
      takes: { var_equal: 'boolean', zz_posthoc: 'zz_posthoc', alpha: 'alpha', detail: 'detail' },
      defaults: { var_equal: true, zz_posthoc: 'none', alpha: 0.05, detail: 'results' },
      forbids: [
        { key: 'zz_posthoc', value: 'tukey', when: { var_equal: false }, reason: 'zz_tukey', message: 'Tukey needs equal variances' },
        { key: 'zz_posthoc', value: 'games_howell', when: { var_equal: true }, reason: 'zz_games_howell', message: 'Games–Howell is for unequal variances' },
      ],
    },
    { id: 'zz_ks', operation: 'stat_zz_ks', family: 'distributions', needs: { value: 'number_column' }, takes: {}, defaults: {}, forbids: [] },
  ],
}

test('the catalogue the editor reads is the engine\'s snapshot, byte for byte', () => {
  assert.equal(read('../assets/stat-catalog.json'), read('../../backend/app/engine/stats/catalog.json'),
    'run python3 .github/scripts/stat_catalog.py')
  assert.ok(s.STAT_CATALOG.tests.length >= 1)
  assert.equal(s.STAT_CATALOG.families.length, 6)
})

test('which operations are hypothesis tests', () => {
  assert.equal(T.id, 't_two_sample')
  assert.ok(s.statIsOp(OP))
  for (const name of ['pivot', 'stat_', 'stat_nope', 't_two_sample', '', null, undefined, 3]) assert.equal(s.statIsOp(name), false, String(name))
  for (const t of s.STAT_CATALOG.tests) {
    assert.equal(t.operation, s.STAT_CATALOG.prefix + t.id)
    assert.ok(s.STAT_CATALOG.families.includes(t.family), t.id)
  }
})

test('the fields of the two-sample t test, each by its kind\'s control', () => {
  const fields = s.statFields(T)
  assert.deepEqual(fields.map((f) => [f.key, f.control, f.required]), [
    ['value', 'column', true], ['group', 'column', true], ['groups', 'levels', false], ['var_equal', 'boolean', false],
    ['mu', 'number', false], ['alternative', 'choice', false], ['alpha', 'number', false],
  ])
  const by = Object.fromEntries(fields.map((f) => [f.key, f]))
  assert.equal(by.value.numeric, true) // only numbers can be measured
  assert.ok(!by.group.numeric)
  assert.deepEqual([by.groups.of, by.groups.count], ['group', 2])
  assert.deepEqual(by.alternative.choices, ['two_sided', 'less', 'greater'])
  assert.deepEqual([by.alpha.above, by.alpha.below], [0, 1])
  // `detail` is the report's: never a field
  assert.ok(!fields.some((f) => f.key === 'detail'))
})

test('every key of every test has a control the form draws', () => {
  assert.deepEqual([...s.STAT_CONTROLS].sort(), [...s.STAT_CATALOG.controls].sort())
  const form = read('../components/ui/StatTestForm.vue')
  for (const control of s.STAT_CATALOG.controls) assert.ok(form.includes(`f.control === '${control}'`), `StatTestForm.vue does not draw «${control}»`)
  for (const t of s.STAT_CATALOG.tests) {
    const kinds = { ...t.needs, ...t.takes }
    for (const [key, kind] of Object.entries(kinds)) {
      const how = s.STAT_CATALOG.kinds[kind]
      assert.ok(how && s.STAT_CONTROLS.includes(how.control), `${t.id}.${key}: ${kind}`)
      if (how.control === 'levels') assert.equal(s.STAT_CATALOG.kinds[kinds[how.of]]?.control, 'column', `${t.id}.${key}`)
    }
  }
})

test('every test, family, field, choice and reason has its words in the five languages', () => {
  const wanted = s.statWordKeys()
  for (const key of ['stat.tests.t_two_sample.name', 'stat.tests.t_two_sample.hint', 'stat.families.means', 'stat.families.distributions',
    'stat.params.value', 'stat.params.groups', 'stat.choices.alternative.two_sided']) assert.ok(wanted.includes(key), key)
  assert.ok(!wanted.some((k) => k.includes('detail')))
  const missing = []
  for (const lang of LANGS) for (const key of wanted) if (!WORDS[lang][key]?.trim()) missing.push(`${lang} ${key}`)
  assert.deepEqual(missing, [])
  for (const lang of LANGS) assert.ok(WORDS[lang]['opSidebar.statisticsGroup'], lang)
})

test('a test added to the catalogue is asked for its words by name', () => {
  const wanted = s.statWordKeys(TWO)
  const lacking = wanted.filter((k) => !(k in WORDS.en))
  assert.deepEqual(lacking, [
    'stat.tests.zz_anova.name', 'stat.tests.zz_anova.hint', 'stat.params.zz_posthoc',
    'stat.choices.zz_posthoc.none', 'stat.choices.zz_posthoc.tukey', 'stat.choices.zz_posthoc.games_howell',
    'stat.reasons.zz_tukey', 'stat.reasons.zz_games_howell',
    'stat.tests.zz_ks.name', 'stat.tests.zz_ks.hint',
  ])
})

test('every family has its icon, and a test takes it with its name', () => {
  const icons = read('../composables/useOpIcons.ts')
  const table = icons.match(/const STAT_FAMILY_META[^=]*= \{([\s\S]*?)\n\}/)[1]
  const families = [...table.matchAll(/^\s*(\w+):\s*\{ icon: (\w+)/gm)].map((m) => m[1])
  assert.deepEqual(families, s.STAT_CATALOG.families)
  assert.equal(new Set([...table.matchAll(/icon: (\w+)/g)].map((m) => m[1])).size, 6) // an icon each
  assert.ok(icons.includes('statNameKey(test)'))
  assert.equal(s.statNameKey(T), 'stat.tests.t_two_sample.name')
})

test('the palette groups the tests by family, in the catalogue\'s order, and only what the engine offers', () => {
  const ops = ['filter', 'stat_zz_ks', 'stat_zz_anova', OP, 'pivot']
  const groups = s.statPalette(ops, '', en, TWO)
  assert.deepEqual(groups.map((g) => [g.family, g.tests.map((t) => t.id)]), [['means', ['t_two_sample', 'zz_anova']], ['distributions', ['zz_ks']]])
  assert.deepEqual(s.statPalette(['filter', 'pivot'], '', en, TWO), []) // an engine without tests: no group
  assert.deepEqual(s.statPalette([OP], '', en).map((g) => g.tests.map((t) => t.id)), [['t_two_sample']])
})

test('the palette\'s search looks in names, hints and families', () => {
  const ops = TWO.tests.map((t) => t.operation)
  const found = (q) => s.statPalette(ops, q, en, TWO).flatMap((g) => g.tests.map((t) => t.id))
  assert.deepEqual(found('two-sample'), ['t_two_sample']) // the name
  assert.deepEqual(found('  WELCH '), ['t_two_sample']) // the hint, whatever the case
  assert.deepEqual(found('means'), ['t_two_sample', 'zz_anova']) // the family: all its tests
  assert.deepEqual(found('anova'), ['zz_anova']) // the operation, while a name is not written yet
  assert.deepEqual(found('zzz'), [])
  // in the person's language
  const it = (key) => WORDS.it[key] ?? key
  assert.deepEqual(s.statPalette([OP], 'campioni', it).flatMap((g) => g.tests.map((t) => t.id)), ['t_two_sample'])
  assert.deepEqual(s.statPalette([OP], 'medie', it).length, 1)
})

test('the palette shows the group between the transformations and the control nodes, closed until opened', () => {
  const sidebar = read('../components/ui/OpSidebar.vue')
  const at = (text) => { const i = sidebar.indexOf(text); assert.ok(i > 0, text); return i }
  assert.ok(at("$t('opSidebar.transformsGroup')") < at("$t('opSidebar.statisticsGroup')"))
  assert.ok(at("$t('opSidebar.statisticsGroup')") < at("$t('opSidebar.controlGroup')"))
  assert.ok(sidebar.includes(':aria-expanded="statOpen"') && sidebar.includes("'tabularia.palette.statistics'"))
  assert.ok(sidebar.includes('const statOpenSaved = ref(false)'))
  // the transformations no longer list the tests
  assert.match(sidebar, /transformOps = computed\(\(\) =>\s*props\.operations\.filter\(\(o\) => o !== 'foreach' && !statIsOp\(o\)/)
})

test('the type selector of a statistics node offers the tests by family; another node\'s does not list them', () => {
  const options = s.statTypeOptions(['filter', 'stat_zz_ks', 'stat_zz_anova', OP], en, TWO)
  assert.deepEqual(options.map((o) => [o.value, o.group]), [[OP, 'Means'], ['stat_zz_anova', 'Means'], ['stat_zz_ks', 'Distributions']])
  assert.equal(options[0].label, 'Two-sample t test')
  const panel = read('../components/ui/NodePanel.vue')
  assert.ok(panel.includes("props.operations.filter((o) => o !== 'foreach' && !statIsOp(o))"))
  assert.ok(panel.includes('<StatTestForm') && panel.includes('v-if="isStatNode"'))
})

test('a new node starts with the catalogue\'s defaults', () => {
  assert.deepEqual(defaultParams(OP), { var_equal: false, mu: 0, alternative: 'two_sided', alpha: 0.05 })
  assert.deepEqual(s.statDefaults('stat_zz_anova', TWO), { var_equal: true, zz_posthoc: 'none', alpha: 0.05 })
  assert.deepEqual(s.statDefaults('pivot'), {})
  assert.deepEqual(defaultParams('filter'), { operator: 'eq' }) // the others as before
})

test('the parameters are the test\'s own keys, read as the engine takes them', () => {
  const p = s.statParams(T, { alpha: '0.1', mu: '', value: 'amount', group: 'channel', groups: ['web', 'shop'], var_equal: 1,
    alternative: 'nope', detail: 'report', other: 3 })
  assert.deepEqual(p, { value: 'amount', group: 'channel', groups: ['web', 'shop'], var_equal: false, alpha: 0.1 })
  assert.deepEqual(Object.keys(p), ['value', 'group', 'groups', 'var_equal', 'alpha']) // the catalogue's order
  // two groups, different: half a choice, or the same twice, is no choice
  for (const groups of [['web'], ['web', ''], ['web', 'web'], ['a', 'b', 'c'], 'web', []]) {
    assert.ok(!('groups' in s.statParams(T, { groups })), JSON.stringify(groups))
  }
  assert.deepEqual(s.statParams(T, { groups: [1, 2] }).groups, ['1', '2'])
  assert.equal(s.statParams(T, { alpha: 5 }).alpha, 5) // the engine says what is wrong with it
  assert.deepEqual(s.statParams(T, null), {})
})

test('changing a field keeps the rest; the groups do not survive another column', () => {
  const p = { value: 'amount', group: 'channel', groups: ['web', 'shop'], alpha: 0.05 }
  assert.deepEqual(s.statSet(T, p, 'alpha', '0.01'), { ...p, alpha: 0.01 })
  assert.deepEqual(s.statSet(T, p, 'group', 'region'), { value: 'amount', group: 'region', alpha: 0.05 })
  assert.deepEqual(s.statSet(T, p, 'group', 'channel'), p) // the same column: nothing lost
  assert.deepEqual(s.statSet(T, p, 'groups', undefined), { value: 'amount', group: 'channel', alpha: 0.05 })
  assert.deepEqual(s.statSet(T, p, 'value', undefined), { group: 'channel', groups: ['web', 'shop'], alpha: 0.05 })
  assert.equal(s.statSet(T, p, 'var_equal', true).var_equal, true)
})

test('a node that becomes another test keeps what the two share', () => {
  const p = { value: 'amount', group: 'channel', groups: ['web', 'shop'], var_equal: false, mu: 2, alternative: 'less', alpha: 0.01 }
  assert.deepEqual(s.statSwitch(OP, 'stat_zz_anova', p, TWO), { value: 'amount', group: 'channel', var_equal: false, zz_posthoc: 'none', alpha: 0.01 })
  assert.deepEqual(s.statSwitch('stat_zz_anova', 'stat_zz_ks', p, TWO), { value: 'amount' })
  assert.deepEqual(s.statSwitch('filter', OP, { column: 'x' }), defaultParams(OP))
})

test('what a test forbids is known to the form, the defaults counting', () => {
  const anova = s.statTestOf('stat_zz_anova', TWO)
  const why = (value, params) => s.statForbidden(anova, 'zz_posthoc', value, params)?.reason ?? null
  assert.equal(why('tukey', { var_equal: false }), 'zz_tukey')
  assert.equal(why('tukey', { var_equal: true }), null)
  assert.equal(why('tukey', {}), null) // equal variances until said otherwise
  assert.equal(why('games_howell', {}), 'zz_games_howell')
  assert.equal(why('games_howell', { var_equal: null }), 'zz_games_howell')
  assert.equal(why('games_howell', { var_equal: false }), null)
  assert.equal(why('none', { var_equal: false }), null)
  assert.equal(s.statForbidden(anova, 'alpha', 'tukey', { var_equal: false }), null)
  assert.equal(s.statForbidden(T, 'alternative', 'less', {}), null) // the t test forbids nothing
  // the form disables the choice and says why
  const form = read('../components/ui/StatTestForm.vue')
  assert.ok(form.includes(':disabled="!!forbidden(f, c) && chosen(f) !== c"') && form.includes('rule.message'))
})

test('a number outside its bounds is said, both excluded', () => {
  const alpha = s.statFields(T).find((f) => f.key === 'alpha')
  for (const ok of [0.05, '0.5', 0.999, '', null, undefined]) assert.equal(s.statNumberProblem(alpha, ok), '', String(ok))
  for (const bad of [0, 1, 5, -0.1, '2']) assert.equal(s.statNumberProblem(alpha, bad), 'between', String(bad))
  assert.equal(s.statNumberProblem({ above: 0 }, 0), 'above')
  assert.equal(s.statNumberProblem({ below: 1 }, 1), 'below')
  assert.equal(s.statNumberProblem({}, 1e9), '')
})

test('a node says the columns it reads, and they are not «removed»', () => {
  const p = { value: 'amount', group: 'channel', groups: ['web', 'shop'], alpha: 0.05 }
  assert.equal(opSummary(OP, p), 'amount ~ channel')
  assert.equal(opSubject(OP, p), 'amount ~ channel')
  assert.equal(opSummary(OP, { value: 'amount' }), 'amount')
  assert.equal(opSummary(OP, {}), '')
  assert.equal(opSummary(OP, null), '')
  assert.deepEqual(consumedColumns(OP, p), ['amount', 'channel'])
  assert.deepEqual(consumedColumns(OP, {}), [])
  assert.deepEqual(consumedColumns('pivot', { on: 'a', values: 'b' }), ['a', 'b']) // the others as before
  const changes = columnChanges(
    [{ name: 'amount', dtype: 'Float64' }, { name: 'channel', dtype: 'String' }, { name: 'city', dtype: 'String' }],
    [{ name: 'section', dtype: 'String' }, { name: 'value', dtype: 'Float64' }], {}, consumedColumns(OP, p))
  assert.deepEqual(changes.removed, ['city'])
})

test('a group is labelled as the engine labels it', () => {
  // catalog.label_of / operations.pivot_label
  assert.equal(s.statLevelLabel('web', 'String'), 'web')
  assert.equal(s.statLevelLabel(true, 'Boolean'), 'true')
  assert.equal(s.statLevelLabel(false, 'Boolean'), 'false')
  assert.equal(s.statLevelLabel(3, 'Int64'), '3')
  assert.equal(s.statLevelLabel(3, 'Float64'), '3.0')
  assert.equal(s.statLevelLabel(2.5, 'Float64'), '2.5')
  assert.equal(s.statLevelLabel(-0, 'Float64'), '0.0')
  assert.equal(s.statLevelLabel(1e16, 'Float64'), '1e+16')
  assert.equal(s.statLevelLabel(1.5e21, 'Float64'), '1.5e+21')
  assert.equal(s.statLevelLabel(0.0001, 'Float64'), '0.0001')
  assert.equal(s.statLevelLabel(0.00001, 'Float64'), '1e-05')
  assert.equal(s.statLevelLabel(123456789012, 'Float32'), '123456789012.0')
  assert.equal(s.statLevelLabel('2024-03-01', 'Date'), '2024-03-01')
  assert.equal(s.statLevelLabel('2024-03-01T10:30:00', "Datetime(time_unit='us', time_zone=None)"), '2024-03-01 10:30:00')
  assert.equal(s.statLevelLabel('2024-03-01T10:30:00Z', "Datetime(time_unit='us', time_zone='UTC')"), '2024-03-01 10:30:00')
  assert.equal(s.statLevelLabel('a T b', 'String'), 'a T b')
  // each once, in text order, nulls out
  assert.deepEqual(s.statLevelLabels(['web', null, 'shop', 'web', 'App'], 'String'), ['App', 'shop', 'web'])
  assert.deepEqual(s.statLevelLabels([10, 9, 2], 'Int64'), ['10', '2', '9'])
  assert.deepEqual(s.statLevelLabels(null), [])
})

test('a node not set yet is told from a node that fails', () => {
  // the engine's sentence for a key it needs: check_params, backend/app/engine/stats/catalog.py
  const engine = read('../../backend/app/engine/stats/catalog.py')
  assert.ok(engine.includes(`raise refusal(test, f"'{key}' is required")`), 'the engine\'s sentence changed: update statIsUnset')
  assert.ok(s.statIsUnset("stat_t_two_sample: 'value' is required"))
  assert.ok(s.statIsUnset("Engine error: stat_anova: 'group' is required"))
  assert.ok(!s.statIsUnset("stat_t_two_sample: the column «city» ('group') is not in the step's input"))
  assert.ok(!s.statIsUnset("required parameter missing: 'columns'"))
  assert.ok(read('../components/FlowEditor.vue').includes('statIsUnset(message)) showInputInstead(nodeId)'))
})

test('the form says nothing about samples: the node works on the rows that reach it', () => {
  const form = read('../components/ui/StatTestForm.vue').split('<template>')[1]
  assert.ok(!/sample|all_rows|allRows/i.test(form))
})

test('every export of the module starts with stat, as auto-imports ask', () => {
  const names = [...read('../composables/statTests.ts').matchAll(/^export (?:const|function|interface|type) (\w+)/gm)].map((m) => m[1])
  assert.ok(names.length > 15)
  assert.deepEqual(names.filter((n) => !/^(stat[A-Z]|STAT_|Stat[A-Z])/.test(n)), [])
})

test('a key that matters only with another is known as such (the defaults counting)', () => {
  const anova = s.statTestOf('stat_anova')
  assert.deepEqual(anova.applies, { p_adjust: { posthoc: ['pairwise_t'] } })
  assert.equal(s.statApplies(anova, 'p_adjust', {}), false) // no post-hoc until one is chosen
  assert.equal(s.statApplies(anova, 'p_adjust', { posthoc: 'tukey' }), false)
  assert.equal(s.statApplies(anova, 'p_adjust', { posthoc: 'pairwise_t' }), true)
  assert.equal(s.statApplies(anova, 'posthoc', {}), true)
  assert.equal(s.statApplies(T, 'alpha', {}), true) // a test without the rule
  assert.ok(read('../components/ui/StatTestForm.vue').includes('statApplies(now, f.key, props.params)'))
})

test('a test may say a key in words of its own; the rest are shared', () => {
  const has = (k) => k in WORDS.en
  const one = s.statTestOf('stat_t_one_sample')
  assert.equal(s.statFieldWordKey(one, 'mu', 'hints', has), 'stat.tests.t_one_sample.hints.mu')
  assert.equal(s.statFieldWordKey(one, 'mu', 'params', has), 'stat.params.mu')
  assert.equal(s.statFieldWordKey(T, 'mu', 'hints', has), 'stat.hints.mu')
  assert.equal(en('stat.tests.t_one_sample.hints.mu'), 'The mean the null hypothesis states.')
  // a test's own words are in the five languages, as the shared ones
  const own = Object.keys(WORDS.en).filter((k) => /^stat\.tests\.\w+\.(params|hints)\./.test(k))
  assert.ok(own.length >= 5)
  const missing = []
  for (const lang of LANGS) for (const key of own) if (!WORDS[lang][key]?.trim()) missing.push(`${lang} ${key}`)
  assert.deepEqual(missing, [])
  for (const key of own) {
    const [, id, , field] = key.split('.').slice(1)
    const test = s.STAT_CATALOG.tests.find((t) => t.id === id)
    assert.ok(test && field in { ...test.needs, ...test.takes }, `${key}: no such key of the test`)
  }
})

test('the tests of the means and variances: their fields, their defaults', () => {
  const fields = (op) => s.statFields(s.statTestOf(op)).map((f) => `${f.key}:${f.control}`)
  assert.deepEqual(fields('stat_t_one_sample'), ['value:column', 'mu:number', 'alternative:choice', 'alpha:number'])
  assert.deepEqual(fields('stat_t_paired'), ['x:column', 'y:column', 'mu:number', 'alternative:choice', 'alpha:number'])
  assert.deepEqual(fields('stat_anova'), ['value:column', 'group:column', 'var_equal:boolean', 'posthoc:choice', 'p_adjust:choice', 'alpha:number'])
  assert.deepEqual(fields('stat_var_f'), ['value:column', 'group:column', 'groups:levels', 'ratio:number', 'alternative:choice', 'alpha:number'])
  assert.deepEqual(fields('stat_levene'), ['value:column', 'group:column', 'center:choice', 'alpha:number'])
  assert.deepEqual(fields('stat_pearson'), ['x:column', 'y:column', 'alternative:choice', 'alpha:number'])
  assert.deepEqual(defaultParams('stat_anova'), { var_equal: true, posthoc: 'none', p_adjust: 'holm', alpha: 0.05 })
  assert.equal(s.statFields(s.statTestOf('stat_var_f')).find((f) => f.key === 'ratio').above, 0)
  assert.equal(opSummary('stat_t_paired', { x: 'before', y: 'after' }), 'before ~ after')
  // a two-sample t test that becomes an ANOVA keeps its columns and its level
  assert.deepEqual(s.statSwitch(OP, 'stat_anova', { value: 'amount', group: 'channel', groups: ['a', 'b'], alpha: 0.01 }),
    { value: 'amount', group: 'channel', var_equal: true, posthoc: 'none', p_adjust: 'holm', alpha: 0.01 })
  const anova = s.statTestOf('stat_anova')
  assert.equal(s.statForbidden(anova, 'posthoc', 'tukey', { var_equal: false })?.reason, 'tukey_equal_variances')
  assert.equal(s.statForbidden(anova, 'posthoc', 'games_howell', {})?.reason, 'games_howell_unequal_variances')
})

test('the tests of proportions and categories: their fields, their defaults, what survives another column', () => {
  const fields = (op) => s.statFields(s.statTestOf(op)).map((f) => `${f.key}:${f.control}`)
  assert.deepEqual(fields('stat_prop_one_sample'), ['value:column', 'success:level', 'p0:number', 'method:choice', 'correction:boolean',
    'alternative:choice', 'alpha:number'])
  assert.deepEqual(fields('stat_prop_two_sample'), ['value:column', 'success:level', 'group:column', 'groups:levels', 'correction:boolean',
    'alternative:choice', 'alpha:number'])
  assert.deepEqual(fields('stat_chi2_independence'), ['x:column', 'y:column', 'correction:boolean', 'posthoc:choice', 'p_adjust:choice', 'alpha:number'])
  assert.deepEqual(fields('stat_chi2_gof'), ['value:column', 'expected:proportions', 'alpha:number'])
  assert.deepEqual(fields('stat_fisher_exact'), ['x:column', 'y:column', 'alternative:choice', 'alpha:number'])
  // any column is an outcome or a category: none is asked to be a number
  assert.ok(s.statFields(s.statTestOf('stat_prop_one_sample')).every((f) => !f.numeric))
  assert.deepEqual(defaultParams('stat_prop_one_sample'), { p0: 0.5, method: 'score', correction: true, alternative: 'two_sided', alpha: 0.05 })
  assert.deepEqual(defaultParams('stat_chi2_gof'), { alpha: 0.05 }) // no proportions: all equal
  const one = s.statTestOf('stat_prop_one_sample')
  assert.equal(s.statApplies(one, 'correction', {}), true)
  assert.equal(s.statApplies(one, 'correction', { method: 'exact' }), false)
  assert.equal(s.statApplies(s.statTestOf('stat_chi2_independence'), 'p_adjust', { posthoc: 'cells' }), true)
  // the success is kept as written, a value of another column is not
  const p = { value: 'answer', success: 'yes', p0: 0.25 }
  assert.deepEqual(s.statParams(one, { ...p, success: '  ' }), { value: 'answer', p0: 0.25 })
  assert.deepEqual(s.statParams(one, { ...p, success: true }).success, true)
  assert.deepEqual(s.statSet(one, p, 'value', 'other'), { value: 'other', p0: 0.25 })
  assert.deepEqual(s.statSet(one, p, 'p0', '0,3'), { ...p, p0: 0.3 })
  const gof = s.statTestOf('stat_chi2_gof')
  assert.deepEqual(s.statSet(gof, { value: 'cat', expected: { a: 0.5, b: 0.5 } }, 'value', 'other'), { value: 'other' })
  assert.equal(opSummary('stat_chi2_independence', { x: 'region', y: 'channel' }), 'region ~ channel')
})

test('the proportions of a goodness of fit: numbers as written, and what is wrong with them', () => {
  assert.deepEqual(s.statShares({ a: '0,25', b: 0.75, c: '', d: 'x' }), { a: 0.25, b: 0.75 })
  assert.deepEqual(s.statShares(null), {})
  assert.deepEqual(s.statShares([0.5]), {})
  const gof = s.statTestOf('stat_chi2_gof')
  assert.deepEqual(s.statParams(gof, { value: 'cat', expected: { a: '0.1', b: 0.9 } }), { value: 'cat', expected: { a: 0.1, b: 0.9 } })
  assert.deepEqual(s.statParams(gof, { value: 'cat', expected: {} }), { value: 'cat' })
  assert.equal(s.statSharesProblem({ a: 0.1, b: 0.2, c: 0.3, d: 0.4 }, ['a', 'b', 'c', 'd']), '') // 1.0000000000000002: within 1e-9
  assert.equal(s.statSharesProblem({ a: 0.5, b: 0.6 }, ['a', 'b']), 'sum')
  assert.equal(s.statSharesProblem({ a: 1 }, ['a', 'b']), 'missing')
  assert.equal(s.statSharesProblem({ a: 1, b: 0 }, ['a', 'b']), 'missing')
  assert.equal(s.statSharesProblem({}, []), '')
})
