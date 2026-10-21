// Ricerca unificata per nome su tutte le risorse: cartelle, flussi, gruppi di
// flussi, datasource, viste salvate, connessioni.
//
// Il gateway applica i permessi per TIPO (le cartelle si vedono se navigabili, i
// contenuti se leggibili, le connessioni solo con CONNECT), quindi qui non serve
// alcun filtro: ciò che torna è già ciò che l'utente può vedere.
//
// `counts` descrive TUTTO ciò che combacia, non la pagina e nemmeno il tipo
// filtrato: sono i numeri da mettere nel selettore per tipo, che altrimenti
// cambierebbero a ogni clic.

export type SearchKind = 'folder' | 'flow' | 'group' | 'datasource' | 'view' | 'connection'

export interface SearchHit {
  kind: SearchKind
  id: number
  name: string
  /** dove si trova: per una cartella è quella che la contiene */
  project_id: number | null
  project_name: string | null
  detail: string
  /** datasource: its row count */
  rows?: number | null
  /** a flow this person cannot open (images or scripts not granted to them): shown
   *  greyed out with the reason, and not opened */
  locked_reason?: string | null
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
    case 'group': return `/flow-group?id=${hit.id}`
    case 'view': return `/viewer?view=${hit.id}`
    // col nome come ricerca: la lista è paginata, e la voce può stare oltre la prima pagina
    case 'datasource': return `/datasources?q=${encodeURIComponent(hit.name)}&ds=${hit.id}`
    case 'connection': return `/connections?q=${encodeURIComponent(hit.name)}&conn=${hit.id}`
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
