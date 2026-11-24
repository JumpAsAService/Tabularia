// The statistical models of a flow (the `model` and `model_predict` nodes) as the editor needs
// them: the palette's group, the starting parameters of a node, the keys its form draws for
// the variant it is on, what makes a node ready to run, the one line that says what it does.
// Everything is read from the catalogue: assets/model-catalog.json is a copy of the engine's
// snapshot (backend/app/engine/models/catalog.json, written from catalog.py; the copy by
// .github/scripts/model_catalog.py). A new model appears here by being added there, and
// tests/model-catalog.test.mjs names the words it lacks.
//
// Pure functions, nothing of Nuxt or the DOM: the tests import them as they are. Each one
// takes the catalogue as its last argument, the real one unless a test hands another.
// Every export starts with `model`, `MODEL_`, `Model`, `predict` or `PREDICT_`: a
// composable's exports are auto-imported everywhere.
//
// The rules of the gateway (gateway-go/internal/modelcatalog `Check`) are the rules here:
// a key the model does not take is refused, a key given while it does not apply for the
// variant is refused too (`applies`: «given» / «not given» / a list of values / a value, the
// defaults counting for what is not given), what `requires` and `forbids` say is refused.
// So the form shows only the keys that apply, and what it saves holds only those.

import raw from '~/assets/model-catalog.json'
import { isNumericDtype } from '~/composables/previewGrid'

export type ModelControl =
  | 'column' | 'columns' | 'level' | 'reference' | 'pairs' | 'choice' | 'number' | 'integer'
  | 'boolean' | 'numbers' | 'number_or_choice' | 'level_numbers' | 'column_choices'

/** How a form asks for one kind of value (the catalogue's `kinds`). */
export interface ModelKind {
  control: ModelControl
  numeric?: boolean
  predictor?: boolean
  types?: string[]
  of?: string
  min?: number
  max?: number
  max_count?: number
  above?: number
  below?: number
  choices?: string[]
  /** each number is a whole one (a period, a window, a lag counted in steps): the catalogue says it, the engine and the
   *  gateway refuse 7.5 by the same flag */
  whole?: boolean
}

/** `key` may not be `value` while every key of `when` holds (a value, a list of values, true/false). */
export interface ModelForbid {
  key: string
  value: unknown
  when: Record<string, unknown>
  reason: string
  message: string
}

/** `key` is needed while every key of `when` holds. */
export interface ModelRequire {
  key: string
  when: Record<string, unknown>
  reason: string
  message: string
}

export interface ModelVariant {
  id: string
  when: Record<string, unknown>
  /** The columns each output adds (`rows`: the names the node's rows carry, a `*` standing for one per quantile or per time). */
  outputs?: Record<string, string[]>
  /** A column of the rows output the runner writes only when its rule holds, as `when` (a holdout's). */
  rows_when?: Record<string, Record<string, unknown>>
  /** The most input rows of this variant, below its model's and its group's (the risk simulation's ARIMA: 20,000); absent: theirs. */
  max_rows?: number
}

export interface ModelDef {
  id: string
  group: string
  family: string
  label: string
  /** Whether the node writes a model (a card the registry can hold): false for the experiments' nodes, which register nothing. */
  card?: boolean
  needs: Record<string, string>
  takes: Record<string, string>
  defaults: Record<string, unknown>
  /** A key that matters only while other keys hold: {mixing: {penalty: ['elastic_net']}},
   *  {horizon: {exog: false}} (false: «not given»). */
  applies: Record<string, Record<string, unknown>>
  requires: ModelRequire[]
  forbids: ModelForbid[]
  disjoint: string[]
  presets: Record<string, Record<string, unknown>>
  variants: ModelVariant[]
}

export interface ModelGroupDef {
  id: string
  language: string
  label: string
  models: string[]
}

export interface PredictDef {
  takes: Record<string, string>
  defaults: Record<string, unknown>
  applies_to: Record<string, string[]>
  columns: Record<string, Record<string, string[]>>
}

export interface ModelCatalog {
  format_version: number
  nodes: { model: { data: string[]; mlflow: string[] }; model_predict: { data: string[] } }
  limits: Record<string, number>
  controls: string[]
  column_kinds: string[]
  kinds: Record<string, ModelKind>
  season_of_frequency: Record<string, number>
  /** Per output (`rows`, `table`, `forecast`, `results`), the type of each column it writes as the engine names it; a
   *  name with a `*` stands for every name it makes, «@<key>» is the type of the input's column the key names. */
  output_types: Record<string, Record<string, string>>
  /** How a runner of a language writes the input's columns back in the rows output, by their kind, where the type changes. */
  rows_input_types: Record<string, Record<string, string>>
  groups: ModelGroupDef[]
  models: ModelDef[]
  predict: PredictDef
}

/** One field of a model's form: a key of the model, with what its control needs to know. */
export interface ModelField extends ModelKind {
  key: string
  kind: string
  required: boolean
  choices: string[]
}

export const MODEL_CATALOG: ModelCatalog = raw as unknown as ModelCatalog

/** The controls components/ui/ModelForm.vue draws: a kind with another one has no field. */
export const MODEL_CONTROLS: readonly ModelControl[] = [
  'column', 'columns', 'level', 'reference', 'pairs', 'choice', 'number', 'integer', 'boolean', 'numbers', 'number_or_choice',
  // a number for each value of a column (the experiments' expected shares)
  'level_numbers',
  // one of the choices for some columns of another key (the direction of each secondary metric of an A/B test)
  'column_choices',
]

/** The node types of a flow that are models. */
export const MODEL_NODE_TYPE = 'model'
/** The groups whose models are a node of their own and not a Model node: the Models palette leaves them out (the node «Bayesian
 *  model» is an item of its own in the palette, docs/specs/bayes-report.md, «Decisions»). One constant here, no field in the catalogue. */
export const MODEL_GROUPS_OF_THEIR_OWN: ReadonlySet<string> = new Set(['bayes'])
export const PREDICT_NODE_TYPE = 'model_predict'

/** The colour of the models: the palette's items, the nodes, the minimap. */
export const MODEL_COLOR = '#2dd4bf'

/** What a new model node starts with, besides its parameters: as a Script node. */
export const MODEL_TIMEOUT_SECONDS = 900
export const MODEL_RETRIES_MAX = 5
export const MODEL_TIMEOUT_MIN = 10

type Params = Record<string, any>
type Text = (key: string) => string

const isEmpty = (v: unknown): boolean => v === undefined || v === null || v === ''

/** The model of a catalogue id, or null. */
export function modelOf(id: unknown, catalog: ModelCatalog = MODEL_CATALOG): ModelDef | null {
  return typeof id === 'string' ? catalog.models.find((m) => m.id === id) ?? null : null
}

/** The group a model belongs to. */
export function modelGroupOf(id: unknown, catalog: ModelCatalog = MODEL_CATALOG): ModelGroupDef | null {
  const m = modelOf(id, catalog)
  return m ? catalog.groups.find((g) => g.id === m.group) ?? null : null
}

/** The fields of a model's form, in the catalogue's order: what it needs, then what it takes
 *  besides. */
export function modelFields(model: ModelDef, catalog: ModelCatalog = MODEL_CATALOG): ModelField[] {
  const out: ModelField[] = []
  for (const [keys, required] of [[model.needs, true], [model.takes, false]] as const) {
    for (const [key, kind] of Object.entries(keys)) {
      const how = catalog.kinds[kind]
      if (!how) continue
      out.push({ ...how, key, kind, required, choices: how.choices ?? [] })
    }
  }
  return out
}

// ── what holds with the parameters as they are ───────────────────────────────

/** A value counts as given when it is not null, and, for a list or an object, not empty
 *  (the gateway's `given_`). */
export function modelGiven(v: unknown): boolean {
  if (v === undefined || v === null || v === '') return false
  if (Array.isArray(v)) return v.length > 0
  if (typeof v === 'object') return Object.keys(v as object).length > 0
  return true
}

/** A rule (true: given, false: not given, a list: one of, a value: that) against a value
 *  (the gateway's `holds`). */
export function modelHolds(rule: unknown, value: unknown): boolean {
  if (typeof rule === 'boolean') return rule ? modelGiven(value) : !modelGiven(value)
  if (Array.isArray(rule)) return rule.some((x) => x === value)
  return rule === value
}

/** Every key of the model with its value: the given one, or its default. */
export function modelFull(model: ModelDef, params: Params | null | undefined): Params {
  const full: Params = {}
  const p = params ?? {}
  for (const key of [...Object.keys(model.needs), ...Object.keys(model.takes)]) {
    full[key] = isEmpty(p[key]) ? model.defaults[key] ?? null : p[key]
  }
  return full
}

