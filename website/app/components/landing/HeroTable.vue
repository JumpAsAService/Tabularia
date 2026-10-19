<script setup lang="ts">
// The hero's key points as the table the product makes: eight rows, each a point and a short
// fact, all readable at once. One row is open, with its text and link; every few seconds the
// next one opens (WCAG 2.2.2: a pause control, and it waits while the pointer or the focus is in
// the table and while the tab is hidden). On load the rows arrive scattered and grey, like the
// fragments of the field behind them, and settle into line: the brand's «messy in, trusted out»
// as one entrance. That entrance is CSS only, so the rows end in place without script too.
// Every open row has the height of the longest text, so the table never changes height and
// nothing below it moves when a row opens.
// Scroll: where the hero is pinned (LandingHero), `scrollIndex` is the row the reader's scroll
// asks for; it opens at once and the rotation waits until the scroll has been still for a few
// seconds. A "Skip" link jumps past the pin. The rotation also rests while the table is out of
// view. In one column (phones, narrow windows) rows open on a tap only and keep their own
// height: an automatic change there would move the page under the reader's thumb. Under reduced
// motion nothing moves by itself.
import { Boxes, Gauge, GitFork, ListChecks, LockOpen, Plug, ShieldCheck, Sparkles } from 'lucide-vue-next'

const props = defineProps<{ scrollIndex?: number | null, skipTo?: string }>()
const t = useCopy()
const items = t.hero.cards.items
// one icon per row, in the order of t.hero.cards.items (the product's own icon set)
const ICONS = [GitFork, Plug, LockOpen, Gauge, Boxes, ListChecks, ShieldCheck, Sparkles]
const n = items.length
const DWELL = 6000 // ms on each open row
const SCROLL_REST = 3500 // ms of still scroll before the rotation resumes

const index = ref(0)
const playing = ref(true)
const hovered = ref(false)
const focused = ref(false)
const reduce = ref(false)
const hidden = ref(false)
const scrolling = ref(false)
const narrow = ref(false) // one column: no rotation, rows at their own height
const visible = ref(true)
const running = computed(() => playing.value && !hovered.value && !focused.value && !reduce.value && !hidden.value
  && !scrolling.value && !narrow.value && visible.value)

// where each row starts its entrance: scattered and tilted, fixed per row (the same on the
// server and in the browser, and the same on every visit)
const SCATTER: [number, number, number][] = [[-56, -34, -3.5], [44, 22, 2.8], [-30, 40, 1.6], [62, -18, -2.4], [-48, 26, 3.2], [36, -38, -1.8], [-62, 14, -2.9], [52, 30, 2.2]]
function scatter (i: number) {
  const [dx, dy, rot] = SCATTER[i % SCATTER.length] ?? [0, 0, 0]
  return { '--i': i, '--dx': `${dx}px`, '--dy': `${dy}px`, '--rot': `${rot}deg` }
}

let timer: ReturnType<typeof setTimeout> | null = null
function schedule () {
  if (timer) { clearTimeout(timer); timer = null }
  if (running.value) timer = setTimeout(() => { index.value = (index.value + 1) % n }, DWELL)
}
watch([running, index], schedule)

function toggle () { playing.value = !playing.value }

const heads: (HTMLButtonElement | null)[] = []
function onKey (e: KeyboardEvent, i: number) {
  const to = e.key === 'ArrowDown' ? (i + 1) % n : e.key === 'ArrowUp' ? (i - 1 + n) % n
    : e.key === 'Home' ? 0 : e.key === 'End' ? n - 1 : -1
  if (to < 0) return
  e.preventDefault()
  index.value = to
  heads[to]?.focus()
}

// the scroll takes over: open the row it asks for, and hold the rotation while it moves
let rest: ReturnType<typeof setTimeout> | null = null
watch(() => props.scrollIndex, (i, old) => {
  if (i === null || i === undefined || reduce.value) return
  if ((old === null || old === undefined) && i === 0) return // the page opening at the top: no jump
  scrolling.value = true
  if (rest) clearTimeout(rest)
  rest = setTimeout(() => { scrolling.value = false }, SCROLL_REST)
  index.value = i
})

