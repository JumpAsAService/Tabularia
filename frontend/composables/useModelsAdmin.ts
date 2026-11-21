// The statistical models' groups in Administration (Admin → Statistical models): enable and
// disable a group, build it again, its resources and limits, and who may use it. The image of a
// group is built by the cluster from the recipe Tabularia ships and proved by the group's
// self-test before it is used (docs/specs/statistical-models.md, «Model groups and their images»).
// Routes: gateway-go/internal/api/models_admin.go.

// rechecking, recheck_retrying: the version in use serves while its self-test runs again with a new
// runner (a recheck the cluster stopped before it reported is tried again a few times)
export type ModelGroupState = 'not_enabled' | 'building' | 'checking' | 'ready' | 'failed' | 'rebuild_needed' | 'rechecking' | 'recheck_retrying'
export type ModelVersionStatus = 'queued' | 'building' | 'checking' | 'ready' | 'failed'
// where the self-test with a new runner is, on the version in use: running, to be tried again,
// stopped (none could run: it serves, self-tested with its previous runner), or none
export type ModelRecheck = 'rechecking' | 'recheck_retrying' | 'recheck_stopped' | ''

export interface ModelGroupVersion {
  number: number
  status: ModelVersionStatus
  current: boolean
  stale: boolean // built from another recipe than the one this Tabularia ships
  recheck?: ModelRecheck
  recheck_attempts?: number // the rechecks that could not run
  recheck_max_attempts?: number // how many are made in all
  recheck_after?: string | null // the next one, not before
  recheck_error?: string | null // why the last one could not run
  error: string | null
  image_ref: string | null
  packages: number // installed, from the lock
  created_at: string
  created_by: string | null
  started_at: string | null
  finished_at: string | null
  // only in the detail of a group
  lock?: string | null
  build_log?: string | null
  selftest?: string | null
}

export interface ModelGroupLimits {
  cpu_limit: string
  memory_limit: string
  max_timeout_seconds: number
  max_rows: number
}

export interface ModelGroupSystemLibrary {
  package: string
  name: string
  // the library's own version («2.7.1»), and the package's as apt installed it («2.7.1+dfsg-6ubuntu2»)
  version?: string
  package_version?: string
}

/** A system library as the card says it: its name and, when known, its own version («GSL 2.7.1»). */
export function systemLibraryLabel(lib: ModelGroupSystemLibrary): string {
  return lib.version ? `${lib.name} ${lib.version}` : lib.name
}

export interface ModelGroupAdmin {
  id: string
  language: 'r' | 'python'
  base: string
  models: string[]
  packages: number
  // the system libraries the group's image installs besides the fixed list (empty for most groups)
  system_libraries: ModelGroupSystemLibrary[]
  enabled: boolean
  state: ModelGroupState
  ready: boolean
  current_version: number | null
  limits: ModelGroupLimits
  defaults: ModelGroupLimits
  everyone: boolean
  group_ids: number[]
  groups: string[]
  enabled_by: string | null
  enabled_at: string | null
  versions: ModelGroupVersion[]
}

export interface ModelsAdminState {
  disabled_reason: 'kubernetes' | 'not_configured' | null
  storage_ready: boolean
  editable: boolean
  groups: ModelGroupAdmin[]
}

export interface ModelGroupAnswer {
  disabled_reason: 'kubernetes' | 'not_configured' | null
  storage_ready: boolean
  editable: boolean
  group: ModelGroupAdmin
}

export interface ModelLimitsInput {
  cpu_limit: string
  memory_limit: string
  max_timeout_seconds: number
  max_rows: number | null // null: the recipe's
}

export interface ModelAccessInput {
  everyone: boolean
  group_ids: number[]
}

// a group is being built or checked: the page follows it
export function groupBusy(g: Pick<ModelGroupAdmin, 'state'>): boolean {
  return g.state === 'building' || g.state === 'checking'
}

// the page follows a group being built or checked, and one whose version in use is self-tested
// again (it serves meanwhile, so it is not busy: it can be built again)
export function groupFollowed(g: Pick<ModelGroupAdmin, 'state'>): boolean {
  return groupBusy(g) || g.state === 'rechecking' || g.state === 'recheck_retrying'
}

// the version in use whose self-test with a new runner is running, to be tried again or stopped
export function recheckOf(g: Pick<ModelGroupAdmin, 'versions'>): ModelGroupVersion | null {
  return g.versions.find((v) => v.current && !!v.recheck) ?? null
}

// the limits as the form keeps them: the longest run in minutes, and a max_rows that may be left empty
export interface LimitsDraft {
  cpu_limit: string
  memory_limit: string
  minutes: number
  max_rows: string
}

export function limitsDraftOf(g: Pick<ModelGroupAdmin, 'limits' | 'defaults'>): LimitsDraft {
  return {
    cpu_limit: g.limits.cpu_limit,
    memory_limit: g.limits.memory_limit,
    minutes: Math.round(g.limits.max_timeout_seconds / 60),
    // what is the recipe's stays empty: it follows the recipe when Tabularia changes it
    max_rows: g.limits.max_rows === g.defaults.max_rows ? '' : String(g.limits.max_rows),
  }
}

export function limitsInputOf(d: LimitsDraft): ModelLimitsInput {
  const rows = d.max_rows.trim()
  return {
    cpu_limit: d.cpu_limit.trim(),
    memory_limit: d.memory_limit.trim(),
    max_timeout_seconds: Math.round(d.minutes * 60),
    max_rows: rows === '' ? null : Number(rows),
  }
}

// the first paragraph of a failure: the sentence the card shows; the rest is in the logs below it
export function failureSentence(error: string | null | undefined): string {
  if (!error) return ''
  return error.split('\n')[0].trim()
}

export function useModelsAdmin() {
  const { apiFetch } = useApiClient()
  const one = (id: string) => `/admin/models/${encodeURIComponent(id)}`
  return {
    get: () => apiFetch<ModelsAdminState>('/admin/models'),
    group: (id: string) => apiFetch<ModelGroupAnswer>(one(id)),
    enable: (id: string) => apiFetch<ModelGroupAnswer>(`${one(id)}/enable`, { method: 'POST' }),
    disable: (id: string) => apiFetch<ModelGroupAnswer>(`${one(id)}/disable`, { method: 'POST' }),
    rebuild: (id: string) => apiFetch<ModelGroupAnswer>(`${one(id)}/rebuild`, { method: 'POST' }),
    saveLimits: (id: string, body: ModelLimitsInput) => apiFetch<ModelGroupAnswer>(`${one(id)}/settings`, { method: 'PUT', body }),
    saveAccess: (id: string, body: ModelAccessInput) => apiFetch<ModelGroupAnswer>(`${one(id)}/access`, { method: 'PUT', body }),
  }
}
