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
