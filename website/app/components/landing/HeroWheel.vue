<script setup lang="ts">
// The hero's key points on a tilted ring: eight cards on a horizontal circle that turns about
// a vertical axis, the circle tilted towards the reader so it reads as an inclined ellipse —
// cards at the back higher, smaller and dimmed, the front card lower and full size. Every card
// faces the reader (it is projected, never rotated), so nothing is mirrored or seen edge-on.
// It turns in steps: one card, a pause on the front card, the next.
// The ring is projected in script: each card gets one transform (translate + scale), opacity
// and stacking order; during a turn those are written every frame, between turns nothing runs.
// WCAG 2.2.2: a visible pause/play control; it also pauses while the pointer is over it, while
// anything inside has focus and while the tab is hidden. Keyboard: the buttons, and the left
// and right arrow keys on the focused ring. Below 560px the ring cannot stay legible, so the
// cards become a flat stacked deck. Under reduced motion the eight points are a still list.
// Without script the first card shows in front.
// Scroll: where the hero is pinned (LandingHero), `scrollIndex` is the card the reader's scroll
// asks for; the ring turns to it at once, and the auto-turn waits until the scroll has been
// still for a few seconds. A "Skip" link jumps past the pin.
const props = defineProps<{ scrollIndex?: number | null, skipTo?: string }>()
const t = useCopy()
const items = t.hero.cards.items
const n = items.length
const DWELL = 6000 // ms on each front card
const TURN = 950 // ms for one step of the auto-turn (ease in and out)
const SCROLL_TURN = 620 // ms for one step asked by the scroll (ease out: it answers at once)
const SCROLL_REST = 3500 // ms of still scroll before the auto-turn resumes
const TILT = 20 * Math.PI / 180

const target = ref(0) // whole steps, grows without bound: the ring keeps turning the same way
const index = computed(() => ((target.value % n) + n) % n)
const playing = ref(true)
const hovered = ref(false)
const focused = ref(false)
const reduce = ref(false)
const hidden = ref(false)
const scrolling = ref(false)
const running = computed(() => playing.value && !hovered.value && !focused.value && !reduce.value && !hidden.value && !scrolling.value)

// ── geometry ──────────────────────────────────────────────────────────────────
const width = ref(640) // the ring's box, measured on mount and on resize (never per frame)
const height = ref(600)
const viewport = ref(1440) // the window's width: the ring reaches past its column only where the page has margin to spare
const flat = computed(() => width.value < 560)
const cardW = computed(() => (flat.value ? width.value : Math.min(440, Math.round(width.value * 0.6))))
// the ring's radius: the side cards may reach a little past the column (into the gap beside the
// tagline and the page margin), so the cards beside the front one show more of themselves
const reach = computed(() => Math.min(1, Math.max(0, (viewport.value - 1280) / 160)))
const ringR = computed(() => Math.max(90, width.value / 2 - (cardW.value * (0.55 + 0.45 * Math.pow(0.5, 0.9))) / 2 + width.value * 0.11 * reach.value))
// how far the ring's front sits below its back: steep enough that each card further round shows
// its title above the cards before it, and no more than the box allows
const tiltA = computed(() => Math.max(60, Math.min(ringR.value * Math.sin(TILT) * 2.05, (height.value - 300) / 2)))
// the plane the ring stands on: the ellipse its cards travel along, seen tilted

const floorW = computed(() => Math.round(ringR.value * 2 + cardW.value * 0.9))
const floorH = computed(() => Math.round(tiltA.value * 2 + 150))
const floorY = computed(() => Math.round(tiltA.value * -0.3 + 130))
// the server's first render: card 0 in front, the default width
interface Pose { x: number, y: number, s: number, o: number, z: number, front: boolean, t: number, b: number, d: number, side: number, back: number, peek: number }

