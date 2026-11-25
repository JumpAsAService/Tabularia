// An expander that opens and closes over a moment instead of at once, so what is below it slides
// instead of jumping. Two shapes, one travel:
//
// - a `<details>`: the box goes from the height it has to the height it will have, clipped
//   meanwhile. plugins/expand-motion.client.ts gives it to every `<details>` of the app. Closing
//   keeps the box open until the travel ends (the body must stay to be seen leaving): for that
//   time it carries `data-closing`, which the styles read to turn the marker at once.
// - a body that comes and goes by `v-if` or `v-show` under a head of its own: `<ExpandBody>`
//   around it (components/ui/ExpandBody.vue) opens it from nothing and closes it to nothing,
//   its height and what it adds around itself (padding, margin, border, the gap of a column). A
//   table row does the same through its cells and the one block each of them holds.
//
// A click while it travels turns it round from where it is. Nothing here moves with reduced
// motion: the callers ask (the plugin), or the hooks answer at once.

/** The attribute a `<details>` carries while it closes. */
export const EXPAND_CLOSING = 'data-closing'
/** On a `<details>` or an ancestor: this expander opens and closes at once, as the browser does. */
export const EXPAND_INSTANT = 'data-expand-instant'

const MIN_MS = 200
const MAX_MS = 420
/** The most one frame moves the travel on: two frames of a 60 Hz screen. */
const MAX_STEP_MS = 34

/**
 * How long a travel of `distance` px takes: a short box is quick, a tall one takes a little more
 * and never long, and closing is quicker than opening (what leaves is not what one looks at).
 */
export function expandDuration(distance: number, closing: boolean): number {
  const opening = Math.min(MAX_MS, Math.max(MIN_MS, 180 + Math.abs(distance) / 6))
  return Math.round(closing ? opening * 0.8 : opening)
}

/** A travel under way. `from` and `to` are how open a body is at its ends, 0 to 1. */
interface Run { animations: Animation[]; closing: boolean; from: number; to: number }
const runs = new WeakMap<Element, Run>()

function halt(root: Element): void {
  const run = runs.get(root)
  if (!run) return
  runs.delete(root)
  for (const animation of run.animations) animation.cancel()
  root.removeAttribute(EXPAND_CLOSING)
}

const reducedMotion = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches
const easing = () => getComputedStyle(document.documentElement).getPropertyValue('--ease-expand').trim() || 'ease-out'

/**
 * Moves the paused animations of `run` on, frame by frame, to their end, then calls `done`.
 *
 * They never run on their own clock: each frame moves them on by the time that frame took, and
 * by no more than MAX_STEP_MS. A page that is busy for a moment (a body laid out and painted for
 * the first time, the page's own answer to the toggle) then holds the travel still and lets it
 * go on from there; on the clock, the time spent busy would be travel skipped, and a jump.
 */
function drive(root: Element, run: Run, duration: number, done: () => void | Promise<void>): void {
  runs.set(root, run)
  let at = 0
  let last = 0
  const frame = async (now: number) => {
    if (runs.get(root) !== run) return // turned round, or taken over
    if (last) at += Math.min(now - last, MAX_STEP_MS)
    last = now
    if (at < duration) {
      for (const animation of run.animations) animation.currentTime = at
      requestAnimationFrame(frame)
      return
    }
    // the last frame is held (fill) until `done` has taken the body away: no frame at full height
    for (const animation of run.animations) animation.currentTime = duration
    await done()
    if (runs.get(root) === run) halt(root)
  }
  requestAnimationFrame(frame)
}

// ── a <details> ─────────────────────────────────────────────────────────────────────────────

/** The box's height with only its head showing. */
function closedHeight(box: HTMLElement, head: HTMLElement): number {
  const style = getComputedStyle(box)
  return head.getBoundingClientRect().bottom - box.getBoundingClientRect().top
    + parseFloat(getComputedStyle(head).marginBottom)
    + parseFloat(style.paddingBottom) + parseFloat(style.borderBottomWidth)
}

function slide(box: HTMLElement, from: number, to: number, closing: boolean, done: () => void | Promise<void>): void {
  if (Math.abs(to - from) < 1) { void done(); return }
  const duration = expandDuration(to - from, closing)
  const animation = box.animate(
    { height: [`${from}px`, `${to}px`], overflow: ['hidden', 'hidden'] },
    { duration, easing: easing(), fill: 'forwards' },
  )
  animation.pause()
  if (closing) box.setAttribute(EXPAND_CLOSING, '')
  drive(box, { animations: [animation], closing, from: 0, to: 0 }, duration, done)
}

/**
 * Opens or closes `box` with the travel. `head` is the part that stays (the `<summary>`),
 * `isOpen` what the box is now, `setOpen` what makes it so.
 */
export async function toggleExpand(
  box: HTMLElement,
  head: HTMLElement,
  isOpen: boolean,
  setOpen: (open: boolean) => void | Promise<void>,
): Promise<void> {
  const run = runs.get(box)
  const from = box.getBoundingClientRect().height
  const close = run ? !run.closing : isOpen
  halt(box)
  if (close) {
    slide(box, from, closedHeight(box, head), true, () => setOpen(false))
    return
  }
  await setOpen(true)
  slide(box, from, box.getBoundingClientRect().height, false, () => {})
}

/**
 * The `<details>` a click asks to open or close, with its `<summary>`; null when the click is not
 * that: already handled, on a control inside the summary (a link, a button, a field), on a
 * summary that is not the box's own, or on an expander that opted out.
 */