// the open height: the tallest text with its link, measured when the width changes (never per
// frame). Each part is measured, not the row: an open row is stretched to this height, with the
// link at its foot, so its own height is no measure of its content.
const root = ref<HTMLElement | null>(null)
const inners: (HTMLElement | null)[] = []
const bodyH = ref<number | null>(null)
const INNER_GAP = 12 // .row__inner: gap
const INNER_FOOT = 18 // .row__inner: padding-bottom
function measure () {
  const h = Math.max(0, ...inners.map((el) => {
    const [text, link] = [el?.firstElementChild as HTMLElement | null, el?.lastElementChild as HTMLElement | null]
    return text && link ? text.offsetHeight + INNER_GAP + link.offsetHeight + INNER_FOOT : 0
  }))
  if (h) bodyH.value = Math.ceil(h)
}

let ro: ResizeObserver | null = null
let io: IntersectionObserver | null = null
let mq: MediaQueryList | null = null
let mqNarrow: MediaQueryList | null = null
const onNarrow = (e: MediaQueryListEvent) => { narrow.value = e.matches }
const onVisibility = () => { hidden.value = document.visibilityState === 'hidden' }
const onMotion = (e: MediaQueryListEvent) => { reduce.value = e.matches; if (e.matches) playing.value = false }
onMounted(() => {
  mq = window.matchMedia('(prefers-reduced-motion: reduce)')
  reduce.value = mq.matches
  if (reduce.value) playing.value = false
  mq.addEventListener('change', onMotion)
  mqNarrow = window.matchMedia('(max-width: 1000px)')
  narrow.value = mqNarrow.matches
  mqNarrow.addEventListener('change', onNarrow)
  document.addEventListener('visibilitychange', onVisibility)
  ro = new ResizeObserver(() => measure())
  if (root.value) ro.observe(root.value)
  io = new IntersectionObserver(([e]) => { visible.value = !!e?.isIntersecting }, { threshold: 0.4 })
  if (root.value) io.observe(root.value)
  measure()
  document.fonts?.ready.then(measure)
  schedule()
})
onBeforeUnmount(() => {
  if (timer) clearTimeout(timer)
  if (rest) clearTimeout(rest)
  ro?.disconnect()
  io?.disconnect()
  mq?.removeEventListener('change', onMotion)
  mqNarrow?.removeEventListener('change', onNarrow)
  document.removeEventListener('visibilitychange', onVisibility)
})
function onFocusOut (e: FocusEvent) {
  if (!root.value?.contains(e.relatedTarget as Node)) focused.value = false
}
</script>

