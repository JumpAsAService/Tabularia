// A panel opening from a button (settings, «New», «Save view»): at opening the focus goes
// into the panel, Esc closes it and the focus goes back to whoever opened it. It is not
// modal: Tab may leave it, and outside the panel closes. (Before: none of these panels
// closed with Esc, and the focus stayed on the button — audit 2026-10-11, F6.)
import { nextTick, onBeforeUnmount, watch, type Ref } from 'vue'

const FOCUSABILI = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'

export function usePopover(isOpen: Ref<boolean>, panel: Ref<HTMLElement | null>, close: () => void) {
  let origin: HTMLElement | null = null

  function onKey(e: KeyboardEvent) {
    if (e.key !== 'Escape') return
    // a Select open inside the panel closes by itself, first
    if ((e.target as HTMLElement | null)?.closest?.('[aria-expanded="true"]')) return
    e.preventDefault()
    close()
  }
  function onFocusOut(e: FocusEvent) {
    const where = e.relatedTarget as Node | null
    // a Select teleported out of the panel stays «inside» while it is open
    if (!where || panel.value?.contains(where) || (where as HTMLElement).closest?.('.sel-panel')) return
    origin = null // the focus went elsewhere on purpose: it is not brought back
    close()
  }

  watch(isOpen, async (ora) => {
    if (typeof document === 'undefined') return
    if (ora) {
      origin = document.activeElement as HTMLElement | null
      document.addEventListener('keydown', onKey)
      await nextTick()
      panel.value?.addEventListener('focusout', onFocusOut)
      panel.value?.querySelector<HTMLElement>(FOCUSABILI)?.focus()
    } else {
      document.removeEventListener('keydown', onKey)
      origin?.focus?.()
      origin = null
    }
  })

  onBeforeUnmount(() => {
    if (typeof document !== 'undefined') document.removeEventListener('keydown', onKey)
  })
}