/** Whether `key` matters with the other parameters as they are: the form shows it only then,
 *  the node saves it only then. */
export function modelApplies(model: ModelDef, key: string, params: Params | null | undefined): boolean {
  const rule = model.applies[key]
  if (!rule) return true
  const full = modelFull(model, params)
  return Object.entries(rule).every(([k, want]) => modelHolds(want, full[k]))
}

/** Why `key` may not be `value` with the other parameters as they are, or null when it may. */
export function modelForbidden(model: ModelDef, key: string, value: unknown, params: Params | null | undefined): ModelForbid | null {
  const full = modelFull(model, params)
  return model.forbids.find((r) => r.key === key && r.value === value && Object.entries(r.when).every(([k, want]) => modelHolds(want, full[k]))) ?? null
}

/** Why a choice's `value` is not offered with the other parameters as they are, or null when it is: the rule that forbids
 *  `key` to be `value`, or the one that forbids another key's value while this choice is the one in `when` («ratio» next to
 *  «aggregated»: whichever the person picks first, the other is greyed with the same reason). */
export function modelForbiddenOption(model: ModelDef, key: string, value: unknown, params: Params | null | undefined): ModelForbid | null {
  const direct = modelForbidden(model, key, value, params)
  if (direct) return direct
  const full = modelFull(model, params)
  return model.forbids.find((r) => {
    // the rule names this choice among its conditions, the value it forbids is the one the other key has, and every other
    // condition holds (a multiplicative decomposition is grey while a transform is chosen, but only with the classical method)
    if (!(key in r.when) || full[r.key] !== r.value) return false
    const want = r.when[key]
    if (!modelHolds(want, value)) return false
    return Object.entries(r.when).every(([k, w]) => k === key || modelHolds(w, full[k]))
  }) ?? null
}

/** The variant the parameters make («unpenalized», «penalized», «manual», «auto», «default»). */
export function modelVariant(model: ModelDef, params: Params | null | undefined): string {
  const full = modelFull(model, params)
  const hit = model.variants.find((v) => Object.entries(v.when).every(([k, want]) => modelHolds(want, full[k])))
  return hit?.id ?? model.variants[0]?.id ?? ''
}

// ── reading what the form writes ─────────────────────────────────────────────

/** One number, by the rule of the lists of numbers (modelNumbersRead): a point is a decimal everywhere; a comma is the decimal
 *  mark in a language that writes one (`decimalComma` true: «0,5» is a half in it, de, fr and es) and never in English
 *  (false: there it separates numbers, and «1,000» is no single number). A value saved in the node, whose language is not
 *  known (undefined), is read only when it reads one way only: «0,5» is a half, «1,000» could be one or a thousand and is no
 *  number. Never a guess: NaN when it is not one, or not only one («1,2,3», «1.000,5»). (The node's boxes of one number are
 *  `type="number"`: the browser gives them with a point.) */
export function modelNumber(value: unknown, decimalComma?: boolean): number {
  if (typeof value === 'number') return value
  if (typeof value !== 'string') return NaN
  let text = value.trim()
  const commas = (text.match(/,/g) ?? []).length
  if (commas > 1 || (commas && decimalComma === false) || (decimalComma === undefined && /^[-+]?\d+,\d{3}$/.test(text))) return NaN
  text = text.replace(',', '.')
  return PLAIN_NUMBER.test(text) ? Number(text) : NaN
}

/** The words of the example under an empty box of numbers: the quantiles' own («e.g. 0.1, 0.9»: proportions, with a point, the
 *  comma being a separator of the list), the times' and the others' («e.g. 12, 24, 36»). */
export function modelNumbersPlaceholderKey(field: Pick<ModelField, 'key'>): string {
  if (field.key === 'periods') return 'models.form.periodsPlaceholder'
  return field.key === 'quantiles' ? 'models.form.quantilesPlaceholder' : 'models.form.numbersPlaceholder'
}

/** The languages that write a decimal with a comma («12,5»): in them the comma is the decimal mark and the list is
 *  separated by «;» or a space; in the others (English) the point is, and a comma separates. */
export const DECIMAL_COMMA_LOCALES = ['it', 'de', 'fr', 'es']
export const modelDecimalComma = (locale: string | null | undefined): boolean => DECIMAL_COMMA_LOCALES.includes((locale ?? '').slice(0, 2).toLowerCase())

