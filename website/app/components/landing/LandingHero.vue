<script setup lang="ts">
// The hero pins for about one extra screen of scroll: while it is pinned, the reader's own
// scroll turns the ring, one card per eighth of that distance, and after the last card the page
// simply carries on. Nothing captures the wheel or the touch: it is a sticky element and a
// passive scroll listener (read once per frame), so a fast flick goes straight through.
// Under reduced motion there is no pin (the ring is a static list of the eight points), and on
// phones and short windows neither: the hero is taller than the window there, and pinning it
// would hide its lower part (the deck turns by itself and with its buttons instead).
const t = useCopy()
const config = useRuntimeConfig()
const n = t.hero.cards.items.length

const pin = ref<HTMLElement | null>(null)
const scrollIndex = ref<number | null>(null) // the card the scroll position asks for; null outside the pin
const pinned = ref(true)
let raf = 0
function measure () {
  raf = 0
  const el = pin.value
  if (!el || !pinned.value) { scrollIndex.value = null; return }
  const r = el.getBoundingClientRect()
  const run = el.offsetHeight - window.innerHeight
  if (run <= 0) { scrollIndex.value = null; return }
  const p = -r.top / run
  scrollIndex.value = p < 0 ? 0 : p >= 1 ? n - 1 : Math.min(n - 1, Math.floor(p * n))
}
const onScroll = () => { if (!raf) raf = requestAnimationFrame(measure) }
// the same condition as the CSS that makes the hero sticky
const PIN_QUERY = '(min-width: 1001px) and (min-height: 680px) and (prefers-reduced-motion: no-preference)'
let mq: MediaQueryList | null = null
const onMotion = () => { pinned.value = !!mq?.matches; onScroll() }
onMounted(() => {
  mq = window.matchMedia(PIN_QUERY)
  pinned.value = mq.matches
  mq.addEventListener('change', onMotion)
  window.addEventListener('scroll', onScroll, { passive: true })
  window.addEventListener('resize', onScroll)
  measure()
})
onBeforeUnmount(() => {
  cancelAnimationFrame(raf)
  mq?.removeEventListener('change', onMotion)
  window.removeEventListener('scroll', onScroll)
  window.removeEventListener('resize', onScroll)
})
</script>

<template>
  <div ref="pin" class="hero-pin">
  <header id="top" class="hero">
    <nav class="nav" aria-label="Main">
      <NuxtLink to="/" class="nav__brand" :aria-label="t.nav.home">
        <BrandMark :size="32" />
        <span class="nav__name">Tabularia</span>
      </NuxtLink>
      <ul role="list" class="nav__links">
        <li class="hide-sm"><a href="#how">{{ t.nav.how }}</a></li>
        <li class="hide-sm"><a href="#engines">{{ t.nav.engines }}</a></li>
        <li class="hide-sm"><a href="#stack">{{ t.nav.stack }}</a></li>
        <li><NuxtLink to="/docs">{{ t.nav.docs }}</NuxtLink></li>
        <li class="hide-xs"><a :href="config.public.repo">{{ t.nav.github }}</a></li>
        <li><ThemeToggle /></li>
      </ul>
    </nav>

    <div class="hero__body">
      <div class="hero__copy">
        <h1 class="hero__title">
          <span>{{ t.hero.titleA }}</span>
          <span><span class="hero__trusted">{{ t.hero.titleB1 }}</span>{{ t.hero.titleB2 }}</span>
        </h1>
        <p class="hero__sentence">{{ t.hero.sentence }}</p>
        <p class="hero__actions">
          <NuxtLink to="/docs/getting-started" class="btn btn--accent">{{ t.nav.getStarted }}</NuxtLink>
          <NuxtLink to="/docs" class="btn">{{ t.hero.readDocs }}</NuxtLink>
        </p>
      </div>
      <LandingHeroWheel class="hero__wheel" :scroll-index="scrollIndex" skip-to="#hero-end" />
    </div>

    <a href="#how" class="hero__cue">
      <span>{{ t.hero.scroll }}</span>
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 5v14M6 13l6 6 6-6" /></svg>
    </a>
  </header>
  </div>
  <span id="hero-end" class="hero-end" tabindex="-1" />
