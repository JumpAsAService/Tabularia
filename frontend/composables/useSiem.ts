// SIEM (Amministrazione → SIEM): il registro di audit inoltrato a Splunk con
// l'HTTP Event Collector. Il token si scrive e non si rilegge.

export interface SiemState {
  kind: string
  enabled: boolean
  url: string // all'osservatore solo schema e host
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
  token?: string // un testo lo sostituisce, "" lo toglie
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
