// `v-label-fields` sul contenitore di un modulo (la shell dell'app, la scheda di
// un dialogo): ogni <label> che precede il suo campo — lo schema di tutti i
// moduli dell'app, `<label>Nome</label><input>` — diventa un'etichetta VERA del
// campo. Prima erano testo accanto al campo: per lo screen reader Username,
// Password e Database del dialogo connessione non avevano nome (audit 2026-10-11,
// F5: 89 etichette così). I campi nativi ricevono `for`/`id`; il trigger del
// Select (un bottone) riceve `aria-labelledby`. Etichette già collegate (`for`,
// o con il campo dentro) e campi con un nome loro (`aria-label`) non si toccano.
// Un MutationObserver ricollega i campi che compaiono dopo (v-if, dialoghi).
import type { Directive } from 'vue'

const CAMPO = 'input:not([type=hidden]):not([type=checkbox]):not([type=radio]), select, textarea, .sel-trigger'
let progressivo = 0

function campoDopo(label: HTMLLabelElement): HTMLElement | null {
  for (let s = label.nextElementSibling; s; s = s.nextElementSibling) {
    if (s.tagName === 'LABEL') return null // il campo successivo ha la sua etichetta
    if (s.matches(CAMPO)) return s as HTMLElement
    const dentro = s.querySelector<HTMLElement>(CAMPO)
    if (dentro) return dentro
  }
  return null
}

function collega(radice: HTMLElement) {
  for (const label of radice.querySelectorAll<HTMLLabelElement>('label')) {
    if (label.htmlFor || label.querySelector('input, select, textarea, button')) continue
    const campo = campoDopo(label)
    if (!campo || campo.hasAttribute('aria-label') || campo.hasAttribute('aria-labelledby')) continue
    if (campo.classList.contains('sel-trigger')) {
      if (!label.id) label.id = `lbl-${++progressivo}`
      campo.setAttribute('aria-labelledby', label.id)
    } else {
      if (!campo.id) campo.id = `fld-${++progressivo}`
      label.htmlFor = campo.id
    }
  }
}

const osservatori = new WeakMap<HTMLElement, MutationObserver>()

const labelFields: Directive<HTMLElement> = {
  getSSRProps: () => ({}), // si collega nel browser: lo screen reader legge il DOM vivo
  mounted(el) {
    collega(el)
    let pendente = false
    const mo = new MutationObserver(() => {
      if (pendente) return
      pendente = true
      requestAnimationFrame(() => { pendente = false; collega(el) })
    })
    mo.observe(el, { childList: true, subtree: true })
    osservatori.set(el, mo)
  },
  unmounted(el) {
    osservatori.get(el)?.disconnect()
    osservatori.delete(el)
  },
}

export default defineNuxtPlugin((nuxtApp) => {
  nuxtApp.vueApp.directive('label-fields', labelFields)
})
