// Ricerca unificata per nome su tutte le risorse: cartelle, flussi, datasource,
// viste salvate, connessioni.
//
// Il gateway applica i permessi per TIPO (le cartelle si vedono se navigabili, i
// contenuti se leggibili, le connessioni solo con CONNECT), quindi qui non serve
// alcun filtro: ciò che torna è già ciò che l'utente può vedere.
//
// `counts` descrive TUTTO ciò che combacia, non la pagina e nemmeno il tipo
// filtrato: sono i numeri da mettere nel selettore per tipo, che altrimenti
// cambierebbero a ogni clic.

export type SearchKind = 'folder' | 'flow' | 'datasource' | 'view' | 'connection'

export interface SearchHit {
  kind: SearchKind
  id: number
  name: string
  /** dove si trova: per una cartella è quella che la contiene */
  project_id: number | null
  project_name: string | null
  detail: string
}

export interface SearchResults {
  items: SearchHit[]
  total: number
  counts: Partial<Record<SearchKind, number>>
}

/** Dove porta un risultato quando lo si apre. Le cartelle restano nell'Explore. */
export function searchHitTarget(hit: SearchHit): string {
  switch (hit.kind) {
    case 'flow': return `/editor?flow=${hit.id}`
    case 'view': return `/viewer?view=${hit.id}`
    case 'datasource': return `/datasources?ds=${hit.id}`
    case 'connection': return `/connections?conn=${hit.id}`
    case 'folder': return `/?folder=${hit.id}`
  }
}

export function useSearch() {
  const { apiFetch } = useApiClient()

  return {
    query: (q: string, opts: { kind?: SearchKind; limit?: number; offset?: number } = {}) => {
      const p = new URLSearchParams({ q })
      if (opts.kind) p.set('kind', opts.kind)
      if (opts.limit != null) p.set('limit', String(opts.limit))
      if (opts.offset != null) p.set('offset', String(opts.offset))
      return apiFetch<SearchResults>(`/search?${p.toString()}`)
    },
  }
}
