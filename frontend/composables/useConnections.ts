// Connessioni a database esterni. Tutte le operazioni richiedono la capability
// CONNECT sulla cartella della connessione; la password non esce mai dalle API.
import type { ApiCallDraft } from '~/composables/useDatasources'
import type { FileTemplatesDraft, FilesCheck } from '~/composables/usePlaceholders'
import { pagedQuery, type Page } from '~/composables/useApi'

export interface ConnectionInfo {
  id: number
  name: string
  description: string
  project_id: number
  owner_id: number | null
  db_type: string
  host: string
  port: number | null
  username: string
  database: string
  db_schema: string
  has_password: boolean
  // opzioni del tipo, JSON (solo SMTP: from_address, from_name, tls,
  // allowed_domains). Non contiene segreti, per questo torna al client.
  extra: string
  // SFTP: a private key is stored (never the key itself), and the server's host
  // key accepted by a test (empty = never accepted: every use is refused)
  has_private_key?: boolean
  host_key_type?: string
  host_key_fingerprint?: string
  updated_at: string | null
}

// what a test of an SFTP connection says about the server's host key. With
// `needs_acceptance` the server offers a key that is not accepted (never, or a
// different one): NOTHING was sent to it, and the user accepts the key — after
// comparing it — with a second test naming it (`expected_fingerprint`)
export interface HostKeyResult {
  ok: boolean
  needs_acceptance?: boolean
  host_key?: { type: string; fingerprint: string }
  changed?: boolean
  previous?: string
  accepted?: boolean
}

// what «Try the call» answers: the request as sent (secrets masked as ***), the
// status, an excerpt of the raw answer and the first rows of the table it makes
export interface ApiTryResult {
  ok: boolean
  calls: number
  request: { method: string; url: string; headers: Record<string, string>; body: string }
  status?: number
  content_type?: string
  bytes?: number
  excerpt?: string
  final_url?: string
  error?: string
  records?: number
  columns?: string[]
  rows?: Record<string, unknown>[]
  next?: string | null
}

export interface KeyAcceptance {
  expected_fingerprint: string
  expected_type?: string
}

export interface ConnectionDraft {
  name: string
  description?: string
  db_type: string
  host: string
  port?: number | null
  username?: string
  password?: string
  database?: string
  db_schema?: string
  extra?: string
  // SFTP with the key method: sent only when typed (empty = keep the stored one)
  private_key?: string
  private_key_passphrase?: string
  // SFTP: the host key the user accepted in the form's test, to accept after saving.
  // Never sent with the connection: create/update accept it with a test of their own.
  accept_host_key?: string
  accept_host_key_type?: string
}

export const DB_TYPES = [
  { value: 'postgresql', label: 'PostgreSQL' },
  { value: 'mysql', label: 'MySQL' },
  { value: 'mariadb', label: 'MariaDB' },
  { value: 'clickhouse', label: 'ClickHouse' },
  { value: 'trino', label: 'Trino' },
  // database = service name (FREEPDB1, ORCLPDB1); schema = owner, vuoto = l'utente
  { value: 'oracle', label: 'Oracle' },
  // extra.tls: verify (certificato verificato) | trust (autofirmato) | none
  { value: 'sqlserver', label: 'Microsoft SQL Server' },
  // object storage: host=endpoint, username=access key, database=bucket, schema=region
  { value: 's3', label: 'S3 / object storage' },
  // posta: le tre impostazioni senza colonna naturale (mittente, TLS, domini
  // ammessi) vivono in `extra`
  { value: 'smtp', label: 'SMTP / email' },
  // file Excel su SharePoint via Microsoft Graph: host=URL del sito,
  // username=client id dell'app Entra ID, password=secret, database=tenant id,
  // extra.library=raccolta documenti
  { value: 'sharepoint', label: 'SharePoint (Excel)' },
  // files on an SSH server: host, port 22, user, password OR private key
  // (extra.auth); the server's host key is accepted by the first test
  { value: 'sftp', label: 'SFTP (files)' },
  // an HTTP API: host = base URL, password = the secret of the auth, extra = {auth,
  // key_name, key_in, token_url, scope, client_auth, headers, test_path}
  { value: 'api', label: 'HTTP API (REST/JSON)' },
]