function pose (i: number, pos: number): Pose {
  if (reduce.value) {
    const front = ((i - Math.round(pos)) % n + n) % n === 0
    return { x: 0, y: 0, s: 1, o: front ? 1 : 0, z: front ? 10 : 0, front, t: 1, b: 1, d: 0, side: 0, back: 0, peek: 0 }
  }
  if (flat.value) {
    // a deck: the front card, the next two showing their top edge behind it
    const d = ((i - pos) % n + n) % n
    const k = d > n - 1 ? d - n : d // 0 = front, then 1, 2… behind; just-left cards slide down and out
    if (k < 0 || k > 2.999) {
      const out = k < 0 ? -k : 0
      return { x: 0, y: 40 * out, s: 1, o: Math.max(0, 1 - out * 1.5), z: 9, front: false, t: 0, b: Math.max(0, 1 - out * 3), d: 0, side: 0, back: 0, peek: 0 }
    }
    return { x: 0, y: -14 * k, s: 1 - 0.05 * k, o: 1 - 0.3 * k, z: 10 - Math.round(k), front: k < 0.5, t: Math.max(0, 1 - k * 3), b: Math.max(0, 1 - k * 3), d: 0, side: 0, back: 0, peek: 0 }
  }
  const theta = ((i - pos) / n) * 2 * Math.PI // 0 = front
  const c = Math.cos(theta)
  const near = (c + 1) / 2 // 1 front … 0 back
  // scale falls off with depth; the side cards (±90°) stay inside the box
  const scale = 0.55 + 0.45 * Math.pow(near, 0.9)
  const R = ringR.value
  // the ring tilted towards the reader: an ellipse whose front is lower and back higher
  // (steep enough that each card further round shows its top strip, with its title, above the
  // cards in front of it)
  const A = tiltA.value
  return {
    x: R * Math.sin(theta),
    y: A * c - A * 0.3,
    s: scale,
    o: 1, // the cards are opaque: depth darkens them (--d) instead of letting the page through
    z: Math.round(near * 100),
    front: c > 0.98,
    // the blue rule belongs to the card in front
    t: Math.min(1, Math.max(0, (c - 0.85) / 0.15)),
    // the body belongs to its card's place: fully read only in front, gone within the first 25°
    b: Math.min(1, Math.max(0, (c - 0.9) / 0.09)),
    // depth, 0 in front … 1 at the back: the card and its title dim with it (see the CSS)
    d: 1 - near,
    // how far round to the right (0 … 1): a card on the right side carries its title to its
    // right edge, the part of it that shows beside the cards in front
    side: Math.min(1, Math.max(0, Math.sin(theta) * 2.2)),
    // the card at the back shows the middle of its top strip, between the two cards before it
    back: Math.min(1, Math.max(0, (-c - 0.75) / 0.2)),
    // for the cards beside the front one: how wide (on screen) the part beside the front card is
    peek: Math.abs(theta % (2 * Math.PI)) > 1e-3 && c > 0.2 && c < 0.995
      ? Math.max(0, Math.abs(R * Math.sin(theta)) + scale * cardW.value / 2 - cardW.value / 2)
      : -1,
  }
}

// one element per item, by index (a ref array in a v-for does not promise the source order)
const cards: (HTMLElement | null)[] = []
// each title's own width and the room beside it, read when the layout changes (never per frame)
let titles: { w: number, room: number, pad: number }[] = []
function measureTitles () {
  titles = cards.map((el) => {
    const title = el?.querySelector<HTMLElement>('.card__title')
    if (!el || !title) return { w: 0, room: 0, pad: 0 }
    const pad = parseFloat(getComputedStyle(el).paddingLeft)
    const inner = el.clientWidth - 2 * pad
    return { w: title.offsetWidth, room: Math.max(0, inner - title.offsetWidth), pad }
  })
}
// where a card's title sits on its card: left on the left side, carried to the right edge on the
// right side, centred at the back; on the two cards beside the front one it is scaled down to fit
// the part of the card that shows. Transforms only.
function titleTransform (i: number, p: Pose) {
  const m = titles[i]
  if (!m || !m.w) return 'none'
  let sc = 1
  if (p.peek >= 0 && !p.front) sc = Math.min(1, Math.max(0.42, (p.peek / p.s - m.pad - 6) / m.w))
  const shrink = m.w * (1 - sc)
  const right = m.room + shrink
  const middle = (m.room + shrink) / 2
  const x = p.back > 0 ? right * p.side * (1 - p.back) + middle * p.back : right * p.side
  return `translateX(${x.toFixed(1)}px) scale(${sc.toFixed(3)})`
}
let pos = 0 // the drawn position: eases towards `target`
function paint () {
  cards.forEach((el, i) => {
    if (!el) return
    const p = pose(i, pos)
    el.style.transform = `translate3d(${p.x.toFixed(2)}px, ${p.y.toFixed(2)}px, 0) scale(${p.s.toFixed(4)})`
    el.style.opacity = p.o.toFixed(3)
    el.style.zIndex = String(p.z)
    el.dataset.lit = p.front ? '1' : '0' // Vue owns the class list; the drawn front is a data attribute
    el.style.setProperty('--title-o', p.t.toFixed(3))
    el.style.setProperty('--body-o', p.b.toFixed(3))
    el.style.setProperty('--d', p.d.toFixed(3))
    el.style.setProperty('--title-t', titleTransform(i, p))
  })
}
// As constant strings: Vue rewrites an object style on every re-render (a hover, a pause, the
// index changing) and would snap every card back to this first pose between two painted frames,
// putting another card's surface in front of the lit one. A string that never changes is
// written once and left alone, so only paint() moves the cards.
const initialStyle = items.map((_, i) => {
  const p = pose(i, 0)
  return `transform: translate3d(${p.x}px, ${p.y}px, 0) scale(${p.s}); opacity: ${p.o}; z-index: ${p.z}; --title-o: ${p.t}; --body-o: ${p.b}; --d: ${p.d}`
})

