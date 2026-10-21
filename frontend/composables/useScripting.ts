// Scripting environments (Admin → Scripting environments): the bucket the Script
// nodes work in, and the Python and R environments their code runs in. Secrets
// (the bucket's secret key, the secret variables) are written and never read back.
import { computed, ref } from 'vue'

export type ScriptLanguage = 'python' | 'r'
export type VersionStatus = 'queued' | 'building' | 'checking' | 'ready' | 'failed'

export interface ScriptStorage {
  endpoint: string
  region: string
  bucket: string
  access_key: string
  secret_key_set: boolean
  updated_at: string | null
  updated_by: string | null
}

export interface ScriptStorageUpdate {
  endpoint?: string
  region?: string
  bucket?: string
  access_key?: string
  secret_key?: string // a text replaces it, "" removes it
}

export interface ScriptStorageTest {
  ok: boolean
  message: string
  can_list: boolean | null
}

export interface EnvironmentSummary {
  id: number
  name: string
  description: string
  language: ScriptLanguage
  enabled: boolean
  language_version: string | null
  current_number: number | null
  latest_number: number | null
  latest_status: VersionStatus | null
  variables: number
  groups: string[]
  updated_at: string
}

export interface EnvironmentVersion {
  id: number
  number: number
  language_version: string
  base_image: string
  packages: string
  status: VersionStatus
  image_ref: string | null
  lock: string | null
  build_log: string | null
  error: string | null
  created_at: string
  created_by: string | null
  started_at: string | null
  finished_at: string | null
  current: boolean
}

export interface EnvironmentVariable {
  name: string
  secret: boolean
  value: string | null // null for a secret one
  from_tabularia: boolean
  updated_at: string
  updated_by: string | null
}

export interface Environment extends EnvironmentSummary {
  packages: string
  cpu_request: string
  cpu_limit: string
  memory_request: string
  memory_limit: string
  max_timeout_seconds: number
  group_ids: number[]
  variable_list: EnvironmentVariable[]
  versions: EnvironmentVersion[]
  flows_using: string[]
}

export interface EnvironmentInput {
  name?: string
  description?: string
  language?: ScriptLanguage
  language_version?: string
  packages?: string
  enabled?: boolean
  cpu_request?: string
  cpu_limit?: string
  memory_request?: string
  memory_limit?: string
  max_timeout_seconds?: number
  group_ids?: number[]
}

export interface PackageHit {
  name: string
  version: string | null // R: the current one
}

export interface PackageVersion {
  version: string
  pre: boolean
  // Python: a wheel exists for the environment's Python; false = it compiles from source
  wheel: boolean | null
}

export interface ScriptingState {
  disabled_reason: 'kubernetes' | 'configuration' | null
  languages: Record<ScriptLanguage, string[]>
  registry: string
  storage: ScriptStorage
  environments: EnvironmentSummary[]
  editable: boolean
}

export function useScripting() {
  const { apiFetch } = useApiClient()
  const env = (id: number) => `/admin/scripting/environments/${id}`
  return {
    get: () => apiFetch<ScriptingState>('/admin/scripting'),
    saveStorage: (body: ScriptStorageUpdate) => apiFetch<ScriptStorage>('/admin/scripting/storage', { method: 'PUT', body }),
    testStorage: () => apiFetch<ScriptStorageTest>('/admin/scripting/storage/test', { method: 'POST' }),
    environment: (id: number) => apiFetch<Environment>(env(id)),
    create: (body: EnvironmentInput) => apiFetch<Environment>('/admin/scripting/environments', { method: 'POST', body }),
    update: (id: number, body: EnvironmentInput) => apiFetch<Environment>(env(id), { method: 'PUT', body }),
    remove: (id: number) => apiFetch<void>(env(id), { method: 'DELETE' }),
    rebuild: (id: number) => apiFetch<Environment>(`${env(id)}/rebuild`, { method: 'POST' }),
    makeCurrent: (id: number, versionId: number) =>
      apiFetch<Environment>(`${env(id)}/versions/${versionId}/current`, { method: 'POST' }),
    setVariable: (id: number, name: string, body: { value?: string; secret: boolean; from_tabularia: boolean }) =>
      apiFetch<Environment>(`${env(id)}/variables/${encodeURIComponent(name)}`, { method: 'PUT', body }),
    searchPackages: (language: ScriptLanguage, q: string) =>
      apiFetch<PackageHit[]>('/admin/scripting/packages', { query: { language, q } }),
    packageVersions: (language: ScriptLanguage, name: string, python?: string) =>
      apiFetch<{ name: string; versions: PackageVersion[] }>(
        `/admin/scripting/packages/${language}/${encodeURIComponent(name)}`, { query: python ? { python } : {} }),
    deleteVariable: (id: number, name: string) =>
      apiFetch<Environment>(`${env(id)}/variables/${encodeURIComponent(name)}`, { method: 'DELETE' }),
  }
}

