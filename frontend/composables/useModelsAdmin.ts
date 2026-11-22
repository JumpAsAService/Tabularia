// The statistical models' groups in Administration (Admin → Statistical models): enable and
// disable a group, build it again, its resources and limits, and who may use it. The image of a
// group is built by the cluster from the recipe Tabularia ships and proved by the group's
// self-test before it is used (docs/specs/statistical-models.md, «Model groups and their images»).
// Routes: gateway-go/internal/api/models_admin.go.

// rechecking, recheck_retrying: the version in use serves while its self-test runs again with a new
// runner (a recheck the cluster stopped before it reported is tried again a few times); licence_pending:
// a build is due and the licence of the group's weights has no acceptance in the audit, so none is queued
export type ModelGroupState = 'not_enabled' | 'building' | 'checking' | 'ready' | 'failed' | 'rebuild_needed' | 'rechecking' | 'recheck_retrying' | 'licence_pending'
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

/** The acceptance a version was built under, read from the version's own lines (it outlives the audit's retention). */
export interface ModelGroupLicenceLine {
  version: number
  licence_sha256: string
  accepted_by: string
  accepted_at: string
  audit: number
  sizes: string[]
  current: boolean // the licence of the line is the text this Tabularia ships
}

/** The licence of a group's weights (a group with weights only: docs/specs/foundation-forecast.md). */
export interface ModelGroupLicence {
  name: string
  sha256: string
  url: string // the text at the pinned revision, on the repository's host
  text: string // the text shipped with Tabularia, the bytes the sha256 names
  sizes: { id: string; bytes: number }[]
  pending: boolean // no valid acceptance in the audit
  line: ModelGroupLicenceLine | null
  audit_expires_at: string | null // when the audit's retention removes the acceptance (null: it keeps it)
  sizes_in_use?: string[] // the sizes the version in use was built with (empty: no version in use)
}

/** What Enable and Build again send for a group with weights: the licence read (its sha256), the acceptance, the sizes. */
export interface ModelLicenceInput {
  licence_sha256: string
  accept: boolean
  sizes: string[]
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
  licence?: ModelGroupLicence | null
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

/** The box's draft: the tick, the sizes asked for, whether a build again is being asked. */
export interface LicenceDraft {
  accept: boolean
  sizes: string[]
  open: boolean
}

/**
 * The box unticked, and the sizes of the version in use ticked (Build again: a click downloads nothing more than what
 * is already there unless the person ticks it); a group with no version in use ticks every size.
 */
export function licenceDraftOf(l: Pick<ModelGroupLicence, 'sizes' | 'sizes_in_use'>): LicenceDraft {
  const all = l.sizes.map((s) => s.id)
  const inUse = all.filter((id) => (l.sizes_in_use ?? []).includes(id))
  return { accept: false, sizes: inUse.length ? inUse : all, open: false }
}

/** The body of an Enable or Build again: the sizes in the recipe's order, the licence read by its sha256. */
export function licenceInputOf(l: Pick<ModelGroupLicence, 'sizes' | 'sha256'>, d: LicenceDraft): ModelLicenceInput {
  return { licence_sha256: l.sha256, accept: d.accept, sizes: l.sizes.map((s) => s.id).filter((id) => d.sizes.includes(id)) }
}

/** The box lets the person go on only with the tick and at least one size. */
export const licenceReady = (d: LicenceDraft): boolean => d.accept && d.sizes.length > 0

/** «28M · 111 MB»: a size of the weights and what it weighs (decimal megabytes, as the Hub lists them). */
export function licenceSizeLabel(s: { id: string; bytes: number }): string {
  return `${s.id.toUpperCase()} · ${Math.round(s.bytes / 1e6)} MB`
}

/** The sizes as a list for a sentence: «28M, 123M». */
export const licenceSizesText = (sizes: string[]): string => sizes.map((s) => s.toUpperCase()).join(', ')

/**
 * Point 2 of the licence, verbatim: the clause is the text's own, found in it, never retyped and never explained
 * (the official file is the authority for its meaning). null when the text has no point 2.
 */
export function licenceClause(text: string): string | null {
  const line = text.split('\n').map((l) => l.trim()).find((l) => /^2\.\s/.test(l))
  return line ? line.replace(/^2\.\s+/, '') : null
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
    // a group with weights: the licence's acceptance and the sizes (the gateway refuses the click without them)
    enable: (id: string, body?: ModelLicenceInput) => apiFetch<ModelGroupAnswer>(`${one(id)}/enable`, { method: 'POST', ...(body ? { body } : {}) }),
    disable: (id: string) => apiFetch<ModelGroupAnswer>(`${one(id)}/disable`, { method: 'POST' }),
    rebuild: (id: string, body?: ModelLicenceInput) => apiFetch<ModelGroupAnswer>(`${one(id)}/rebuild`, { method: 'POST', ...(body ? { body } : {}) }),
    saveLimits: (id: string, body: ModelLimitsInput) => apiFetch<ModelGroupAnswer>(`${one(id)}/settings`, { method: 'PUT', body }),
    saveAccess: (id: string, body: ModelAccessInput) => apiFetch<ModelGroupAnswer>(`${one(id)}/access`, { method: 'PUT', body }),
  }
}
