// Consenso informato all'uso dell'assistente: si dichiara che dall'altra parte
// c'è un sistema automatico, e finché non lo si è letto la chat non si usa
// (AI Act, art. 50 — obbligo di trasparenza verso chi interagisce).
//
// Si ricorda nel BROWSER, non sull'account. Su un'installazione aperta l'ospite
// è condiviso: l'accettazione di un visitatore non può valere per il successivo,
// perché la persona da informare è chi guarda lo schermo, non la riga nella
// tabella utenti. È anche il motivo per cui non passa dal server.
import { ref } from 'vue'

const CHIAVE = 'tabularia.aiDisclosure'
// Da alzare quando il testo cambia nella SOSTANZA (non per una virgola): chi
// aveva accettato la versione precedente se lo vede richiedere, ed è giusto —
// ha acconsentito a un'altra cosa.
const VERSIONE = '1'

// `null` = non ancora saputo (siamo sul server, o il mount non è avvenuto).
// Serve a non far lampeggiare il dialogo a chi ha già accettato.
const accettata = ref<boolean | null>(null)

export function useAiDisclosure() {
  function leggi() {
    if (!import.meta.client) return
    try {
      accettata.value = localStorage.getItem(CHIAVE) === VERSIONE
    } catch {
      // niente storage (finestra privata, cookie bloccati): si chiede ogni volta.
      // Meglio insistere che dare per accettato ciò che non risulta.
      accettata.value = false
    }
  }

  function accetta() {
    accettata.value = true
    try {
      localStorage.setItem(CHIAVE, VERSIONE)
    } catch {
      /* non poterlo ricordare non impedisce di proseguire adesso */
    }
  }

  return { accettata, leggi, accetta }
}
