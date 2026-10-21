// Accessibilità dei dialoghi modali, in un punto solo.
//
// I quattro dialoghi (connessione, run, datasource DB, schedulazione) sono
// strutturalmente identici e avevano tutti lo stesso problema: `@keydown.esc`
// era agganciato a un <div> senza tabindex, e un div non focalizzabile non
// riceve eventi da tastiera — quindi Esc non chiudeva nulla, a meno che il
// fuoco fosse già dentro un campo del dialogo.
//
// Qui: fuoco iniziale sulla card (così lo screen reader annuncia il titolo),
// Esc catturato a livello di documento, Tab confinato dentro il dialogo, e
// fuoco restituito a chi lo ha aperto alla chiusura.
import { nextTick, onBeforeUnmount, watch, type Ref } from 'vue'

const FOCUSABILI = [
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
  aperto: () => boolean,
  chiudi: () => void,
) {
  // chi aveva il fuoco prima dell'apertura: ci torna alla chiusura
  let origine: HTMLElement | null = null
  // where the dialog that held `origine` sends its own focus back, if `origine` is gone by then
  let fallback: HTMLElement | null = null
  const self = { card, returnTo: () => (origine?.isConnected ? origine : fallback) }

  function elencoFocusabili(): HTMLElement[] {
    if (!card.value) return []
    // offsetParent null = elemento nascosto (v-if/v-show a monte)
    return Array.from(card.value.querySelectorAll<HTMLElement>(FOCUSABILI)).filter(
      (el) => el.offsetParent !== null,
    )
  }

  function suTasto(e: KeyboardEvent) {
    if (!aperto()) return
    // a code editor uses Esc (its suggestions, its find bar) and Tab (indenting) itself
    if ((e.target as HTMLElement | null)?.closest?.('[data-own-escape]')) return

    if (e.key === 'Escape') {
      // un Select (o altro elemento a comparsa) aperto dentro il dialogo si chiude
      // da sé: chiudere qui tutto il dialogo buttava via quello che si era scritto
      if ((e.target as HTMLElement | null)?.closest?.('[aria-haspopup][aria-expanded="true"], [role="combobox"][aria-expanded="true"]')) return
      e.preventDefault()
      e.stopPropagation()
      chiudi()
      return
    }

    if (e.key !== 'Tab') return

    const els = elencoFocusabili()
    if (!els.length) {
      // niente da mettere a fuoco: il Tab non deve uscire dal dialogo
      e.preventDefault()
      card.value?.focus()
      return
    }

    const primo = els[0]
    const ultimo = els[els.length - 1]
    const attivo = document.activeElement as HTMLElement | null

    // il ciclo si chiude su se stesso: dal fondo si torna in cima e viceversa
    if (e.shiftKey && (attivo === primo || attivo === card.value)) {
      e.preventDefault()
      ultimo.focus()
    } else if (!e.shiftKey && attivo === ultimo) {
      e.preventDefault()
      primo.focus()
    }
  }

  watch(aperto, async (ora) => {
    if (typeof document === 'undefined') return

    if (ora) {
      origine = document.activeElement as HTMLElement | null
      const host = Array.from(openDialogs).find((d) => !!origine && !!d.card.value?.contains(origine))
      fallback = host?.returnTo() ?? null
      openDialogs.add(self)
      // in cattura: il dialogo vede Esc prima di qualunque handler sottostante
      document.addEventListener('keydown', suTasto, true)
      await nextTick()
      // sulla card, non sul primo campo: l'apertura annuncia il dialogo
      // invece di scaraventare l'utente dentro un input
      card.value?.focus()
    } else {
      openDialogs.delete(self)
      document.removeEventListener('keydown', suTasto, true)
      self.returnTo()?.focus?.()
      origine = null
      fallback = null
    }
  })

  onBeforeUnmount(() => {
    if (typeof document === 'undefined') return
    openDialogs.delete(self)
    document.removeEventListener('keydown', suTasto, true)
  })
}
