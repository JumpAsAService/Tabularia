// The flow editor is one per flow. Vue reuses a page when only its query changes, so
// another flow opened from the editor (the search, a link) used to leave the old one on
// screen under the new address. A new flow taking its address when first saved
// (`?flow=<id>`) is still the same flow, and keeps its editor.
type Query = Record<string, unknown>

const one = (v: unknown) => (Array.isArray(v) ? v[0] : v) ?? null
const text = (v: unknown) => (one(v) == null ? '' : String(one(v)))

export function editorKeyOf(q: Query): string {
  return text(q.flow) ? `flow:${text(q.flow)}` : `new:${text(q.engine)}:${text(q.project)}`
}

/** Whether going from `from` to `to` shows a flow other than `shownFlowId`, the one open. */
export function opensAnotherFlow(to: Query, from: Query, shownFlowId: number | null): boolean {
  if (text(to.flow)) return Number(text(to.flow)) !== shownFlowId
  return !!text(from.flow) || text(to.engine) !== text(from.engine) || text(to.project) !== text(from.project)
}
