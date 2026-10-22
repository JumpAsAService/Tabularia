// Saved scripts: Python or R code kept in a folder, with its versions
// (docs/specs/saved-scripts.md). The gateway shows a script only to whoever has VIEW
// on its folder AND may use its environment: for anyone else it does not exist.

export interface ScriptFile {
  path: string
  content: string
}

/** a datasource read by the script, by id (a name is unique only in its folder) */
export interface ScriptInput {
  datasource_id: number
  alias: string
}

export interface ScriptOutputSettings {
  publish: boolean
  name: string
  project_id: number | null
  overwrite: boolean
}

export interface ScriptContent {
  files: ScriptFile[]
  entrypoint: string
  inputs: ScriptInput[]
  output: ScriptOutputSettings
  timeout_seconds: number
  retries: number
  retry_delay_seconds: number
}

export interface SavedScript {
  id: number
  name: string
  description: string
  project_id: number
  environment_id: number
  environment_name: string | null
  language: 'python' | 'r' | null
  current_version: number
  run_schedule: string | null
  run_scheduled_by: number | null
  next_run_at: string | null
  created_at: string
  updated_at: string
  /** EDIT on its folder */
  editable: boolean
  /** RUN on its folder */
  runnable: boolean
  /** its latest run of its own (on its page, scheduled, in a group) */
  last_run_status: string | null
  last_run_at: string | null
}

/** A declared input as this reader may know it. */
export interface ScriptInputInfo {
  datasource_id: number
  alias: string
  name: string | null
  folder: string | null
  project_id: number | null
  columns: string[]
  /** missing: deleted; hidden: not readable by this reader; empty: no data yet */
  state: 'ok' | 'missing' | 'hidden' | 'empty'
}

export interface SavedScriptDetail extends SavedScript {
  content: ScriptContent
  inputs: ScriptInputInfo[]
}

/** A run of the script: its orchestration, with what the script run says. */
export interface ScriptRun {
  run_id: number
  status: 'PENDING' | 'STARTED' | 'SUCCESS' | 'FAILURE'
  trigger_type: string
  started_at: string | null
  finished_at: string | null
  error: string | null
  launched_by: string | null
  script_run_id: number | null
  script_version: number | null
  attempts: number | null
  exit_code: number | null
  log: string
  /** emptied after ORCHESTRATOR__LOG_RETENTION_DAYS */
  log_expired: boolean
  rows: number | null
  output: boolean
  columns: { name: string; dtype: string }[]
  datasource_id: number | null
}

export interface OutputPreview {
  columns: { name: string; dtype?: string }[]
  rows: Record<string, unknown>[]
}

export interface ScriptVersion {
  version: number
  /** a code the interface translates: created, edited, restored from vN */
  note: string
  created_at: string
  created_by: string | null
  current: boolean
}

export interface ScriptUpdate {
  name?: string
  description?: string
  project_id?: number
  environment_id?: number
  content?: Partial<ScriptContent>
}

export function useSavedScripts() {
  const { apiFetch } = useApiClient()
  return {
    list: () => apiFetch<SavedScript[]>('/scripts'),
    listByProject: (projectId: number) => apiFetch<SavedScript[]>(`/projects/${projectId}/scripts`),
    get: (id: number) => apiFetch<SavedScriptDetail>(`/scripts/${id}`),
    create: (projectId: number, body: { name: string; description?: string; environment_id: number; content?: Partial<ScriptContent> }) =>
      apiFetch<SavedScriptDetail>(`/projects/${projectId}/scripts`, { method: 'POST', body }),
    update: (id: number, body: ScriptUpdate) => apiFetch<SavedScriptDetail>(`/scripts/${id}`, { method: 'PATCH', body }),
    duplicate: (id: number) => apiFetch<SavedScriptDetail>(`/scripts/${id}/duplicate`, { method: 'POST' }),
    remove: (id: number) => apiFetch<void>(`/scripts/${id}`, { method: 'DELETE' }),
    versions: (id: number) => apiFetch<ScriptVersion[]>(`/scripts/${id}/versions`),
    version: (id: number, version: number) => apiFetch<ScriptContent>(`/scripts/${id}/versions/${version}`),
    restore: (id: number, version: number) =>
      apiFetch<SavedScriptDetail>(`/scripts/${id}/versions/${version}/restore`, { method: 'POST' }),
    run: (id: number) => apiFetch<{ run_id: number }>(`/scripts/${id}/run`, { method: 'POST' }),
    setSchedule: (id: number, cron: string) =>
      apiFetch<SavedScriptDetail>(`/scripts/${id}/schedule`, { method: 'PUT', body: { cron } }),
    runs: (id: number) => apiFetch<ScriptRun[]>(`/scripts/${id}/runs`),
    preview: (id: number, runId: number) => apiFetch<OutputPreview>(`/scripts/${id}/runs/${runId}/preview?limit=100`),
  }
}

/** The alias proposed for a datasource, as the gateway proposes it
 * (services/saved_scripts.propose_alias): `Vendite Nord 2024` → `VENDITE_NORD_2024`,
 * the folder's name added when taken (homonyms of two folders), then a number. */
export function proposeAlias(name: string, folder: string, taken: Set<string>): string {
  const clean = (text: string) => text.normalize('NFKD').replace(/[^\x00-\x7F]/g, '')
    .replace(/[^A-Za-z0-9]+/g, '_').replace(/^_+|_+$/g, '').toUpperCase()
  let base = clean(name) || 'INPUT'
  if (!/^[A-Z]/.test(base)) base = `DS_${base}`
  base = base.slice(0, 50)
  const candidates = [base]
  if (folder && clean(folder)) candidates.push(`${base}_${clean(folder)}`.slice(0, 60))
  for (const c of candidates) if (!taken.has(c)) return c
  const last = candidates[candidates.length - 1]!
  let n = 2
  while (taken.has(`${last}_${n}`.slice(0, 63))) n++
  return `${last}_${n}`.slice(0, 63)
}

export const ALIAS_RE = /^[A-Z][A-Z0-9_]{0,62}$/

/** The variables Tabularia gives a saved script (the editor lists them). */
export function savedScriptVariables(aliases: string[]): string[] {
  return [
    ...aliases.map((a) => `TABULARIA_INPUT_${a}`), 'TABULARIA_INPUTS', 'TABULARIA_OUTPUT', 'TABULARIA_RUN_ID',
    'TABULARIA_STEP_ID', 'TABULARIA_SCRIPT_ID', 'TABULARIA_SCRIPT_NAME', 'TABULARIA_SCRIPT_VERSION', 'TABULARIA_ATTEMPT',
  ]
}

/** The translated label of a version's note (created, edited, restored from v2). */
export function versionNote(note: string, t: (key: string, params?: Record<string, unknown>) => string): string {
  const restored = /^restored from v(\d+)$/.exec(note)
  if (restored) return t('scripts.versionRestored', { n: restored[1] })
  if (note === 'created') return t('scripts.versionCreated')
  if (note === 'edited') return t('scripts.versionEdited')
  return note
}