const PLAIN_NUMBER = /^[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?$/

export interface ModelNumbersRead {
  values: number[]
  /** '' when the text reads one way only; 'ambiguous' when a comma could be a decimal mark or a separator (a decimal-comma
   *  language writing «12,24,36»); 'notNumber' when a part is no number at all. */
  problem: '' | 'ambiguous' | 'notNumber'
  /** The part that is the problem. */
  part?: string
}

/** A list of numbers as a person types it, read the way their language writes numbers: «;» and spaces always separate;
 *  the comma separates in English (point decimal) and is the decimal mark between two digits in it, de, fr and es
 *  («12,5; 24» is 12.5 and 24). A point is a decimal everywhere. A part that is no number, or a chain «12,24,36» that a
 *  decimal-comma language cannot read one way only, is a problem, never silently dropped or split. */
export function modelNumbersRead(text: string, decimalComma: boolean): ModelNumbersRead {
  const out: number[] = []
  for (const raw of text.split(decimalComma ? /[;\s]+/ : /[;,\s]+/)) {
    const part = decimalComma ? raw.replace(/^,+|,+$/g, '') : raw
    if (part === '') continue
    if (decimalComma && (part.match(/,/g) ?? []).length > 1) return { values: [], problem: 'ambiguous', part }
    const n = PLAIN_NUMBER.test(decimalComma ? part.replace(',', '.') : part) ? Number(decimalComma ? part.replace(',', '.') : part) : NaN
    if (!Number.isFinite(n)) return { values: [], problem: 'notNumber', part }
    out.push(n)
  }
  return { values: [...new Set(out)].sort((a, b) => a - b), problem: '' }
}

/** A number as a language writes it in a list («12,5» with a decimal comma, «12.5» without). */
export const modelNumberText = (n: number, decimalComma: boolean): string => (decimalComma ? String(n).replace('.', ',') : String(n))

/** A list of numbers as the box shows it again: «12,5; 24» with a decimal comma, «12.5, 24» without. */
export function modelNumbersText(values: unknown, decimalComma: boolean): string {
  return Array.isArray(values) ? values.map((n) => modelNumberText(Number(n), decimalComma)).join(decimalComma ? '; ' : ', ') : ''
}

/** A list of numbers as it is saved (an array) or, as text, with a point decimal and «;», commas or spaces between. */
export function modelNumbers(value: unknown): number[] {
  const parts = Array.isArray(value) ? value : typeof value === 'string' ? value.split(/[;,\s]+/) : []
  const out: number[] = []
  for (const part of parts) {
    const n = typeof part === 'number' ? part : PLAIN_NUMBER.test(String(part).trim()) ? Number(String(part).trim()) : NaN
    if (Number.isFinite(n)) out.push(n)
  }
  return [...new Set(out)].sort((a, b) => a - b)
}

function readOne(f: ModelField, v: unknown): unknown {
  switch (f.control) {
    case 'column':
      return typeof v === 'string' && v ? v : undefined
    case 'columns': {
      const list = Array.isArray(v) ? v.filter((x) => typeof x === 'string' && x) as string[] : []
      const names = [...new Set(list)]
      return names.length ? names : undefined
    }
    case 'level':
      // a value of the column, as chosen or written: a text, or the number or boolean a column holds
      if (typeof v === 'string') return v.trim() !== '' ? v : undefined
      return typeof v === 'boolean' || (typeof v === 'number' && Number.isFinite(v)) ? v : undefined
    case 'boolean':
      return v === true
    case 'number':
    case 'integer': {
      const n = modelNumber(v)
      return Number.isFinite(n) ? n : undefined
    }
    case 'choice':
      return typeof v === 'string' && f.choices.includes(v) ? v : undefined
    case 'numbers': {
      const list = modelNumbers(v)
      return list.length ? list : undefined
    }
    case 'level_numbers': {
      // {value: number}: what is written is kept as a number, and a value with none is left out
      if (!v || typeof v !== 'object' || Array.isArray(v)) return undefined
      const out: Record<string, number> = {}
      for (const [label, raw] of Object.entries(v as Params)) {
        const n = modelNumber(raw)
        if (label && Number.isFinite(n)) out[label] = n
      }
      return Object.keys(out).length ? out : undefined
    }
    case 'number_or_choice': {
      if (typeof v === 'string' && f.choices.includes(v)) return v
      const n = modelNumber(v)
      return Number.isFinite(n) ? n : undefined
    }
    default:
      return undefined // reference, pairs, column choices: read with the columns they name
  }
}

/** A node's parameters as the gateway takes them: the model's own keys in the catalogue's
 *  order, each read as its control writes it; what is empty, unknown or half given is left
 *  out, and so is every key that does not apply to the variant the rest makes (the gateway
 *  refuses a key given while it does not apply). Dropping one can make another stop applying
 *  (a cluster column once `se` is gone): it goes on until nothing changes. */
export function modelParams(model: ModelDef, params: Params | null | undefined, catalog: ModelCatalog = MODEL_CATALOG): Params {
  const p = params ?? {}
  const fields = modelFields(model, catalog)
  let read: Params = {}
  for (const f of fields) {
    const v = p[f.key]
    if (isEmpty(v)) continue
    if (f.control !== 'reference' && f.control !== 'pairs') {
      const one = readOne(f, v)
      if (one !== undefined) read[f.key] = one
    }
  }
  // a level, a reference, a pair names columns that must still be the ones it is of
  for (const f of fields) {
    const v = p[f.key]
    if (isEmpty(v)) continue
    if (f.control === 'reference' && v && typeof v === 'object' && !Array.isArray(v)) {
      const of: string[] = Array.isArray(read[f.of ?? '']) ? read[f.of ?? ''] : []
      const out: Params = {}
      for (const [column, level] of Object.entries(v as Params)) {
        const one = readOne({ ...f, control: 'level' }, level)
        if (of.includes(column) && one !== undefined) out[column] = one
      }
      if (Object.keys(out).length) read[f.key] = out
    } else if (f.control === 'column_choices' && v && typeof v === 'object' && !Array.isArray(v)) {
      // {column: choice}, for the columns of `of` only: one not named keeps the node's own setting
      const of: string[] = Array.isArray(read[f.of ?? '']) ? read[f.of ?? ''] : []
      const out: Params = {}
      for (const [column, choice] of Object.entries(v as Params)) {
        if (of.includes(column) && typeof choice === 'string' && f.choices.includes(choice)) out[column] = choice
      }
      if (Object.keys(out).length) read[f.key] = out
    } else if (f.control === 'pairs' && Array.isArray(v)) {
      const of: string[] = Array.isArray(read[f.of ?? '']) ? read[f.of ?? ''] : []
      const seen = new Set<string>()
      const out: [string, string][] = []
      for (const pair of v) {
        if (!Array.isArray(pair) || pair.length !== 2) continue
        const [a, b] = pair as [unknown, unknown]
        if (typeof a !== 'string' || typeof b !== 'string' || a === b || !of.includes(a) || !of.includes(b)) continue
        const id = [a, b].sort().join('\u0001')
        if (seen.has(id)) continue
        seen.add(id)
        out.push([a, b])
      }
      if (out.length) read[f.key] = out
    }
  }
  for (let round = 0; round < fields.length + 1; round++) {
    const next: Params = {}
    for (const [key, v] of Object.entries(read)) if (modelApplies(model, key, read)) next[key] = v
    const changed = Object.keys(next).length !== Object.keys(read).length
    read = next
    if (!changed) break
  }
  const out: Params = {}
  for (const f of fields) if (f.key in read) out[f.key] = read[f.key]
  return out
}

/** What a new node of the model starts with: the catalogue's defaults of the keys that apply
 *  and say something (a null, an empty list and an empty object are «not given»), with the
 *  preset's values over them. */
export function modelDefaults(id: string, preset?: string, catalog: ModelCatalog = MODEL_CATALOG): Params {
  const model = modelOf(id, catalog)
  if (!model) return {}
  const start: Params = {}
  for (const [key, v] of Object.entries(model.defaults)) if (modelGiven(v)) start[key] = v
  Object.assign(start, preset ? model.presets[preset] ?? {} : {})
  return modelParams(model, start, catalog)
}

/** The parameters after one field changed. A level chosen among a column's values, or a number for each of them, does
 *  not survive another column. */
export function modelSet(model: ModelDef, params: Params | null | undefined, key: string, value: unknown, catalog: ModelCatalog = MODEL_CATALOG): Params {
  const before = params ?? {}
  const next: Params = { ...before, [key]: value }
  if (JSON.stringify(before[key]) !== JSON.stringify(value)) {
    for (const f of modelFields(model, catalog)) if ((f.control === 'level' || f.control === 'level_numbers') && f.of === key) delete next[f.key]
  }
  return modelParams(model, next, catalog)
}

/** The keys the node still lacks to run: what the model needs, and what `requires` asks for with the other
 *  parameters as they are. */
export function modelUnset(model: ModelDef, params: Params | null | undefined, catalog: ModelCatalog = MODEL_CATALOG): string[] {
  const p = modelParams(model, params, catalog)
  const full = modelFull(model, p)
  const missing = Object.keys(model.needs).filter((k) => !modelGiven(p[k]) && !modelGiven(model.defaults[k]))
  for (const r of model.requires) {
    if (Object.entries(r.when).every(([k, want]) => modelHolds(want, full[k])) && !modelGiven(full[r.key]) && !missing.includes(r.key)) missing.push(r.key)
  }
  return missing
}

/** The words of what `requires` says about a key not given yet, or ''. */
export function modelRequiredReason(model: ModelDef, key: string, params: Params | null | undefined): ModelRequire | null {
  const full = modelFull(model, params)
  return model.requires.find((r) => r.key === key && Object.entries(r.when).every(([k, want]) => modelHolds(want, full[k]))) ?? null
}

/** The columns two keys of the model both name (the model refuses it): the column, and the keys. */
export function modelOverlaps(model: ModelDef, params: Params | null | undefined): { column: string; keys: string[] }[] {
  const p = params ?? {}
  const owners = new Map<string, string[]>()
  for (const key of model.disjoint) {
    const v = p[key]
    const names = Array.isArray(v) ? v : typeof v === 'string' && v ? [v] : []
    for (const c of names) owners.set(c, [...(owners.get(c) ?? []), key])
  }
  return [...owners.entries()].filter(([, keys]) => keys.length > 1).map(([column, keys]) => ({ column, keys }))
}

/** The columns other keys of the model already name, for `key`: the form does not offer them
 *  (the model refuses a column that is both its outcome and one of its predictors). */
export function modelTaken(model: ModelDef, params: Params | null | undefined, key: string): Set<string> {
  const taken = new Set<string>()
  if (!model.disjoint.includes(key)) return taken
  const p = params ?? {}
  for (const other of model.disjoint) {
    if (other === key) continue
    const v = p[other]
    for (const c of Array.isArray(v) ? v : typeof v === 'string' && v ? [v] : []) taken.add(c)
  }
  return taken
}

/** What is wrong with a number against its field's bounds: '' when nothing. «between» and
 *  «above»/«below» exclude the bound; «range» and «atLeast»/«atMost» include it; «whole» is a
 *  number that is not an integer where a whole number is asked. */
export function modelNumberProblem(field: Pick<ModelField, 'control' | 'above' | 'below' | 'min' | 'max'> & { whole?: boolean }, value: unknown): '' | 'between' | 'above' | 'below' | 'range' | 'atLeast' | 'atMost' | 'whole' {
  if (isEmpty(value)) return ''
  const n = modelNumber(value)
  if (!Number.isFinite(n)) return ''
  if ((field.control === 'integer' || modelWhole(field)) && !Number.isInteger(n)) return 'whole'
  const low = field.above !== undefined && !(n > field.above)
  const high = field.below !== undefined && !(n < field.below)
  if (low || high) {
    if (field.above !== undefined && field.below !== undefined) return 'between'
    return low ? 'above' : 'below'
  }
  const under = field.min !== undefined && n < field.min
  const over = field.max !== undefined && n > field.max
  if (under || over) {
    if (field.min !== undefined && field.max !== undefined) return 'range'
    return under ? 'atLeast' : 'atMost'
  }
  return ''
}

/** The values of a column that have no number yet in a `level_numbers` value ({value: number}): the field wants one for every
 *  value of the column once it is given at all (the model refuses a split that leaves a variant out). */
export function modelLevelNumbersMissing(value: unknown, labels: readonly string[]): string[] {
  const given = value && typeof value === 'object' && !Array.isArray(value) ? (value as Params) : {}
  return labels.filter((l) => !Number.isFinite(modelNumber(given[l])))
}

/** The weights of a `level_numbers` value as shares of their sum, in the labels' order (1:1:2 → 0.25, 0.25, 0.5); null while
 *  one is missing or not above zero, or there is nothing to share. */
export function modelLevelShares(value: unknown, labels: readonly string[]): number[] | null {
  const given = value && typeof value === 'object' && !Array.isArray(value) ? (value as Params) : {}
  const weights = labels.map((l) => modelNumber(given[l]))
  if (!weights.length || weights.some((w) => !(w > 0) || !Number.isFinite(w))) return null
  const sum = weights.reduce((a, b) => a + b, 0)
  return weights.map((w) => w / sum)
}

/** Whether a field's numbers are whole (a period or a span counted in steps, a lag): its kind's `whole` in the catalogue,
 *  the flag the engine and the Go gateway read to refuse «365.25» (an `integer` kind is whole by its control). */
export const modelWhole = (field: { whole?: boolean }): boolean => field.whole === true

/** The levels Timestral forecasts (its 21 quantiles): the form draws the quantiles key as a box for each, not as a free list. */
export const MODEL_QUANTILE_LEVELS: readonly number[] = [0.01, 0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.35, 0.4, 0.45, 0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95, 0.99]
/** The level that is the forecast column itself, not a quantile column. */
export const MODEL_MEDIAN_LEVEL = 0.5

/** The numbers of a list a kind lets be chosen among (a fixed set, not written), or null: the form then draws a box for each. */
export function modelNumbersAmong(field: Pick<ModelField, 'kind'>): readonly number[] | null {
  return field.kind === 'foundation_quantiles' ? MODEL_QUANTILE_LEVELS : null
}

/** The levels a node asks for now: the ones it holds, else the model's default; as the engine reads them (sorted, once each). */
export function modelLevelsChosen(model: ModelDef, key: string, params: Params | null | undefined): number[] {
  const given = modelNumbers((params ?? {})[key])
  return given.length ? given : modelNumbers(model.defaults[key])
}

/** The choices of a key the form offers: the kind's own, except the sizes the group's ready version does not hold (`sizes`: null
 *  when the route did not say). The one the node already has stays, so the form never shows a node with nothing chosen. */
export function modelChoicesOffered(field: Pick<ModelField, 'kind' | 'choices'>, current: unknown, sizes: readonly string[] | null | undefined): string[] {
  if (field.kind !== 'foundation_size' || !sizes) return field.choices
  return field.choices.filter((c) => sizes.includes(c) || c === current)
}

/** Whether a size is chosen that the ready version does not hold (the gateway refuses it at the run: say it before). */
export function modelSizeAbsent(field: Pick<ModelField, 'kind'>, current: unknown, sizes: readonly string[] | null | undefined): boolean {
  return field.kind === 'foundation_size' && !!sizes && typeof current === 'string' && !sizes.includes(current)
}

/** Covariates the node holds in both lists, against the model's bound for the two together (the catalogue bounds each alone). */
export function modelCovariatesOver(params: Params | null | undefined, catalog: ModelCatalog = MODEL_CATALOG): { n: number; max: number } | null {
  const p = params ?? {}
  const n = ['past_covariates', 'known_covariates'].reduce((sum, k) => sum + (Array.isArray(p[k]) ? p[k].length : 0), 0)
  const max = catalog.limits.max_covariates
  return max !== undefined && n > max ? { n, max } : null
}

/** What is wrong with a list of numbers against its field's bounds (the quantiles in (0, 1), the times from 0,
 *  at most `max_count` of them): '' when nothing, else the first number's problem or 'tooMany'. */
export function modelNumbersProblem(field: Pick<ModelField, 'above' | 'below' | 'min' | 'max' | 'max_count'> & { whole?: boolean }, value: unknown): ReturnType<typeof modelNumberProblem> | 'tooMany' {
  const list = modelNumbers(value)
  for (const n of list) {
    const problem = modelNumberProblem({ control: 'number', whole: field.whole, above: field.above, below: field.below, min: field.min, max: field.max }, n)
    if (problem) return problem
  }
  return field.max_count !== undefined && list.length > field.max_count ? 'tooMany' : ''
}

// ── what the node says ───────────────────────────────────────────────────────

const TEMPORAL = /^(Date|Datetime|Time|Duration)/i
const WHOLE = /^U?Int/i

/** Whether a column of a type (as a preview names it) can be what a kind of field asks for: a number
 *  for a numeric kind, a date, a datetime or an integer for a time column, anything but a date
 *  for a predictor (the model refuses a date there: compute a number from it first). */
export function modelColumnFits(kind: Pick<ModelKind, 'numeric' | 'types' | 'predictor'>, dtype: string): boolean {
  if (kind.numeric && !isNumericDtype(dtype)) return false
  if (kind.types && !kind.types.includes(modelColumnKind(dtype))) return false
  if (kind.predictor && TEMPORAL.test(dtype)) return false
  return true
}

/** The kind of a column from its type as a preview names it, as the engine's `column_kind` says it: «integer», «number»,
 *  «text», «boolean», «date», «datetime», «other» (what the catalogue's `types` lists). */
export function modelColumnKind(dtype: string): string {
  const name = String(dtype || '')
  if (/^U?Int/.test(name)) return 'integer'
  if (/^(Float|Decimal)/.test(name)) return 'number'
  if (name === 'String' || name === 'Utf8' || name === 'Categorical' || name.startsWith('Enum')) return 'text'
  if (name === 'Boolean') return 'boolean'
  if (name === 'Date') return 'date'
  if (name.startsWith('Datetime')) return 'datetime'
  return 'other'
}

/** Whether a predictor of this type is a set of categories (text, boolean) and so has a reference level. */
export function modelIsCategorical(dtype: string): boolean {
  return !isNumericDtype(dtype) && !TEMPORAL.test(dtype)
}

/** The columns a model reads, in its keys' order. */
export function modelColumns(id: string, params: Params | null | undefined, catalog: ModelCatalog = MODEL_CATALOG): string[] {
  const model = modelOf(id, catalog)
  if (!model) return []
  const p = modelParams(model, params, catalog)
  const out: string[] = []
  for (const f of modelFields(model, catalog)) {
    const v = p[f.key]
    if (f.control === 'column' && typeof v === 'string') out.push(v)
    else if (f.control === 'columns' && Array.isArray(v)) out.push(...v)
  }
  return [...new Set(out)]
}

/** The period of an ARIMA node: the one given, else the frequency's own. */
export function modelSeason(params: Params | null | undefined, catalog: ModelCatalog = MODEL_CATALOG): number | null {
  const p = params ?? {}
  const given = modelNumber(p.s)
  if (Number.isFinite(given)) return given
  const frequency = typeof p.frequency === 'string' ? p.frequency : ''
  return catalog.season_of_frequency[frequency] ?? null
}

/** What the orders make of an ARIMA node: «AR(1)», «ARMA(1,1)», «ARIMA(1,1,1)», «SARIMA(1,1,1)(0,1,1)[12]»,
 *  an exogenous column adds an X. Automatic orders: «ARIMA (auto)». Codes only, no language. */
export function modelArimaTitle(params: Params | null | undefined, catalog: ModelCatalog = MODEL_CATALOG): string {
  const model = modelOf('arima', catalog)
  const full = modelFull(model ?? ({ needs: {}, takes: {}, defaults: {} } as unknown as ModelDef), params)
  const x = Array.isArray(full.exog) && full.exog.length > 0 ? 'X' : ''
  if (full.orders === 'auto') return `ARIMA${x} (auto)`
  const n = (k: string) => (Number.isFinite(modelNumber(full[k])) ? modelNumber(full[k]) : 0)
  const [p, d, q, P, D, Q] = ['p', 'd', 'q', 'P', 'D', 'Q'].map(n) as [number, number, number, number, number, number]
  if (P + D + Q > 0) return `SARIMA${x}(${p},${d},${q})(${P},${D},${Q})[${modelSeason(params, catalog) ?? '?'}]`
  if (d > 0) return `ARIMA${x}(${p},${d},${q})`
  if (p > 0 && q > 0) return `ARMA${x}(${p},${q})`
  if (p > 0) return `AR${x}(${p})`
  if (q > 0) return `MA${x}(${q})`
  return `ARIMA${x}(${p},${d},${q})`
}

/** What a GARCH node's options make of it, as the engine's `garch_name` writes it (the title of its report): «GARCH(1,1), t»,
 *  «GJR-GARCH(1,1), skewed t», «AR(1)-EGARCH(1,1), normal», «ARCH(2), t». Bollerslev's order: p lags of the variance
 *  (`garch_order`), then q of the shocks (`arch_order`). Codes and the law's name only, no language. */
export function modelGarchTitle(params: Params | null | undefined, catalog: ModelCatalog = MODEL_CATALOG): string {
  const model = modelOf('garch', catalog)
  const full = modelFull(model ?? ({ needs: {}, takes: {}, defaults: {} } as unknown as ModelDef), params)
  const whole = (k: string, fallback: number) => (Number.isFinite(modelNumber(full[k])) ? Math.trunc(modelNumber(full[k])) : fallback)
  const [q, p, ar, ma] = [whole('arch_order', 1), whole('garch_order', 1), whole('ar_order', 0), whole('ma_order', 0)]
  const kind = typeof full.variance_model === 'string' ? full.variance_model : 'garch'
  const names: Record<string, string> = { garch: 'GARCH', gjr: 'GJR-GARCH', egarch: 'EGARCH' }
  const core = kind === 'arch' ? `ARCH(${q})` : `${names[kind] ?? 'GARCH'}(${p},${q})`
  const mean = !(ar || ma) ? '' : !ma ? `AR(${ar})` : !ar ? `MA(${ma})` : `ARMA(${ar},${ma})`
  const law = ({ norm: 'normal', std: 't', sstd: 'skewed t' } as Record<string, string>)[String(full.distribution)] ?? 't'
  return `${mean ? `${mean}-` : ''}${core}, ${law}`
}

/** The title a node of a model says by what its options make of it, or null when the model's own name is the title: an
 *  ARIMA's orders, a GARCH's variance model. The one place the canvas, the panel and the table's header read it from. */
export function modelCodeTitle(id: unknown, params: Params | null | undefined, catalog: ModelCatalog = MODEL_CATALOG): string | null {
  if (id === 'arima') return modelArimaTitle(params, catalog)
  if (id === 'garch') return modelGarchTitle(params, catalog)
  return null
}

// The distributions of a parametric survival node as its one line names them: the names of the
// distributions, not words of a language («auto» as ARIMA's «(auto)»).
const DISTRIBUTION_NAMES: Record<string, string> = {
  weibull: 'Weibull', exponential: 'exponential', lognormal: 'log-normal', loglogistic: 'log-logistic',
  gengamma: 'generalized gamma', gompertz: 'Gompertz', auto: 'auto',
}

/** What a model node does, in one line: «amount ~ price + channel», «time, event ~ arm», an
 *  ARIMA's orders. */
export function modelSummary(id: string, params: Params | null | undefined, catalog: ModelCatalog = MODEL_CATALOG,
  words?: { unit?: (unit: string) => string }): string {
  const model = modelOf(id, catalog)
  if (!model) return ''
  const p = modelParams(model, params, catalog)
  const list = (v: unknown) => (Array.isArray(v) ? v.join(' + ') : '')
  if (id === 'arima') return [modelArimaTitle(p, catalog), typeof p.value === 'string' ? p.value : ''].filter(Boolean).join(' · ')
  if (id === 'garch') return [modelGarchTitle(p, catalog), typeof p.value === 'string' ? p.value : ''].filter(Boolean).join(' · ')
  if (id === 'risk_simulation') {
    // the model it fits over the series, and the paths: «GARCH(1,1), t · returns · N=10,000»
    const full = modelFull(model, p)
    const fitted = full.model === 'arima' ? modelArimaTitle(p, catalog) : modelGarchTitle(p, catalog)
    return [fitted, typeof p.value === 'string' ? p.value : '', `N=${modelNumber(full.paths).toLocaleString('en-US')}`].filter(Boolean).join(' · ')
  }
  if (id === 'kaplan_meier') {
    const left = [p.time, p.event].filter((x) => typeof x === 'string').join(', ')
    return [left, p.strata].filter((x) => typeof x === 'string' && x).join(' ~ ')
  }
  if (id === 'parametric_survival') {
    const left = [p.time, p.event].filter((x) => typeof x === 'string').join(', ')
    const shape = DISTRIBUTION_NAMES[typeof p.distribution === 'string' ? p.distribution : 'weibull'] ?? ''
    return [[left, list(p.predictors)].filter(Boolean).join(' ~ '), shape].filter(Boolean).join(' · ')
  }
  if (id === 'clv' || id === 'bg_bb') {
    const columns = [p.frequency, p.recency, p.age ?? p.opportunities].filter((x) => typeof x === 'string').join(', ')
    const unit = p.time_unit ?? p.period_unit
    // the unit in the person's language (the caller's words), the spend as the column that holds it
    const unitText = typeof unit === 'string' ? (words?.unit ? words.unit(unit) : unit) : ''
    return [columns, unitText, typeof p.spend === 'string' && p.spend ? `+ ${p.spend}` : ''].filter(Boolean).join(' · ')
  }
  if (id === 'ab_test' || id === 'ab_plan') {
    // the metric (a column, a ratio of two, or the summed rows' two) over the variant column
    const named = (k: string) => (typeof p[k] === 'string' ? (p[k] as string) : '')
    const metric = named('metric') || [named('numerator'), named('denominator')].filter(Boolean).join(' / ') || [named('units_count'), named('metric_sum')].filter(Boolean).join(', ')
    return [metric, named('variant')].filter(Boolean).join(' ~ ')
  }
  if (id === 'decomposition') {
    // the method over the series: «STL (7) · sales»; MSTL lists its periods, EMD has no period of its own to say
    const method = typeof p.method === 'string' ? p.method : 'stl'
    const shown = method === 'mstl' ? list(Array.isArray(p.periods) ? p.periods : []).replace(/ \+ /g, ', ') : method === 'emd' ? '' : p.period === undefined || p.period === null ? '' : String(p.period)
    const name = method === 'classical' ? (p.type === 'multiplicative' ? 'classical ×' : 'classical') : method.toUpperCase()
    return [shown ? `${name} (${shown})` : name, typeof p.value === 'string' ? p.value : ''].filter(Boolean).join(' · ')
  }
  if (id === 'timestral') {
    // the size over the series: «28M · orders», the series column after a tilde
    const size = typeof p.size === 'string' ? p.size.toUpperCase() : String(model.defaults.size ?? '').toUpperCase()
    return [size, typeof p.value === 'string' ? p.value : '', typeof p.series === 'string' && p.series ? `~ ${p.series}` : ''].filter(Boolean).join(' · ')
  }
  if (id === 'cox') {
    const left = [p.time, p.event].filter((x) => typeof x === 'string').join(', ')
    return [left, list(p.predictors)].filter(Boolean).join(' ~ ')
  }
  return [typeof p.outcome === 'string' ? p.outcome : '', list(p.predictors)].filter(Boolean).join(' ~ ')
}

/** The data of a new model node: what the gateway reads (`nodes.model.data` of the catalogue). */
export function modelNodeData(id: string, preset?: string, catalog: ModelCatalog = MODEL_CATALOG): Params {
  return { model: id, params: modelDefaults(id, preset, catalog), timeoutSeconds: MODEL_TIMEOUT_SECONDS, retries: 0 }
}

/** The models a Risk simulation node can start from: the two it fits. */
export const MODEL_RISK_FROM: readonly string[] = ['arima', 'garch']

/** The Risk simulation node that starts from an ARIMA or a GARCH node («Simulate risk from this model»): its model, and every option
 *  the two nodes have in common with the values the source node has (a default it relies on is copied as the value it is, so the two
 *  fit the same model), the same series columns, its time limit and retries. What only the simulation has starts as the catalogue says.
 *  `refused`: 'model' for a node that is neither of the two, 'exog' for an ARIMA with exogenous columns (their future values cannot
 *  be simulated: the spec's form has none). */
export function modelRiskFrom(source: Params | null | undefined, catalog: ModelCatalog = MODEL_CATALOG): { data: Params } | { refused: 'model' | 'exog' } {
  const from = modelOf(source?.model, catalog)
  const risk = modelOf('risk_simulation', catalog)
  if (!from || !risk || !MODEL_RISK_FROM.includes(from.id)) return { refused: 'model' }
  const own = modelParams(from, source?.params, catalog)
  if (from.id === 'arima' && modelGiven(own.exog)) return { refused: 'exog' }
  const full = modelFull(from, own)
  const start: Params = { model: from.id }
  // `output` is each node's own (a table of the series, or the risk's)
  for (const key of [...Object.keys(risk.needs), ...Object.keys(risk.takes)]) {
    if (key === 'model' || key === 'output' || !(key in from.needs || key in from.takes)) continue
    // an option that does not apply to the source is not copied
    if (!modelApplies(from, key, own)) continue
    if (!modelGiven(full[key])) continue
    start[key] = full[key]
  }
  const data = modelNodeData('risk_simulation', undefined, catalog)
  data.params = modelParams(risk, { ...data.params, ...start }, catalog)
  for (const key of ['timeoutSeconds', 'retries']) if (typeof source?.[key] === 'number') data[key] = source[key]
  return { data }
}

/** The data of a new Predict node. */
export function predictNodeData(): Params {
  return { connectionId: null, experiment: '', registeredModel: '', version: '', params: {}, timeoutSeconds: MODEL_TIMEOUT_SECONDS, retries: 0 }
}

/** The MLflow block of a model node as the gateway takes it: with a connection, the
 *  experiment (required) and what is optional; without, nothing. */
export function modelMlflow(block: Params | null | undefined): Params | undefined {
  if (!block || typeof block.connectionId !== 'number') return undefined
  const out: Params = { connectionId: block.connectionId, experiment: typeof block.experiment === 'string' ? block.experiment.trim() : '' }
  if (typeof block.registerAs === 'string' && block.registerAs.trim()) out.registerAs = block.registerAs.trim()
  if (block.continueWithout === true) out.continueWithout = true
  return out
}

/** What the MLflow block still lacks: 'experiment' when it has a connection and no experiment. */
export function modelMlflowProblem(block: Params | null | undefined): '' | 'experiment' {
  const m = modelMlflow(block)
  return m && !m.experiment ? 'experiment' : ''
}

// ── the groups the person may use ────────────────────────────────────────────

/** One group as the editor's route says it (`GET /models/groups`, see useModelGroups): `enabled` by
 *  the administrator, `usable` by this person (enabled, and they may use it), `ready` with a
 *  built version. */
export interface ModelGroupState {
  id: string
  enabled: boolean
  usable: boolean
  ready?: boolean
  max_timeout_seconds?: number | null
  max_rows?: number | null
  // a group with weights under their own licence (docs/specs/foundation-forecast.md): the sizes its ready version holds
  // (the node's form offers only those; absent: every size the catalogue has) and the licence the administrator accepted
  sizes?: string[] | null
  licence?: ModelGroupLicence | null
}

/** The licence of a group's weights as the node's form says it under the size: its name and where to read it, the
 *  gateway's (`/models/groups`): the text at the pinned revision of each size (`urls`), `url` the first one's. */
export interface ModelGroupLicence {
  name?: string
  url?: string
  urls?: Record<string, string>
}

/** The licence line's data for a group, as the gateway says it, the link of the node's size when it has one; null for a group
 *  with no licence of its own (or a gateway that does not say). Only an https address becomes a link: the route's text is data. */
export function modelGroupLicence(group: string, state: ModelGroupsState | null | undefined, size?: string | null): ModelGroupLicence | null {
  const said = state?.groups.find((g) => g.id === group)?.licence
  if (!said || typeof said !== 'object') return null
  const own = size && said.urls && typeof said.urls === 'object' ? said.urls[size] : undefined
  const url = typeof own === 'string' ? own : said.url
  const name = typeof said.name === 'string' ? said.name : undefined
  return { name, url: typeof url === 'string' && /^https:\/\//.test(url) ? url : undefined }
}

/** The sizes the ready version of a group holds, or null when the route does not say (every one of the catalogue then). */
export function modelGroupSizes(group: string, state: ModelGroupsState | null | undefined): string[] | null {
  const sizes = state?.groups.find((g) => g.id === group)?.sizes
  return Array.isArray(sizes) ? sizes.filter((x) => typeof x === 'string') : null
}

/** What the route answers. `disabled_reason`: 'kubernetes' on an installation without it, 'not_configured'
 *  when the scripting machinery is not set up; null otherwise. */
export interface ModelGroupsState {
  disabled_reason: string | null
  groups: ModelGroupState[]
}

/** Whether this person may use the Bayesian models (the top bar's place, Explore's «New»): the group
 *  `bayes` usable by them, and never on Compose, as the Scripting tab. */
export function bayesUsable(state: Pick<ModelGroupsState, 'disabled_reason' | 'groups'> | null | undefined): boolean {
  return !!state && !state.disabled_reason && state.groups.some((g) => g.id === 'bayes' && !!g.usable)
}

/** Why a model's node cannot run for this person, or '' when it can. 'unknown' while the route has
 *  not answered. */
export type ModelOff = '' | 'kubernetes' | 'not_configured' | 'not_enabled' | 'not_allowed' | 'unknown'

export function modelOff(group: string, state: ModelGroupsState | null | undefined): ModelOff {
  if (!state) return 'unknown'
  if (state.disabled_reason === 'kubernetes') return 'kubernetes'
  if (state.disabled_reason) return 'not_configured'
  const g = state.groups.find((x) => x.id === group)
  if (!g || !g.enabled) return 'not_enabled'
  return g.usable ? '' : 'not_allowed'
}

/** The groups the palette shows: the ones the person may use; on Compose (no Kubernetes) all of
 *  them, greyed out; with none enabled and nothing to say, none (the whole Models group is
 *  hidden). */
export function modelShownGroups(state: ModelGroupsState | null | undefined, catalog: ModelCatalog = MODEL_CATALOG): ModelGroupDef[] {
  if (!state) return []
  const own = catalog.groups.filter((g) => !MODEL_GROUPS_OF_THEIR_OWN.has(g.id))
  if (state.disabled_reason === 'kubernetes') return own
  if (state.disabled_reason) return []
  return own.filter((g) => modelOff(g.id, state) === '')
}

/** One entry of the palette. `kind` is what a drag carries (application/tabularia). */
export interface ModelPaletteItem {
  kind: string
  model: string
  preset?: string
  nameKey: string
  hintKey: string
}

export interface ModelPaletteGroup {
  group: string
  items: ModelPaletteItem[]
}

export const modelKindOf = (model: string, preset?: string): string => (preset ? `model:${model}:${preset}` : `model:${model}`)

/** A palette kind read back: {model, preset} of «model:arima:sarima», or null. */
export function modelFromKind(kind: string, catalog: ModelCatalog = MODEL_CATALOG): { model: string; preset?: string } | null {
  const [head, id, preset] = kind.split(':')
  if (head !== 'model' || !id) return null
  const model = modelOf(id, catalog)
  if (!model || MODEL_GROUPS_OF_THEIR_OWN.has(model.group)) return null // a model of a node of its own is no Model node's
  if (preset !== undefined && !(preset in model.presets)) return null
  return preset ? { model: id, preset } : { model: id }
}

export const modelNameKey = (id: string): string => `models.${id}.name`
export const modelHintKey = (id: string): string => `models.${id}.hint`
export const modelPresetNameKey = (preset: string): string => `models.presets.${preset}.name`
export const modelPresetHintKey = (preset: string): string => `models.presets.${preset}.hint`
export const modelGroupKey = (group: string): string => `models.groups.${group}`

// Models whose form and words the editor has, but whose runner is not built yet: the palette leaves them out. Nothing is
// in it now: the A/B nodes (lot E9), the decomposition (lot D6), Forecast (Timestral) (lot F7) and Volatility (GARCH) and
// Risk simulation (lot G7) are all in. A model being built goes here until its runner runs.
export const MODEL_NOT_IN_THE_PALETTE_YET: ReadonlySet<string> = new Set<string>()

/** The palette's Models group: the shown groups in the catalogue's order, each with its models
 *  and, after ARIMA, the six presets of that same node. With a search, only the items whose
 *  name, hint or group holds it. */
export function modelPalette(shown: readonly ModelGroupDef[], query: string, text: Text, catalog: ModelCatalog = MODEL_CATALOG,
  hidden: ReadonlySet<string> = MODEL_NOT_IN_THE_PALETTE_YET): ModelPaletteGroup[] {
  const q = query.trim().toLowerCase()
  const has = (s: string) => s.toLowerCase().includes(q)
  const out: ModelPaletteGroup[] = []
  for (const group of shown) {
    if (MODEL_GROUPS_OF_THEIR_OWN.has(group.id)) continue
    const groupHit = !!q && has(text(modelGroupKey(group.id)))
    const items: ModelPaletteItem[] = []
    for (const id of group.models) {
      const model = modelOf(id, catalog)
      if (!model || hidden.has(id)) continue
      items.push({ kind: modelKindOf(id), model: id, nameKey: modelNameKey(id), hintKey: modelHintKey(id) })
      for (const preset of Object.keys(model.presets)) {
        items.push({ kind: modelKindOf(id, preset), model: id, preset, nameKey: modelPresetNameKey(preset), hintKey: modelPresetHintKey(preset) })
      }
    }
    const hits = items.filter((i) => !q || groupHit || has(text(i.nameKey)) || has(text(i.hintKey)) || has(i.model) || has(i.preset ?? ''))
    if (hits.length) out.push({ group: group.id, items: hits })
  }
  return out
}

// ── the words ────────────────────────────────────────────────────────────────

/** The words of a key of a model's form, its label (`params`) or its hint (`hints`): the
 *  model's own when it has some (`models.<id>.params.<key>`), else the words every model
 *  shares (`models.params.<key>`). `has` says whether the interface's catalogue holds a key. */
export function modelFieldWordKey(model: ModelDef, key: string, part: 'params' | 'hints', has: (k: string) => boolean): string {
  const own = `models.${model.id}.${part}.${key}`
  return has(own) ? own : `models.${part}.${key}`
}

/** The words of a choice: the kind's own when it has some (`models.choices.<kind>.<choice>`), else the
 *  words every kind shares (`models.choice.<choice>`). */
export function modelChoiceWordKey(kind: string, choice: string, has: (k: string) => boolean): string {
  const own = `models.choices.${kind}.${choice}`
  return has(own) ? own : `models.choice.${choice}`
}

export const modelReasonKey = (reason: string): string => `models.reasons.${reason}`

/** Every key of the interface's catalogues the models need: the names and hints, the groups,
 *  the presets, the fields' labels, the reasons of what is required or forbidden. A field or
 *  choice that has its own words for a kind is not required to: the shared ones are. */
export function modelWordKeys(catalog: ModelCatalog = MODEL_CATALOG): string[] {
  const keys = new Set<string>()
  for (const g of catalog.groups) keys.add(modelGroupKey(g.id))
  for (const m of catalog.models) {
    keys.add(modelNameKey(m.id)).add(modelHintKey(m.id))
    for (const preset of Object.keys(m.presets)) keys.add(modelPresetNameKey(preset)).add(modelPresetHintKey(preset))
    for (const f of modelFields(m, catalog)) {
      keys.add(`models.params.${f.key}`)
      for (const c of f.choices) keys.add(`models.choice.${c}`)
    }
    for (const r of m.forbids) keys.add(modelReasonKey(r.reason))
    for (const r of m.requires) keys.add(modelReasonKey(r.reason))
  }
  for (const key of Object.keys(catalog.predict.takes)) keys.add(`predict.params.${key}`)
  return [...keys]
}

// ── Predict ──────────────────────────────────────────────────────────────────

/** The version of a registered model a Predict node means: the one it names, or, for «latest», the
 *  newest one (the highest number). Its kind and its variant (the tag tabularia.variant) are the
 *  ones the form draws: two versions of a model may differ (v1 regression, v2 binary). */
export function versionHeld<V extends { model: string; version: string }>(versions: readonly V[], registeredModel: string | null | undefined, version: string | null | undefined): V | null {
  const ofModel = versions.filter((v) => v.model === registeredModel)
  if (version && version !== 'latest') return ofModel.find((v) => v.version === version) ?? null
  const number = (v: V) => Number(v.version)
  return ofModel.reduce<V | null>((best, v) => (best === null || number(v) > number(best) ? v : best), null)
}

/** What a model's Predict options are: the keys of `predict.takes` that apply to a model of the
 *  catalogue, with their kinds. */
export function predictFields(modelId: string | null | undefined, catalog: ModelCatalog = MODEL_CATALOG,
  variant?: string | null): ModelField[] {
  if (!modelId) return []
  const out: ModelField[] = []
  for (const [key, kind] of Object.entries(catalog.predict.takes)) {
    // an entry of `applies_to` is a model, or `model:variant` for one variant of it (the version's tag
    // tabularia.variant): the boosting models' threshold is for their binary variant only
    const list = catalog.predict.applies_to[key]
    if (!list?.includes(modelId) && !(variant && list?.includes(`${modelId}:${variant}`))) continue
    const how = catalog.kinds[kind]
    if (how) out.push({ ...how, key, kind, required: false, choices: how.choices ?? [] })
  }
  return out
}

/** A Predict node's options as the gateway takes them: only the keys of the model the version holds,
 *  read as the form writes them, what is empty left out. */
export function predictParams(modelId: string | null | undefined, params: Params | null | undefined, catalog: ModelCatalog = MODEL_CATALOG,
  variant?: string | null): Params {
  const p = params ?? {}
  const out: Params = {}
  for (const f of predictFields(modelId, catalog, variant)) {
    const v = p[f.key]
    if (isEmpty(v)) continue
    const one = readOne(f, v)
    // an option that is off is not given
    if (one === undefined || one === false) continue
    out[f.key] = one
  }
  return out
}

/** The columns a Predict step adds for a model with the options given (`survival_at_*` is one per time, `quantile_*_low` the
 *  interval of each quantile, under a key `a+b` given when both options are). A quantile of 0.5 adds none: it is the median,
 *  the column `prediction` (the runner writes no `quantile_0.5`, as the gateway's `PredictColumns` lists none). */
export function predictColumns(modelId: string | null | undefined, params: Params | null | undefined, catalog: ModelCatalog = MODEL_CATALOG,
  variant?: string | null): string[] {
  // `model:variant` comes first (the boosting models), then the model
  const rows = modelId ? (variant ? catalog.predict.columns[`${modelId}:${variant}`] : null) ?? catalog.predict.columns[modelId] : null
  if (!rows) return []
  const given = predictParams(modelId, params, catalog, variant)
  const out = [...(rows.always ?? [])]
  // the options in alphabetical order, as the gateway lists them (`PredictColumns`)
  for (const [option, names] of Object.entries(rows).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))) {
    // a key `a+b` is given when both options are; its `*` is one per entry of the first (the interval of each quantile, of each time)
    const parts = option.split('+')
    if (option === 'always' || parts.some((part) => given[part] === undefined)) continue
    const list = Array.isArray(given[parts[0]]) && parts[0] === 'quantiles' ? given[parts[0]].filter((t: number) => t !== 0.5) : given[parts[0]]
    for (const name of names) {
      const at = name.indexOf('*')
      if (at >= 0 && Array.isArray(list)) out.push(...list.map((t: number) => `${name.slice(0, at)}${t}${name.slice(at + 1)}`))
      else if (at < 0) out.push(name)
    }
  }
  return out
}