<template>
  <section
    ref="root" class="points" :aria-label="t.hero.cards.label" :style="bodyH && !narrow ? { '--body-h': `${bodyH}px` } : undefined"
    @mouseenter="hovered = true" @mouseleave="hovered = false" @focusin="focused = true" @focusout="onFocusOut"
  >
    <ul class="points__table" role="list" :aria-label="`${t.hero.cards.label}: ${t.hero.cards.keys}`">
      <li
        v-for="(c, i) in items" :key="c.title" class="row"
        :class="{ 'is-open': i === index, 'is-run': i === index && running }"
        :style="scatter(i)"
      >
        <h2 class="row__h">
          <button
            :id="`point-${i}`" :ref="(el) => { heads[i] = el as HTMLButtonElement | null }" type="button" class="row__head"
            :aria-expanded="i === index" :aria-controls="`point-${i}-body`" @click="index = i" @keydown="onKey($event, i)"
          >
            <span class="row__icon" aria-hidden="true"><component :is="ICONS[i] ?? Sparkles" :size="17" :stroke-width="1.9" /></span>
            <span class="row__title">{{ c.title }}</span>
            <span class="row__fact"><span class="sr-only">{{ t.hero.cards.factLabel }}: </span>{{ c.fact }}</span>
          </button>
        </h2>
        <div :id="`point-${i}-body`" class="row__body" role="region" :aria-labelledby="`point-${i}`" :inert="i === index ? undefined : true">
          <div class="row__clip">
            <div :ref="(el) => { inners[i] = el as HTMLElement | null }" class="row__inner">
              <p class="row__text">{{ c.text }}</p>
              <NuxtLink :to="c.link[0]" class="row__link">{{ c.link[1] }}</NuxtLink>
            </div>
          </div>
        </div>
        <span :key="`${index}-${i}`" class="row__time" :style="{ animationDuration: `${DWELL}ms` }" aria-hidden="true" />
      </li>
    </ul>

    <div class="points__controls">
      <button type="button" class="pbtn" :aria-label="playing ? t.hero.cards.pause : t.hero.cards.play" :aria-pressed="!playing" @click="toggle">
        <svg v-if="playing" width="14" height="14" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><rect x="6" y="5" width="4" height="14" rx="1" /><rect x="14" y="5" width="4" height="14" rx="1" /></svg>
        <svg v-else width="14" height="14" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M8 5.5v13a1 1 0 0 0 1.5.86l10.5-6.5a1 1 0 0 0 0-1.72L9.5 4.64A1 1 0 0 0 8 5.5Z" /></svg>
      </button>
      <a v-if="skipTo" :href="skipTo" class="points__skip">{{ t.hero.cards.skip }}</a>
    </div>
  </section>
</template>

<style scoped>
.points {
  display: grid; gap: 14px; width: 100%; max-width: 640px; justify-self: end;
  --row-pad: clamp(6px, calc((100svh - 600px) / 23), 13px);
}

/* the table: one surface, rows separated by space and a tone step, never by lines */
.points__table { margin: 0; padding: 6px; list-style: none; border-radius: var(--radius); background: var(--tile); display: grid; gap: 2px; }
.row { position: relative; border-radius: 6px; overflow: hidden; transition: background-color 320ms var(--ease-out); }
.row.is-open { background: var(--tile-2); }

.row__h { margin: 0; font: inherit; }
.row__head {
  display: grid; grid-template-columns: auto minmax(0, 1fr) auto; align-items: center; gap: 14px; width: 100%;
  /* the rows tighten on short windows, so the pinned hero still holds the whole table */
  padding: var(--row-pad, 13px) 18px var(--row-pad, 13px) 20px; border: 0; background: none; color: inherit; text-align: left; cursor: pointer; border-radius: 6px;
}
.row__head:focus-visible { outline: 3px solid var(--focus); outline-offset: -3px; }
/* the icon: grey on a closed row, the flow's blue in its tinted square on the open one */
.row__icon {
  display: grid; place-items: center; width: 30px; height: 30px; border-radius: 8px; color: var(--muted);
  transition: background-color 320ms var(--ease-out), color 320ms var(--ease-out);
}
.row__head:hover .row__icon { color: var(--text); }
.row.is-open .row__icon { background: var(--chip-bg); color: var(--chip-ink); }
.row__title {
  font-size: clamp(17px, calc(13px + 0.75svh), 19px); line-height: 1.3; font-weight: 600; letter-spacing: -0.01em; color: var(--soft);
  transition: color 320ms var(--ease-out);
}
.row__head:hover .row__title, .row.is-open .row__title { color: var(--text); }
.row__fact { flex: none; font-size: var(--fs-small); color: var(--muted); font-variant-numeric: tabular-nums; white-space: nowrap; }
.row.is-open .row__fact { color: var(--accent-ink); font-weight: 600; }

/* the open row's text: every open row is as tall as the longest text, so the table keeps its height */
/* opening is a grid track from 0fr to 1fr: no height is animated, and a row at its own height
   (one column) opens as smoothly as a row at the table's */
