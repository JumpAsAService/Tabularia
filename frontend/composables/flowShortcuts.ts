// The flow editor's right-click menu (components/ui/FlowQuickMenu.vue): which
// transformations it offers, and where its items sit on the ring. Pure functions, no Nuxt:
// the tests import them as they are (tests/flow-shortcuts.test.mjs).

/** The menu until the person chooses: the eight transformations a flow uses most. */
export const FLOW_SHORTCUTS_DEFAULT: readonly string[] = [
  'filter', 'select', 'join', 'group_by', 'sort', 'compute', 'rename', 'union',
]

/** How many the menu holds (the gateway refuses more). */
export const FLOW_SHORTCUTS_MAX = 8

/** The ring's radius, centre to item centre, and an item's radius (px). */
export const QUICK_MENU_RADIUS = 76
export const QUICK_MENU_ITEM_RADIUS = 20

/** The transformations the menu shows: the saved ones, or the defaults while none were
 *  saved, keeping only what this editor offers (the engine's operations, as the palette;
 *  never `foreach`, a container) and each once. */
export function flowShortcutsShown(saved: readonly string[] | null | undefined, available: readonly string[]): string[] {
  const offered = new Set(available.filter((o) => o !== 'foreach'))
  const out: string[] = []
  for (const op of saved ?? FLOW_SHORTCUTS_DEFAULT) {
    if (offered.has(op) && !out.includes(op)) out.push(op)
  }
  return out.slice(0, FLOW_SHORTCUTS_MAX)
}

/** What is wrong with a choice before it is sent ('' = nothing): the gateway's own rules. */
export function flowShortcutsProblem(list: readonly string[], available: readonly string[]): '' | 'tooMany' | 'twice' | 'unknown' {
  if (list.length > FLOW_SHORTCUTS_MAX) return 'tooMany'
  if (new Set(list).size !== list.length) return 'twice'
  if (list.some((o) => o === 'foreach' || !available.includes(o))) return 'unknown'
  return ''
}

/** Item `i` of `n` on the ring: the first at 12 o'clock, the others clockwise, evenly
 *  spaced. Offset from the centre, in px (screen axes: y grows downwards). */
export function radialItemOffset(i: number, n: number, radius = QUICK_MENU_RADIUS): { x: number; y: number; angle: number } {
  const angle = n > 0 ? (360 / n) * i : 0
  const rad = (angle * Math.PI) / 180
  const round = (v: number) => Math.round(v * 100) / 100 || 0 // never -0
  return { x: round(radius * Math.sin(rad)), y: round(-radius * Math.cos(rad)), angle }
}

/** Where an item's label goes: outwards along its radius. Items near 12 and 6 o'clock
 *  put it above and below, the others to their side. */
export function radialLabelSide(angle: number): 'top' | 'right' | 'bottom' | 'left' {
  const a = ((angle % 360) + 360) % 360
  if (a < 20 || a > 340) return 'top'
  if (a > 160 && a < 200) return 'bottom'
  return a < 180 ? 'right' : 'left'
}

export interface QuickMenuBox { left: number; top: number; right: number; bottom: number }

/** The box the open menu covers around its centre (0, 0): the ring, its items and every
 *  label (`labels[i]` = width and height of item i's label, `gap` between item and label). */
export function radialMenuExtent(n: number, labels: readonly { width: number; height: number }[], gap = 8): QuickMenuBox {
  const r = QUICK_MENU_ITEM_RADIUS
  const box = { left: -QUICK_MENU_RADIUS - r, top: -QUICK_MENU_RADIUS - r, right: QUICK_MENU_RADIUS + r, bottom: QUICK_MENU_RADIUS + r }
  for (let i = 0; i < n; i++) {
    const { x, y, angle } = radialItemOffset(i, n)
    const { width, height } = labels[i] ?? { width: 0, height: 0 }
    const side = radialLabelSide(angle)
    if (side === 'right') box.right = Math.max(box.right, x + r + gap + width)
    if (side === 'left') box.left = Math.min(box.left, x - r - gap - width)
    if (side === 'top') box.top = Math.min(box.top, y - r - gap - height)
    if (side === 'bottom') box.bottom = Math.max(box.bottom, y + r + gap + height)
    if (side === 'top' || side === 'bottom') {
      box.left = Math.min(box.left, x - width / 2)
      box.right = Math.max(box.right, x + width / 2)
    } else {
      box.top = Math.min(box.top, y - height / 2)
      box.bottom = Math.max(box.bottom, y + height / 2)
    }
  }
  return box
}

/** The menu's centre: the click, moved inwards just enough for the whole `extent` to stay
 *  inside `bounds` with a `margin`; centred on an axis the bounds are too small for. */
export function radialMenuCenter(
  click: { x: number; y: number }, extent: QuickMenuBox, bounds: QuickMenuBox, margin = 8,
): { x: number; y: number } {
  const axis = (v: number, lo: number, hi: number, below: number, above: number) => {
    const min = lo + margin - below // below <= 0: how far the menu reaches before the centre
    const max = hi - margin - above
    return min > max ? (lo + hi) / 2 : Math.min(Math.max(v, min), max)
  }
  return {
    x: axis(click.x, bounds.left, bounds.right, extent.left, extent.right),
    y: axis(click.y, bounds.top, bounds.bottom, extent.top, extent.bottom),
  }
}
