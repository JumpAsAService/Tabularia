// Accessibility of the modal dialogs, in one place.
//
// The four dialogs (connection, run, DB datasource, scheduling) are structurally identical
// and all had the same problem: `@keydown.esc` was hooked on a <div> without tabindex,
// and a non-focusable div receives no keyboard events — so Esc closed nothing, unless the
// focus was already inside a field of the dialog.
//
// Here: initial focus on the card (so the screen reader announces the title), Esc caught
// at document level, Tab confined inside the dialog, and the focus given back to whoever
// opened it at closing.
import { nextTick, onBeforeUnmount, watch, type Ref } from 'vue'

const FOCUSABLE = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(', ')

// The dialogs open right now, each with where its focus goes back. A dialog opened from inside
// another one that closes at the same moment (the Flows export menu → the dbt dialog) remembers
// a button that is then removed: its focus goes back instead to whoever opened the outer one.
const openDialogs = new Set<{ card: Ref<HTMLElement | null>; returnTo: () => HTMLElement | null }>()

export function useDialogA11y(
  card: Ref<HTMLElement | null>,
  isOpen: () => boolean,
  close: () => void,
) {
  // whoever had the focus before the opening: it goes back there at closing
  let origin: HTMLElement | null = null
  // where the dialog that held `origine` sends its own focus back, if `origine` is gone by then
  let fallback: HTMLElement | null = null
  const self = { card, returnTo: () => (origin?.isConnected ? origin : fallback) }

  function focusables(): HTMLElement[] {
    if (!card.value) return []
    // offsetParent null = hidden element (v-if/v-show upstream)
    return Array.from(card.value.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
      (el) => el.offsetParent !== null,
    )
  }

  function onKey(e: KeyboardEvent) {
    if (!isOpen()) return
    // a code editor uses Esc (its suggestions, its find bar) and Tab (indenting) itself
    if ((e.target as HTMLElement | null)?.closest?.('[data-own-escape]')) return

    if (e.key === 'Escape') {
      // a Select (or another pop-up element) open inside the dialog closes by itself: closing
      // the whole dialog here threw away what had been written
      if ((e.target as HTMLElement | null)?.closest?.('[aria-haspopup][aria-expanded="true"], [role="combobox"][aria-expanded="true"]')) return
      e.preventDefault()
      e.stopPropagation()
      close()
      return
    }

    if (e.key !== 'Tab') return

    const els = focusables()
    if (!els.length) {
      // nothing to focus: Tab must not leave the dialog
      e.preventDefault()
      card.value?.focus()
      return
    }

    const first = els[0]
    const last = els[els.length - 1]
    const active = document.activeElement as HTMLElement | null

    // the cycle closes on itself: from the bottom back to the top and vice versa
    if (e.shiftKey && (active === first || active === card.value)) {
      e.preventDefault()
      last.focus()
    } else if (!e.shiftKey && active === last) {
      e.preventDefault()
      first.focus()
    }
  }

  watch(isOpen, async (ora) => {
    if (typeof document === 'undefined') return

    if (ora) {
      origin = document.activeElement as HTMLElement | null
      const host = Array.from(openDialogs).find((d) => !!origin && !!d.card.value?.contains(origin))
      fallback = host?.returnTo() ?? null
      openDialogs.add(self)
      // in capture: the dialog sees Esc before any handler underneath
      document.addEventListener('keydown', onKey, true)
      await nextTick()
      // on the card, not on the first field: the opening announces the dialog instead of
      // throwing the user into an input
      card.value?.focus()
    } else {
      openDialogs.delete(self)
      document.removeEventListener('keydown', onKey, true)
      self.returnTo()?.focus?.()
      origin = null
      fallback = null
    }
  }, { immediate: true }) // a dialog mounted already open (v-if with open=true) gets its keys and focus too

  onBeforeUnmount(() => {
    if (typeof document === 'undefined') return
    openDialogs.delete(self)
    document.removeEventListener('keydown', onKey, true)
  })
}
