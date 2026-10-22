// Backups (Administration → Backups): the automatic backup of Tabularia's database into
// the object storage, its schedule, and the backups there (gateway routes/backups.py).

export interface BackupItem {
  name: string
  size: number
  modified: string | null
}

export interface BackupState {
  available: boolean
  disabled_reason: 'kubernetes' | 'postgres' | 'pg_dump' | null
  enabled: boolean
  schedule: string
  keep: number
  timezone: string
  defaults: { enabled: boolean; schedule: string; keep: number }
  changed: { enabled: boolean; schedule: boolean; keep: boolean }
  next_at: string | null
  running_since: string | null
  requested_at: string | null
  last_ok_at: string | null
  last_name: string | null
  last_size: number | null
  last_seconds: number | null
  last_error: string | null // not to the observer
  last_error_at: string | null
  bucket: string | null
  prefix: string | null
  same_as_data: boolean | null
  items: BackupItem[]
  storage_error: string | null
  editable: boolean
}

export interface BackupUpdate {
  enabled?: boolean
  schedule?: string
  keep?: number
  reset?: ('enabled' | 'schedule' | 'keep')[] // back to the installation's value
}

export function useBackups() {
  const { apiFetch } = useApiClient()
  const base = useRuntimeConfig().public.apiBase as string
  return {
    get: () => apiFetch<BackupState>('/admin/backups'),
    save: (body: BackupUpdate) => apiFetch<BackupState>('/admin/backups', { method: 'PUT', body }),
    runNow: () => apiFetch<BackupState>('/admin/backups/run', { method: 'POST' }),
    // the browser saves the file as it arrives (the session cookie signs the request)
    downloadUrl: (name: string) => `${base}/admin/backups/${encodeURIComponent(name)}/download`,
  }
}
