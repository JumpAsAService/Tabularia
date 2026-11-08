// Global toasts: transient success/error notifications that appear at the bottom right
// and disappear by themselves. State shared through useState (one queue for the app);
// the rendering is in components/ui/ToastHost.vue (app.vue).

export interface Toast {
  id: number
  kind: 'success' | 'error' | 'warning' | 'info'
  message: string
  // a toast that speaks for a recurring situation: a new one with the same key replaces it
  // instead of piling up beside it
  key?: string
  // where it shows: the page's corner (bottom right) unless a surface keeps its own stack
  // (the flow editor's, over the canvas, away from the preview's numbers)
  host?: string
}

let nextId = 1
const timers = new Map<number, ReturnType<typeof setTimeout>>()

export function useToast() {
  const toasts = useState<Toast[]>('toasts', () => [])

  function dismiss(id: number) {
    toasts.value = toasts.value.filter((t) => t.id !== id)
    clearTimeout(timers.get(id))
    timers.delete(id)
  }

  function push(kind: Toast['kind'], message: string, ttlMs?: number, key?: string, host?: string) {
    const same = key ? toasts.value.find((t) => t.key === key) : undefined
    const id = same?.id ?? nextId++
    toasts.value = same
      ? toasts.value.map((t) => (t.id === id ? { ...t, kind, message } : t))
      : [...toasts.value, { id, kind, message, key, host }]
    // the errors stay longer: they must be read, not glimpsed
    const ttl = ttlMs ?? (kind === 'error' ? 6500 : kind === 'warning' ? 5000 : 3500)
    if (import.meta.client) {
      clearTimeout(timers.get(id))
      timers.set(id, setTimeout(() => dismiss(id), ttl))
    }
  }

  return {
    toasts,
    dismiss,
    success: (message: string) => push('success', message),
    error: (message: string) => push('error', message),
    // warnings: something to know, not to fix
    warning: (message: string, key?: string, host?: string) => push('warning', message, undefined, key, host),
    info: (message: string) => push('info', message),
  }
}