// ── stepping ──────────────────────────────────────────────────────────────────
let raf = 0
let timer: ReturnType<typeof setTimeout> | null = null
const easeInOut = (x: number) => (x < 0.5 ? 4 * x * x * x : 1 - Math.pow(-2 * x + 2, 3) / 2)
const easeOut = (x: number) => 1 - Math.pow(1 - x, 4)
let fromScroll = false
function animateTo (to: number) {
  cancelAnimationFrame(raf)
  if (reduce.value) { pos = to; paint(); return }
  const from = pos
  const start = performance.now()
  const ease = fromScroll ? easeOut : easeInOut
  const dur = (fromScroll ? SCROLL_TURN : TURN) * Math.min(1.6, Math.max(1, Math.abs(to - from) * 0.6))
  fromScroll = false
  const step = (now: number) => {
    const k = Math.min(1, (now - start) / dur)
    pos = from + (to - from) * ease(k)
    paint()
    if (k < 1) raf = requestAnimationFrame(step)
  }
  raf = requestAnimationFrame(step)
}
watch(target, to => animateTo(to))

function schedule () {
  if (timer) { clearTimeout(timer); timer = null }
  if (running.value) timer = setTimeout(() => { target.value++ }, DWELL)
}
watch(running, schedule)
watch(target, schedule)

function next () { target.value++ }
function prev () { target.value-- }
function go (i: number) {
  let d = i - index.value
  if (d > n / 2) d -= n
  if (d < -n / 2) d += n
  target.value += d
}
function toggle () { playing.value = !playing.value }

// the scroll takes over: turn to the card it asks for, and hold the auto-turn while it moves
let rest: ReturnType<typeof setTimeout> | null = null
watch(() => props.scrollIndex, (i, old) => {
  if (i === null || i === undefined || reduce.value) return
  if ((old === null || old === undefined) && i === 0) return // the page opening at the top: no jump
  scrolling.value = true
  if (rest) clearTimeout(rest)
  rest = setTimeout(() => { scrolling.value = false }, SCROLL_REST)
  if (i !== index.value) { fromScroll = true; go(i) }
})
function onKey (e: KeyboardEvent) {
  if (e.key === 'ArrowRight' || e.key === 'ArrowDown') { e.preventDefault(); next() }
  else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') { e.preventDefault(); prev() }
}

const root = ref<HTMLElement | null>(null)
const ring = ref<HTMLElement | null>(null)
let ro: ResizeObserver | null = null
let mq: MediaQueryList | null = null
const onVisibility = () => { hidden.value = document.visibilityState === 'hidden' }
const onMotion = (e: MediaQueryListEvent) => { reduce.value = e.matches; if (e.matches) playing.value = false; paint() }
onMounted(() => {
  mq = window.matchMedia('(prefers-reduced-motion: reduce)')
  reduce.value = mq.matches
  if (reduce.value) playing.value = false
  mq.addEventListener('change', onMotion)
  document.addEventListener('visibilitychange', onVisibility)
  ro = new ResizeObserver(() => { width.value = ring.value?.clientWidth || width.value; height.value = ring.value?.clientHeight || height.value; viewport.value = window.innerWidth; nextTick(() => { measureTitles(); paint() }) })
  if (ring.value) ro.observe(ring.value)
  width.value = ring.value?.clientWidth || width.value
  height.value = ring.value?.clientHeight || height.value
  viewport.value = window.innerWidth
  nextTick(() => { measureTitles(); paint() })
  document.fonts?.ready.then(() => { measureTitles(); paint() })
  schedule()
})
onBeforeUnmount(() => {
  cancelAnimationFrame(raf)
  if (timer) clearTimeout(timer)
  if (rest) clearTimeout(rest)
  ro?.disconnect()
  mq?.removeEventListener('change', onMotion)
  document.removeEventListener('visibilitychange', onVisibility)
})
function onFocusOut (e: FocusEvent) {
  if (!root.value?.contains(e.relatedTarget as Node)) focused.value = false
}
</script>

