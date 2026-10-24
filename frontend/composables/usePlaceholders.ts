// The placeholder language shared by every input and output ({today}, {now-2h},
// {today:yyyyMMdd}, {day} of a range…): the variables' text syntax, the moving part
// of a file datasource, and the live preview that asks the server what each template
// gives NOW. The grammar lives on the engine (app.core.placeholders): nothing here
// resolves a placeholder, so the preview can never disagree with a refresh or a run.
import { onBeforeUnmount, reactive, watch } from 'vue'

// A variable of a datasource: a LIST (one expansion per value) or a RANGE — the
// values from `start` (included) to `end` (excluded) by `step`, with {name_end}
// the end of each window. start/end are expressions without braces.
export interface TemplateVariable {
  name: string
  type: 'list' | 'range'
  values: string[]
  start: string
  end: string
  step: string
}

// the moving part of a file datasource (SFTP, SharePoint, S3); the API call has
// the same fields with the same names
export interface FileTemplatesDraft {
  timezone: string
  variables: TemplateVariable[]
  last_refresh_initial: string
  mode: 'snapshot' | 'append'
  keys: string[]
}

export function emptyFileTemplates(): FileTemplatesDraft {
  return { timezone: 'UTC', variables: [], last_refresh_initial: '', mode: 'snapshot', keys: [] }
}

export const DEFAULT_STEP = '1d'

// `name = today-7 .. today step 1d` (step optional) — the rest is a list
const RANGE = /^(.*?)\s*\.\.\s*(.*?)(?:\s+step\s+(\S+))?\s*$/i

/** One variable per line: `symbol = AAPL, MSFT` or `day = today-7 .. today step 1d`. */
export function parseVariables(text: string): TemplateVariable[] {
  const out: TemplateVariable[] = []
  for (const raw of text.split('\n')) {
    const line = raw.trim()
    if (!line) continue
    const i = line.indexOf('=')
    const name = (i < 0 ? line : line.slice(0, i)).trim()
    const rest = i < 0 ? '' : line.slice(i + 1).trim()
    const m = rest.includes('..') ? RANGE.exec(rest) : null
    if (m) {
      out.push({ name, type: 'range', values: [], start: m[1].trim(), end: m[2].trim(), step: (m[3] ?? '').trim() || DEFAULT_STEP })
    } else {
      out.push({ name, type: 'list', values: rest.split(',').map((x) => x.trim()).filter(Boolean), start: '', end: '', step: '' })
    }
  }
  return out
}

export function variablesToText(vars: Partial<TemplateVariable>[]): string {
  return vars
    .map((v) =>
      v.type === 'range'
        ? `${v.name} = ${v.start ?? ''} .. ${v.end ?? ''}${v.step && v.step !== DEFAULT_STEP ? ` step ${v.step}` : ''}`
        : `${v.name} = ${(v.values ?? []).join(', ')}`,
    )
    .join('\n')
}

/** What the server wants: named variables only, each in the shape of its type. */
export function cleanVariables(vars: TemplateVariable[]): TemplateVariable[] {
  return vars
    .filter((v) => v.name)
    .map((v) =>
      v.type === 'range'
        ? { name: v.name, type: 'range' as const, values: [], start: v.start.trim(), end: v.end.trim(), step: v.step.trim() || DEFAULT_STEP }
        : { name: v.name, type: 'list' as const, values: v.values, start: '', end: '', step: '' },
    )
}

export function splitKeys(text: string): string[] {
  return text.split(/[,;\s]+/).map((k) => k.trim()).filter(Boolean)
}

/** The fields a file datasource sends (creation and «Check the files»). */
export function cleanFileTemplates(d: FileTemplatesDraft): FileTemplatesDraft {
  return {
    timezone: d.timezone.trim() || 'UTC',
    variables: cleanVariables(d.variables),
    last_refresh_initial: d.last_refresh_initial.trim(),
    mode: d.mode,
    keys: d.mode === 'append' ? d.keys : [],
  }
}

// a value of the variables whose file was not there (the expanded pattern)
export interface MissingValue { params: Record<string, string>; pattern: string }

// what «Check the files» answers: the files found, and with placeholders or
// variables the values without a file
export interface FilesCheck {
  files: { path: string; size: number; modified_at: string }[]
  total: number
  missing?: MissingValue[]
  missing_total?: number
  expansions?: number
  // the sheets of the first workbook taken: the sheet is chosen from them
  sheets?: { file: string; sheets: string[]; hidden: string[] } | null
}

// the notes of a successful refresh (RunInfo.notes, JSON): the values skipped
// and, for an append by key, how many keys came on more than one new row
export interface RunNotes { missing: MissingValue[]; missing_count: number; expansions: number | null; duplicate_keys: number }

export function runNotes(notes: string | null | undefined): RunNotes | null {
  if (!notes) return null
  try {
    const n = JSON.parse(notes)
    const missing = Array.isArray(n?.missing) ? n.missing : []
    const count = Number(n?.missing_count) || missing.length
    const repeated = Number(n?.duplicate_keys) || 0
    return count || repeated ? { missing, missing_count: count, expansions: n?.expansions ?? null, duplicate_keys: repeated } : null
  } catch {
    return null
  }
}

