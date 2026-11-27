// Edges whose source node sits in the step cache show data "passing" (assets/main.css,
// `.edge-cached`). Pure helpers, so the rule is testable without the canvas.

export const EDGE_CACHED_CLASS = 'edge-cached'

export interface EdgeCacheFlags {
  /** a sequence (orchestration) edge: never marked */
  seq?: boolean
  /** the engine is working along this edge: that look wins */
  animated?: boolean
}

/** Whether a data edge leaving `sourceId` shows cached data passing. */
export function edgeCacheClass(
  sourceId: string,
  cachedNodes: { has(id: string): boolean },
  flags: EdgeCacheFlags = {},
): string {
  if (flags.seq || flags.animated) return ''
  return cachedNodes.has(sourceId) ? EDGE_CACHED_CLASS : ''
}

/** Whether an edge's class (maybe undefined) carries the cache class. */
export const hasEdgeCacheClass = (current: unknown): boolean =>
  typeof current === 'string' && current.split(/\s+/).includes(EDGE_CACHED_CLASS)

/**
 * What the marks depend on, as one string: the edges (which, from where, worked along or not, marked
 * or not) and the cached nodes. When it changes the marks are set again. The mark is part of it
 * because edges are rebuilt with the same ids and no class (an undo, a definition applied again).
 */
export function edgeCacheKey(
  edges: readonly { id: string; source: string; animated?: boolean; class?: unknown }[],
  cachedNodes: Iterable<string>,
): string {
  const marks = edges.map((e) => `${e.id}>${e.source}${e.animated ? '*' : ''}${hasEdgeCacheClass(e.class) ? '+' : ''}`).join('|')
  return `${marks}#${[...cachedNodes].sort().join(',')}`
}

/** `current` (an edge's class, maybe undefined) with the cache class set or removed. */
export function withEdgeCacheClass(current: unknown, on: boolean): string | undefined {
  const rest = (typeof current === 'string' ? current : '')
    .split(/\s+/)
    .filter((c) => c && c !== EDGE_CACHED_CLASS)
  if (on) rest.push(EDGE_CACHED_CLASS)
  return rest.length ? rest.join(' ') : undefined
}