</template>

<style scoped>
/* The pin: about one extra screen of scroll, during which the hero stays in place and the ring
   turns. Only where the whole hero fits in the window (two columns, at least 680px tall) and
   motion is welcome; on phones and short windows the hero scrolls normally (see PIN_QUERY). */
.hero-pin { position: relative; }
.hero { position: relative; min-height: 92svh; padding-inline: var(--gutter); display: flex; flex-direction: column; }
/* on very wide screens the hero takes a wider column than the sections below, so the ring has room */
@media (min-width: 1600px) {
  .hero { padding-inline: max(var(--margin), calc((100% - 1480px) / 2)); }
  .hero__cue { left: max(var(--margin), calc((100% - 1480px) / 2)); }
}
.hero-end { display: block; height: 0; outline: none; }
@media (min-width: 1001px) and (min-height: 680px) and (prefers-reduced-motion: no-preference) {
  .hero-pin { height: 200svh; }
  .hero { position: sticky; top: 0; height: 100svh; min-height: 0; overflow: hidden; }
}
.nav { display: flex; align-items: center; justify-content: space-between; gap: 16px; padding-top: 20px; }
.nav__brand { display: inline-flex; align-items: center; gap: 10px; text-decoration: none; color: var(--text); }
.nav__name { font-weight: 600; font-size: var(--fs-body); letter-spacing: -0.02em; }
.nav__links { display: flex; align-items: center; gap: 4px; margin: 0; padding: 0; list-style: none; }
.nav__links a {
  display: inline-block; padding: 10px 12px; border-radius: 6px; color: var(--soft); text-decoration: none; font-size: var(--fs-small);
  transition: color 160ms ease, background-color 160ms ease;
}
.nav__links a:hover { color: var(--text); background: var(--tile); }

/* the desktop hero: the message on the left, the wheel on the right, both centred on one line */
.hero__body {
  flex: 1; display: grid; grid-template-columns: minmax(0, 5fr) minmax(0, 6fr); gap: clamp(32px, 5vw, 80px);
  align-items: center; padding: 40px 0 72px;
}
.hero__title { font-size: clamp(56px, 6.2vw, 104px); line-height: .92; letter-spacing: -0.045em; font-weight: 600; }
.hero__title > span { display: block; }
.hero__trusted { color: var(--accent); }
.hero__sentence { margin-top: 28px; font-size: var(--fs-body); max-width: 40ch; }
.hero__actions { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; margin-top: 32px; }

.hero__cue {
  position: absolute; left: var(--gutter); bottom: 20px; display: inline-flex; align-items: center; gap: 8px;
  padding: 10px 14px; border-radius: 6px; background: var(--tile); color: var(--text);
  font-size: var(--fs-small); font-weight: 600; text-decoration: none; transition: background-color 160ms ease;
}
.hero__cue:hover { background: var(--tile-2); }
@media (prefers-reduced-motion: no-preference) {
  .hero__cue svg { animation: cue 2.4s var(--ease-out) infinite; }
}
@keyframes cue { 0%, 60%, 100% { transform: none; } 30% { transform: translateY(4px); } }

@media (max-width: 1000px) {
  .hero { min-height: 0; }
  .nav { padding-top: 14px; }
  .hero__body { grid-template-columns: 1fr; padding: 48px 0 96px; gap: 40px; }
  .hero__title { font-size: clamp(48px, 13vw, 96px); }
  .hide-sm { display: none; }
}
@media (max-width: 460px) { .hide-xs { display: none; } }
/* reduced motion: the eight points are a list; the message stays at the top beside it */
@media (prefers-reduced-motion: reduce) {
  .hero__body { align-items: start; }
  .hero__copy { position: sticky; top: 24px; }
}
</style>
