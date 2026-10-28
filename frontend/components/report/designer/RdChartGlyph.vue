<script setup lang="ts">
// The outline of a chart kind: what the design view and the kind picker draw instead of
// data (the preview shows the real chart). Drawn in a 40×28 box, in the current colour,
// the filled parts at a lower opacity.
import type { ChartKind } from '~/composables/reportDesignModel'

defineProps<{ kind: ChartKind; size?: number }>()
</script>

<template>
  <svg class="glyph" viewBox="0 0 40 28" :width="size ?? 40" :height="size ? (size * 28) / 40 : 28" aria-hidden="true" preserveAspectRatio="xMidYMid meet">
    <g fill="currentColor" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round">
      <template v-if="kind === 'column'">
        <rect x="5" y="14" width="5" height="11" rx="0.8" stroke="none" opacity="0.55" />
        <rect x="13" y="7" width="5" height="18" rx="0.8" stroke="none" opacity="0.85" />
        <rect x="21" y="11" width="5" height="14" rx="0.8" stroke="none" opacity="0.7" />
        <rect x="29" y="4" width="5" height="21" rx="0.8" stroke="none" />
        <line x1="3" y1="25.5" x2="37" y2="25.5" fill="none" opacity="0.5" />
      </template>
      <template v-else-if="kind === 'bar'">
        <rect x="5" y="3" width="22" height="4.5" rx="0.8" stroke="none" />
        <rect x="5" y="9.5" width="30" height="4.5" rx="0.8" stroke="none" opacity="0.85" />
        <rect x="5" y="16" width="15" height="4.5" rx="0.8" stroke="none" opacity="0.7" />
        <rect x="5" y="22.5" width="9" height="4" rx="0.8" stroke="none" opacity="0.55" />
        <line x1="4.4" y1="2" x2="4.4" y2="27" fill="none" opacity="0.5" />
      </template>
      <template v-else-if="kind === 'line'">
        <polyline points="4,21 11,15 18,17 25,9 32,11 37,5" fill="none" stroke-width="1.6" />
        <line x1="3" y1="25.5" x2="37" y2="25.5" fill="none" opacity="0.5" />
      </template>
      <template v-else-if="kind === 'area'">
        <polygon points="4,25 4,19 11,14 18,16 25,8 32,10 37,5 37,25" stroke="none" opacity="0.4" />
        <polyline points="4,19 11,14 18,16 25,8 32,10 37,5" fill="none" stroke-width="1.5" />
      </template>
      <template v-else-if="kind === 'pie'">
        <circle cx="20" cy="14" r="11" stroke="none" opacity="0.4" />
        <path d="M20 14 L20 3 A11 11 0 0 1 30.4 17.6 Z" stroke="none" />
      </template>
      <template v-else-if="kind === 'donut'">
        <circle cx="20" cy="14" r="9" fill="none" stroke-width="4.5" opacity="0.4" />
        <path d="M20 5 A9 9 0 0 1 28.6 16.6" fill="none" stroke-width="4.5" stroke-linecap="butt" />
      </template>
      <template v-else-if="kind === 'combo'">
        <rect x="5" y="13" width="5" height="12" rx="0.8" stroke="none" opacity="0.5" />
        <rect x="13" y="9" width="5" height="16" rx="0.8" stroke="none" opacity="0.5" />
        <rect x="21" y="15" width="5" height="10" rx="0.8" stroke="none" opacity="0.5" />
        <rect x="29" y="7" width="5" height="18" rx="0.8" stroke="none" opacity="0.5" />
        <polyline points="7.5,9 15.5,5 23.5,10 31.5,3" fill="none" stroke-width="1.6" />
      </template>
      <template v-else-if="kind === 'waterfall'">
        <rect x="4" y="12" width="6" height="13" rx="0.6" stroke="none" opacity="0.85" />
        <rect x="12" y="6" width="6" height="6" rx="0.6" stroke="none" opacity="0.6" />
        <rect x="20" y="6" width="6" height="9" rx="0.6" stroke="none" opacity="0.35" />
        <rect x="28" y="9" width="6" height="16" rx="0.6" stroke="none" />
        <line x1="3" y1="25.5" x2="37" y2="25.5" fill="none" opacity="0.5" />
      </template>
      <template v-else-if="kind === 'funnel'">
        <rect x="4" y="3" width="32" height="5" rx="1" stroke="none" />
        <rect x="8" y="10" width="24" height="5" rx="1" stroke="none" opacity="0.8" />
        <rect x="12" y="17" width="16" height="4.5" rx="1" stroke="none" opacity="0.6" />
        <rect x="16" y="23" width="8" height="4" rx="1" stroke="none" opacity="0.45" />
      </template>
      <template v-else-if="kind === 'treemap'">
        <rect x="3" y="3" width="19" height="22" rx="1" stroke="none" />
        <rect x="23.5" y="3" width="13.5" height="12" rx="1" stroke="none" opacity="0.7" />
        <rect x="23.5" y="16.5" width="7" height="8.5" rx="1" stroke="none" opacity="0.5" />
        <rect x="32" y="16.5" width="5" height="8.5" rx="1" stroke="none" opacity="0.35" />
      </template>
      <template v-else-if="kind === 'heatmap'">
        <template v-for="r in 4" :key="r">
          <rect v-for="c in 6" :key="c" :x="3 + (c - 1) * 5.8" :y="3 + (r - 1) * 5.6" width="5" height="4.8" rx="0.6"
                stroke="none" :opacity="0.2 + (((r * 7 + c * 5) % 9) / 9) * 0.8" />
        </template>
      </template>
      <template v-else-if="kind === 'scatter'">
        <circle cx="8" cy="20" r="1.8" stroke="none" />
        <circle cx="12" cy="16" r="1.8" stroke="none" opacity="0.7" />
        <circle cx="17" cy="18" r="1.8" stroke="none" />
        <circle cx="21" cy="11" r="1.8" stroke="none" opacity="0.7" />
        <circle cx="26" cy="13" r="1.8" stroke="none" />
        <circle cx="30" cy="7" r="1.8" stroke="none" opacity="0.7" />
        <circle cx="34" cy="9" r="1.8" stroke="none" />
        <line x1="3" y1="25.5" x2="37" y2="25.5" fill="none" opacity="0.5" />
      </template>
      <template v-else-if="kind === 'gauge'">
        <path d="M6 23 A14 14 0 0 1 34 23" fill="none" stroke-width="4" opacity="0.35" stroke-linecap="butt" />
        <path d="M6 23 A14 14 0 0 1 26 10.4" fill="none" stroke-width="4" stroke-linecap="butt" />
        <line x1="20" y1="23" x2="27" y2="14" fill="none" stroke-width="1.4" />
        <circle cx="20" cy="23" r="1.6" stroke="none" />
      </template>
      <template v-else-if="kind === 'bullet'">
        <rect x="3" y="8" width="34" height="12" rx="1" stroke="none" opacity="0.2" />
        <rect x="3" y="8" width="23" height="12" rx="1" stroke="none" opacity="0.2" />
        <rect x="3" y="11.5" width="20" height="5" rx="0.8" stroke="none" />
        <line x1="29" y1="6" x2="29" y2="22" fill="none" stroke-width="1.6" />
      </template>
    </g>
  </svg>
</template>

<style scoped>
.glyph { display: block; flex: none; }
</style>