// the notes in a few words: «2 value(s) without a file · 3 key(s) on several new rows»
export function notesText(n: RunNotes, t: (key: string, values?: Record<string, unknown>) => string): string {
  return [n.missing_count ? t('placeholders.missingCount', { n: n.missing_count }) : '',
    n.duplicate_keys ? t('placeholders.repeatedKeys', { n: n.duplicate_keys }) : ''].filter(Boolean).join(' · ')
}

export const isTemplated = (s: string | null | undefined) => /[{}]/.test(s ?? '')

// the cheat sheets (code literals, never in the messages: vue-i18n reads braces)
export const INPUT_PLACEHOLDERS = [
  '{today}', '{yesterday}', '{now}', '{today-7d}', '{today-1m}', '{now-2h}', '{start_of_month}', '{start_of_week}',
  '{start_of_year}', '{last_refresh}', '{today:yyyyMMdd}', '{today:%Y-%m-%d}', '{now:HH:mm}', '{now:epoch}',
  '{day}', '{day_end}', '{{ }}',
]
export const OUTPUT_PLACEHOLDERS = [
  '{today}', '{yesterday}', '{now}', '{today-1d}', '{start_of_month}', '{today:yyyyMMdd}', '{now:HH:mm}',
  '{date}', '{datetime}', '{run_id}', '{flow}', '{{ }}',
]
export const RANGE_EXAMPLE = 'day = today-7 .. today step 1d'

// ── live preview ────────────────────────────────────────────────────────────
export type TemplateTarget = 'file' | 'path' | 'text'

export interface PreviewRequest {
  templates: Record<string, string>
  kind: 'input' | 'output'
  timezone: string
  variables?: TemplateVariable[]
  last_refresh_initial?: string
  targets?: Record<string, TemplateTarget>
  flow?: string
}

export interface FieldPreview { value: string | null; last: string | null; error: string | null }
export interface PreviewAnswer {
  fields: Record<string, FieldPreview>
  expansions: number
  error: string | null
  now: string
}

export interface PlaceholderPreview {
  kind: 'input' | 'output'
  templates: Record<string, string> // what the form holds now
  answer: PreviewAnswer | null // for the templates last asked about
  error: string // not tied to one template: the zone, the variables, the request
  loading: boolean
}

// a 422 of the gateway's validation is a list; the engine's message is a string
function detailOf(e: any): string {
  const d = e?.data?.detail
  if (Array.isArray(d)) return d.map((x) => String(x?.msg ?? '').replace(/^Value error, /, '')).filter(Boolean).join('; ')
  return typeof d === 'string' ? d : e?.data?.message ?? e?.message ?? 'error'
}

/**
 * The value of each template NOW, asked to the server while the user types:
 * debounced, the stale answers dropped (sequence counter), no call at all when no
 * template has a brace. `source` gives the request (null = nothing to preview).
 */
export function usePlaceholderPreview(source: () => PreviewRequest | null, delayMs = 300): PlaceholderPreview {
  const { apiFetch } = useApiClient()
  const state = reactive<PlaceholderPreview>({ kind: 'input', templates: {}, answer: null, error: '', loading: false })
  let seq = 0
  let timer: ReturnType<typeof setTimeout> | null = null

  async function ask(req: PreviewRequest, mine: number) {
    try {
      const answer = await apiFetch<PreviewAnswer>('/placeholders/preview', { method: 'POST', body: req })
      if (mine !== seq) return
      state.answer = answer
      state.error = answer.error ?? ''
    } catch (e) {
      if (mine !== seq) return
      state.answer = null
      state.error = detailOf(e)
    } finally {
      if (mine === seq) state.loading = false
    }
  }

  watch(
    () => JSON.stringify(source()),
    () => {
      const req = source()
      const mine = ++seq
      if (timer) clearTimeout(timer)
      state.kind = req?.kind ?? 'input'
      state.templates = req?.templates ?? {}
      const templated = Object.fromEntries(Object.entries(state.templates).filter(([, t]) => isTemplated(t)))
      // a zone or variables can be wrong with no template using them: check those too
      const dynamic = !!req && (Object.keys(templated).length > 0 || !!req.variables?.length
        || (!!req.timezone.trim() && req.timezone.trim() !== 'UTC'))
      if (!req || !dynamic) {
        state.answer = null
        state.error = ''
        state.loading = false
        return
      }
      state.loading = true
      const body: PreviewRequest = { ...req, templates: templated, timezone: req.timezone.trim() || 'UTC' }
      if (body.variables) body.variables = cleanVariables(body.variables)
      if (body.targets) body.targets = Object.fromEntries(Object.entries(body.targets).filter(([k]) => k in templated))
      timer = setTimeout(() => ask(body, mine), delayMs)
    },
    { immediate: true },
  )
  onBeforeUnmount(() => {
    seq++
    if (timer) clearTimeout(timer)
  })
  return state
}
