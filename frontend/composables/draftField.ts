import type { Directive } from 'vue'

type Draft = HTMLInputElement | HTMLTextAreaElement
type Kept = { value: string; start: number | null; end: number | null }
const kept = new WeakMap<Draft, Kept>()

function selection(el: Draft): [number | null, number | null] {
  try {
    return [el.selectionStart, el.selectionEnd]
  } catch {
    return [null, null] // a number field has no selection
  }
}

/**
 * `v-draft="<the same expression as :value>"` on a field bound with `:value` and saved on
 * `change`: what is being typed survives a redraw of its component.
 *
 * Vue writes a bound `value` back into the field at every redraw of the component, and a
 * field saved on `change` keeps what is typed only in the page until it is left. A panel
 * that redraws while someone types (the «Running… 3 s» of a run ticks every second) wrote
 * the saved value over the typing, and the text vanished under the cursor. While the field
 * has the focus, the directive puts back what was typed, and the cursor, after a redraw
 * that did not change the saved value; when the saved value changed (Enter committed it,
 * an undo, another node selected) it wins, and without the focus the directive does
 * nothing.
 */
export const vDraft: Directive<Draft, unknown> = {
  beforeUpdate(el) {
    if (document.activeElement !== el) return
    const [start, end] = selection(el)
    kept.set(el, { value: el.value, start, end })
  },
  updated(el, binding) {
    const k = kept.get(el)
    kept.delete(el)
    // the saved value itself changed (Enter committed it, an undo, another node): it wins
    if (binding.value !== binding.oldValue) return
    if (!k || document.activeElement !== el || el.value === k.value) return
    el.value = k.value
    if (k.start !== null && k.end !== null) {
      try {
        el.setSelectionRange(k.start, k.end)
      } catch { /* a number field */ }
    }
  },
}
