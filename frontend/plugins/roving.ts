// `v-roving` on a row of tabs (role="tablist" with role="tab" inside) or on a choice among
// a few (role="radiogroup" with role="radio"): ONE tab stop for the row, on the chosen
// item, and the arrows move along it, going round; Home and End where the keyboard has
// them. As ARIA asks: in a radio group the arrow chooses (the item's own click), on a row
// of tabs it moves the focus and Enter or Space chooses, so that walking past a tab never
// loads what it shows. `.vertical`: ↑ ↓ too (a column of tabs). `.main`: the page's main
// row, the one «,» and «.» walk from anywhere (components/ui/AppKeys.vue).
// The look does not change: only roles, tabindex and keys.
import type { Directive } from 'vue'
import { rovingKey } from '~/composables/appKeys'

const ITEM = '[role="tab"], [role="radio"]'
const items = (el: HTMLElement) =>
  [...el.querySelectorAll<HTMLElement>(ITEM)].filter((b) => !(b as HTMLButtonElement).disabled)
const chosen = (b: HTMLElement) => b.getAttribute('aria-selected') === 'true' || b.getAttribute('aria-checked') === 'true'

function sync(el: HTMLElement) {
  const list = items(el)
  // the focus keeps the stop while it is in the row (a tab focused, not chosen yet)
  const stop = list.find((b) => b === document.activeElement) ?? list.find(chosen) ?? list[0]
  for (const b of list) b.tabIndex = b === stop ? 0 : -1
}

function onKey(e: KeyboardEvent) {
  if (e.ctrlKey || e.metaKey || e.altKey || e.shiftKey) return
  const el = e.currentTarget as HTMLElement
  const list = items(el)
  const at = list.indexOf((e.target as HTMLElement).closest<HTMLElement>(ITEM) as HTMLElement)
  if (at < 0) return
  const to = rovingKey(e.key, at, list.length, el.dataset.rovingVertical === '1')
  if (to < 0 || to === at) return
  e.preventDefault()
  e.stopPropagation()
  const next = list[to]
  for (const b of list) b.tabIndex = b === next ? 0 : -1
  next.focus()
  if (next.getAttribute('role') === 'radio' && next.getAttribute('aria-disabled') !== 'true' && !chosen(next)) next.click()
}
// the stop follows the focus out of the row: back on the chosen item
function onFocusOut(e: FocusEvent) {
  const el = e.currentTarget as HTMLElement
  if (!el.contains(e.relatedTarget as Node | null)) queueMicrotask(() => sync(el))
}

const roving: Directive<HTMLElement> = {
  mounted(el, binding) {
    if (binding.modifiers.vertical) el.dataset.rovingVertical = '1'
    if (binding.modifiers.main) el.dataset.keytabs = 'main'
    el.addEventListener('keydown', onKey)
    el.addEventListener('focusout', onFocusOut)
    sync(el)
  },
  updated(el) { sync(el) },
  unmounted(el) {
    el.removeEventListener('keydown', onKey)
    el.removeEventListener('focusout', onFocusOut)
  },
  // rendered on the server as it is: the tab stops are set in the browser
  getSSRProps: () => ({}),
}

export default defineNuxtPlugin((nuxtApp) => {
  nuxtApp.vueApp.directive('roving', roving)
})
