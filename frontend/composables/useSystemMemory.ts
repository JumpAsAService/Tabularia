// The host's RAM in real time (gateway → node-exporter). It serves the user to see how
// much memory is left WHILE building/running a flow: Polars works in RAM and saturating
// it fails the run.
//
// SINGLETON state with a refcount: several components may use it (the shell's topbar +
// the editor's toolbar) but only one poller runs.
import { ref, computed, onMounted, onUnmounted } from 'vue'

export interface MemoryInfo {
  total_bytes: number
  available_bytes: number
  used_bytes: number
  used_percent: number
}

/** A 5-level Likert-like scale on the % of RAM used. */
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

// How often it asks. Every open tab makes its own request: at 5 seconds, five hundred
// people connected were a hundred requests a second without anyone doing anything —
// half the gateway's traffic, measured. The data needs to be fresh only where one works
// (the editor, while a flow runs); elsewhere knowing roughly how the machine is doing is
// enough. And a tab nobody is looking at does not ask at all: it starts again, at once,
// when it comes back to the foreground.
const SLOW_MS = 30000
const FAST_MS = 10000

// state shared among all the callers
const memory = ref<MemoryInfo | null>(null)
const unavailable = ref(false) // node-exporter down → the UI hides the badge
let timer: ReturnType<typeof setInterval> | null = null
let consumers = 0
let fastConsumers = 0
let tick: (() => Promise<void>) | null = null

function visible(): boolean {
  return typeof document === 'undefined' || document.visibilityState === 'visible'
}

/** (Re)arms the poller according to who is using it and whether the tab is in view. */
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
  arm(visible()) // back in view: fresh data at once, then the normal rhythm
}

/**
 * @param opts.fast  whoever needs fresh data (the editor): while there is at least one
 *                   such consumer, it asks every 10 seconds instead of 30.
 */
// the installation answered 404: there is nothing to read, for the life of the page
let absent = false

export function useSystemMemory(opts: { fast?: boolean } = {}) {
  const { apiFetch } = useApiClient()

  onMounted(() => {
    tick = async () => {
      if (absent) return
      try {
        memory.value = await apiFetch<MemoryInfo>('/system/memory')
        unavailable.value = false
      } catch (e: any) {
        unavailable.value = true // 503/offline: not an error to show in a toast
        // 404: this installation has no such reading (on Kubernetes, without node-exporter),
        // and asking every 10 s filled the console with red for nothing: asked no more
        if ((e?.response?.status ?? e?.statusCode) === 404) absent = true
      }
    }
    consumers++
    if (opts.fast) fastConsumers++
    if (consumers === 1) document.addEventListener('visibilitychange', onVisibility)
    // the first consumer starts with data; a «fast» one joining shortens the rhythm without
    // making the request again
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
