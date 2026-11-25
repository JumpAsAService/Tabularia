// An expander that opens and closes over a moment instead of at once. The box (a `<details>`, or
// any box with a head that stays and a body that comes and goes) travels from the height it has
// to the height it will have, clipped meanwhile, so what is below it slides instead of jumping.
//
// Closing keeps the box open until the travel ends (the body must stay to be seen leaving): for
// that time the box carries `data-closing`, which the styles read to turn the arrow at once.
// A click while it travels turns it round from where it is.
//
// plugins/expand-motion.client.ts gives it to every `<details>` of the app; a box that is not a
// `<details>` calls toggleExpand() itself. Nothing here runs with reduced motion: the callers ask.

/** The attribute a box carries while it closes. */
export const EXPAND_CLOSING = 'data-closing'
/** On a `<details>` or an ancestor: this expander opens and closes at once, as the browser does. */
export const EXPAND_INSTANT = 'data-expand-instant'

const MIN_MS = 200
const MAX_MS = 340

/**
 * How long a travel of `distance` px takes: a short box is quick, a tall one takes a little more
 * and never long, and closing is quicker than opening (what leaves is not what one looks at).
 */
export function expandDuration(distance: number, closing: boolean): number {
  const opening = Math.min(MAX_MS, Math.max(MIN_MS, 180 + Math.abs(distance) / 8))
  return Math.round(closing ? opening * 0.8 : opening)
}

interface Run { animation: Animation; closing: boolean }
const runs = new WeakMap<HTMLElement, Run>()

function halt(box: HTMLElement): void {
  const run = runs.get(box)
  if (!run) return
  runs.delete(box)
  run.animation.onfinish = null
  run.animation.cancel()
  box.removeAttribute(EXPAND_CLOSING)
}

/** The box's height with only its head showing. */
function closedHeight(box: HTMLElement, head: HTMLElement): number {
  const style = getComputedStyle(box)
  return head.getBoundingClientRect().bottom - box.getBoundingClientRect().top
    + parseFloat(getComputedStyle(head).marginBottom)
    + parseFloat(style.paddingBottom) + parseFloat(style.borderBottomWidth)
}

function slide(box: HTMLElement, from: number, to: number, closing: boolean, done: () => void | Promise<void>): void {
  if (Math.abs(to - from) < 1) { void done(); return }
  const easing = getComputedStyle(document.documentElement).getPropertyValue('--ease-expand').trim() || 'ease-out'
  const animation = box.animate(
    { height: [`${from}px`, `${to}px`], overflow: ['hidden', 'hidden'] },
    // the last frame is held until `done` has taken the body away: no frame at full height
    { duration: expandDuration(to - from, closing), easing, fill: 'forwards' },
  )
  runs.set(box, { animation, closing })
  if (closing) box.setAttribute(EXPAND_CLOSING, '')
  animation.onfinish = async () => {
    await done()
    if (runs.get(box)?.animation === animation) halt(box)
  }
}

/**
 * Opens or closes `box` with the travel. `head` is the part that stays (the `<summary>`),
 * `isOpen` what the box is now, `setOpen` what makes it so: awaited, so that a state that reaches
 * the page a tick later (Vue) is on the page before the heights are read.
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