export function expanderOf(event: Event): { details: HTMLDetailsElement; summary: HTMLElement } | null {
  if (event.defaultPrevented || !(event.target instanceof Element)) return null
  const summary = event.target.closest('summary')
  const details = summary?.parentElement
  if (!summary || !(details instanceof HTMLDetailsElement)) return null
  if (details.querySelector(':scope > summary') !== summary) return null
  if (details.closest(`[${EXPAND_INSTANT}]`)) return null
  const control = event.target.closest('a, button, input, select, textarea, label')
  if (control && summary.contains(control)) return null
  return { details, summary }
}

// ── a body that comes and goes ──────────────────────────────────────────────────────────────

const BLOCK = ['height', 'paddingTop', 'paddingBottom', 'marginTop', 'marginBottom', 'borderTopWidth', 'borderBottomWidth'] as const
const CELL = ['paddingTop', 'paddingBottom', 'borderTopWidth', 'borderBottomWidth'] as const
type Sizes = Partial<Record<(typeof BLOCK)[number], number>>

/** One element of a body: what it measures open and shut, and whether it is clipped meanwhile. */
interface Part { el: HTMLElement; open: Sizes; shut: Sizes; clip: boolean }

/** The gap a column (flex or grid) puts before `el`: the body brings it along when it appears. */
function gapBefore(el: HTMLElement): number {
  const parent = el.parentElement
  if (!parent || parent.children.length < 2) return 0
  const style = getComputedStyle(parent)
  const column = style.display.endsWith('grid') || (style.display.endsWith('flex') && style.flexDirection.startsWith('column'))
  return column ? parseFloat(style.rowGap) || 0 : 0
}

function part(el: HTMLElement, names: readonly (typeof BLOCK)[number][], clip: boolean): Part {
  // Measured clipped, as it travels: clipping keeps inside the element the margins of its first
  // and last child, which at rest pass through it. Read unclipped, its height would be short of
  // them and the travel would end, or start, with a jump of that much.
  const overflow = el.style.overflow
  if (clip) el.style.overflow = 'hidden'
  const style = getComputedStyle(el)
  const open: Sizes = {}
  const shut: Sizes = {}
  for (const name of names) { open[name] = parseFloat(style[name]) || 0; shut[name] = 0 }
  if (clip) el.style.overflow = overflow
  // shut, the body still counts as an item of its column: its margin takes that one gap back
  if (clip) shut.marginTop = -gapBefore(el)
  return { el, open, shut, clip }
}

/** A table row cannot be clipped: its cells lose their padding, and what each holds its height. */
function partsOf(root: HTMLElement): Part[] {
  if (!(root instanceof HTMLTableRowElement)) return [part(root, BLOCK, true)]
  const parts: Part[] = []
  for (const cell of root.cells) {
    parts.push(part(cell, CELL, false))
    for (const child of cell.children) if (child instanceof HTMLElement) parts.push(part(child, BLOCK, true))
  }
  return parts
}

function frameOf(p: Part, openness: number): Keyframe {
  const frame: Keyframe = p.clip ? { overflow: 'hidden' } : {}
  for (const name of Object.keys(p.open) as (typeof BLOCK)[number][]) {
    const shut = p.shut[name] ?? 0
    frame[name] = `${shut + ((p.open[name] ?? 0) - shut) * openness}px`
  }
  return frame
}

/** How open a travelling body is right now, 0 to 1 (the animation's progress is the eased one). */
function openness(run: Run): number {
  const progress = run.animations[0]?.effect?.getComputedTiming().progress ?? 1
  return run.from + (run.to - run.from) * progress
}

/** Takes `root`, at rest and as it is when open, from `from` to `to` (how open: 0 to 1). */
function travel(root: HTMLElement, from: number, to: number, done: () => void): void {
  const distance = root.getBoundingClientRect().height * Math.abs(to - from)
  if (distance < 1) { done(); return }
  const closing = to < from
  const duration = expandDuration(distance, closing)
  const curve = easing()
  const animations = partsOf(root).map((p) => {
    const animation = p.el.animate([frameOf(p, from), frameOf(p, to)], { duration, easing: curve, fill: 'forwards' })
    animation.pause()
    return animation
  })
  drive(root, { animations, closing, from, to }, duration, done)
}

// the bodies closing under each parent: a `v-if` body turned round while it closes is taken away
// by Vue and a new element comes in its place, which goes on from where that one was
const leaving = new WeakMap<Node, Set<HTMLElement>>()

/** `<Transition>` hook: the body has just come onto the page. */
export function expandEnter(el: Element, done: () => void): void {
  if (!(el instanceof HTMLElement) || reducedMotion()) { done(); return }
  let from = 0
  const own = runs.get(el)
  if (own) from = openness(own) // `v-show`: the same element, turned round
  else {
    for (const gone of (el.parentNode && leaving.get(el.parentNode)) || []) {
      const cut = runs.get(gone)
      if (!cut || gone.isConnected) continue // another body of the same parent, closing on its own
      from = openness(cut)
      halt(gone)
      leaving.get(el.parentNode!)?.delete(gone)
    }
  }
  halt(el)
  travel(el, from, 1, done)
}

/** `<Transition>` hook: the body is to leave the page; `done` takes it away. */
export function expandLeave(el: Element, done: () => void): void {
  if (!(el instanceof HTMLElement) || reducedMotion()) { done(); return }
  const own = runs.get(el)
  const from = own ? openness(own) : 1
  halt(el)
  const parent = el.parentNode
  if (parent) {
    if (!leaving.has(parent)) leaving.set(parent, new Set())
    leaving.get(parent)!.add(el)
  }
  travel(el, from, 0, () => {
    if (parent) leaving.get(parent)?.delete(el)
    done()
  })
}
