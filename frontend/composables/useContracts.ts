// Data contracts: ciò che chi produce una datasource promette a chi la consuma.
// Qui le chiamate all'API e il vocabolario condiviso da icona e dialogo.
//
// Le severità sono DUE, per regola: `warning` (non bloccante: i dati si
// pubblicano e la violazione si segnala) ed `error` (bloccante: i dati nuovi non
// vengono pubblicati, la datasource resta sull'ultimo snapshot buono).

export type ContractStatus = 'pending' | 'passed' | 'warning' | 'failed'
export type Severity = 'warning' | 'error'
export type RuleKind =
  | 'column' | 'not_null' | 'unique' | 'accepted_values' | 'range'
  | 'pattern' | 'row_count' | 'freshness' | 'expression'
export type Dtype = 'integer' | 'number' | 'string' | 'boolean' | 'date' | 'datetime'

/** Lo stato del contratto di una datasource, come arriva negli elenchi. */
export interface ContractSummary {
  status: ContractStatus
  checked_at: string | null
  errors: number
  warnings: number
  blocked: boolean // l'ultimo aggiornamento è stato rifiutato
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

/** L'esito di una regola nell'ultimo referto. */
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
  editable: boolean // chi legge può anche modificarlo (lo decide il server)
  // chi avvisare quando lo stato cambia (vuoti per chi non può modificare)
  notify_emails: string
  notify_connection_id: number | null
}

/** Chi avvisare al cambio di stato: indirizzi separati da virgola + connessione SMTP. */
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

/** Un contratto non valido: il server dice quale regola e perché, con un codice. */
export interface ContractProblem {
  index: number | null
  code: string
}

export const RULE_KINDS: RuleKind[] = [
  'column', 'not_null', 'unique', 'accepted_values', 'range', 'pattern', 'row_count', 'freshness', 'expression',
]
export const DTYPES: Dtype[] = ['integer', 'number', 'string', 'boolean', 'date', 'datetime']
/** Le regole che riguardano UNA colonna (le altre riguardano la tabella intera). */
export const COLUMN_KINDS: RuleKind[] = ['column', 'not_null', 'accepted_values', 'range', 'pattern']

/** La colonna a cui una regola appartiene, per raggrupparla; null = la tabella. */
export function ruleColumn(r: ContractRule): string | null {
  if (r.column && r.kind !== 'freshness') return r.column
  if (r.kind === 'unique' && r.columns?.length === 1) return r.columns[0]
  return null
}

/** Gli errori di forma dal 422 del server, o null se l'errore è un altro. */
export function contractProblems(e: unknown): ContractProblem[] | null {
  const detail = (e as any)?.data?.detail ?? (e as any)?.response?._data?.detail
  return detail && Array.isArray(detail.errors) ? detail.errors : null
}

export function useContracts() {
  const { apiFetch } = useApiClient()
  const base = (id: number) => `/datasources/${id}/contract`

  return {
    /** Il contratto, o null se la datasource non ne ha uno. */
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
        // 0 = nessuna connessione (null vorrebbe dire «non toccarla»)
        body: { document, enabled, notify_emails: notify.emails, notify_connection_id: notify.connectionId ?? 0 },
      }),
    remove: (datasourceId: number) => apiFetch<void>(base(datasourceId), { method: 'DELETE' }),
    check: (datasourceId: number) => apiFetch<ContractInfo>(`${base(datasourceId)}/check`, { method: 'POST' }),
    propose: (datasourceId: number) =>
      apiFetch<{ document: ContractDocument }>(`${base(datasourceId)}/proposal`, { method: 'POST' }),
    history: (datasourceId: number) => apiFetch<ContractHistoryEntry[]>(`${base(datasourceId)}/history?limit=15`),
    /** Il contratto nel formato aperto ODCS (YAML), da consegnare ad altri strumenti. */
    odcs: (datasourceId: number) => apiFetch<Blob>(`${base(datasourceId)}/odcs`, { responseType: 'blob' }),
  }
}
