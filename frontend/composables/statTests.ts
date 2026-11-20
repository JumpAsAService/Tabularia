// The hypothesis tests of a flow (the `stat_*` operations) as the editor needs them: which
// operations are tests and of which family, the fields of a test's form, its defaults, the
// one line that says what a node does, the palette's group. Everything is read from the
// catalogue: assets/stat-catalog.json is a copy of the engine's snapshot
// (backend/app/engine/stats/catalog.json, written from catalog.py; the copy by
// .github/scripts/stat_catalog.py). A new test appears here by being added there, and
// tests/stat-tests.test.mjs names the words it lacks.
//
// Pure functions, nothing of Nuxt or the DOM: the tests import them as they are. Each one
// takes the catalogue as its last argument, the real one unless a test hands another.
// Every export starts with `stat`, `STAT_` or `Stat`: a composable's exports are
// auto-imported everywhere.

import raw from '~/assets/stat-catalog.json'

export type StatControl = 'column' | 'levels' | 'boolean' | 'number' | 'choice' | 'level' | 'proportions'

/** How a form asks for one kind of value (the catalogue's `KINDS`). */
export interface StatKind {
  control: StatControl
  numeric?: boolean
  of?: string
  count?: number
  above?: number
  below?: number
  choices?: string[]
  hidden?: boolean
}

/** `key` may not be `value` while every key of `when` has that value. */
export interface StatForbid {
  key: string
  value: unknown
  when: Record<string, unknown>
  reason: string
  message: string
}

export interface StatTest {
  id: string
  operation: string
  family: string
  needs: Record<string, string>
  takes: Record<string, string>
  defaults: Record<string, unknown>
  forbids: StatForbid[]
  /** A key that matters only while other keys have one of some values (the defaults count):
   *  {p_adjust: {posthoc: ['pairwise_t']}}. */
  applies?: Record<string, Record<string, unknown[]>>
}

export interface StatCatalog {
  prefix: string
  families: string[]
  controls: string[]
  kinds: Record<string, StatKind>
  tests: StatTest[]
}

/** One field of a test's form: a key of the test, with what its control needs to know. */
export interface StatField extends StatKind {
  key: string
  kind: string
  required: boolean
  choices: string[]
}

export interface StatFamilyGroup {
  family: string
  tests: StatTest[]
}

export const STAT_CATALOG: StatCatalog = raw as unknown as StatCatalog

/** The controls components/ui/StatTestForm.vue draws: a kind with another one has no field. */
export const STAT_CONTROLS: readonly StatControl[] = ['column', 'levels', 'boolean', 'number', 'choice', 'level', 'proportions']

/** The controls whose values are some of a column's (the key `of` names it): they are read at the
 *  node's input, and they do not survive another column. */
export const STAT_VALUE_CONTROLS: readonly StatControl[] = ['levels', 'level', 'proportions']

/** Past this many values a column's groups are not all listed (the editor reads 200). */
export const STAT_LEVELS_SHOWN = 200

type Params = Record<string, any>
type Text = (key: string) => string

/** The test an operation runs, or null when it is not a hypothesis test of the catalogue. */
export function statTestOf(operation: unknown, catalog: StatCatalog = STAT_CATALOG): StatTest | null {
  if (typeof operation !== 'string' || !operation.startsWith(catalog.prefix)) return null
  return catalog.tests.find((t) => t.operation === operation) ?? null
}

/** Whether an operation of a flow is a hypothesis test. */
export function statIsOp(operation: unknown, catalog: StatCatalog = STAT_CATALOG): boolean {
  return statTestOf(operation, catalog) !== null
}

/** The fields of a test's form, in the catalogue's order: what it needs, then what it takes
 *  besides. A key the node sets by itself (`detail`) is not a field. */
export function statFields(test: StatTest, catalog: StatCatalog = STAT_CATALOG): StatField[] {
  const out: StatField[] = []
  for (const [keys, required] of [[test.needs, true], [test.takes, false]] as const) {
    for (const [key, kind] of Object.entries(keys)) {
      const how = catalog.kinds[kind]
      if (!how || how.hidden) continue
      out.push({ ...how, key, kind, required, choices: how.choices ?? [] })
    }
  }
  return out
}

const isEmpty = (v: unknown): boolean => v === undefined || v === null || v === ''

/** A node's parameters as the engine takes them: the test's own keys in the catalogue's
 *  order, each read as its control writes it; what is empty, unknown or half given is left
 *  out (the engine then uses its default, or says which key it needs). */
