<script setup lang="ts">
// The guided tour on screen (the machine is composables/useTour.ts). One for the whole app, in
// app.vue: the pages each wrap their own shell and the editor has none, so a host in the shell
// would be rebuilt at every step. It goes to the step's page, WAITS for the anchor (an element
// with `data-tour="…"`), brings it into view, draws a spotlight round it and the card beside it.
// An anchor that does not come in time is not a stop: the step stands as a card at the centre.
//
// It is not modal: the app under it stays clickable, and its own keys go on working. A page the
// person reaches by themselves ends the tour (what step it was is remembered, and the settings menu
// offers to resume it). While a dialog or a menu of the app is open the tour steps aside: a menu
// opened under the spotlight would be dimmed by it, and the card would lie on top of it.
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  TOUR_MIN_WIDTH, tourCardKey, tourIsPublicPath, tourKeepsKey, useTour, tourPlaceCard, tourShouldAutostart, tourSpotBox,
  type TourBox, type TourSide, type TourStep,
} from '~/composables/useTour'

const { t } = useI18n()
const { user } = useAuth()
const tour = useTour()
const route = useRoute()
const router = useRouter()

const cardEl = ref<HTMLElement | null>(null)
const box = ref<TourBox | null>(null) // the anchor's, in the window
const ready = ref(false) // the step has settled: its anchor is found, or it stands at the centre
const pos = ref<{ x: number; y: number; side: TourSide }>({ x: 0, y: 0, side: 'center' })
const announce = ref('')
const aside = ref(false) // a dialog or a menu of the app is open: the tour is not drawn

const step = tour.current
const isFirst = computed(() => step.value?.id === 'welcome')
const isLast = computed(() => tour.index.value + 1 >= tour.total.value)
const reduced = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches

// ── Going to the step and finding its anchor ─────────────────────────────────

const POLL_MS = 100 // while the anchor is looked for
const POLL_SETTLED_MS = 250 // once the step stands: the anchor is only followed
const WAIT_MS = 6000 // a page that is slow to arrive: after this the step is a card at the centre
const WAIT_OPTIONAL_MS = 1500 // a step that is only there when the page has its anchor

let token = 0 // the step being entered: an answer for an earlier one is dropped
let expectedPath = ''
let pushing = false
let timer: ReturnType<typeof setTimeout> | undefined
let frame = 0
let target: HTMLElement | null = null
let fresh = false // the tour has just started: its first card takes the focus
let returnTo: HTMLElement | null = null // where the focus was when it started

const visible = (el: Element) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0 }
const FIELD = 'input, textarea, select, [contenteditable=""], [contenteditable="true"]'
// what the tour steps aside for (the card is a dialog too, but not a modal one)
const OVERLAY = '[aria-modal="true"], [role="menu"], .menu-pop, .gk-pop, .topbar .menu'
const overlayOpen = () => [...document.querySelectorAll(OVERLAY)].some((el) => !el.closest('[data-tour-layer]') && el.getClientRects().length > 0)
/** The focus is nowhere a person put it: on the page itself, or on the card. */
const focusIsFree = () => {
  const a = document.activeElement
  return !a || a === document.body || a === document.documentElement || !!cardEl.value?.contains(a)
}

function pick(s: TourStep): HTMLElement | null {
  for (const a of s.anchors) {
    const el = document.querySelector<HTMLElement>(`[data-tour="${CSS.escape(a)}"]`)
    if (el && visible(el)) return el
  }
  return null
}

function sameBox(a: TourBox | null, b: TourBox | null) {
  return a === b || (!!a && !!b && Math.abs(a.x - b.x) < 0.5 && Math.abs(a.y - b.y) < 0.5 && Math.abs(a.w - b.w) < 0.5 && Math.abs(a.h - b.h) < 0.5)
}

