// When the editor asks a step's column profile (components/FlowEditor.vue). By itself it asks,
// after the count, only what the parquet footer says (no query); the whole profile is asked
// when a column is opened. A column can be opened before that: while the count still runs
// (seconds on a big table), when nothing is asked yet. The opening is then remembered, and the
// step's first request is the whole profile; before, the opening was lost, the footer's answer
// (nothing, after a filter) came, and the open column said «No profile for this column» until
// it was closed and opened again (found on kind, 2026-11-10).

export type ProfileStart = 'known' | 'whole' | 'footer'
export type ProfileOpen = 'remember' | 'ask' | 'nothing'

/** The step's first request, once its count is done: what is remembered for it, the whole
 *  profile when a column was opened meanwhile (a footer remembered does not stand for it), or
 *  the footer only. */
export function profileStart(known: { partial?: boolean } | null | undefined, wholeWanted: boolean): ProfileStart {
  if (known && !(wholeWanted && known.partial)) return 'known'
  return wholeWanted ? 'whole' : 'footer'
}

/** A column opened: remember it when the step's profile is not asked yet, ask the whole
 *  profile when only the footer's (or nothing) is on screen and the whole is not asked yet. */
export function profileOpen(asked: boolean, shown: { partial?: boolean } | null | undefined, wholeAskedAlready: boolean): ProfileOpen {
  if (shown && !shown.partial) return 'nothing'
  if (!asked) return 'remember'
  return wholeAskedAlready ? 'nothing' : 'ask'
}
