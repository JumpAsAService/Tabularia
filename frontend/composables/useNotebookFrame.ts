// The address of a notebook's frame (docs/specs/notebooks.md, «An origin of their own»).
// marimo shows whatever HTML a notebook's code writes, so the app and the editor are served
// from an address of their own (NOTEBOOKS__ORIGIN), where that code cannot use the session
// of whoever looks. The page asks the gateway for a ticket each time it shows the frame: the
// frame's address carries it, the gateway uses it up once, within a minute, and the frame
// goes on with a cookie of its own for that notebook alone.
//
// On an installation that serves them on the interface's own address
// (NOTEBOOKS__ALLOW_SAME_ORIGIN, one machine only) there is no ticket: `sameOrigin` says so,
// and the page warns.
import { ref } from 'vue'
import { errMessage } from '~/composables/useApi'
import { useNotebooks, type NotebookMode } from '~/composables/useNotebooks'

export function useNotebookFrame(mode: NotebookMode) {
  const api = useNotebooks()
  /** the iframe's address; empty while it is asked for, or when it could not be */
  const src = ref('')
  const sameOrigin = ref(false)
  /** why the frame cannot open, in the gateway's words */
  const problem = ref('')
  let generation = 0

  async function open(id: number) {
    const mine = ++generation
    src.value = ''
    problem.value = ''
    try {
      const ticket = await api.ticket(id, mode)
      if (mine !== generation) return
      sameOrigin.value = ticket.same_origin
      src.value = ticket.url ?? api.frameUrl(id, mode)
    } catch (e) {
      if (mine === generation) problem.value = errMessage(e)
    }
  }

  /** forgets the frame: a ticket asked meanwhile is dropped */
  function leave() {
    generation++
    src.value = ''
    problem.value = ''
  }

  return { src, sameOrigin, problem, open, leave }
}