export function statParams(test: StatTest, params: Params | null | undefined, catalog: StatCatalog = STAT_CATALOG): Params {
  const p = params ?? {}
  const out: Params = {}
  for (const f of statFields(test, catalog)) {
    const v = p[f.key]
    if (isEmpty(v)) continue
    switch (f.control) {
      case 'column':
        if (typeof v === 'string') out[f.key] = v
        break
      case 'levels': {
        const labels = Array.isArray(v) ? v.filter((x) => !isEmpty(x)).map(String) : []
        if (labels.length === (f.count ?? labels.length) && new Set(labels).size === labels.length && labels.length) out[f.key] = labels
        break
      }
      case 'boolean':
        out[f.key] = v === true
        break
      case 'number': {
        const n = typeof v === 'number' ? v : Number(String(v).trim().replace(',', '.'))
        if (Number.isFinite(n)) out[f.key] = n
        break
      }
      case 'choice':
        if (f.choices.includes(v)) out[f.key] = v
        break
      case 'level':
        // a value of the column, as chosen or written: a text, or the number or boolean a column holds
        if (typeof v === 'string' ? v.trim() !== '' : typeof v === 'boolean' || (typeof v === 'number' && Number.isFinite(v))) out[f.key] = v
        break
      case 'proportions': {
        // a proportion for each category; nothing given (or nothing usable): all equal
        const shares = statShares(v)
        if (Object.keys(shares).length) out[f.key] = shares
        break
      }
    }
  }
  return out
}

/** The proportions of a goodness of fit as the engine reads them: {category: number}, the
 *  entries that are numbers kept (a box being written may hold «0,25»), the others left out. */
export function statShares(value: unknown): Record<string, number> {
  const out: Record<string, number> = {}
  if (!value || typeof value !== 'object' || Array.isArray(value)) return out
  for (const [label, raw] of Object.entries(value as Record<string, unknown>)) {
    if (isEmpty(raw)) continue
    const n = typeof raw === 'number' ? raw : Number(String(raw).trim().replace(',', '.'))
    if (Number.isFinite(n)) out[label] = n
  }
  return out
}

/** What is wrong with the proportions given for `labels` (the column's categories): '' when they
 *  are a proportion above 0 for each and sum to 1 (within 1e-9, as the engine), 'missing' when one
 *  lacks or is not above 0, 'sum' when they do not sum to 1. */
export function statSharesProblem(shares: Record<string, number>, labels: readonly string[]): '' | 'missing' | 'sum' {
  if (!labels.length) return ''
  if (labels.some((l) => !(shares[l] > 0))) return 'missing'
  const sum = labels.reduce((s, l) => s + shares[l]!, 0)
  return Math.abs(sum - 1) > 1e-9 ? 'sum' : ''
}

/** What a new node of the test starts with: the catalogue's defaults of its form's fields. */
export function statDefaults(operation: string, catalog: StatCatalog = STAT_CATALOG): Params {
  const test = statTestOf(operation, catalog)
  return test ? statParams(test, test.defaults, catalog) : {}
}

/** The parameters after one field changed. Values chosen among a column's (the two groups
 *  to compare) do not survive another column; a choice the change makes forbidden goes back to
 *  one that is allowed (`statSetChecked` says which). */
export function statSet(test: StatTest, params: Params | null | undefined, key: string, value: unknown, catalog: StatCatalog = STAT_CATALOG): Params {
  return statSetChecked(test, params, key, value, catalog).params
}

/** A choice put back because the change before it forbade it. */
export interface StatRepair {
  key: string
  from: unknown
  to: unknown
  rule: StatForbid
}

/** `statSet`, and what it put back: a choice that the other parameters, as they are now,
 *  forbid (Tukey's HSD once the variances are not equal) is not sent to the engine, which
 *  refuses it: it goes to the test's default if that is allowed, else the first choice that is.
 *  The key just set is the person's own and is never changed here (the form disables what is
 *  forbidden). */
export function statSetChecked(test: StatTest, params: Params | null | undefined, key: string, value: unknown,
  catalog: StatCatalog = STAT_CATALOG): { params: Params; repaired: StatRepair[] } {
  const next: Params = { ...(params ?? {}), [key]: value }
  if ((params ?? {})[key] !== value) {
    for (const f of statFields(test, catalog)) if (STAT_VALUE_CONTROLS.includes(f.control) && f.of === key) delete next[f.key]
  }
  const repaired: StatRepair[] = []
  const fields = statFields(test, catalog)
  for (let pass = 0; pass < test.forbids.length; pass++) {
    const now: Params = { ...test.defaults }
    for (const [k, v] of Object.entries(next)) if (!isEmpty(v)) now[k] = v
    const rule = test.forbids.find((r) => r.key !== key && now[r.key] === r.value && statForbidden(test, r.key, now[r.key], next))
    if (!rule) break
    const field = fields.find((f) => f.key === rule.key)
    const allowed = (c: unknown) => !statForbidden(test, rule.key, c, next)
    const to = [test.defaults[rule.key], ...(field?.choices ?? [])].find((c) => c !== undefined && c !== rule.value && allowed(c))
    if (to === undefined) break
    next[rule.key] = to
    repaired.push({ key: rule.key, from: rule.value, to, rule })
  }
  return { params: statParams(test, next, catalog), repaired }
}