/** Where the anchor and the card are now. */
function measure() {
  const r = target?.isConnected ? target.getBoundingClientRect() : null
  const next = r && r.width > 0 && r.height > 0 ? { x: r.left, y: r.top, w: r.width, h: r.height } : null
  if (!sameBox(box.value, next)) box.value = next
  const c = cardEl.value?.getBoundingClientRect()
  const p = tourPlaceCard(next, { w: c?.width || 360, h: c?.height || 200 }, { w: window.innerWidth, h: window.innerHeight })
  if (p.x !== pos.value.x || p.y !== pos.value.y || p.side !== pos.value.side) pos.value = p
}

function schedule() {
  if (!frame) frame = requestAnimationFrame(() => { frame = 0; measure() })
}

function stopWatching() {
  if (!import.meta.client) return
  if (timer) clearTimeout(timer)
  timer = undefined
  if (frame) cancelAnimationFrame(frame)
  frame = 0
  window.removeEventListener('resize', schedule)
  window.removeEventListener('scroll', schedule, true)
}

// A step that talks about what a chosen node shows chooses one, when the person has not: the last
// operation of the flow, whose data are the most worked. Pressed as a click, so the editor does
// what it does for a person's own. False while there is no node to press yet (the step's anchor,
// the preview's frame, is on the page before the flow's nodes are).
function press(s: TourStep): boolean {
  if (!s.press || document.querySelector('.vue-flow__node.selected')) return true
  const all = document.querySelectorAll<HTMLElement>(`[data-tour="${CSS.escape(s.press)}"]`)
  const el = all[all.length - 1]
  if (!el) return false
  el.click()
  return true
}

let settling = false
async function settle(mine: number) {
  if (mine !== token || !step.value || settling) return
  settling = true
  try { await show(mine) } finally { settling = false }
}

async function show(mine: number) {
  if (target) {
    const r = target.getBoundingClientRect()
    if (r.top < 56 || r.bottom > window.innerHeight) target.scrollIntoView({ block: 'nearest', behavior: reduced() ? 'auto' : 'smooth' })
  }
  measure()
  await nextTick()
  measure() // the card's own size is known now
  if (mine !== token || !step.value) return
  ready.value = true
  // The card takes the focus (a dialog's name and description are then read out) unless the person
  // has put it somewhere of their own meanwhile: then the step is only announced.
  const first = fresh
  fresh = false
  const take = focusIsFree() || (first && !document.activeElement?.closest(FIELD))
  announce.value = take ? '' : t('tour.announce', { n: tour.index.value + 1, total: tour.total.value, title: t(`tour.steps.${step.value.id}.title`) })
  await nextTick()
  if (take && mine === token) cardEl.value?.focus({ preventScroll: true })
}

async function enter(s: TourStep) {
  const mine = ++token
  stopWatching()
  ready.value = false
  target = null
  box.value = null
  if (s.route) {
    const want = new URL(s.route, window.location.origin)
    const here = route
    const there = here.path === want.pathname && [...want.searchParams].every(([k, v]) => String(here.query[k]) === v)
    expectedPath = want.pathname
    if (!there) {
      // A navigation the page refuses does not throw, it answers with a failure: an editor with
      // unsaved changes asked, and the person chose to stay. The step's page is not this one, so
      // the tour ends here, remembering the step (the menu offers to resume it).
      let refused: unknown
      pushing = true
      try { refused = await router.push(s.route) } catch (e) { refused = e }
      pushing = false
      if (mine !== token) return
      if (refused) { expectedPath = route.path; tour.leave(); return }
    }
  } else {
    expectedPath = route.path
  }
  if (mine !== token) return

  const started = performance.now()
  const limit = s.optional ? WAIT_OPTIONAL_MS : WAIT_MS
  let pressed = false
  window.addEventListener('resize', schedule)
  window.addEventListener('scroll', schedule, true)
  const tick = () => {
    if (mine !== token) return
    const was = aside.value
    aside.value = overlayOpen()
    if (s.anchors.length) {
      // the best anchor may come after a worse one (a node after its canvas): the spotlight moves on
      target = pick(s)
      if (!ready.value) {
        if (target) void settle(mine)
        else if (performance.now() - started > limit) {
          if (s.optional) { tour.passOver(); return }
          void settle(mine)
        }
      }
    } else if (!ready.value) void settle(mine)
    if (ready.value) {
      if (!pressed && performance.now() - started < WAIT_MS) pressed = press(s)
      measure()
      // back from behind a menu or a dialog that has closed: the card has the focus again, if nobody else does
      if (was && !aside.value && focusIsFree()) void nextTick(() => cardEl.value?.focus({ preventScroll: true }))
    }
    timer = setTimeout(tick, ready.value ? POLL_SETTLED_MS : POLL_MS)
  }
  tick()
}

