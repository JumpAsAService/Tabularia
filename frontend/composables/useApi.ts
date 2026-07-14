// Client dell'API dati. Passa dal gateway (che autentica e inoltra all'engine).
// Tutte le chiamate viaggiano con il Bearer token via useApiClient().

export interface ColumnInfo {
  name: string
  dtype: string
}

export interface PreviewResult {
  columns: ColumnInfo[]
  rows: Record<string, any>[]
  row_count: number
  truncated: boolean
}

export interface IngestResult {
  dataset_id: string
  status: 'ready' | 'processing'
  parquet_key: string
  raw_key: string
  format: string
  size_bytes: number
  dataset?: { dataset_id: string; rows: number; columns: ColumnInfo[] }
  task_id?: string
}

export interface TaskResponse {
  task_id: string
  status: string
  message: string
}

export interface TaskStatus {
  task_id: string
  status: string
  result?: any
  error?: string
  message?: string
}

export interface Operation {
  type: string
  params: Record<string, any>
}

export function useApi() {
  const { apiFetch } = useApiClient()

  return {
    async operations(): Promise<string[]> {
      return await apiFetch<string[]>('/tasks/operations')
    },

    async uploadFile(file: File, dtypeOverrides?: Record<string, string>): Promise<IngestResult> {
      const fd = new FormData()
      fd.append('file', file)
      if (dtypeOverrides && Object.keys(dtypeOverrides).length) {
        fd.append('dtype_overrides', JSON.stringify(dtypeOverrides))
      }
      return await apiFetch<IngestResult>('/files', { method: 'POST', body: fd })
    },

    async preview(body: {
      bucket: string
      input_key: string
      operations: Operation[]
      limit?: number
    }): Promise<PreviewResult> {
      return await apiFetch<PreviewResult>('/tasks/preview', { method: 'POST', body })
    },

    async transform(body: {
      bucket: string
      input_key: string
      output_key: string
      operations: Operation[]
    }): Promise<TaskResponse> {
      return await apiFetch<TaskResponse>('/tasks/transform-data', { method: 'POST', body })
    },

    async taskStatus(id: string): Promise<TaskStatus> {
      return await apiFetch<TaskStatus>(`/tasks/${id}`)
    },

    // download diretto del risultato (anche da un nodo intermedio) come csv/xlsx
    async exportData(body: {
      bucket: string
      input_key: string
      operations: Operation[]
      format: 'csv' | 'xlsx'
      limit?: number
      filename?: string
    }): Promise<Blob> {
      return await apiFetch<Blob>('/tasks/export', { method: 'POST', body, responseType: 'blob' })
    },
  }
}

// Una pagina di risultati dalle liste paginate (ricerca server-side).
export interface Page<T> {
  items: T[]
  total: number
}

// Query-string per gli endpoint paginati /…/search
export function pagedQuery(p: { q?: string; limit: number; offset: number }): string {
  const qs = new URLSearchParams()
  if (p.q) qs.set('q', p.q)
  qs.set('limit', String(p.limit))
  qs.set('offset', String(p.offset))
  return qs.toString()
}

// Estrae un messaggio d'errore leggibile da un errore di $fetch.
export function errMessage(e: any): string {
  return e?.data?.detail ?? e?.data?.message ?? e?.message ?? 'Errore sconosciuto'
}