<template>
  <section
    ref="root" class="ring" :class="{ 'is-flat': flat }" aria-roledescription="carousel" :aria-label="t.hero.cards.label"
    @mouseenter="hovered = true" @mouseleave="hovered = false" @focusin="focused = true" @focusout="onFocusOut"
  >
    <div
      ref="ring" class="ring__stage" tabindex="0" :aria-label="`${t.hero.cards.label}: ${t.hero.cards.keys}`"
      :aria-live="running ? 'off' : 'polite'" :style="{ '--card-w': `${cardW}px`, '--floor-w': `${floorW}px`, '--floor-h': `${floorH}px`, '--floor-y': `${floorY}px` }" @keydown="onKey"
    >
      <span class="ring__floor" aria-hidden="true" />
      <article
        v-for="(c, i) in items" :key="c.title" :ref="(el) => { cards[i] = el as HTMLElement | null }" class="card"
        :class="{ 'is-front': i === index }" :data-i="i" :data-lit="i === 0 ? '1' : '0'" :style="initialStyle[i]"
        role="group" aria-roledescription="slide" :aria-label="`${i + 1} ${t.hero.cards.of} ${n}`"
        :aria-hidden="reduce || i === index ? undefined : 'true'" :inert="reduce || i === index ? undefined : true"
      >
        <h2 class="card__title">{{ c.title }}</h2>
        <div class="card__body">
          <p class="card__text">{{ c.text }}</p>
          <NuxtLink :to="c.link[0]" class="card__link">{{ c.link[1] }}</NuxtLink>
        </div>
      </article>
    </div>

    <div class="ring__controls">
      <div class="ring__buttons">
        <button type="button" class="cbtn" :aria-label="playing ? t.hero.cards.pause : t.hero.cards.play" :aria-pressed="!playing" @click="toggle">
          <svg v-if="playing" width="14" height="14" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><rect x="6" y="5" width="4" height="14" rx="1" /><rect x="14" y="5" width="4" height="14" rx="1" /></svg>
          <svg v-else width="14" height="14" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M8 5.5v13a1 1 0 0 0 1.5.86l10.5-6.5a1 1 0 0 0 0-1.72L9.5 4.64A1 1 0 0 0 8 5.5Z" /></svg>
        </button>
        <button type="button" class="cbtn" :aria-label="t.hero.cards.prev" @click="prev">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m15 18-6-6 6-6" /></svg>
        </button>
        <button type="button" class="cbtn" :aria-label="t.hero.cards.next" @click="next">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m9 18 6-6-6-6" /></svg>
        </button>
      </div>
      <ol class="ring__marks" role="list">
        <li v-for="(c, i) in items" :key="c.title">
          <button
            type="button" class="mark" :class="{ 'is-on': i === index, 'is-run': i === index && running }"
            :aria-label="`${t.hero.cards.show}: ${c.title}`" :aria-current="i === index ? 'true' : undefined" @click="go(i)"
          ><span :key="`${target}-${i}`" :style="{ animationDuration: `${DWELL}ms` }" /></button>
        </li>
      </ol>
      <a v-if="skipTo" :href="skipTo" class="ring__skip">{{ t.hero.cards.skip }}</a>
    </div>
  </section>
</template>

<style scoped>
.ring { display: grid; gap: 20px; width: 100%; }
.ring__stage { position: relative; height: clamp(460px, calc(100svh - 270px), 620px); border-radius: var(--radius); }
@media (min-width: 1600px) { .ring__stage { height: clamp(460px, calc(100svh - 270px), 660px); } .card { height: 340px; margin-top: -170px; } }
.ring__stage:focus-visible { outline: 3px solid var(--focus); outline-offset: 6px; }

