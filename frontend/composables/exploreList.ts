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
