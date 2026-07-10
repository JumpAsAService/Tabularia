// Esecuzioni dei flussi (run history) via gateway. Il lancio richiede la
// capability RUN sul progetto del flusso; la cronologia basta VIEW.

export interface RunInfo {
  id: number
  flow_id: number
  status: 'PENDING' | 'STARTED' | 'SUCCESS' | 'FAILURE' | string
  launched_by: number | null
  output_key: string
  rows_written: number | null
  error: string | null
  publish_name: string | null
  datasource_id: number | null
  destination: string | null // JSON: {db_type, host, database, table, mode}
  started_at: string | null
  finished_at: string | null
}

export interface PublishSpec {
  name: string
  project_id: number
  description?: string
}

export interface DestinationSpec {
  connection_id: number
  table: string
  mode: 'append' | 'replace'
  post_sql?: string
}

export function useRuns() {
  const { apiFetch } = useApiClient()

  return {
    launch: (
      flowId: number,
      body: {
        bucket: string
        input_key: string
        operations: any[]
        publish?: PublishSpec | null
        destination?: DestinationSpec | null
      },
    ) => apiFetch<RunInfo>(`/flows/${flowId}/runs`, { method: 'POST', body }),

    listByFlow: (flowId: number) => apiFetch<RunInfo[]>(`/flows/${flowId}/runs`),

    get: (id: number) => apiFetch<RunInfo>(`/runs/${id}`),
  }
}
