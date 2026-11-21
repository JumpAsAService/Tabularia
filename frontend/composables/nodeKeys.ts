// Which keys of the flow editor are the canvas's own. Delete, Backspace (the key a Mac calls «delete» and a ThinkPad uses to
// erase) and Ctrl/Cmd+X remove the selected nodes: they do so only with the focus on the canvas or on nothing, never with it on
// a button, a Select's trigger, a checkbox or a chip of the node's panel, of the toolbar or of the preview.

/** Whether the element that has the focus is the canvas, something inside it, or nothing (the page's body). */
export function focusOnCanvas(active: Element | null | undefined, canvas: Element | null | undefined, body?: Element | null): boolean {
  if (!active || active === body || active.tagName === 'BODY' || active.tagName === 'HTML') return true
  return !!canvas && (active === canvas || canvas.contains(active))
}

/** What Delete and Backspace remove. `boxed`: the nodes of the canvas's own selection (a box drawn with Shift, Ctrl/Cmd+click);
 *  `edges`: its selected edges; `panel`: the node open in the panel. A click on a stroke selects that edge alone: the edge goes,
 *  not the node still open in the panel. A box selects the nodes in it and the edges that touch them: the nodes go, with
 *  their edges, and so does any other edge selected with them (before, only the edges went and the nodes stayed). */
export function removedByDeleteKey(boxed: string[], edges: { id: string; source: string; target: string }[], panel: string | null):
  { nodes: string[]; edges: string[] } {
  if (edges.length) {
    const going = new Set(boxed)
    return { nodes: boxed, edges: edges.filter((e) => !going.has(e.source) && !going.has(e.target)).map((e) => e.id) }
  }
  return { nodes: boxed.length > 1 ? boxed : panel ? [panel] : boxed, edges: [] }
}

/** Gives the focus to a node of the canvas: a node reached from the keyboard, or just added (from the palette, dropped), so
 *  that Delete and Backspace act on it (focusOnCanvas) without a click on it first. Whether it has it: a node Vue Flow has
 *  just added is drawn hidden until it is measured, and a hidden element takes no focus. */
export function focusNodeOf(canvas: Element | null | undefined, id: string): boolean {
  const el = canvas?.querySelector(`.vue-flow__node[data-id="${CSS.escape(id)}"]`) as HTMLElement | null
  el?.focus({ preventScroll: true })
  return !!el && el.ownerDocument.activeElement === el
}
