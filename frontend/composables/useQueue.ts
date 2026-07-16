// Coda di esecuzione (Celery) — pannello admin. Il gateway fa da proxy verso
// l'engine; qui solo le chiamate REST. Il polling near-real-time lo gestisce la
// pagina (setInterval), come per la RAM in tempo reale.
export interface QueueJob {
  task_id: string
  task_name: string | null
  worker: string
  state: 'running' | 'reserved' | string
  runtime_s: number | null
}

export interface QueueWorker {
  name: string
  concurrency: number | null
  running: number
  reserved: number
}

export interface QueueOverview {
  workers: QueueWorker[]
  running: QueueJob[]
  reserved: QueueJob[]
  queues: { name: string; messages: number }[]
  queued: number // accodati nel broker, non ancora presi da un worker
  reserved_count: number
  running_count: number
  waiting: number // queued + reserved
}

export function useQueue() {
  const { apiFetch } = useApiClient()

  return {
    overview: () => apiFetch<QueueOverview>('/queue'),
    stopJob: (taskId: string) =>
      apiFetch<{ task_id: string; status: string }>(`/queue/jobs/${taskId}/stop`, {
        method: 'POST',
      }),
  }
}
