// SIEM (Administration → SIEM): the audit log forwarded to Splunk with the HTTP Event
// Collector. The token is written and not read back.

export interface SiemState {
  kind: string
  enabled: boolean
  url: string // to the observer only scheme and host
  token_set: boolean
  index: string | null
  source: string | null
  sourcetype: string | null
  host: string | null
  verify_tls: boolean
  editable: boolean
  sent_total: number
  backlog: number
  oldest_pending_at: string | null
  last_ok_at: string | null
  last_error: string | null
  last_error_at: string | null
  failures: number
  next_attempt_at: string | null
}

export interface SiemUpdate {
  enabled?: boolean
  url?: string
  token?: string // a text replaces it, "" removes it
  index?: string
  source?: string
  sourcetype?: string
  host?: string
  verify_tls?: boolean
}

export interface SiemTest {
  ok: boolean
  status_code: number | null
  message: string
}

export function useSiem() {
  const { apiFetch } = useApiClient()
  return {
    get: () => apiFetch<SiemState>('/admin/siem'),
    save: (body: SiemUpdate) => apiFetch<SiemState>('/admin/siem', { method: 'PUT', body }),
    test: () => apiFetch<SiemTest>('/admin/siem/test', { method: 'POST' }),
  }
}
