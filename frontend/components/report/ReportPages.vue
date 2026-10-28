<script setup lang="ts">
// A report's pages on screen: the SVG of each page (the render's `svg` format, laid out by
// the same Typst as the PDF) shown as an <img> from a Blob URL, never as markup in the
// page — the SVG holds the data's text. Over each page, where a click does something (a
// column's header sorts, a group's row closes or opens) is a button of its own: it emits
// `hotspot` and the owner renders again with the new view. Shared by the viewer
// (pages/report.vue) and the designer's preview.
//
// - `zoom`: 'fit' fits the page to the width; a number is the scale of the real size
//   (1 = 100%, a millimetre is 96/25.4 px). The scroll position is kept, as a fraction,
//   when the zoom changes, and as it is when new pages replace the old ones (a sort, a
//   group closed): the new images in view are decoded before they are swapped in, so
//   nothing jumps; the others load as they come near.
// - `busy`: a render is running. Without pages yet, a page-shaped skeleton; with pages,
//   they stay readable, a thin bar says something is coming.
// - `definition`: the report the pages come from, to place the hotspots on the table's
//   columns and name them; without it they are a fair guess around the text.
// Emits `page` (the page most in view, from 1) and `scale` (the zoom as displayed).
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  pageSizeMm, placeHotspots, type PlacedHotspot, type ReportDefinition, type ReportHotspot,
} from '~/composables/useReports'

const props = withDefaults(defineProps<{
  pages: string[]
  hotspots?: ReportHotspot[]
  zoom?: number | 'fit'
  busy?: boolean
  definition?: ReportDefinition | null
  /** the region's name for assistive technology (the report's name) */
  label?: string
}>(), { hotspots: () => [], zoom: 'fit', busy: false, definition: null, label: '' })

const emit = defineEmits<{
  (e: 'hotspot', h: ReportHotspot): void
  (e: 'page', n: number): void
  (e: 'scale', s: number): void
}>()

const { t } = useI18n()
const MM_PX = 96 / 25.4
const GUTTER = 24 // px around the pages: the desk under the paper

const scroller = ref<HTMLElement | null>(null)
const width = ref(0)

// ── the images: Blob URLs, swapped once the new ones in view are decoded ─────
const shown = ref<string[]>([])
const shownSvgs = ref<string[]>([])
let generation = 0
// the page most in view, from 1 (see «which page is in view» below)
const current = ref(1)
// the pages decoded before a swap: the one in view and its neighbours. The others load
// as they come near (loading="lazy"): decoding the 167 pages of a long report before
// showing any took some twelve seconds, and the memory of every page at once
const DECODE_AROUND = 2
// new pages on their way in (decoding): the region is still busy, the old pages still dim —
// the owner's render is over, but what is on screen is not its pages yet
const swapping = ref(false)
const pending = computed(() => props.busy || swapping.value)

function revokeLater(urls: string[]) {
  // the <img> has its new source by the next frame; the old URL can go
  if (urls.length) requestAnimationFrame(() => setTimeout(() => urls.forEach((u) => URL.revokeObjectURL(u)), 0))
}

watch(() => props.pages, async (pages) => {
  const mine = ++generation
  swapping.value = true
  const urls = pages.map((svg) => URL.createObjectURL(new Blob([svg], { type: 'image/svg+xml' })))
  const at = Math.min(current.value, urls.length) - 1
  await Promise.all(urls.slice(Math.max(0, at - 1), at + DECODE_AROUND + 1).map((u) => {
    const img = new Image()
    img.src = u
    return img.decode().catch(() => undefined) // an image that fails shows its alt text
  }))
  if (mine !== generation) {
    urls.forEach((u) => URL.revokeObjectURL(u))
    return
  }
  const old = shown.value
  shown.value = urls
  shownSvgs.value = pages
  swapping.value = false
  revokeLater(old)
}, { immediate: true })

onBeforeUnmount(() => {
  generation++
  shown.value.forEach((u) => URL.revokeObjectURL(u))
})

// ── sizes and zoom ───────────────────────────────────────────────────────────
const sizes = computed(() => shownSvgs.value.map((svg) => pageSizeMm(svg, props.definition)))
const widest = computed(() => sizes.value.reduce((m, s) => Math.max(m, s.w), 0) || pageSizeMm(undefined, props.definition).w)
const scale = computed(() => {
  if (props.zoom !== 'fit') return props.zoom
  const room = width.value - 2 * GUTTER
  return room > 0 ? room / (widest.value * MM_PX) : 1
})
watch(scale, (s) => emit('scale', s), { immediate: true })

// the place in the document stays where it was when the zoom changes: the line at the top
// of the view stays at the top (at the very top, it stays there), the middle stays across
watch(scale, (now, before) => {
  const el = scroller.value
  if (!el || !before || now === before || !el.scrollTop && !el.scrollLeft) return
  const fy = el.scrollTop / el.scrollHeight
  const fx = el.scrollWidth > el.clientWidth ? (el.scrollLeft + el.clientWidth / 2) / el.scrollWidth : 0.5
  nextTick(() => {
    el.scrollTop = Math.max(0, fy * el.scrollHeight)
    el.scrollLeft = Math.max(0, fx * el.scrollWidth - el.clientWidth / 2)
  })
})