/** The labels a name with a `*` is made of, as the engine writes them (catalog.py `_ROWS_WILDCARDS`): `pred_quantile_`, `pred_surv_at_`
 *  one per number of a list of the node's options (`quantile_` too: the pre-trained forecast's; increasing, each once; the quantile 0.5 is the median: no column of its own,
 *  but in the risk simulation's `quantile_` and `price_quantile_`, which keep it;
 *  a number as its shortest form, 12 and not 12.0); `seasonal_` one per seasonal period of a decomposition (an MSTL's `periods`,
 *  else the node's `period`, else the frequency's own; whole numbers above 1); `imf_` one per IMF up to `max_imf`. null: a name
 *  the data fill (`pred_prob_*`, one per class), not known before a run. */
function wildcardLabels(prefix: string, full: Params, catalog: ModelCatalog): string[] | null {
  const numbers = (key: string): number[] =>
    Array.isArray(full[key]) ? [...new Set((full[key] as unknown[]).filter((v): v is number => typeof v === 'number' && Number.isFinite(v)))].sort((a, b) => a - b) : []
  // the risk simulation's summary (the node with an `es_level`) has no median column besides its quantiles: `quantile_` and
  // `price_quantile_` keep the 0.5 (docs/specs/garch-risk.md)
  if (prefix === 'price_quantile_' || (prefix === 'quantile_' && 'es_level' in full)) return numbers('quantiles').map(numberLabel)
  // `quantile_` is otherwise the pre-trained forecast's: the same list, the same median left out
  if (prefix === 'pred_quantile_' || prefix === 'quantile_') return numbers('quantiles').filter((v) => v !== 0.5).map(numberLabel)
  if (prefix === 'pred_surv_at_') return numbers('times').map(numberLabel)
  if (prefix === 'seasonal_') {
    const frequency = typeof full.frequency === 'string' ? catalog.season_of_frequency[full.frequency] : undefined
    const given = full.method === 'mstl' ? numbers('periods') : typeof full.period === 'number' ? [full.period] : [frequency ?? 1]
    return [...new Set(given.filter((v) => Number.isInteger(v) && v > 1))].sort((a, b) => a - b).map(String)
  }
  if (prefix === 'imf_') {
    const most = catalog.limits.max_imfs ?? 16
    const n = typeof full.max_imf === 'number' && Number.isInteger(full.max_imf) ? full.max_imf : 10
    return Array.from({ length: Math.max(0, Math.min(n, most)) }, (_, i) => String(i + 1))
  }
  return null
}

