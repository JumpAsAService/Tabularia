// Un pannello che si apre da un bottone (impostazioni, «New», «Save view»):
// all'apertura il fuoco entra nel pannello, Esc lo chiude e il fuoco torna a chi
// l'ha aperto. Non è modale: il Tab può uscirne, e fuori il pannello si chiude.
// (Prima: nessuno di questi pannelli si chiudeva con Esc, e il fuoco restava sul
// bottone — audit 2026-10-11, F6.)
import { nextTick, onBeforeUnmount, watch, type Ref } from 'vue'

const FOCUSABILI = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'

export function usePopover(aperto: Ref<boolean>, pannello: Ref<HTMLElement | null>, chiudi: () => void) {
  let origine: HTMLElement | null = null

  function suTasto(e: KeyboardEvent) {
    if (e.key !== 'Escape') return
    // un Select aperto dentro il pannello si chiude da sé, prima
    if ((e.target as HTMLElement | null)?.closest?.('[aria-expanded="true"]')) return
    e.preventDefault()
    chiudi()
  }
  function suFuocoFuori(e: FocusEvent) {
    const dove = e.relatedTarget as Node | null
    // un Select teleportato fuori dal pannello resta «dentro» finché è aperto
    if (!dove || pannello.value?.contains(dove) || (dove as HTMLElement).closest?.('.sel-panel')) return
    origine = null // il fuoco è andato altrove di proposito: non lo si riporta indietro
    chiudi()
  }

  watch(aperto, async (ora) => {
    if (typeof document === 'undefined') return
    if (ora) {
      origine = document.activeElement as HTMLElement | null
      document.addEventListener('keydown', suTasto)
      await nextTick()
      pannello.value?.addEventListener('focusout', suFuocoFuori)
      pannello.value?.querySelector<HTMLElement>(FOCUSABILI)?.focus()
    } else {
      document.removeEventListener('keydown', suTasto)
      origine?.focus?.()
      origine = null
    }
  })

  onBeforeUnmount(() => {
    if (typeof document !== 'undefined') document.removeEventListener('keydown', suTasto)
  })
}