/** A node that becomes another test keeps what the two share: a key both have, of the same
 *  kind (the columns, alpha, the alternative); the rest starts from the new test's defaults. */
export function statSwitch(from: string, to: string, params: Params | null | undefined, catalog: StatCatalog = STAT_CATALOG): Params {
  const a = statTestOf(from, catalog)
  const b = statTestOf(to, catalog)
  if (!b) return {}
  const kept: Params = { ...b.defaults }
  if (a) {
    const before = { ...a.needs, ...a.takes }
    for (const [key, kind] of Object.entries({ ...b.needs, ...b.takes })) {
      if (before[key] === kind && !isEmpty((params ?? {})[key])) kept[key] = (params ?? {})[key]
    }
  }
  return statParams(b, kept, catalog)
}

/** Why `key` may not be `value` with the other parameters as they are (the defaults count
 *  for what is not given), or null when it may. */
export function statForbidden(test: StatTest, key: string, value: unknown, params: Params | null | undefined): StatForbid | null {
  const now: Params = { ...test.defaults }
  for (const [k, v] of Object.entries(params ?? {})) if (!isEmpty(v)) now[k] = v
  return test.forbids.find((r) => r.key === key && r.value === value && Object.entries(r.when).every(([k, v]) => now[k] === v)) ?? null
}

/** Whether `key` matters with the other parameters as they are (the catalogue's `applies`,
 *  the defaults counting): the form shows it only then, the node reads it only then. */
export function statApplies(test: StatTest, key: string, params: Params | null | undefined): boolean {
  const rule = test.applies?.[key]
  if (!rule) return true
  const now: Params = { ...test.defaults }
  for (const [k, v] of Object.entries(params ?? {})) if (!isEmpty(v)) now[k] = v
  return Object.entries(rule).every(([k, values]) => values.includes(now[k]))
}

/** What is wrong with a number against its field's bounds, both excluded: '' when nothing. */
export function statNumberProblem(field: Pick<StatField, 'above' | 'below'>, value: unknown): '' | 'between' | 'above' | 'below' {
  if (isEmpty(value)) return ''
  const n = Number(value)
  if (!Number.isFinite(n)) return ''
  const low = field.above !== undefined && !(n > field.above)
  const high = field.below !== undefined && !(n < field.below)
  if (!low && !high) return ''
  if (field.above !== undefined && field.below !== undefined) return 'between'
  return low ? 'above' : 'below'
}

/** The columns a test reads, in its keys' order. */
export function statColumns(operation: string, params: Params | null | undefined, catalog: StatCatalog = STAT_CATALOG): string[] {
  const test = statTestOf(operation, catalog)
  if (!test) return []
  const p = params ?? {}
  return statFields(test, catalog)
    .filter((f) => f.control === 'column' && typeof p[f.key] === 'string' && p[f.key])
    .map((f) => String(p[f.key]))
}

/** What a node does, in one line: the columns it reads, «amount ~ channel». */
export function statSummary(operation: string, params: Params | null | undefined, catalog: StatCatalog = STAT_CATALOG): string {
  return statColumns(operation, params, catalog).join(' ~ ')
}

// ── a group's label ───────────────────────────────────────────────────────────
// The engine names a group by its value as text, the same on every engine
// (`catalog.label_of`): what the output's `level` column holds and what `groups` is matched
// against. The form writes the same text from the values a preview returns.

function floatLabel(x: number): string {
  if (Number.isNaN(x)) return 'nan'
  if (!Number.isFinite(x)) return x > 0 ? 'inf' : '-inf'
  if (x === 0) return '0.0'
  const a = Math.abs(x)
  if (a >= 1e16 || a < 1e-4) {
    const [mantissa, exponent] = x.toExponential().split('e')
    const digits = String(Math.abs(Number(exponent))).padStart(2, '0')
    return `${mantissa}e${Number(exponent) < 0 ? '-' : '+'}${digits}`
  }
  return Number.isInteger(x) ? `${x}.0` : String(x)
}