/** The columns a model node's `rows` output adds, as the engine lists them when it refuses an input that has one already:
 *  the variant's `outputs.rows`, the `*` of a quantile's or a time's name made one per number (the quantile 0.5 is the median:
 *  no column of its own; a number as its shortest form, 12 and not 12.0). The other names with a `*` stay out. */
export function modelRowsColumns(model: ModelDef, params: Params | null | undefined, catalog: ModelCatalog = MODEL_CATALOG): string[] {
  const read = modelParams(model, params, catalog)
  const full = modelFull(model, read)
  const variant = model.variants.find((v) => v.id === modelVariant(model, read))
  const names = (variant?.outputs?.rows ?? []).filter((name) => {
    const rule = variant?.rows_when?.[name]
    return !rule || Object.entries(rule).every(([k, r]) => modelHolds(r, full[k]))
  })
  const out: string[] = []
  for (const name of names) {
    const at = name.indexOf('*')
    if (at < 0) {
      out.push(name)
      continue
    }
    out.push(...(wildcardLabels(name.slice(0, at), full, catalog) ?? []).map((label) => `${name.slice(0, at)}${label}${name.slice(at + 1)}`))
  }
  return out
}

// ── what a model node hands on before it has run ─────────────────────────────

/** A column as a preview names it. */
export interface ModelColumn { name: string; dtype: string }

