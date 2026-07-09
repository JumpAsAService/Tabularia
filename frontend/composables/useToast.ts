// Toast globali: notifiche transitorie di successo/errore che appaiono in
// basso a destra e spariscono da sole. Stato condiviso via useState (una sola
// coda per l'app); il rendering è in components/ui/ToastHost.vue (app.vue).

export interface Toast {
  id: number
  kind: 'success' | 'error' | 'info'
  message: string
}

let nextId = 1

export function useToast() {
  const toasts = useState<Toast[]>('toasts', () => [])

  function dismiss(id: number) {
    toasts.value = toasts.value.filter((t) => t.id !== id)
  }

  function push(kind: Toast['kind'], message: string, ttlMs?: number) {
    const id = nextId++
    toasts.value = [...toasts.value, { id, kind, message }]
    // gli errori restano di più: vanno letti, non intravisti
    const ttl = ttlMs ?? (kind === 'error' ? 6500 : 3500)
    if (import.meta.client) setTimeout(() => dismiss(id), ttl)
  }

  return {
    toasts,
    dismiss,
    success: (message: string) => push('success', message),
    error: (message: string) => push('error', message),
    info: (message: string) => push('info', message),
  }
}