watch(step, (s, old) => {
  if (s) {
    if (!old) { fresh = true; returnTo = document.activeElement as HTMLElement | null }
    void enter(s)
    return
  }
  // The tour is over. The card goes, and a focus that was on it would fall on nothing: it goes back
  // where it was when the tour started, or to the gear, whose menu is where the tour is found again.
  const had = !!cardEl.value?.contains(document.activeElement)
  token++; stopWatching(); ready.value = false; aside.value = false; box.value = null; target = null
  const back = returnTo
  returnTo = null
  if (had) void nextTick(() => {
    const to = back && back !== document.body && back.isConnected && visible(back) ? back : document.querySelector<HTMLElement>('[data-tour="settings"] button')
    to?.focus({ preventScroll: true })
  })
}, { immediate: true })

// the person went to another page by themselves: the tour ends here, remembering the step
watch(() => route.path, (p) => {
  if (tour.active.value && !pushing && p !== expectedPath) tour.leave()
})

// ── Keys ─────────────────────────────────────────────────────────────────────

function onCardKey(e: KeyboardEvent) {
  if (e.defaultPrevented) return
  // the keys that act on a list stay here: the page below has listeners of its own for them
  if (tourKeepsKey(e)) e.stopPropagation()
  const onButton = !!(e.target as HTMLElement | null)?.closest('button')
  const act = tourCardKey(e, { ready: ready.value, first: isFirst.value, onButton })
  if (!act) return
  e.preventDefault()
  if (act === 'leave') tour.leave()
  else if (act === 'next') tour.next()
  else tour.back()
}

// Esc from the app below leaves the tour too, but only when nothing else is using it: a focus on the page
// itself, not in a field or a menu. It leaves, it does not skip: the menu still offers to resume.
function onDocKey(e: KeyboardEvent) {
  if (!tour.active.value || e.key !== 'Escape' || e.defaultPrevented || e.repeat || aside.value) return
  const a = document.activeElement
  if (cardEl.value?.contains(a)) return
  if (!a || a === document.body || a === document.documentElement) tour.leave()
}

// ── The first visit ──────────────────────────────────────────────────────────

let decided = false
async function maybeAutostart() {
  if (!import.meta.client || decided || !user.value || tourIsPublicPath(route.path)) return
  decided = true
  tour.readStored()
  if (tour.stored.value) return // seen, skipped or left half way: asks nothing
  if (window.innerWidth < TOUR_MIN_WIDTH) return
  if (tour.knowsNoSample()) return // found without one earlier in this session: nothing is asked again
  const who = user.value.id
  if (!(await tour.ensureSample())) return
  await new Promise((r) => setTimeout(r, 900)) // the page settles first
  if (user.value?.id !== who) return
  // someone already typing (a link straight into a page with a field) is not interrupted: the
  // tour waits for the next page
  if (document.activeElement?.closest(FIELD)) { decided = false; return }
  tour.readStored()
  if (tourShouldAutostart({
    stored: tour.stored.value, sampleFound: true, width: window.innerWidth, path: route.path,
    modalOpen: overlayOpen(),
  }) && !tour.active.value) void tour.start()
}
// another person on this tab: a tour on screen was the other's, and what is remembered is read again
watch(() => user.value?.id, () => { decided = false; tour.leave(); tour.readStored(); void maybeAutostart() })
watch(() => route.path, () => { void maybeAutostart() })

let mql: MediaQueryList | null = null
const onNarrow = () => {
  tour.narrow.value = !!mql?.matches
  if (mql?.matches) tour.leave()
}