/* the floor: the tilted ellipse the cards travel along, one tone off the page */
.ring__floor {
  position: absolute; left: 50%; top: 50%; width: var(--floor-w); height: var(--floor-h);
  transform: translate(-50%, calc(-50% + var(--floor-y))); border-radius: 50%;
  background: color-mix(in srgb, var(--tile) 70%, transparent);
}
.ring.is-flat .ring__floor { display: none; }
/* the cards: one surface tone, a lit top edge, and depth from the ring itself */
.card {
  position: absolute; left: 50%; top: 50%; width: var(--card-w); height: 320px; overflow: hidden;
  margin-left: calc(var(--card-w) / -2); margin-top: -160px;
  display: flex; flex-direction: column; gap: 14px; align-items: flex-start; padding: 28px 30px 26px;
  border-radius: 12px; color: var(--text);
  /* depth darkens the card towards the page: the ring reads front to back without transparency */
  background: color-mix(in srgb, var(--tile) calc(100% - var(--d, 0) * 60%), var(--ground));
  box-shadow: inset 0 1px 0 color-mix(in srgb, var(--text) 9%, transparent); /* taste-skill-ignore: the light on the card's edge */
  transform-origin: 50% 50%; will-change: transform, opacity;
}
/* the front card floats above the ring, with the flow blue as a short rule (taste-skill-ignore: the owner asked for depth) */
.card[data-lit="1"] {
  /* taste-skill-ignore: the one floating card (the owner asked for depth and shadow on the ring) */
  box-shadow: inset 0 1px 0 color-mix(in srgb, var(--text) 12%, transparent), 0 28px 56px -28px rgb(0 0 0 / .6); /* taste-skill-ignore */
}
.card::before {
  content: ''; display: block; width: 36px; height: 3px; border-radius: 2px; background: var(--accent);
  opacity: var(--title-o, 1); flex: none;
}
.card__title {
  font-size: var(--fs-statement); line-height: 1.1; letter-spacing: -0.02em; font-weight: 600; max-width: 18ch;
  text-wrap: balance; width: fit-content; transform: var(--title-t, none); transform-origin: 0 0;
  /* every card shows its own title; the deeper the card, the closer the title comes to its ground
     (about 6:1 on the side cards, 3:1 at the very back, in both themes) */
  color: color-mix(in srgb, var(--text) calc(100% - var(--d, 0) * 57%), var(--ground));
}
/* only the front card's body is read; the others are the ring's shapes */
.card__body { display: flex; flex-direction: column; gap: 14px; flex: 1; align-items: flex-start; opacity: var(--body-o, 0); }
.card__text { font-size: var(--fs-body); line-height: 1.55; max-width: 40ch; }
.card__link { margin-top: auto; color: var(--accent-ink); font-weight: 600; text-decoration: none; }
.card__link:hover { text-decoration: underline; text-underline-offset: 4px; }

/* the controls: one quiet bar */
.ring__controls { display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }
.ring__buttons { display: flex; gap: 2px; padding: 3px; border-radius: 10px; background: var(--tile); }
.cbtn {
  display: inline-grid; place-items: center; width: 38px; height: 38px; padding: 0; border: 0; border-radius: 8px;
  background: transparent; color: var(--soft); cursor: pointer;
  transition: background-color 160ms ease, color 160ms ease, transform 160ms var(--ease-out);
}
.cbtn:hover { background: var(--tile-2); color: var(--text); }
.cbtn:active { transform: scale(.94); }
.ring__marks { display: flex; gap: 6px; margin: 0; padding: 0; list-style: none; flex: 1; min-width: 160px; }
.ring__marks li { flex: 1; }
.mark { display: block; width: 100%; height: 24px; padding: 10px 0; border: 0; background: none; cursor: pointer; }
.mark span { display: block; height: 3px; border-radius: 2px; background: var(--tile-3); position: relative; overflow: hidden; transition: transform 160ms ease; }
.mark:hover span { transform: scaleY(1.7); }
.mark.is-on span { background: color-mix(in srgb, var(--accent) 40%, var(--tile-3)); }
.mark span::after { content: ''; position: absolute; inset: 0; background: var(--accent); transform: scaleX(0); transform-origin: left; }
.mark.is-on:not(.is-run) span::after { transform: none; }
.mark.is-run span::after { animation: fill linear forwards; animation-duration: inherit; }
@keyframes fill { to { transform: scaleX(1); } }
.ring__skip { font-size: var(--fs-small); font-weight: 600; color: var(--soft); text-underline-offset: 4px; }
.ring__skip:hover { color: var(--text); }

/* the flat deck on narrow screens */
.ring.is-flat .ring__stage { height: 340px; }
.ring.is-flat .card { top: 36px; margin-top: 0; height: auto; min-height: 290px; padding: 24px; }

/* reduced motion: no ring, the eight points as a still list */
@media (prefers-reduced-motion: reduce) {
  .ring__stage, .ring.is-flat .ring__stage { height: auto; display: grid; gap: 8px; }
  .card, .ring.is-flat .card {
    position: relative; left: auto; top: auto; margin: 0; width: 100%; height: auto; min-height: 0;
    transform: none !important; opacity: 1 !important; padding: 18px 20px; gap: 8px;
  }
  .card::before, .ring__floor { display: none; }
  .card__title { font-size: var(--fs-body); opacity: 1; color: var(--text) !important; max-width: none; transform: none; }
  .card__body { opacity: 1; }
  .card__link { margin-top: 0; }
  .ring__buttons, .ring__marks { display: none; }
}
</style>
