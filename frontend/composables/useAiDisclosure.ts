// Informed consent to using the assistant: it is declared that on the other side there
// is an automated system, and until one has read it the chat cannot be used (AI Act,
// art. 50 — the transparency obligation towards whoever interacts).
//
// It is remembered in the BROWSER, not on the account. On an open installation the guest
// is shared: one visitor's acceptance cannot hold for the next, because the person to
// inform is whoever looks at the screen, not the row in the users' table. It is also why
// it does not go through the server.
import { ref } from 'vue'

const KEY = 'tabularia.aiDisclosure'
// To raise when the text changes in SUBSTANCE (not for a comma): whoever had accepted the
// earlier version sees it asked again, and rightly — they consented to something else.
const VERSION = '1'
// The dialog says something else in metadata-only mode: each text is accepted on its own, so
// that whoever read «no value of your data reaches the model» reads the other one when
// an administrator turns the mode off, and the other way round.
const versionFor = (metadataOnly: boolean) => (metadataOnly ? `${VERSION}-metadata` : VERSION)

// `null` = not known yet (we are on the server, or the mount has not happened). It serves
// to keep the dialog from flashing to whoever has accepted already.
const accepted = ref<boolean | null>(null)
let current = VERSION

export function useAiDisclosure() {
  function read(metadataOnly = false) {
    if (!import.meta.client) return
    current = versionFor(metadataOnly)
    try {
      accepted.value = localStorage.getItem(KEY) === current
    } catch {
      // no storage (private window, cookies blocked): it asks every time. Better to insist than
      // to take as accepted what does not show.
      accepted.value = false
    }
  }

  function accept() {
    accepted.value = true
    try {
      localStorage.setItem(KEY, current)
    } catch {
      /* not being able to remember it does not prevent going on now */
    }
  }

  return { accepted, read, accept }
}
