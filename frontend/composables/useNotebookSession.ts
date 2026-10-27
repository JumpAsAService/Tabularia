// Starting a notebook's pod and waiting for it (docs/specs/notebooks.md, «Runtime»): the
// app page and the editor page ask the gateway for a session, then look at it until marimo
// answers — soon at first, then every second, then every two (composables/waits.ts). The
// seconds since the start tick on screen: a pod can take a minute while the cluster fetches
// the environment's image.
//
// Leaving the page does NOT stop the pod: the gateway stops it when nobody has used it for a
// while.
import { onBeforeUnmount, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { errMessage } from '~/composables/useApi'
import { FIRST_PAUSE_MS, nextPause } from '~/composables/waits'
import {
  editorOf, useNotebooks, type NotebookAuthor, type NotebookMode, type NotebookSession,
} from '~/composables/useNotebooks'

/** idle: not asked yet; busy: somebody else is editing (409) */
export type SessionPhase = 'idle' | 'starting' | 'ready' | 'failed' | 'busy'

// a session missing while it was starting is asked for again, this many times
const RESTARTS = 2
// failed looks in a row before giving up (a gateway restarting answers again soon)
const MISSES = 5

const statusOf = (e: any): number | undefined => e?.response?.status ?? e?.statusCode ?? e?.status

export function useNotebookSession(mode: NotebookMode) {
  const { t } = useI18n()
  const api = useNotebooks()

  const phase = ref<SessionPhase>('idle')
  const session = ref<NotebookSession | null>(null)
  /** what went wrong: a heading in the interface's words, the gateway's reason under it */
  const problem = ref<{ title: string; detail: string } | null>(null)
  /** who is editing already, on a 409 */
  const editor = ref<NotebookAuthor | null>(null)
  /** seconds since the start was asked */
  const elapsed = ref(0)

  // every start (or the page leaving) makes the previous wait stale
  let generation = 0
  let ticker: ReturnType<typeof setInterval> | null = null

  function stopTicking() {
    if (ticker) clearInterval(ticker)
    ticker = null
  }

  function fail(title: string, detail: string) {
    stopTicking()
    problem.value = { title, detail }
    phase.value = 'failed'
  }

  function refused(e: unknown) {
    const code = statusOf(e)
    const detail = errMessage(e)
    if (code === 409) {
      stopTicking()
      editor.value = editorOf(e)
      problem.value = { title: '', detail }
      phase.value = 'busy'
    } else if (code === 400) {
      fail(t('notebook.session.offTitle'), detail)
    } else if (code === 503) {
      fail(t('notebook.session.capTitle'), detail)
    } else if (code === 422) {
      fail(t('notebook.session.notReadyTitle'), detail)
    } else {
      fail(mode === 'app' ? t('notebook.session.appFailedTitle') : t('notebook.session.editFailedTitle'), detail)
    }
  }

  /** false while it is still starting */
  function settle(s: NotebookSession): boolean {
    session.value = s
    if (s.status === 'ready') {
      stopTicking()
      phase.value = 'ready'
      return true
    }
    if (s.status === 'failed') {
      fail(mode === 'app' ? t('notebook.session.appFailedTitle') : t('notebook.session.editFailedTitle'),
        s.error || t('notebook.session.noReason'))
      return true
    }
    phase.value = 'starting'
    return false
  }

  async function start(id: number) {
    const mine = ++generation
    stopTicking()
    problem.value = null
    editor.value = null
    session.value = null
    phase.value = 'starting'
    const since = Date.now()
    elapsed.value = 0
    ticker = setInterval(() => { elapsed.value = Math.floor((Date.now() - since) / 1000) }, 1000)

    let first: NotebookSession
    try {
      first = await api.startSession(id, mode)
    } catch (e) {
      if (mine === generation) refused(e)
      return
    }
    if (mine !== generation || settle(first)) return

    let pause = FIRST_PAUSE_MS
    let restarts = 0
    let misses = 0
    while (mine === generation) {
      await new Promise((r) => setTimeout(r, pause))
      pause = nextPause(pause, since, 2000)
      if (mine !== generation) return
      let found: NotebookSession | undefined
      try {
        found = (await api.sessions(id)).find((s) => s.mode === mode)
        misses = 0
      } catch (e) {
        if (mine !== generation) return
        if (++misses >= MISSES) {
          fail(mode === 'app' ? t('notebook.session.appFailedTitle') : t('notebook.session.editFailedTitle'), errMessage(e))
          return
        }
        continue
      }
      if (mine !== generation) return
      if (!found) {
        // gone while starting (stopped by someone with EDIT, or by the gateway): asked again,
        // a couple of times
        if (++restarts > RESTARTS) {
          fail(mode === 'app' ? t('notebook.session.appFailedTitle') : t('notebook.session.editFailedTitle'),
            t('notebook.session.vanished'))
          return
        }
        try {
          found = await api.startSession(id, mode)
        } catch (e) {
          if (mine === generation) refused(e)
          return
        }
        if (mine !== generation) return
      }
      if (settle(found)) return
    }
  }

  /** forgets the wait: the pod is left to the gateway */
  function leave() {
    generation++
    stopTicking()
  }

  onBeforeUnmount(leave)

  return { phase, session, problem, editor, elapsed, start, leave }
}