onMounted(() => {
  mql = window.matchMedia(`(max-width: ${TOUR_MIN_WIDTH - 1}px)`)
  mql.addEventListener('change', onNarrow)
  tour.narrow.value = mql.matches
  document.addEventListener('keydown', onDocKey)
  void maybeAutostart()
})
onBeforeUnmount(() => {
  token++
  stopWatching()
  mql?.removeEventListener('change', onNarrow)
  document.removeEventListener('keydown', onDocKey)
})

const spot = computed(() => (box.value ? tourSpotBox(box.value) : null))
</script>

<template>
  <Teleport to="body">
    <div v-if="step" v-show="!aside" class="tour" :class="{ ready }" data-tour-layer>
      <div
        v-if="spot && ready"
        class="tour-spot"
        aria-hidden="true"
        :style="{ left: `${spot.x}px`, top: `${spot.y}px`, width: `${spot.w}px`, height: `${spot.h}px` }"
      />
      <div
        ref="cardEl"
        class="tour-card"
        :class="`side-${pos.side}`"
        role="dialog"
        data-tour-card
        :aria-labelledby="`tour-title-${step.id}`"
        :aria-describedby="`tour-text-${step.id}`"
        tabindex="-1"
        :style="{ left: `${pos.x}px`, top: `${pos.y}px` }"
        @keydown="onCardKey"
      >
        <p class="tour-count">{{ t('tour.progress', { n: tour.index.value + 1, total: tour.total.value }) }}</p>
        <h2 :id="`tour-title-${step.id}`">{{ t(`tour.steps.${step.id}.title`) }}</h2>
        <p :id="`tour-text-${step.id}`" class="tour-text">{{ t(`tour.steps.${step.id}.text`) }}</p>
        <div class="tour-actions">
          <!-- at the last step there is nothing left to skip -->
          <button v-if="!isLast" type="button" class="tour-skip" @click="tour.skip()">{{ isFirst ? t('tour.notNow') : t('tour.skip') }}</button>
          <span class="tour-gap" />
          <button v-if="!isFirst" type="button" @click="tour.back()">{{ t('tour.back') }}</button>
          <button type="button" class="primary" @click="tour.next()">{{ isFirst ? t('tour.start') : isLast ? t('tour.done') : t('tour.next') }}</button>
        </div>
      </div>
      <p class="sr-only" role="status" aria-live="polite">{{ announce }}</p>
    </div>
  </Teleport>
</template>

<style scoped>
.tour-spot {
  position: fixed; z-index: 1790; pointer-events: none;
  border-radius: var(--radius);
  box-shadow: 0 0 0 2px var(--accent), 0 0 0 100vmax var(--scrim);
  transition: left var(--dur-3) var(--ease-out), top var(--dur-3) var(--ease-out), width var(--dur-3) var(--ease-out), height var(--dur-3) var(--ease-out);
}
.tour-card {
  position: fixed; z-index: 1800;
  width: min(360px, calc(100vw - 32px)); box-sizing: border-box;
  padding: 16px 18px 14px;
  background: var(--panel); color: var(--text);
  border: 1px solid var(--border); border-radius: 14px; box-shadow: var(--shadow-2);
  opacity: 0; pointer-events: none;
  transition: left var(--dur-3) var(--ease-out), top var(--dur-3) var(--ease-out), opacity var(--dur-2);
}
.tour.ready .tour-card { opacity: 1; pointer-events: auto; }
.tour-card:focus { outline: none; }
.tour-count { margin: 0 0 6px; font-size: 10.5px; font-weight: 600; letter-spacing: 0.05em; text-transform: uppercase; color: var(--muted); font-variant-numeric: tabular-nums; }
.tour-card h2 { margin: 0 0 6px; font-size: 15px; font-weight: 600; line-height: 1.3; }
.tour-text { margin: 0; font-size: 13px; line-height: 1.55; }
.tour-actions { display: flex; align-items: center; gap: 8px; margin-top: 14px; }
.tour-gap { flex: 1; }
.tour-skip { color: var(--muted); border-color: transparent; background: transparent; }
@media (prefers-reduced-motion: reduce) {
  .tour-spot, .tour-card { transition: none; }
}
</style>
