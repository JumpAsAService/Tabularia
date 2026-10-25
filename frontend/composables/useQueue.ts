// Run queue (Celery) — admin panel. The gateway proxies the engine; here only the REST
// calls. The page handles the near-real-time polling (setInterval), as for the real-time
// RAM.
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
  queued: number // queued in the broker, not taken by a worker yet
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
