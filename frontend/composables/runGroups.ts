// Runs, as the Runs page lists them: a schedule that fails every five minutes for the same
// reason is ONE thing to repair, not nine rows; and a failed run leads to the object to
// repair. Both from the page of rows already loaded: nothing more is asked of the server.

export interface GroupableRun {
  id: number
  kind?: string
  status: string
  error: string | null
  flow_id: number | null
  flow_group_id?: number | null
  sql_script_id?: number | null
  saved_script_id?: number | null
  datasource_id: number | null
  flow_name?: string | null
  source_name?: string | null
  group_name?: string | null
  sql_script_name?: string | null
  script_name?: string | null
  script?: string | null
  email?: string | null
}

export interface RunGroup<R> {
  /** the newest run of the group (the list comes newest first): the row shown */
  head: R
  runs: R[]
}

// what a run ran, as far as the row says: two runs of the same thing have the same source
function sourceOf(r: GroupableRun): string {
  // a Script or Container node's run, a Pushmail cell's: the node or the cell is part of it
  const part = r.kind === 'script' || r.kind === 'container' ? r.script ?? '' : r.kind === 'pushmail' ? r.email ?? '' : ''
  let label = ''
  if (part) {
    try {
      const p = JSON.parse(part)
      label = String(p.label ?? p.cell ?? '')
    } catch { /* an unreadable record: the run stands alone on its kind and ids */ }
  }
  return JSON.stringify([
    r.kind ?? '', r.flow_id, r.flow_group_id ?? null, r.sql_script_id ?? null, r.saved_script_id ?? null,
    r.kind === 'ingest' ? r.datasource_id : null,
    r.flow_name ?? r.sql_script_name ?? r.source_name ?? r.group_name ?? '', label,
  ])
}

/**
 * Rows that FOLLOW one another, failed, with the same source and the same error, as one
 * group. Only failures (a success has no error to share), only neighbours (a success in
 * between is news: the thing worked for a while), and never across what the page did not
 * load. A run with no error text stands alone.
 */
export function groupRuns<R extends GroupableRun>(rows: R[]): RunGroup<R>[] {
  const groups: RunGroup<R>[] = []
  let key = ''
  for (const r of rows) {
    const k = r.status === 'FAILURE' && r.error ? `${sourceOf(r)}\u0001${r.error}` : ''
    const last = groups[groups.length - 1]
    if (k && last && k === key) last.runs.push(r)
    else groups.push({ head: r, runs: [r] })
    key = k
  }
  return groups
}

export interface RunOpens {
  /** which words the link carries: runs.open_<what> */
  what: 'flow' | 'group' | 'sqlScript' | 'script' | 'datasource'
  to: string
}

/**
 * The object a run leads to, when the row names one that still exists: its id and its
 * name both come with the row (a deleted flow leaves its runs without either). The Runs
 * page lists only the runs of folders the person can read, which is what opening takes.
 */
export function runOpens(r: GroupableRun): RunOpens | null {
  if (r.kind === 'sql') return r.sql_script_id && r.sql_script_name ? { what: 'sqlScript', to: `/sql-script?id=${r.sql_script_id}` } : null
  if (r.kind === 'ingest') return r.datasource_id && r.source_name ? { what: 'datasource', to: `/datasources?ds=${r.datasource_id}` } : null
  // a script of the Scripting page run by itself (inside a flow it is the flow's run)
  if (!r.flow_id && r.saved_script_id && r.script_name) return { what: 'script', to: `/script?id=${r.saved_script_id}` }
  if (r.flow_id && r.flow_name) return { what: 'flow', to: `/editor?flow=${r.flow_id}` }
  if (r.flow_group_id && r.group_name) return { what: 'group', to: `/flow-group?id=${r.flow_group_id}` }
  return null
}
