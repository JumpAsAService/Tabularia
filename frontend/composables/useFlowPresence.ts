// "This flow is also open by …".
//
// Two people may open the same flow, and whoever saves LAST overwrites the other
// silently. By choice nothing is blocked: it warns. The editor tells the gateway "I am
// here" every few seconds and gets back who else is there; whoever closes the tab
// without saying goodbye expires by itself on the server.
import { onBeforeUnmount, ref, watch, type Ref } from 'vue'
import { useApiClient } from './useApiClient'

export interface PresenceOther {
  user_id: number
  email: string
  full_name: string
  since: string
}

export function useFlowPresence(flowId: Ref<number | null>) {
  const { apiFetch } = useApiClient()
  const others = ref<PresenceOther[]>([])
  // one id per TAB: a second tab of the same user overwrites too
  const instance = 'ed-' + (globalThis.crypto?.randomUUID?.() ?? Math.random().toString(36).slice(2) + Date.now().toString(36)).replace(/[^A-Za-z0-9]/g, '').slice(0, 16)
  let timer: ReturnType<typeof setTimeout> | null = null
  let current: number | null = null
  let every = 15
  let stopped = false // after the unmount no heartbeat must start any more

  async function beat() {
    if (stopped) return
    const id = flowId.value
    if (id == null || document.hidden) return schedule() // tab hidden: no traffic, it is let expire
    try {
      const res = await apiFetch<{ others: PresenceOther[]; heartbeat_seconds: number }>(`/flows/${id}/presence`, {
        method: 'POST',
        body: { instance },
      })
      if (flowId.value === id) others.value = res.others
      every = res.heartbeat_seconds || every
    } catch {
      // the warning is a courtesy: if the gateway does not answer (or is a version that does
      // not know the route) the editor must keep working silently
      others.value = []
    }
    schedule()
  }
  function schedule() {
    if (timer) clearTimeout(timer)
    // a beat() in flight at the unmount resolves AFTER: without this it would set the timer
    // again and the presence would go on for the whole session
    if (stopped) return
    timer = setTimeout(beat, every * 1000)
  }
  function leave(id: number | null) {
    if (id == null) return
    // keepalive: the request goes out even while the page is closing
    apiFetch(`/flows/${id}/presence/${instance}`, { method: 'DELETE', keepalive: true }).catch(() => {})
  }
  const onVisible = () => { if (!document.hidden) beat() }
  const onPageHide = () => leave(current)

  watch(flowId, (id) => {
    if (current !== null && current !== id) leave(current)
    current = id
    others.value = []
    if (id != null) beat()
  }, { immediate: true })

  if (import.meta.client) {
    document.addEventListener('visibilitychange', onVisible)
    window.addEventListener('pagehide', onPageHide)
  }
  onBeforeUnmount(() => {
    stopped = true
    if (timer) clearTimeout(timer)
    if (import.meta.client) {
      document.removeEventListener('visibilitychange', onVisible)
      window.removeEventListener('pagehide', onPageHide)
    }
    leave(current)
  })

  return { others }
}