/** The kind of a column's type an R runner reads it as (the catalogue's `column_kind`, for what changes on the way back). */
const columnKind = (dtype: string): string => (WHOLE.test(dtype) ? 'integer' : isNumericDtype(dtype) ? 'number' : dtype)

/** A number as the runner writes it in a column's name, its shortest form: 10, 0.25 (the engine's `_number_label`). */
const numberLabel = (v: number): string => String(v)

/** What a model node hands to the next step before it has run, from its options and the catalogue alone
 *  (backend/app/engine/models/catalog.py `output_columns`, on the same cases: tests/model-catalog.test.mjs): the input's
 *  columns first for the rows output (as the runner writes them back), then the model's, in the runner's order, each
 *  quantile and time with its interval; the ten columns of the results table. `later`: the names the data fill
 *  («pred_prob_*», one per class), not columns until the model runs. Options not given take their defaults. */
export function modelOutputColumns(model: ModelDef, params: Params | null | undefined, input: readonly ModelColumn[] | null | undefined,
  catalog: ModelCatalog = MODEL_CATALOG): { columns: ModelColumn[]; later: string[] } {
  const full = modelFull(model, modelParams(model, params, catalog))
  // (an output that does not apply is the ten columns: the Bayesian model's prior predictive writes them alone)
  const output = typeof full.output === 'string' && full.output && modelApplies(model, 'output', full) ? full.output : 'results'
  const types = catalog.output_types?.[output] ?? {}
  if (output === 'results') return { columns: Object.entries(types).map(([name, dtype]) => ({ name, dtype })), later: [] }
  // (a metric type not chosen yet gives the first variant, whose outputs are those of every variant of the experiments)
  const variant = model.variants.find((v) => v.id === modelVariant(model, full))
  const names = (variant?.outputs?.[output] ?? []).filter((name) => {
    const rule = variant?.rows_when?.[name]
    return !rule || Object.entries(rule).every(([k, r]) => modelHolds(r, full[k]))
  })
  const made: { name: string; pattern: string }[] = []
  const later: string[] = []
  for (let i = 0; i < names.length;) {
    const at = names[i]!.indexOf('*')
    if (at < 0) {
      made.push({ name: names[i]!, pattern: names[i]! })
      i++
      continue
    }
    // the names with the same start go together, one number at a time: as the runner writes them
    const start = names[i]!.slice(0, at)
    const group = [names[i]!]
    while (i + group.length < names.length && names[i + group.length]!.includes('*') && names[i + group.length]!.slice(0, names[i + group.length]!.indexOf('*')) === start) {
      group.push(names[i + group.length]!)
    }
    i += group.length
    const labels = wildcardLabels(start, full, catalog)
    if (!labels) {
      later.push(...group)
      continue
    }
    for (const label of labels) {
      for (const pattern of group) made.push({ name: pattern.replace('*', label), pattern })
    }
  }
  const inputs = new Map((input ?? []).map((c) => [c.name, c.dtype]))
  const columns: ModelColumn[] = made.map(({ name, pattern }) => {
    let dtype = types[pattern] ?? 'Unknown'
    if (dtype.startsWith('@')) {
      const source = inputs.get(typeof full[dtype.slice(1)] === 'string' ? full[dtype.slice(1)] : '')
      dtype = source === undefined ? 'Unknown' : columnKind(source) === 'integer' ? 'Int32' : source
    } else if (dtype.startsWith('=')) {
      // the column keeps its own type, as the runner writes it (no Int32 rule): the experiments' variant
      const source = inputs.get(typeof full[dtype.slice(1)] === 'string' ? full[dtype.slice(1)] : '')
      dtype = source === undefined ? 'Unknown' : source
    }
    return { name, dtype }
  })
  if (output !== 'rows') return { columns, later }
  const language = catalog.groups.find((g) => g.id === model.group)?.language ?? ''
  const kept = catalog.rows_input_types?.[language] ?? {}
  return { columns: [...(input ?? []).map((c) => ({ name: c.name, dtype: kept[columnKind(c.dtype)] ?? c.dtype })), ...columns], later }
}

