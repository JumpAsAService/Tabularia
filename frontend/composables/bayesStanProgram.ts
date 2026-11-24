// The program the gateway generates for the graph on the canvas (docs/specs/bayes-graph-editor.md, «The
// Stan code, read only»; the check route: docs/specs/bayes-model-store.md). It is asked for 600 ms after
// the last change the mirror finds without error, at once when the Stan code tab opens, and never for a
// graph the mirror finds an error in (the tab shows the last saved version's program then). The answer of
// an older request never replaces a newer one. A save hands the version it stored in with `adopt`, so the
// tab shows it with no second call.
//
// Nothing of Nuxt here (only Vue's reactivity): the client is handed in, so the tests run it in Node with a
// fake one and a fake clock.
import { ref, shallowRef } from 'vue'
import { bayesStanMapping, type BayesMapEntry } from '~/composables/bayesStanCode'

/** A program with what comes with it: its mapping, its hash and the generator that wrote it. */
export interface BayesStanProgram {
  text: string
  sha256: string
  mapping: BayesMapEntry[]
  codegenVersion: number | null
  /** the saved version it is (stored), or null: generated for the graph on the canvas, not saved yet */
  version: number | null
}

/** The part of the check's answer the tab reads. */
export interface BayesStanAnswer {
  ok: boolean
  findings?: Array<Record<string, unknown>>
  program?: string
  program_sha256?: string
  mapping?: unknown
  codegen_version?: number
}

export interface BayesStanClient {
  check: (graph: Record<string, any>) => Promise<BayesStanAnswer>
}

export interface BayesStanOptions {
  /** the wait after the last change (ms) */
  delayMs?: number
  /** for the tests: a fake clock */
  schedule?: (fn: () => void, ms: number) => unknown
  cancel?: (handle: unknown) => void
}

export type BayesStanStatus = 'idle' | 'waiting' | 'checking' | 'ready' | 'refused' | 'failed'

/** A stored version (or a check's answer) as a program; null when it holds none. */
export function bayesStanProgramOf (v: { program?: string, program_sha256?: string, mapping?: unknown, codegen_version?: number, version?: number } | null | undefined): BayesStanProgram | null {
  if (!v || typeof v.program !== 'string' || !v.program) return null
  return {
    text: v.program,
    sha256: typeof v.program_sha256 === 'string' ? v.program_sha256 : '',
    mapping: bayesStanMapping(v.mapping),
    codegenVersion: typeof v.codegen_version === 'number' ? v.codegen_version : null,
    version: typeof v.version === 'number' ? v.version : null,
  }
}

export function createBayesStanProgram (client: BayesStanClient, opts: BayesStanOptions = {}) {
  const delay = opts.delayMs ?? 600
  const schedule = opts.schedule ?? ((fn, ms) => setTimeout(fn, ms))
  const cancelTimer = opts.cancel ?? ((h) => clearTimeout(h as ReturnType<typeof setTimeout>))
  /** the program of the last graph the gateway answered for (or a save stored): `key` is that graph's */
  const program = shallowRef<BayesStanProgram | null>(null)
  const key = ref<string>('')
  /** the findings of a graph the gateway refused though the mirror did not: the page puts them where a save's go */
  const refusal = shallowRef<{ key: string, findings: Array<Record<string, unknown>> } | null>(null)
  const status = ref<BayesStanStatus>('idle')
  const failure = ref('')
  let timer: unknown = null
  let seq = 0

  function stop (): void {
    if (timer !== null) cancelTimer(timer)
    timer = null
    seq++
  }

  async function run (graph: Record<string, any>, k: string): Promise<void> {
    const mine = ++seq
    status.value = 'checking'
    failure.value = ''
    try {
      const a = await client.check(graph)
      if (mine !== seq) return
      const p = a.ok ? bayesStanProgramOf(a) : null
      if (p) {
        program.value = p
        key.value = k
        refusal.value = null
        status.value = 'ready'
      } else {
        // the gateway finds an error the mirror did not: the findings are the page's (a save says them)
        refusal.value = { key: k, findings: a.findings ?? [] }
        status.value = 'refused'
      }
    } catch (e) {
      if (mine !== seq) return
      failure.value = String((e as Error)?.message ?? '')
      status.value = 'failed'
    }
  }

  /**
   * The graph on the canvas, as `graph`, whose program-relevant text is `k`. With `now` the call goes at once
   * (the tab just opened); otherwise after the wait, and a later change starts the wait again. Nothing is
   * asked when the program held is already this graph's.
   */
  function want (graph: Record<string, any>, k: string, now = false): void {
    if (timer !== null) { cancelTimer(timer); timer = null }
    if (program.value && key.value === k) {
      seq++
      status.value = 'ready'
      return
    }
    seq++
    if (now) { void run(graph, k); return }
    status.value = 'waiting'
    // the graph is read when the wait ends, not now: it is a document that goes on changing
    const snapshot = JSON.parse(JSON.stringify(graph)) as Record<string, any>
    timer = schedule(() => { timer = null; void run(snapshot, k) }, delay)
  }

  /** The graph has an error (or the tab is shut): nothing is asked, what was asked is dropped. */
  function hold (): void {
    stop()
    status.value = program.value ? 'ready' : 'idle'
  }

  /** The program a save (or a version opened) stored for the graph `k`: shown with no call. */
  function adopt (k: string, p: BayesStanProgram | null): void {
    stop()
    if (!p) return
    program.value = p
    key.value = k
    status.value = 'ready'
  }

  /** A document closed or another one opened: nothing of the old one stays. */
  function reset (): void {
    stop()
    program.value = null
    key.value = ''
    refusal.value = null
    status.value = 'idle'
    failure.value = ''
  }

  return { program, key, status, failure, refusal, want, hold, adopt, reset }
}

export type BayesStanState = ReturnType<typeof createBayesStanProgram>
