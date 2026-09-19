import { computed } from 'vue'

// Righe e colonne di un nodo, dall'ultima anteprima che l'ha toccato: le
// fornisce l'editor (provide 'nodeStats'), nessuna query in piu'. Le etichette
// sono gia' tradotte; `rows` manca quando si conoscono solo le colonne.
export type CacheState = 'hit' | 'pending' | 'skipped' | 'off'
export type NodeStats = { rows?: number; truncated?: boolean; cols: number; cacheState?: CacheState | null; cacheCap?: number | null }
export type NodeStatsLabels = {
  rows: string | null
  cols: string
  title: string
  // il passo a monte supera il tetto della cache: tooltip per il nodo e riga per il pannello
  noCache: string | null
  cacheHint: string | null
}

export function useNodeStats(
  id: () => string,
  inject: <T>(key: string, fallback: T) => T,
  t: (k: string, p?: any) => string,
) {
  const all = inject<Record<string, NodeStats>>('nodeStats', {})
  return computed<NodeStatsLabels | null>(() => {
    const s = all[id()]
    if (!s) return null
    const rows = s.rows == null
      ? null
      : t(s.truncated ? 'nodeStats.rowsMore' : 'nodeStats.rows', { n: s.rows.toLocaleString() })
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