// ── for the flow editor (any person who may write Script nodes) ─────────────
export interface EditorVariable {
  name: string
  secret: boolean
  value: string | null // null for a secret one
  from_tabularia: boolean
}

export interface EditorEnvironment {
  id: number
  name: string
  description: string
  language: ScriptLanguage
  language_version: string | null
  version: number | null
  ready: boolean
  max_timeout_seconds: number
  variables: EditorVariable[]
  packages: string[]
}

export interface EditorScripting {
  disabled_reason: 'kubernetes' | 'configuration' | null
  storage_ready: boolean
  environments: EditorEnvironment[]
  tabularia_variables: string[]
  lint: boolean // the code editor may ask Ruff (POST /scripting/lint)
}

// what Ruff found in one Python file; lines and columns from 1, the end excluded
export interface LintDiagnostic {
  line: number
  column: number
  end_line: number
  end_column: number
  code: string
  message: string
  severity: 'error' | 'warning'
}

// the latest run of each Script node of a flow, and the output the editor previews the
// nodes after it from (GET /scripting/flows/{id}/outputs)
export interface ScriptNodeRun {
  run_id: number
  status: string
  trial: boolean
  error: string | null
  started_at: string | null
  finished_at: string | null
  attempts: number | null
  exit_code: number | null
  log: string
  stale: boolean // the saved code is no longer the one that ran
}
export interface ScriptNodeOutput {
  run_id: number
  bucket: string
  key: string
  rows: number | null
  trial: boolean
  finished_at: string | null
  stale: boolean
}
export type ScriptOutputs = Record<string, { last: ScriptNodeRun | null; output: ScriptNodeOutput | null }>

// one copy for the whole editor (palette, node panel, code editor); loaded again when
// the editor page opens and when the code editor opens, so what administrators changed
// in the meantime (variables, a new build) shows up without reloading the browser
const editorState = ref<EditorScripting | null>(null)
let editorLoading: Promise<EditorScripting | null> | null = null
const completionsCache = new Map<string, Promise<Record<string, string[]>>>() // by environment and build

export function useScriptingEditor() {
  const { apiFetch } = useApiClient()
  function load(force = false): Promise<EditorScripting | null> {
    if (editorLoading && !force) return editorLoading
    editorLoading = apiFetch<EditorScripting>('/scripting')
      .then((s) => (editorState.value = s))
      .catch(() => (editorState.value = null))
    return editorLoading
  }
  function completions(environmentId: number, version: number | null): Promise<Record<string, string[]>> {
    const key = `${environmentId}:${version}`
    if (!completionsCache.has(key)) {
      completionsCache.set(key,
        apiFetch<Record<string, string[]>>(`/scripting/environments/${environmentId}/completions`).catch(() => ({})))
    }
    return completionsCache.get(key)!
  }
  // the editor's «Run» of a Script node of the saved flow; follow the run with GET /runs/{id}
  const runNode = (flowId: number, nodeId: string) =>
    apiFetch<{ run_id: number }>(`/scripting/flows/${flowId}/nodes/${encodeURIComponent(nodeId)}/run`, { method: 'POST' })
  const outputs = (flowId: number) => apiFetch<ScriptOutputs>(`/scripting/flows/${flowId}/outputs`)
  // files: the paths of the node's files, the modules its code may import
  const lint = (environmentId: number, path: string, content: string, files: string[]) =>
    apiFetch<{ diagnostics: LintDiagnostic[]; skipped: boolean }>('/scripting/lint', {
      method: 'POST', body: { environment_id: environmentId, path, content, files },
    })
  // the palette offers the Script node when there is an environment to write it for
  const available = computed(() => !!editorState.value && !editorState.value.disabled_reason
    && editorState.value.environments.length > 0)
  return { state: editorState, load, completions, available, runNode, outputs, lint }
}
