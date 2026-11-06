// Explore's list (components/ProjectBrowser.vue): the decisions that do not need Vue, kept
// here so they are tested without a browser (tests/explore-list.test.mjs).

/**
 * What the list area shows. The folder tree and the open folder's lists arrive separately:
 * until BOTH are in, an empty list is not news, it is a list still on its way (before, the
 * top level said «No folders yet» for the moment the tree took to arrive).
 */
export type ListState = 'loading' | 'empty' | 'nomatch' | 'rows'

export function listState(o: { treeLoaded: boolean; folderLoading: boolean; total: number; visible: number }): ListState {
  if (!o.treeLoaded || o.folderLoading) return 'loading'
  if (!o.total) return 'empty'
  return o.visible ? 'rows' : 'nomatch'
}

/**
 * A row's commands, in ONE order for every kind (only what applies is shown): first what
 * belongs to the kind, then what every object has, Delete last and apart. The menu («⋯»,
 * right click, Shift+F10) shows them all; the row itself keeps at most its kind's main one.
 */
export type RowAction = 'history' | 'refresh' | 'contract' | 'editConnection' | 'rename' | 'duplicate' | 'move' | 'delete'
export const ROW_ACTION_ORDER: readonly RowAction[] = ['history', 'refresh', 'contract', 'editConnection', 'rename', 'duplicate', 'move', 'delete']
const GROUP_OF: Record<RowAction, number> = { history: 0, refresh: 0, contract: 0, editConnection: 0, rename: 1, duplicate: 1, move: 1, delete: 2 }

export interface RowFacts {
  kind: string
  /** a flow this person cannot open: no command at all */
  locked?: boolean
  /** a personal folder its person may not move nor delete */
  fixedFolder?: boolean
  /** a datasource imported from a database (its snapshot can be refreshed) */
  dbDatasource?: boolean
}

function applies(a: RowAction, r: RowFacts): boolean {
  switch (a) {
    case 'history': return r.kind === 'flow'
    case 'refresh': return r.kind === 'datasource' && !!r.dbDatasource
    case 'contract': return r.kind === 'datasource'
    case 'editConnection': return r.kind === 'connection'
    case 'rename': return r.kind !== 'connection'
    case 'duplicate': return r.kind !== 'folder'
    case 'move': return !(r.kind === 'folder' && r.fixedFolder)
    case 'delete': return !(r.kind === 'folder' && r.fixedFolder)
  }
}

/** The commands of a row, in order, each with whether a separator goes above it. */
export function rowActions(r: RowFacts): { id: RowAction; sep: boolean }[] {
  if (r.locked) return []
  const out: { id: RowAction; sep: boolean }[] = []
  for (const a of ROW_ACTION_ORDER) {
    if (!applies(a, r)) continue
    const prev = out[out.length - 1]
    out.push({ id: a, sep: !!prev && GROUP_OF[prev.id] !== GROUP_OF[a] })
  }
  return out
}

/** The one command the row shows by itself, beside «⋯»: its kind's main one, if any. */
export function primaryAction(r: RowFacts): RowAction | null {
  if (r.locked) return null
  if (r.kind === 'flow') return 'history'
  if (r.kind === 'datasource' && r.dbDatasource) return 'refresh'
  return null
}

/**
 * Explore's keys, outside the text fields: `/` the filter, ↑ ↓ the active row, Enter opens
 * it, Backspace or Alt+↑ the parent folder, F2 renames, Shift+F10 or the Menu key its
 * commands. With Ctrl or ⌘ the browser's own shortcuts win; Enter on a button or a link is
 * the control's own (it opens the same thing).
 */
export type KeyCommand = 'filter' | 'next' | 'prev' | 'open' | 'up' | 'rename' | 'menu'

export function keyCommand(k: {
  key: string
  altKey?: boolean
  ctrlKey?: boolean
  metaKey?: boolean
  shiftKey?: boolean
  /** the focus is in a text field (input, textarea, select, contenteditable) */
  inField?: boolean
  /** the focus is on a button or a link, which answers Enter by itself */
  onControl?: boolean
}): KeyCommand | null {
  if (k.inField || k.ctrlKey || k.metaKey) return null
  if (k.key === 'ArrowUp' && k.altKey) return 'up'
  if (k.altKey) return null
  switch (k.key) {
    case '/': return 'filter'
    case 'ArrowDown': return 'next'
    case 'ArrowUp': return 'prev'
    case 'Enter': return k.onControl ? null : 'open'
    case 'Backspace': return 'up'
    case 'F2': return 'rename'
    case 'F10': return k.shiftKey ? 'menu' : null
    case 'ContextMenu': return 'menu'
  }
  return null
}

/** The row index after ↑ or ↓ from `at` (-1 = none yet), held at the ends. */
export function stepRow(at: number, count: number, dir: 1 | -1): number {
  if (!count) return -1
  if (at < 0) return dir > 0 ? 0 : count - 1
  return Math.min(count - 1, Math.max(0, at + dir))
}

/**
 * What uses an object, from the lineage graph around it (GET /lineage, one hop): who reads
 * it or imports from it (its edges out, `read` and `ingest`), and who runs or refreshes it
 * (its edges in, `orchestrate` and `refresh`). What it publishes or writes does not depend
 * on it. Counted by kind; the nodes this person may not open count too (they would break
 * just the same). Only the kinds the graph tracks: flows, datasources, connections, scripts,
 * SQL scripts, notebooks and flow groups — not reports, dashboards nor views.
 */
export const DEPENDENT_TYPES = ['flow', 'flow_group', 'datasource', 'sql_script', 'script', 'notebook'] as const
export type DependentType = typeof DEPENDENT_TYPES[number]

export function dependentsOf(
  graph: { edges: { source: string; target: string; kind: string }[]; nodes: { id: string; type: string }[] },
  center: string,
): Partial<Record<DependentType, number>> {
  const typeOf = new Map(graph.nodes.map((n) => [n.id, n.type]))
  const seen = new Set<string>()
  for (const e of graph.edges) {
    if (e.source === center && (e.kind === 'read' || e.kind === 'ingest')) seen.add(e.target)
    if (e.target === center && (e.kind === 'orchestrate' || e.kind === 'refresh')) seen.add(e.source)
  }
  seen.delete(center)
  const out: Partial<Record<DependentType, number>> = {}
  for (const id of seen) {
    const t = typeOf.get(id) as DependentType | undefined
    if (t && (DEPENDENT_TYPES as readonly string[]).includes(t)) out[t] = (out[t] ?? 0) + 1
  }
  return out
}

/** The lineage graph's name for an Explore kind; null for the kinds it does not track. */
export function lineageType(kind: string): 'flow' | 'datasource' | 'connection' | 'script' | 'sql' | 'notebook' | 'group' | null {
  return (['flow', 'datasource', 'connection', 'script', 'sql', 'notebook', 'group'] as const).find((k) => k === kind) ?? null
}
