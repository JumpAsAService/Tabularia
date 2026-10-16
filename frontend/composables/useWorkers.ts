// Workers (Admin → Workers): the processes each worker runs, the workers alive now,
// the tasks waiting and, on Kubernetes, the autoscaling bounds of their pods. The
// processes per worker are engine settings (useEngineSettings saves them); the
// bounds go to the cluster through the gateway.

export type WorkerKind = 'run' | 'preview'

export interface WorkerState {
  name: string
  queues: string[]
  processes: number | null
  target: number | null // the size it is moving to (it shrinks when processes are free)
  started_with: number | null
  busy: number | null
  waiting: number | null
  memory_used: number | null // bytes
  memory_limit: number | null
  memory_per_process: number | null
  memory_share: number | null // part of the limit the processes share
  memory_floor: number | null // below this a process would be recycled too often
  autoscale: boolean
  started_at: number | null // epoch seconds
  seen_at: number | null
}

export interface WorkerConcurrency {
  key: string // the engine setting, e.g. "workers.run_concurrency"
  min: number
  max: number
  value: number | null // from the panel; null = every worker keeps its starting size
  applied: number | null
  updated_at: string | null
  updated_by: string | null
}

export interface WorkerScaling {
  mode: 'kubernetes' | 'none'
  deployment: string | null
  autoscaler: 'hpa' | 'scaledobject' | null
  autoscaler_name: string | null
  replicas: number | null
  current: number | null
  ready: number | null
  desired: number | null
  min: number | null
  max: number | null
  floor: number | null
  ceiling: number | null
  saved: boolean
  saved_min: number | null
  saved_max: number | null
  updated_at: string | null
  updated_by: string | null
  error: string | null
}

export interface WorkerGroup {
  kind: WorkerKind
  queue: string
  queued: number | null
  concurrency: WorkerConcurrency
  workers: WorkerState[]
  scaling: WorkerScaling
}

export interface WorkersState {
  groups: WorkerGroup[]
  workers_known: boolean
  concurrency_editable: boolean
  scaling_editable: boolean
  panel_enabled: boolean
  tick_seconds: number
}

// a refusal of the bounds: a code, the sentence is written in the viewer's language
export interface ScalingError {
  code: 'no_autoscaler' | 'min' | 'order' | 'ceiling'
  limit?: number
}

export function useWorkers() {
  const { apiFetch } = useApiClient()
  return {
    get: () => apiFetch<WorkersState>('/admin/workers'),
    scale: (kind: WorkerKind, min: number, max: number) =>
      apiFetch<WorkersState>(`/admin/workers/${kind}/scaling`, {
        method: 'PUT', body: { min_replicas: min, max_replicas: max },
      }),
    // the bounds go back to the chart at its next deployment
    release: (kind: WorkerKind) => apiFetch<WorkersState>(`/admin/workers/${kind}/scaling`, { method: 'DELETE' }),
  }
}
