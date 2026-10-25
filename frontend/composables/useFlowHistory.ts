// The editor's history: undo / redo.
//
// By SNAPSHOTS and not by commands: a state is the same string that is saved
// (`serializeCanvas`), and a step is born by itself every time that string changes and
// then stays still for a moment. So there is no list of «undoable» actions to keep
// updated: dragging, connecting, changing a parameter, reordering, and whatever the
// editor learns to do tomorrow, all go through here without anyone remembering it.
//
// The pause (`settleMs`) is what makes a drag ONE step and not a hundred, and a word typed
// in a field one step and not one per letter.
//
// Three things must NOT become steps, and the same mechanism holds for all of them —
// «the next still state is adopted as it is» (`settling`):
//   - loading the flow, and the sizes the canvas takes after drawing the nodes (they
//     change the string without anyone having done anything);
//   - the restore made by undo/redo: if it counted as a change it would delete the
//     «redo» road;
//   - the updates that do not come from the user (a source reattached to its new
//     snapshot): undoing them would bring back a key that is no longer there.

export interface FlowHistoryOptions {
  /** The canvas's state, in the form it is saved in. */
  serialize: () => string
  /** Puts the canvas back in a state taken from `serialize`. */
  restore: (snapshot: string) => void
  /** Called when what can be undone or redone changes. */
  onChange?: () => void
  /** How many steps are kept. */
  limit?: number
  /** How long the state must stay still for the step to close (ms). */
  settleMs?: number
  // for the tests: a fake clock
  schedule?: (fn: () => void, ms: number) => unknown
  cancel?: (handle: unknown) => void
}

export function createFlowHistory(opts: FlowHistoryOptions) {
  const limit = opts.limit ?? 100
  const settleMs = opts.settleMs ?? 350
  const schedule = opts.schedule ?? ((fn, ms) => setTimeout(fn, ms))
  const cancel = opts.cancel ?? ((h) => clearTimeout(h as ReturnType<typeof setTimeout>))

  const past: string[] = []
  const future: string[] = []
  let present: string | null = null
  let settling = true
  let timer: unknown = null

  function arm() {
    if (timer !== null) cancel(timer)
    timer = schedule(settle, settleMs)
  }

  // The state is still: either it is adopted as it is, or it becomes a step.
  function settle() {
    timer = null
    const now = opts.serialize()
    if (settling || present === null) {
      present = now
      settling = false
    } else if (now !== present) {
      past.push(present)
      if (past.length > limit) past.shift()
      present = now
      future.length = 0
    }
    opts.onChange?.()
  }

  /** Closes the pending step at once, if any. */
  function flush() {
    if (timer === null) return
    cancel(timer)
    settle()
  }

  function step(from: string[], to: string[]): boolean {
    flush() // a change still «hot» is a step of its own: it must be closed before moving
    const target = from.pop()
    if (target === undefined || present === null) return false
    to.push(present)
    present = target
    settling = true
    opts.restore(target)
    arm() // what the canvas adjusts after the restore is adopted, not recorded
    opts.onChange?.()
    return true
  }

  return {
    /** The state may have changed (whoever watches the canvas calls it). */
    touch: arm,
    undo: () => step(past, future),
    redo: () => step(future, past),
    /** A new flow or one just loaded: it starts again from here, without steps. */
    reset() {
      past.length = 0
      future.length = 0
      present = null
      settling = true
      arm()
      opts.onChange?.()
    },
    /** Runs a change that does NOT come from the user: it does not become a step. */
    quietly(change: () => void) {
      flush() // what the user was doing stays a step of theirs
      change()
      settling = true
      arm()
    },
    flush,
    /** The editor closes: no step must close any more. */
    dispose() {
      if (timer !== null) cancel(timer)
      timer = null
    },
    canUndo: () => past.length > 0,
    canRedo: () => future.length > 0,
    /** The last still state (null until the first has settled). */
    current: () => present,
  }
}

export type FlowHistory = ReturnType<typeof createFlowHistory>
