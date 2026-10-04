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

// Quanto spesso si chiede. Ogni scheda aperta fa la sua richiesta: a 5 secondi,
// cinquecento persone collegate erano cento richieste al secondo senza che
// nessuno facesse niente — metà del traffico del gateway, misurato. Il dato
// serve fresco solo dove si lavora (l'editor, mentre un flusso gira); altrove
// basta sapere a grandi linee come sta la macchina. E una scheda che nessuno
// sta guardando non chiede affatto: riparte, subito, quando torna in primo piano.
const SLOW_MS = 30000
const FAST_MS = 10000

// stato condiviso fra tutti i chiamanti
const memory = ref<MemoryInfo | null>(null)
const unavailable = ref(false) // node-exporter giù → l'UI nasconde il badge
let timer: ReturnType<typeof setInterval> | null = null
let consumers = 0
let fastConsumers = 0
let tick: (() => Promise<void>) | null = null

function visible(): boolean {
  return typeof document === 'undefined' || document.visibilityState === 'visible'
}

/** (Ri)arma il poller secondo chi lo sta usando e se la scheda è in vista. */
function arm(immediate: boolean) {
  if (timer) {
    clearInterval(timer)
    timer = null
  }
  if (!tick || consumers === 0 || !visible()) return
  if (immediate) tick()
  timer = setInterval(tick, fastConsumers > 0 ? FAST_MS : SLOW_MS)
}

function onVisibility() {
  arm(visible()) // tornata in vista: un dato fresco subito, poi il ritmo normale
}

/**
 * @param opts.fast  chi ha bisogno del dato fresco (l'editor): finché c'è almeno
 *                   un consumatore così, si chiede ogni 10 secondi invece di 30.
 */
export function useSystemMemory(opts: { fast?: boolean } = {}) {
  const { apiFetch } = useApiClient()

  onMounted(() => {
    tick = async () => {
      try {
        memory.value = await apiFetch<MemoryInfo>('/system/memory')
        unavailable.value = false
      } catch {
        unavailable.value = true // 503/offline: non è un errore da mostrare in toast
      }
    }
    consumers++
    if (opts.fast) fastConsumers++
    if (consumers === 1) document.addEventListener('visibilitychange', onVisibility)
    // il primo consumatore parte con un dato; uno «veloce» che si aggiunge
    // accorcia il ritmo senza rifare la richiesta
    arm(consumers === 1)
  })

  onUnmounted(() => {
    consumers--
    if (opts.fast) fastConsumers--
    if (consumers === 0) document.removeEventListener('visibilitychange', onVisibility)
    arm(false)
  })

  return {
    memory,
    unavailable,
    level: computed(() => (memory.value ? memoryLevel(memory.value.used_percent) : null)),
  }
}
