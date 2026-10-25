// `v-label-fields` on a form's container (the app's shell, a dialog's card): every <label>
// preceding its field — the scheme of all the app's forms, `<label>Name</label><input>` —
// becomes a REAL label of the field. Before, they were text next to the field: for the
// screen reader Username, Password and Database of the connection dialog had no name
// (audit 2026-10-11, F5: 89 labels like that). The native fields get `for`/`id`; the
// Select's trigger (a button) gets `aria-labelledby`. Labels already linked (`for`, or
// with the field inside) and fields with a name of their own (`aria-label`) are not
// touched. A MutationObserver links again the fields that appear later (v-if, dialogs).
import type { Directive } from 'vue'

const FIELD = 'input:not([type=hidden]):not([type=checkbox]):not([type=radio]), select, textarea, .sel-trigger'
let progressive = 0

function nextField(label: HTMLLabelElement): HTMLElement | null {
  for (let s = label.nextElementSibling; s; s = s.nextElementSibling) {
    if (s.tagName === 'LABEL') return null // the next field has its label
    if (s.matches(FIELD)) return s as HTMLElement
    const inside = s.querySelector<HTMLElement>(FIELD)
    if (inside) return inside
  }
  return null
}

function link(root: HTMLElement) {
  for (const label of root.querySelectorAll<HTMLLabelElement>('label')) {
    if (label.htmlFor || label.querySelector('input, select, textarea, button')) continue
    const field = nextField(label)
    if (!field || field.hasAttribute('aria-label') || field.hasAttribute('aria-labelledby')) continue
    if (field.classList.contains('sel-trigger')) {
      if (!label.id) label.id = `lbl-${++progressive}`
      field.setAttribute('aria-labelledby', label.id)
    } else {
      if (!field.id) field.id = `fld-${++progressive}`
      label.htmlFor = field.id
    }
  }
}

const observers = new WeakMap<HTMLElement, MutationObserver>()

const labelFields: Directive<HTMLElement> = {
  getSSRProps: () => ({}), // it links in the browser: the screen reader reads the live DOM
  mounted(el) {
    link(el)
    let pending = false
    const mo = new MutationObserver(() => {
      if (pending) return
      pending = true
      requestAnimationFrame(() => { pending = false; link(el) })
    })
    mo.observe(el, { childList: true, subtree: true })
    observers.set(el, mo)
  },
  unmounted(el) {
    observers.get(el)?.disconnect()
    observers.delete(el)
  },
}

export default defineNuxtPlugin((nuxtApp) => {
  nuxtApp.vueApp.directive('label-fields', labelFields)
})