/** The label of a group's value, as the engine writes it: `dtype` is the column's type as a
 *  preview names it (a float keeps its «.0», a datetime has a space and no zone). */
export function statLevelLabel(value: unknown, dtype = ''): string {
  if (typeof value === 'boolean') return value ? 'true' : 'false'
  if (typeof value === 'number') return /^Float/.test(dtype) ? floatLabel(value) : String(value)
  if (typeof value === 'string' && dtype.startsWith('Datetime')) return value.replace('T', ' ').replace(/(Z|[+-]00:00)$/, '')
  return String(value)
}

/** A column's values as the labels a person chooses among: each once, in text order (the
 *  order the engine compares two groups in when none is chosen). */
export function statLevelLabels(values: readonly unknown[] | null | undefined, dtype = ''): string[] {
  const labels = new Set<string>()
  for (const v of values ?? []) if (v !== null && v !== undefined) labels.add(statLevelLabel(v, dtype))
  return [...labels].sort((a, b) => (a < b ? -1 : a > b ? 1 : 0))
}

// ── words ─────────────────────────────────────────────────────────────────────

export function statNameKey(test: StatTest): string {
  return `stat.tests.${test.id}.name`
}

export function statHintKey(test: StatTest): string {
  return `stat.tests.${test.id}.hint`
}

/** The words of a key of a test's form, its label (`params`) or its hint (`hints`): the
 *  test's own when it has some (`stat.tests.<id>.params.<key>`: «mu» is a mean under H0 for
 *  one sample, a difference for two), else the words every test shares (`stat.params.<key>`).
 *  `has` says whether the interface's catalogue holds a key. */
export function statFieldWordKey(test: StatTest, key: string, part: 'params' | 'hints', has: (k: string) => boolean): string {
  const own = `stat.tests.${test.id}.${part}.${key}`
  return has(own) ? own : `stat.${part}.${key}`
}

/** The words of a reason something is forbidden. No test forbids anything yet: the form
 *  falls back on the catalogue's English sentence while a language lacks the key. */
export function statReasonKey(reason: string): string {
  return `stat.reasons.${reason}`
}

/** Every key of the interface's catalogues the tests need: the names and hints of the
 *  tests, the families, the fields' labels, the choices, the reasons of what is forbidden. */
export function statWordKeys(catalog: StatCatalog = STAT_CATALOG): string[] {
  const keys = new Set<string>(catalog.families.map((f) => `stat.families.${f}`))
  for (const test of catalog.tests) {
    keys.add(statNameKey(test)).add(statHintKey(test))
    for (const f of statFields(test, catalog)) {
      keys.add(`stat.params.${f.key}`)
      for (const c of f.choices) keys.add(`stat.choices.${f.kind}.${c}`)
    }
    for (const r of test.forbids) keys.add(statReasonKey(r.reason))
  }
  return [...keys]
}

// ── the palette, the type selector ────────────────────────────────────────────

/** The palette's Statistics group: the tests the engine offers, by family in the catalogue's
 *  order, each family with its tests in the catalogue's order. With a search, only the tests
 *  whose name, hint or family holds it (or whose operation does). `text` writes a key in the
 *  interface's language. */
export function statPalette(operations: readonly string[], query: string, text: Text, catalog: StatCatalog = STAT_CATALOG): StatFamilyGroup[] {
  const offered = new Set(operations)
  const q = query.trim().toLowerCase()
  const has = (s: string) => s.toLowerCase().includes(q)
  const out: StatFamilyGroup[] = []
  for (const family of catalog.families) {
    const familyHit = !!q && has(text(`stat.families.${family}`))
    const tests = catalog.tests.filter((t) => t.family === family && offered.has(t.operation)
      && (!q || familyHit || has(text(statNameKey(t))) || has(text(statHintKey(t))) || has(t.operation)))
    if (tests.length) out.push({ family, tests })
  }
  return out
}

/** What the type selector of a statistics node offers: the other tests, by family. */
export function statTypeOptions(operations: readonly string[], text: Text, catalog: StatCatalog = STAT_CATALOG): { value: string; label: string; group: string }[] {
  return statPalette(operations, '', text, catalog).flatMap((g) =>
    g.tests.map((t) => ({ value: t.operation, label: text(statNameKey(t)), group: text(`stat.families.${g.family}`) })))
}

/** Whether an engine's refusal only says that a statistics node is not set yet (a key it
 *  needs is not chosen): the editor then shows the hint to set it, not an error. The
 *  sentence is `check_params`' in backend/app/engine/stats/catalog.py. */
export function statIsUnset(message: string): boolean {
  return /\bstat_\w+: '\w+' is required/.test(message)
}
