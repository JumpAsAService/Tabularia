<script setup lang="ts">
// The Tabularia mark, «order from noise» (brand/board.html is the reference,
// brand/BRAND.md the rules): loose fragments before a flow, a clean table grid
// after it. Geometry is copied verbatim from the board's #M and #m symbols; only
// the colours come from the theme, so the mark follows all five themes on its own:
//  - GRID      = text colour (order),
//  - FRAGMENTS = muted colour (noise),
//  - FLOW      = accent (the transformation: the same colour as the buttons).
// Below 24 px a 3×3 grid and three fragments turn into a smudge, so the REDUCED
// mark takes over (two fragments, a 2×2 grid, its own 24-unit drawing).
// The channel the flow cuts through the grid is a mask, not a stroke in the
// background colour, and the grid is clipped to the side after the flow: the mark
// sits on any surface (bar, panel, card).
import { computed, useId } from 'vue'

const props = withDefaults(defineProps<{ size?: number; title?: string }>(), { size: 28, title: '' })
const small = computed(() => props.size < 24)
// mask / clip ids unique per instance and identical on server and browser
const uid = useId()
const maskId = `bm-gap-${uid}`
const clipId = `bm-after-${uid}`
</script>

<template>
  <svg
    class="brand-mark-svg"
    :width="size"
    :height="size"
    :viewBox="small ? '0 0 24 24' : '0 0 48 48'"
    fill="none"
    :role="title ? 'img' : undefined"
    :aria-label="title || undefined"
    :aria-hidden="title ? undefined : 'true'"
    focusable="false"
  >
    <template v-if="small">
      <clipPath :id="clipId"><path d="M24 0V24H0V20.5C8 20.5 11 5 24 4Z" /></clipPath>
      <mask :id="maskId" maskUnits="userSpaceOnUse" x="0" y="0" width="24" height="24">
        <rect width="24" height="24" fill="#fff" />
        <path d="M2.5 19.5C9.5 19.5 12 5 21.5 4.5" stroke="#000" stroke-width="5.4" />
      </mask>
      <g :mask="`url(#${maskId})`">
        <g :clip-path="`url(#${clipId})`" class="grid" stroke-width="2.2" stroke-linejoin="round">
          <rect x="3" y="3" width="18" height="18" rx="4.5" />
          <path d="M12 3v18M3 12h18" />
        </g>
      </g>
      <g class="noise">
        <rect x="4" y="5" width="4.5" height="2.2" rx="1.1" />
        <rect x="3" y="10" width="3.4" height="2.2" rx="1.1" />
      </g>
      <path class="flow" d="M2.5 19.5C9.5 19.5 12 5 21.5 4.5" stroke-width="2.8" stroke-linecap="round" />
    </template>
    <template v-else>
      <clipPath :id="clipId"><path d="M48 0V48H0V41C16 41 22 9.5 48 8Z" /></clipPath>
      <mask :id="maskId" maskUnits="userSpaceOnUse" x="0" y="0" width="48" height="48">
        <rect width="48" height="48" fill="#fff" />
        <path d="M5 39C19 39 24 10 43 9" stroke="#000" stroke-width="9" />
      </mask>
      <g :mask="`url(#${maskId})`">
        <g :clip-path="`url(#${clipId})`" class="grid" stroke-width="3" stroke-linejoin="round">
          <rect x="6" y="6" width="36" height="36" rx="8" />
          <path d="M18 6v36M30 6v36M6 18h36M6 30h36" />
        </g>
      </g>
      <g class="noise">
        <rect x="9" y="9.5" width="7" height="3" rx="1.5" />
        <rect x="5.5" y="17" width="5" height="3" rx="1.5" />
        <rect x="11" y="24" width="5.5" height="3" rx="1.5" />
      </g>
      <path class="flow" d="M5 39C19 39 24 10 43 9" stroke-width="4.6" stroke-linecap="round" />
    </template>
  </svg>
</template>

<style scoped>
.brand-mark-svg { display: block; flex: none; }
.grid { stroke: var(--text); }
.noise { fill: var(--muted); }
.flow { stroke: var(--accent); }
</style>