const px = (mm: number) => `${Math.round(mm * MM_PX * scale.value * 100) / 100}px`

let resize: ResizeObserver | null = null
onMounted(() => {
  if (!scroller.value) return
  width.value = scroller.value.clientWidth
  resize = new ResizeObserver(() => { if (scroller.value) width.value = scroller.value.clientWidth })
  resize.observe(scroller.value)
})
onBeforeUnmount(() => resize?.disconnect())

// ── the hotspots ─────────────────────────────────────────────────────────────
// those of the pages on screen: until new pages are swapped in, the old hotspots stay
// with them (they were placed on those)
const shownHotspots = ref<ReportHotspot[]>([])
watch(shown, () => { shownHotspots.value = props.hotspots }, { flush: 'sync' })
watch(() => props.hotspots, (h) => { if (props.pages === shownSvgs.value) shownHotspots.value = h })
const placed = computed(() => placeHotspots(shownHotspots.value, props.definition))
const byPage = computed(() => {
  const out = new Map<number, PlacedHotspot[]>()
  for (const h of placed.value) out.set(h.page, [...(out.get(h.page) ?? []), h])
  return out
})

const pct = (part: number, whole: number) => `${(part / whole) * 100}%`
function boxStyle(h: PlacedHotspot, i: number) {
  const s = sizes.value[i]
  return { left: pct(h.box.x, s.w), top: pct(h.box.y, s.h), width: pct(h.box.w, s.w), height: pct(h.box.h, s.h) }
}

function groupName(h: ReportHotspot): string {
  const key = h.key ?? []
  const last = key[key.length - 1]
  return last === null || last === undefined ? t('report.emptyGroup') : String(last)
}
function hotLabel(h: PlacedHotspot): string {
  if (h.kind === 'sort') {
    const name = h.header ?? t('report.columnN', { n: (h.column ?? 0) + 1 })
    return t('report.sortBy', { name })
  }
  return h.closed ? t('report.expandGroup', { name: groupName(h) }) : t('report.collapseGroup', { name: groupName(h) })
}
// a stable key: Vue keeps the same button across a render, so the focus stays on it
const hotKey = (h: ReportHotspot) => `${h.kind}:${h.item}:${h.column ?? ''}:${JSON.stringify(h.key ?? null)}`

// ── which page is in view, and going to one ──────────────────────────────────
// the page whose top passed a third of the view; the last one when the view is at the end
// a page asked for (scrollToPage): it is the current one at once, and the pages the
// smooth scroll passes on its way do not change it — two quick «next» go two pages on
let seeking: { page: number; until: number } | null = null
let ticking = false
function setCurrent(n: number) {
  if (n === current.value) return
  current.value = n
  emit('page', n)
}
function onScroll() {
  if (ticking) return
  ticking = true
  requestAnimationFrame(() => {
    ticking = false
    const el = scroller.value
    if (!el) return
    const all = el.querySelectorAll<HTMLElement>('.rp-page')
    const mark = el.getBoundingClientRect().top + el.clientHeight / 3
    let n = 1
    all.forEach((p, i) => {
      if (p.getBoundingClientRect().top <= mark) n = i + 1
    })
    if (all.length && el.scrollTop + el.clientHeight >= el.scrollHeight - 2) n = all.length
    if (seeking) {
      if (n !== seeking.page && Date.now() < seeking.until) return
      seeking = null
    }
    setCurrent(n)
  })
}
watch(shown, () => nextTick(onScroll))

function scrollToPage(n: number, behavior: ScrollBehavior = 'smooth') {
  const el = scroller.value
  const page = el?.querySelectorAll<HTMLElement>('.rp-page')[n - 1]
  if (!el || !page) return
  const reduce = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
  seeking = { page: n, until: Date.now() + 900 }
  setCurrent(n)
  el.scrollTo({ top: page.offsetTop - GUTTER, behavior: reduce ? 'auto' : behavior })
}

defineExpose({ scrollToPage, scroller })

// the skeleton has the page's proportions, at the size the pages will have
const skeleton = computed(() => pageSizeMm(undefined, props.definition))
</script>