// the type a cast step gives a column (the engine's dtypes.py), as a preview names it
const CAST_TYPES: Record<string, string> = {
  int: 'Int64', int64: 'Int64', int32: 'Int32', float: 'Float64', float64: 'Float64', float32: 'Float32', str: 'String',
  string: 'String', utf8: 'String', bool: 'Boolean', boolean: 'Boolean', date: 'Date', datetime: "Datetime(time_unit='us', time_zone=None)",
}

/** The columns after a step that only keeps, removes, orders, renames, casts or filters them, known without its rows (the
 *  steps after a model that has not run); null after any other step, whose columns come from computing it. */
export function modelColumnsAfterStep(opType: string, params: Params | null | undefined, columns: readonly ModelColumn[]): ModelColumn[] | null {
  const p = params ?? {}
  const listed = (v: unknown): string[] => (Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string') : [])
  switch (opType) {
    case 'filter': case 'sort': case 'limit': case 'unique': case 'drop_nulls': case 'fill_null': case 'sample':
      return [...columns]
    case 'select': {
      const byName = new Map(columns.map((c) => [c.name, c]))
      return listed(p.columns).flatMap((n) => (byName.has(n) ? [byName.get(n)!] : []))
    }
    case 'drop': {
      const gone = new Set(listed(p.columns))
      return columns.filter((c) => !gone.has(c.name))
    }
    case 'reorder': {
      const first = listed(p.columns).filter((n) => columns.some((c) => c.name === n))
      return [...first.map((n) => columns.find((c) => c.name === n)!), ...columns.filter((c) => !first.includes(c.name))]
    }
    case 'rename': {
      const mapping = p.mapping && typeof p.mapping === 'object' ? (p.mapping as Record<string, unknown>) : {}
      return columns.map((c) => (typeof mapping[c.name] === 'string' && mapping[c.name] ? { ...c, name: mapping[c.name] as string } : c))
    }
    case 'cast': {
      const to = p.columns && typeof p.columns === 'object' ? (p.columns as Record<string, unknown>) : {}
      return columns.map((c) => {
        const t = typeof to[c.name] === 'string' ? CAST_TYPES[(to[c.name] as string).toLowerCase()] : undefined
        return t ? { ...c, dtype: t } : c
      })
    }
    default:
      return null
  }
}

/** Whether Predict is offered: a group the person may use, and an MLflow connection they may use. */
export function predictAvailable(state: ModelGroupsState | null | undefined, mlflowConnections: number, catalog: ModelCatalog = MODEL_CATALOG): boolean {
  return mlflowConnections > 0 && modelShownGroups(state, catalog).length > 0
}
