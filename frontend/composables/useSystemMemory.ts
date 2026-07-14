// RAM dell'host in tempo reale (gateway → node-exporter). Serve all'utente per
// vedere quanta memoria resta MENTRE costruisce/esegue un flusso: Polars lavora
// in RAM e saturarla fa fallire il run.
//
// Stato SINGLETON con refcount: più componenti possono usarlo (topbar della
// shell + toolbar dell'editor) ma gira un solo poller.
import { ref, computed, onMounted, onUnmounted } from 'vue'

export interface MemoryInfo {
  total_bytes: number
  available_bytes: number
  used_bytes: number
  used_percent: number
}

/** Scala tipo Likert a 5 livelli sulla % di RAM usata. */
export interface MemoryLevel {
  step: 1 | 2 | 3 | 4 | 5
  label: string
  color: string
}

const LEVELS: { max: number; level: MemoryLevel }[] = [
  { max: 50, level: { step: 1, label: 'libera', color: '#22c55e' } },
  { max: 70, level: { step: 2, label: 'ok', color: '#84cc16' } },
  { max: 85, level: { step: 3, label: 'media', color: '#facc15' } },
  { max: 93, level: { step: 4, label: 'alta', color: '#fb923c' } },
  { max: Infinity, level: { step: 5, label: 'critica', color: '#ef4444' } },
]

export function memoryLevel(usedPercent: number): MemoryLevel {
  return (LEVELS.find((l) => usedPercent < l.max) ?? LEVELS[LEVELS.length - 1]).level
}

export function formatGB(bytes: number): string {
  return `${(bytes / 1e9).toFixed(1)} GB`
}

const POLL_MS = 5000

// stato condiviso fra tutti i chiamanti
const memory = ref<MemoryInfo | null>(null)
const unavailable = ref(false) // node-exporter giù → l'UI nasconde il badge
let timer: ReturnType<typeof setInterval> | null = null
let consumers = 0

export function useSystemMemory() {
  const { apiFetch } = useApiClient()

  async function tick() {
    try {
      memory.value = await apiFetch<MemoryInfo>('/system/memory')
      unavailable.value = false
    } catch {
      unavailable.value = true // 503/offline: non è un errore da mostrare in toast
    }
  }

  onMounted(() => {
    consumers++
    if (consumers === 1) {
      tick()
      timer = setInterval(tick, POLL_MS)
    }
  })

  onUnmounted(() => {
    consumers--
    if (consumers === 0 && timer) {
      clearInterval(timer)
      timer = null
    }
  })

  return {
    memory,
    unavailable,
    level: computed(() => (memory.value ? memoryLevel(memory.value.used_percent) : null)),
  }
}