<template>
  <div
    ref="scroller"
    class="rp"
    tabindex="0"
    role="region"
    :aria-label="label || $t('report.pagesRegion')"
    :aria-busy="pending ? 'true' : 'false'"
    @scroll.passive="onScroll"
  >
    <div v-if="pending && shown.length" class="rp-progress" aria-hidden="true"><span /></div>
    <div class="rp-stack" :class="{ dim: pending && shown.length }" :style="{ padding: `${GUTTER}px` }">
      <template v-if="shown.length">
        <div
          v-for="(url, i) in shown"
          :key="i"
          class="rp-page"
          :style="{ width: px(sizes[i].w), aspectRatio: `${sizes[i].w} / ${sizes[i].h}` }"
        >
          <img :src="url" :alt="$t('report.pageAlt', { n: i + 1, total: shown.length })" loading="lazy" decoding="async" draggable="false" />
          <button
            v-for="h in byPage.get(i + 1) ?? []"
            :key="hotKey(h)"
            type="button"
            class="rp-hot"
            :class="[h.kind, { closed: h.closed }]"
            :style="boxStyle(h, i)"
            :aria-label="hotLabel(h)"
            :title="hotLabel(h)"
            @click="emit('hotspot', h)"
          />
        </div>
      </template>
      <div
        v-else-if="pending"
        class="rp-page rp-skeleton"
        :style="{ width: px(skeleton.w), aspectRatio: `${skeleton.w} / ${skeleton.h}` }"
        aria-hidden="true"
      >
        <!-- a header, three figures, a chart, a table's rows: sized by the page's width -->
        <span class="sk" style="width: 46%; aspect-ratio: 46 / 3.4" />
        <span class="sk rule" />
        <div class="sk-row">
          <span v-for="k in 3" :key="k" class="sk" style="width: 31.5%; aspect-ratio: 31.5 / 12" />
        </div>
        <span class="sk" style="width: 100%; aspect-ratio: 100 / 32" />
        <span
          v-for="r in 12"
          :key="r"
          class="sk"
          :style="{ width: r % 4 === 1 ? '100%' : '93%', marginLeft: r % 4 === 1 ? '0' : '7%', aspectRatio: r % 4 === 1 ? '100 / 2.8' : '93 / 2.2' }"
        />
      </div>
    </div>
  </div>
</template>

<style scoped>
.rp {
  position: relative; overflow: auto; min-height: 0; height: 100%;
  /* the desk under the paper: a step off the page's background, so a white page has an
     edge in the light themes too */
  background: color-mix(in srgb, var(--bg) 80%, var(--border)); outline: none; overscroll-behavior: contain;
  /* the scrollbar's room is kept: «fit» does not change width when it comes and goes */
  scrollbar-gutter: stable;
}
.rp:focus-visible { box-shadow: inset 0 0 0 2px var(--accent); }
.rp-stack {
  display: flex; flex-direction: column; align-items: center; gap: 20px;
  width: max-content; min-width: 100%; box-sizing: border-box;
  transition: opacity 0.18s ease 0.15s; /* a quick render does not blink */
}
.rp-stack.dim { opacity: 0.62; }
.rp-page {
  position: relative; flex-shrink: 0; background: #fff;
  border-radius: 2px; box-shadow: var(--shadow-2), 0 0 0 1px var(--border);
}
.rp-page img { display: block; width: 100%; height: 100%; user-select: none; }

/* where a click sorts or opens: invisible on the paper until pointed at or focused */
.rp-hot {
  position: absolute; padding: 0; margin: 0; min-height: 0; border: 0; border-radius: 2px;
  background: transparent; box-shadow: none; cursor: pointer;
}
.rp-hot:hover { background: color-mix(in srgb, var(--accent) 12%, transparent); box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--accent) 55%, transparent); }
.rp-hot:active { transform: none; background: color-mix(in srgb, var(--accent) 20%, transparent); }
.rp-hot:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; background: color-mix(in srgb, var(--accent) 10%, transparent); }

/* a render coming: a thin bar at the top of the view, the pages stay readable under it */
.rp-progress { position: sticky; top: 0; left: 0; height: 2px; margin-bottom: -2px; z-index: 2; overflow: hidden; }
.rp-progress span {
  position: absolute; inset: 0; width: 34%; background: var(--accent);
  animation: rp-slide 1.1s cubic-bezier(0.4, 0, 0.2, 1) infinite;
}
@keyframes rp-slide { from { transform: translateX(-100%); } to { transform: translateX(300%); } }

.rp-skeleton {
  background: var(--panel); box-shadow: 0 0 0 1px var(--border); overflow: hidden;
  display: flex; flex-direction: column; align-items: flex-start; padding: 6%; box-sizing: border-box;
}
/* heights from the width (aspect-ratio), spaces from the width (a margin's %): a page at
   any zoom has the same skeleton */
.rp-skeleton .sk { display: block; height: auto; border-radius: 3px; margin-bottom: 1.6%; background: var(--border-soft); }
.rp-skeleton .sk.rule { width: 100%; height: 1px; margin-bottom: 4%; }
.sk-row { display: flex; justify-content: space-between; width: 100%; margin-bottom: 2.4%; }
.sk-row .sk { margin-bottom: 0; }

@media (prefers-reduced-motion: reduce) {
  .rp-progress span { animation: none; width: 100%; opacity: 0.6; }
  .rp-stack { transition: none; }
}
</style>