.row__body { display: grid; grid-template-rows: 0fr; transition: grid-template-rows 420ms var(--ease-out); }
.row.is-open .row__body { grid-template-rows: 1fr; }
.row__clip { min-height: 0; overflow: hidden; }
/* the link sits at the foot of every open row, whatever the length of its text */
/* the text lines up under the title, past the icon (20 + 30 + 14) */
.row__inner { display: flex; flex-direction: column; align-items: flex-start; gap: 12px; padding: 0 20px 18px 64px; height: var(--body-h, auto); }
.row__text { font-size: clamp(16px, calc(12px + 0.55svh), 17px); line-height: 1.5; max-width: 52ch; color: var(--text); }
.row__link { margin-top: auto; color: var(--accent-ink); font-weight: 600; text-decoration: none; }
.row__link:hover { text-decoration: underline; text-underline-offset: 4px; }

/* the time left on the open row: a short filled bar along its foot */
.row__time { position: absolute; left: 64px; right: 18px; bottom: 6px; height: 3px; border-radius: 2px; opacity: 0; }
.row.is-run .row__time { opacity: 1; background: color-mix(in srgb, var(--accent) 18%, transparent); }
.row.is-run .row__time::after {
  content: ''; position: absolute; inset: 0; border-radius: inherit; background: var(--accent);
  transform: scaleX(0); transform-origin: left; animation: fill linear forwards; animation-duration: inherit;
}
@keyframes fill { to { transform: scaleX(1); } }

.points__controls { display: flex; align-items: center; gap: 16px; padding-left: 6px; }
.pbtn {
  display: inline-grid; place-items: center; width: 38px; height: 38px; padding: 0; border: 0; border-radius: 8px;
  background: var(--tile); color: var(--soft); cursor: pointer;
  transition: background-color 160ms ease, color 160ms ease, transform 160ms var(--ease-out);
}
.pbtn:hover { background: var(--tile-2); color: var(--text); }
.pbtn:active { transform: scale(.94); }
.points__skip { font-size: var(--fs-small); font-weight: 600; color: var(--soft); text-underline-offset: 4px; }
.points__skip:hover { color: var(--text); }

/* the entrance: each row arrives scattered, tilted and in the field's grey, and settles into line */
@media (prefers-reduced-motion: no-preference) {
  .points__table { animation: surface 700ms var(--ease-out) both; }
  .row { animation: arrive 900ms var(--ease-out) both; animation-delay: calc(200ms + var(--i) * 80ms); }
  .row__title { animation: settle 1100ms var(--ease-out) both; animation-delay: calc(200ms + var(--i) * 80ms); }
  /* each icon draws itself as its row settles; every Lucide stroke is shorter than 100 units */
  .row__icon :deep(svg > *) {
    stroke-dasharray: 100; stroke-dashoffset: 100;
    animation: draw 1300ms var(--ease-out) both; animation-delay: calc(560ms + var(--i) * 80ms);
  }
  .row__icon :deep(svg) { transition: transform 420ms var(--ease-out); }
  .row__head:hover .row__icon :deep(svg) { transform: rotate(-8deg) scale(1.1); }
}
@keyframes draw { to { stroke-dashoffset: 0; } }
@keyframes surface { from { opacity: 0; } }
@keyframes arrive {
  from { opacity: 0; transform: translate(var(--dx), var(--dy)) rotate(var(--rot)); }
  45% { opacity: 1; }
}
@keyframes settle { from { color: var(--muted); } }

@media (max-width: 1000px) { .points { justify-self: stretch; max-width: none; --row-pad: 13px; } .row__title { font-size: 19px; } }
/* nothing turns by itself in one column or under reduced motion, and nothing is pinned: no controls */
@media (max-width: 1000px), (prefers-reduced-motion: reduce) { .points__controls { display: none; } }
@media (max-width: 520px) {
  /* the fact goes under the title */
  .row__head { grid-template-columns: auto minmax(0, 1fr); grid-template-rows: auto auto; gap: 2px 12px; padding: 12px 14px 12px 14px; }
  .row__icon { grid-row: 1 / 3; align-self: start; }
  .row__fact { grid-column: 2; }
  .row__title { font-size: 18px; }
  .row__inner { padding: 0 14px 18px 56px; }
  .row__time { left: 56px; right: 14px; }
}
</style>
