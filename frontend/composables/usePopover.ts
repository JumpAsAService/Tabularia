// A panel opening from a button (settings, «New», «Save view», a column's profile): at
// opening the focus goes into the panel, Esc closes it and the focus goes back to whoever
// opened it. It is not modal: Tab may leave it, and a click outside closes it. (Before: none
// of these panels closed with Esc, and the focus stayed on the button — audit 2026-10-11,
// F6. A click on a part of the page that takes no focus left the panel open, since the
// focus did not move: the Viewer's column profile, critique of 2026-11-08.)
import { nextTick, onBeforeUnmount, watch, type Ref } from 'vue'

const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'

export interface PopoverOptions {
  // where the focus goes at opening: the panel's first control (the default), or the panel
  // itself (tabindex="-1"), when a first control taken by a second Enter would act
  focus?: 'first' | 'panel'
  // where the focus goes back at closing, when it is not whoever opened it (a panel that
  // moved to another column with the arrows)
  returnTo?: () => HTMLElement | null | undefined
}

export function usePopover(isOpen: Ref<boolean>, panel: Ref<HTMLElement | null>, close: () => void, opts: PopoverOptions = {}) {
  let origin: HTMLElement | null = null

  function onKey(e: KeyboardEvent) {
    if (e.key !== 'Escape') return
    // a Select open inside the panel closes by itself, first
    if ((e.target as HTMLElement | null)?.closest?.('[aria-expanded="true"]')) return
    e.preventDefault()
    close()
  }
  // a Select teleported out of the panel stays «inside» while it is open
  const inside = (node: Node | null) =>
    !!node && (!!panel.value?.contains(node) || !!(node as HTMLElement).closest?.('.sel-panel'))
  function onFocusOut(e: FocusEvent) {
    const where = e.relatedTarget as Node | null
    // no new target: the window lost the focus, or a click landed where nothing takes it
    // (that one is the pointer's: onPointerDown)
    if (!where || inside(where)) return
    origin = null // the focus went elsewhere on purpose: it is not brought back
    close()
  }
  // A press outside the panel closes it, whatever it lands on. Not on the button that
  // opened it: that one toggles the panel by itself.
  function onPointerDown(e: PointerEvent) {
    const el = e.target as Node | null
    if (inside(el) || (origin && el && origin.contains(el))) return
    origin = null
    close()
  }

  watch(isOpen, async (ora) => {
    if (typeof document === 'undefined') return
    if (ora) {
      origin = document.activeElement as HTMLElement | null
      document.addEventListener('keydown', onKey)
      document.addEventListener('pointerdown', onPointerDown, true)
      await nextTick()
      panel.value?.addEventListener('focusout', onFocusOut)
      if (opts.focus === 'panel') panel.value?.focus()
      else panel.value?.querySelector<HTMLElement>(FOCUSABLE)?.focus()
    } else {
      document.removeEventListener('keydown', onKey)
      document.removeEventListener('pointerdown', onPointerDown, true)
      // back without scrolling: a grid scrolled by the focus closes a popover opened next
      const back = origin ? (opts.returnTo?.() ?? origin) : null
      back?.focus?.({ preventScroll: true })
      origin = null
    }
  })

  onBeforeUnmount(() => {
    if (typeof document === 'undefined') return
    document.removeEventListener('keydown', onKey)
    document.removeEventListener('pointerdown', onPointerDown, true)
  })
}
