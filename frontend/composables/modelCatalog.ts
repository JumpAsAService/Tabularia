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
  | 'boolean' | 'numbers' | 'number_or_choice'

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
}

export interface ModelDef {
  id: string
  group: string
  family: string
  label: string
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
]

/** The node types of a flow that are models. */
export const MODEL_NODE_TYPE = 'model'
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

/** The variant the parameters make («unpenalized», «penalized», «manual», «auto», «default»). */
export function modelVariant(model: ModelDef, params: Params | null | undefined): string {
  const full = modelFull(model, params)
  const hit = model.variants.find((v) => Object.entries(v.when).every(([k, want]) => modelHolds(want, full[k])))
  return hit?.id ?? model.variants[0]?.id ?? ''
}

// ── reading what the form writes ─────────────────────────────────────────────

/** A number as a box writes it: «0,5» is a half. NaN when it is not one. */
export function modelNumber(value: unknown): number {
  if (typeof value === 'number') return value
  if (typeof value !== 'string' || value.trim() === '') return NaN
  return Number(value.trim().replace(',', '.'))
}

/** A list of numbers as a box writes it («10, 20 30») or as it is saved. */
export function modelNumbers(value: unknown): number[] {
  const parts = Array.isArray(value) ? value : typeof value === 'string' ? value.split(/[;,\s]+/) : []
  const out: number[] = []
  for (const part of parts) {
    const n = modelNumber(part)
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
    case 'number_or_choice': {
      if (typeof v === 'string' && f.choices.includes(v)) return v
      const n = modelNumber(v)
      return Number.isFinite(n) ? n : undefined
    }
    default:
      return undefined // reference and pairs: read with the columns they name
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

/** The parameters after one field changed. A level chosen among a column's values does not
 *  survive another column. */
export function modelSet(model: ModelDef, params: Params | null | undefined, key: string, value: unknown, catalog: ModelCatalog = MODEL_CATALOG): Params {
  const before = params ?? {}
  const next: Params = { ...before, [key]: value }
  if (JSON.stringify(before[key]) !== JSON.stringify(value)) {
    for (const f of modelFields(model, catalog)) if (f.control === 'level' && f.of === key) delete next[f.key]
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
export function modelNumberProblem(field: Pick<ModelField, 'control' | 'above' | 'below' | 'min' | 'max'>, value: unknown): '' | 'between' | 'above' | 'below' | 'range' | 'atLeast' | 'atMost' | 'whole' {
  if (isEmpty(value)) return ''
  const n = modelNumber(value)
  if (!Number.isFinite(n)) return ''
  if (field.control === 'integer' && !Number.isInteger(n)) return 'whole'
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

// ── what the node says ───────────────────────────────────────────────────────

const TEMPORAL = /^(Date|Datetime|Time|Duration)/i
const WHOLE = /^U?Int/i

/** Whether a column of a type (as a preview names it) can be what a kind of field asks for: a number
 *  for a numeric kind, a date, a datetime or an integer for a time column, anything but a date
 *  for a predictor (the model refuses a date there: compute a number from it first). */
export function modelColumnFits(kind: Pick<ModelKind, 'numeric' | 'types' | 'predictor'>, dtype: string): boolean {
  if (kind.numeric && !isNumericDtype(dtype)) return false
  if (kind.types && !(TEMPORAL.test(dtype) || WHOLE.test(dtype) || (kind.types.includes('number') && isNumericDtype(dtype)))) return false
  if (kind.predictor && TEMPORAL.test(dtype)) return false
  return true
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

/** What a model node does, in one line: «amount ~ price + channel», «time, event ~ arm», an
 *  ARIMA's orders. */
export function modelSummary(id: string, params: Params | null | undefined, catalog: ModelCatalog = MODEL_CATALOG): string {
  const model = modelOf(id, catalog)
  if (!model) return ''
  const p = modelParams(model, params, catalog)
  const list = (v: unknown) => (Array.isArray(v) ? v.join(' + ') : '')
  if (id === 'arima') return [modelArimaTitle(p, catalog), typeof p.value === 'string' ? p.value : ''].filter(Boolean).join(' · ')
  if (id === 'kaplan_meier') {
    const left = [p.time, p.event].filter((x) => typeof x === 'string').join(', ')
    return [left, p.strata].filter((x) => typeof x === 'string' && x).join(' ~ ')
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
}

/** What the route answers. `disabled_reason`: 'kubernetes' on an installation without it, 'not_configured'
 *  when the scripting machinery is not set up; null otherwise. */
export interface ModelGroupsState {
  disabled_reason: string | null
  groups: ModelGroupState[]
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
  if (state.disabled_reason === 'kubernetes') return catalog.groups
  if (state.disabled_reason) return []
  return catalog.groups.filter((g) => modelOff(g.id, state) === '')
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
  if (!model) return null
  if (preset !== undefined && !(preset in model.presets)) return null
  return preset ? { model: id, preset } : { model: id }
}

export const modelNameKey = (id: string): string => `models.${id}.name`
export const modelHintKey = (id: string): string => `models.${id}.hint`
export const modelPresetNameKey = (preset: string): string => `models.presets.${preset}.name`
export const modelPresetHintKey = (preset: string): string => `models.presets.${preset}.hint`
export const modelGroupKey = (group: string): string => `models.groups.${group}`

/** The palette's Models group: the shown groups in the catalogue's order, each with its models
 *  and, after ARIMA, the six presets of that same node. With a search, only the items whose
 *  name, hint or group holds it. */
export function modelPalette(shown: readonly ModelGroupDef[], query: string, text: Text, catalog: ModelCatalog = MODEL_CATALOG): ModelPaletteGroup[] {
  const q = query.trim().toLowerCase()
  const has = (s: string) => s.toLowerCase().includes(q)
  const out: ModelPaletteGroup[] = []
  for (const group of shown) {
    const groupHit = !!q && has(text(modelGroupKey(group.id)))
    const items: ModelPaletteItem[] = []
    for (const id of group.models) {
      const model = modelOf(id, catalog)
      if (!model) continue
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

/** What a model's Predict options are: the keys of `predict.takes` that apply to a model of the
 *  catalogue, with their kinds. */
export function predictFields(modelId: string | null | undefined, catalog: ModelCatalog = MODEL_CATALOG): ModelField[] {
  if (!modelId) return []
  const out: ModelField[] = []
  for (const [key, kind] of Object.entries(catalog.predict.takes)) {
    if (!catalog.predict.applies_to[key]?.includes(modelId)) continue
    const how = catalog.kinds[kind]
    if (how) out.push({ ...how, key, kind, required: false, choices: how.choices ?? [] })
  }
  return out
}

/** A Predict node's options as the gateway takes them: only the keys of the model the version holds,
 *  read as the form writes them, what is empty left out. */
export function predictParams(modelId: string | null | undefined, params: Params | null | undefined, catalog: ModelCatalog = MODEL_CATALOG): Params {
  const p = params ?? {}
  const out: Params = {}
  for (const f of predictFields(modelId, catalog)) {
    const v = p[f.key]
    if (isEmpty(v)) continue
    const one = readOne(f, v)
    // an option that is off is not given
    if (one === undefined || one === false) continue
    out[f.key] = one
  }
  return out
}

/** The columns a Predict step adds for a model with the options given (`survival_at_*` is one per time). */
export function predictColumns(modelId: string | null | undefined, params: Params | null | undefined, catalog: ModelCatalog = MODEL_CATALOG): string[] {
  const rows = modelId ? catalog.predict.columns[modelId] : null
  if (!rows) return []
  const given = predictParams(modelId, params, catalog)
  const out = [...(rows.always ?? [])]
  for (const [option, names] of Object.entries(rows)) {
    if (option === 'always' || given[option] === undefined) continue
    for (const name of names) {
      if (name.endsWith('*') && Array.isArray(given[option])) out.push(...given[option].map((t: number) => `${name.slice(0, -1)}${t}`))
      else if (!name.endsWith('*')) out.push(name)
    }
  }
  return out
}

/** Whether Predict is offered: a group the person may use, and an MLflow connection they may use. */
export function predictAvailable(state: ModelGroupsState | null | undefined, mlflowConnections: number, catalog: ModelCatalog = MODEL_CATALOG): boolean {
  return mlflowConnections > 0 && modelShownGroups(state, catalog).length > 0
}
