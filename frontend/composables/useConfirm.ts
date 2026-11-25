// The app's confirmation dialog, in place of the browser's confirm(): a question that is
// answered with a Promise<boolean>, so the code that asked reads as it did before
//
//   if (!(await confirmAction(t('flows.confirmDelete', { name }), 'delete'))) return
//
// The questions wait in a queue and the host (components/ui/ConfirmHost.vue, mounted once in
// app.vue) shows one at a time. `kind` names the action: it picks the button's label and the
// dialog's title, and whether the question is destructive (a red button, the focus on Cancel).
// Plain module state, no Nuxt: a confirmation only ever asks on the client, from a click or
// a navigation, and the tests load this file as it is.
import { ref } from 'vue'

export type ConfirmKind =
  | 'confirm' | 'delete' | 'remove' | 'revoke' | 'discard' | 'leave' | 'restore' | 'stop'
  | 'disable' | 'grant' | 'replace' | 'reset' | 'rotate' | 'overwrite' | 'forget' | 'close'

/** The kinds whose question is about losing or cutting something: red, and Cancel has the focus. */
export const DESTRUCTIVE_KINDS: readonly ConfirmKind[] = [
  'delete', 'remove', 'revoke', 'discard', 'leave', 'stop', 'disable', 'reset', 'rotate', 'forget', 'close',
]

export interface ConfirmQuestion {
  id: number
  message: string
  kind: ConfirmKind
  destructive: boolean
  resolve: (ok: boolean) => void
}

let nextId = 1
const queue = ref<ConfirmQuestion[]>([])

export function useConfirm() {
  /** Asks; resolves true on the action's button, false on Cancel, Esc or a click outside. */
  function confirmAction(message: string, kind: ConfirmKind = 'confirm', opts: { destructive?: boolean } = {}): Promise<boolean> {
    return new Promise<boolean>((resolve) => {
      const destructive = opts.destructive ?? DESTRUCTIVE_KINDS.includes(kind)
      queue.value = [...queue.value, { id: nextId++, message, kind, destructive, resolve }]
    })
  }

  /** The question on screen: the oldest one still waiting. */
  function current(): ConfirmQuestion | null {
    return queue.value[0] ?? null
  }

  /** Answers the question on screen; the next one, if any, takes its place. */
  function answer(ok: boolean) {
    const q = queue.value[0]
    if (!q) return
    queue.value = queue.value.slice(1)
    q.resolve(ok)
  }

  return { confirmAction, current, answer, queue }
}
