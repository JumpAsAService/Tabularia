import { computed } from 'vue'
import { nodeRowsPhrase, type RowCount } from '~/composables/previewGrid'
import { useFormat } from '~/composables/useFormat'

// A node's rows and columns, from the last preview that touched it: the editor provides
// them (provide 'nodeStats'), no extra query. The labels are translated already; `rows`
// is missing when only the columns are known.
export type CacheState = 'hit' | 'pending' | 'skipped' | 'off'
// `count`: the step's real row count, asked after the preview (POST /tasks/count)
export type NodeStats = {
  rows?: number; truncated?: boolean; cols: number; cacheState?: CacheState | null; cacheCap?: number | null
  count?: RowCount
}
export type NodeStatsLabels = {
  rows: string | null
  cols: string
  title: string
  // the upstream step exceeds the cache's cap: tooltip for the node and line for the panel
  noCache: string | null
  cacheHint: string | null
}

export function useNodeStats(
  id: () => string,
  inject: <T>(key: string, fallback: T) => T,
  t: (k: string, p?: any) => string,
) {
  const all = inject<Record<string, NodeStats>>('nodeStats', {})
  const fmt = useFormat()
  return computed<NodeStatsLabels | null>(() => {
    const s = all[id()]
    if (!s) return null
    // the same words as the grid's summary: «764 rows», «more than 10,000,000 rows», «first 100 rows»
    const rows = s.rows == null
      ? null
      : nodeRowsPhrase(s.rows, !!s.truncated, s.count ?? { state: 'none' }, t, (n) => fmt.count(n))
    const skipped = s.cacheState === 'skipped'
    const cap = (s.cacheCap ?? 0).toLocaleString()
    return {
      rows,
      cols: t('nodeStats.cols', { n: s.cols }),
      title: t('nodeStats.hint'),
      noCache: skipped ? t('nodeStats.noCache', { n: cap }) : null,
      cacheHint: skipped ? t('nodePanel.cacheSkipped', { n: cap }) : null,
    }
  })
}
