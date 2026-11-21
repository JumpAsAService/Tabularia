// Which keys of the flow editor are the canvas's own. Delete, Backspace (the key a Mac calls «delete» and a ThinkPad uses to
// erase) and Ctrl/Cmd+X remove the selected nodes: they do so only with the focus on the canvas or on nothing, never with it on
// a button, a Select's trigger, a checkbox or a chip of the node's panel, of the toolbar or of the preview.

/** Whether the element that has the focus is the canvas, something inside it, or nothing (the page's body). */
export function focusOnCanvas(active: Element | null | undefined, canvas: Element | null | undefined, body?: Element | null): boolean {
  if (!active || active === body || active.tagName === 'BODY' || active.tagName === 'HTML') return true
  return !!canvas && (active === canvas || canvas.contains(active))
}

/** Gives the focus to a node of the canvas: a node reached from the keyboard, or just added (from the palette, dropped), so
 *  that Delete and Backspace act on it (focusOnCanvas) without a click on it first. Whether it has it: a node Vue Flow has
 *  just added is drawn hidden until it is measured, and a hidden element takes no focus. */
export function focusNodeOf(canvas: Element | null | undefined, id: string): boolean {
  const el = canvas?.querySelector(`.vue-flow__node[data-id="${CSS.escape(id)}"]`) as HTMLElement | null
  el?.focus({ preventScroll: true })
  return !!el && el.ownerDocument.activeElement === el
}
