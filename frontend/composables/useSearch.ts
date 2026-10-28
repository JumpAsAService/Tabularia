// Unified search by name over all the resources: folders, flows, flow groups,
// datasources, saved views, scripts, notebooks, reports, connections.
//
// The gateway applies the permissions per KIND (folders are seen when navigable, contents
// when readable, connections only with CONNECT), so here no filter is needed: what comes
// back is what the user can see already.
//
// `counts` describes EVERYTHING that matches, not the page and not even the filtered
// kind: they are the numbers to put in the kind selector, which would otherwise change at
// every click.

export type SearchKind = 'folder' | 'flow' | 'group' | 'datasource' | 'view' | 'script' | 'sql' | 'notebook' | 'report' | 'connection'

export interface SearchHit {
  kind: SearchKind
  id: number
  name: string
  /** where it is: for a folder it is the one containing it */
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

/** Where a result leads when it is opened. The folders stay in the Explore. */
export function searchHitTarget(hit: SearchHit): string {
  switch (hit.kind) {
    case 'flow': return `/editor?flow=${hit.id}`
    case 'group': return `/flow-group?id=${hit.id}`
    case 'view': return `/viewer?view=${hit.id}`
    case 'script': return `/script?id=${hit.id}`
    case 'sql': return `/sql-script?id=${hit.id}`
    case 'notebook': return `/notebook?id=${hit.id}`
    case 'report': return `/report?id=${hit.id}`
    // with the name as search: the list is paginated, and the item may sit past the first page
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
