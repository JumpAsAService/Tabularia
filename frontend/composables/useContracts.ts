// Data contracts: what whoever produces a datasource promises to whoever consumes it.
// Here the API calls and the vocabulary shared by icon and dialog.
//
// The severities are TWO, per rule: `warning` (not blocking: the data is published and
// the violation is flagged) and `error` (blocking: the new data is not published, the
// datasource stays on the last good snapshot).

export type ContractStatus = 'pending' | 'passed' | 'warning' | 'failed'
export type Severity = 'warning' | 'error'
export type RuleKind =
  | 'column' | 'not_null' | 'unique' | 'accepted_values' | 'range'
  | 'pattern' | 'row_count' | 'freshness' | 'expression'
export type Dtype = 'integer' | 'number' | 'string' | 'boolean' | 'date' | 'datetime'

/** A datasource's contract state, as it arrives in the listings. */
export interface ContractSummary {
  status: ContractStatus
  checked_at: string | null
  errors: number
  warnings: number
  blocked: boolean // the last update was refused
  version: number
}

export interface ContractRule {
  id?: string
  kind: RuleKind
  severity: Severity
  column?: string
  columns?: string[]
  dtype?: Dtype
  values?: (string | number | boolean)[]
  min?: number | string | null
  max?: number | string | null
  regex?: string
  max_age_hours?: number
  sql?: string
  name?: string
}

export interface ContractDocument {
  description?: string
  rules: ContractRule[]
}

/** A rule's outcome in the last report. */
export interface RuleResult {
  id: string
  kind: RuleKind
  severity: Severity
  passed: boolean
  violations?: number
  observed?: unknown
  expected?: unknown
  sample?: unknown[]
  error?: string
}

export interface ContractReport {
  outcome: Exclude<ContractStatus, 'pending'>
  rows: number | null
  errors: number
  warnings: number
  rules: RuleResult[]
  error?: string
}

export interface ContractInfo extends ContractSummary {
  datasource_id: number
  enabled: boolean
  document: ContractDocument
  blocked_at: string | null
  blocked_run_id: number | null
  report: ContractReport | null
  updated_at: string | null
  check_error: string | null
  editable: boolean // whoever reads may also change it (the server decides)
  // whom to notify when the state changes (empty for whoever cannot change it)
  notify_emails: string
  notify_connection_id: number | null
}

/** Whom to notify at a change of state: comma-separated addresses + SMTP connection. */
export interface ContractNotify {
  emails: string
  connectionId: number | null
}

export interface ContractHistoryEntry {
  id: number
  contract_version: number
  trigger: 'refresh' | 'publish' | 'save' | 'manual' | 'freshness'
  run_id: number | null
  outcome: Exclude<ContractStatus, 'pending'>
  blocked: boolean
  errors: number
  warnings: number
  rows: number | null
  evaluated_at: string
}

/** An invalid contract: the server says which rule and why, with a code. */
export interface ContractProblem {
  index: number | null
  code: string
}

export const RULE_KINDS: RuleKind[] = [
  'column', 'not_null', 'unique', 'accepted_values', 'range', 'pattern', 'row_count', 'freshness', 'expression',
]
export const DTYPES: Dtype[] = ['integer', 'number', 'string', 'boolean', 'date', 'datetime']
/** The rules about ONE column (the others are about the whole table). */
export const COLUMN_KINDS: RuleKind[] = ['column', 'not_null', 'accepted_values', 'range', 'pattern']

/** The column a rule belongs to, to group it; null = the table. */
export function ruleColumn(r: ContractRule): string | null {
  if (r.column && r.kind !== 'freshness') return r.column
  if (r.kind === 'unique' && r.columns?.length === 1) return r.columns[0]
  return null
}

/** The form errors from the server's 422, or null when the error is another. */
export function contractProblems(e: unknown): ContractProblem[] | null {
  const detail = (e as any)?.data?.detail ?? (e as any)?.response?._data?.detail
  return detail && Array.isArray(detail.errors) ? detail.errors : null
}

export function useContracts() {
  const { apiFetch } = useApiClient()
  const base = (id: number) => `/datasources/${id}/contract`

  return {
    /** The contract, or null when the datasource has none. */
    async get(datasourceId: number): Promise<ContractInfo | null> {
      try {
        return await apiFetch<ContractInfo>(base(datasourceId))
      } catch (e: any) {
        if ((e?.status ?? e?.statusCode ?? e?.response?.status) === 404) return null
        throw e
      }
    },
    save: (datasourceId: number, document: ContractDocument, enabled: boolean, notify: ContractNotify) =>
      apiFetch<ContractInfo>(base(datasourceId), {
        method: 'PUT',
        // 0 = no connection (null would mean «do not touch it»)
        body: { document, enabled, notify_emails: notify.emails, notify_connection_id: notify.connectionId ?? 0 },
      }),
    remove: (datasourceId: number) => apiFetch<void>(base(datasourceId), { method: 'DELETE' }),
    check: (datasourceId: number) => apiFetch<ContractInfo>(`${base(datasourceId)}/check`, { method: 'POST' }),
    propose: (datasourceId: number) =>
      apiFetch<{ document: ContractDocument }>(`${base(datasourceId)}/proposal`, { method: 'POST' }),
    history: (datasourceId: number) => apiFetch<ContractHistoryEntry[]>(`${base(datasourceId)}/history?limit=15`),
    /** The contract in the open ODCS format (YAML), to hand to other tools. */
    odcs: (datasourceId: number) => apiFetch<Blob>(`${base(datasourceId)}/odcs`, { responseType: 'blob' }),
  }
}