export function useConnections() {
  const { apiFetch } = useApiClient()

  const accept = (id: number, key: KeyAcceptance) =>
    apiFetch<HostKeyResult>(`/connections/${id}/test`, { method: 'POST', body: key })

  return {
    // tutte quelle usabili dall'utente (per il picker delle sorgenti DB)
    list: () => apiFetch<ConnectionInfo[]>('/connections'),

    // pagina globale: ricerca server-side (sull'intero dataset) + paginazione
    listPaged: (p: { q?: string; limit: number; offset: number }) =>
      apiFetch<Page<ConnectionInfo>>(`/connections/search?${pagedQuery(p)}`),

    listByProject: (projectId: number) =>
      apiFetch<ConnectionInfo[]>(`/projects/${projectId}/connections`),

    // SFTP: the key shown by the form's test is accepted right after saving, by a
    // test of the saved connection that keeps it only if the server still offers it
    create: async (projectId: number, body: ConnectionDraft) => {
      const { accept_host_key, accept_host_key_type, ...rest } = body
      const saved = await apiFetch<ConnectionInfo>(`/projects/${projectId}/connections`, { method: 'POST', body: rest })
      if (accept_host_key) await accept(saved.id, { expected_fingerprint: accept_host_key, expected_type: accept_host_key_type })
      return saved
    },

    update: async (id: number, body: Partial<ConnectionDraft> & { project_id?: number }) => {
      const { accept_host_key, accept_host_key_type, ...rest } = body
      const saved = await apiFetch<ConnectionInfo>(`/connections/${id}`, { method: 'PATCH', body: rest })
      if (accept_host_key) await accept(id, { expected_fingerprint: accept_host_key, expected_type: accept_host_key_type })
      return saved
    },

    remove: (id: number) => apiFetch<void>(`/connections/${id}`, { method: 'DELETE' }),

    // the password comes along, still encrypted, for whoever has CONNECT
    duplicate: (id: number) => apiFetch<ConnectionInfo>(`/connections/${id}/duplicate`, { method: 'POST' }),

    // test di una connessione GIÀ salvata. SFTP: without `key` it shows a key that
    // is not accepted (needs_acceptance); with `key` it accepts exactly that one
    test: (id: number, key?: KeyAcceptance) => (key ? accept(id, key) : apiFetch<HostKeyResult>(`/connections/${id}/test`, { method: 'POST' })),

    // test del form PRIMA di salvare, col solo segreto scritto nel form; stores nothing
    testDraft: (projectId: number, body: ConnectionDraft, key?: KeyAcceptance) => {
      const { accept_host_key: _a, accept_host_key_type: _b, ...rest } = body
      return apiFetch<HostKeyResult>(`/projects/${projectId}/connections/test`, { method: 'POST', body: { ...rest, ...(key ?? {}) } })
    },

    // «Try the call»: the first page of the first call, before saving
    tryApi: (id: number, call: ApiCallDraft) =>
      apiFetch<ApiTryResult>(`/connections/${id}/api/try`, { method: 'POST', body: call }),

    // the files a folder + glob takes on an SFTP connection; with placeholders or
    // variables every expansion, and the values without a file
    sftpFiles: (id: number, body: { folder: string; pattern: string } & Partial<FileTemplatesDraft>) =>
      apiFetch<FilesCheck>(`/connections/${id}/sftp/files`, { method: 'POST', body }),

    // i file Excel che un percorso/glob prende su una connessione SharePoint
    sharepointFiles: (id: number, body: { path: string } & Partial<FileTemplatesDraft>) =>
      apiFetch<FilesCheck>(`/connections/${id}/sharepoint/files`, { method: 'POST', body }),

    // the objects a folder + glob takes in a bucket of an S3 connection (empty bucket =
    // the connection's default)
    s3Files: (id: number, body: { bucket: string; folder: string; pattern: string } & Partial<FileTemplatesDraft>) =>
      apiFetch<FilesCheck>(`/connections/${id}/s3/files`, { method: 'POST', body }),
    tables: (id: number) => apiFetch<{ tables: string[] }>(`/connections/${id}/tables`),
  }
}
