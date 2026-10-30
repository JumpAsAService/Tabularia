<script setup lang="ts">
// The «order from noise» mark (brand/BRAND.md): grid = text colour, fragments = muted,
// flow = accent. Below 24 px the reduced drawing takes over, as the brand requires.
const props = withDefaults(defineProps<{ size?: number, title?: string }>(), { size: 32, title: '' })
const uid = useId()
const small = computed(() => props.size < 24)
// the moment below plays when the page loads: true while hydrating what the server drew, and
// false for a mark born later, since most pages bring their own bar with their own mark
const enter = import.meta.server || useNuxtApp().isHydrating
</script>

<template>
  <svg
    v-if="!small" :width="size" :height="size" viewBox="0 0 48 48" fill="none" class="brand-mark" :class="{ enter }"
    :role="title ? 'img' : undefined" :aria-label="title || undefined" :aria-hidden="title ? undefined : 'true'"
  >
    <clipPath :id="`${uid}-a`"><path d="M48 0V48H0V41C16 41 22 9.5 48 8Z" /></clipPath>
    <mask :id="`${uid}-g`" maskUnits="userSpaceOnUse" x="0" y="0" width="48" height="48"><rect width="48" height="48" fill="#fff" /><path d="M5 39C19 39 24 10 43 9" fill="none" stroke="#000" stroke-width="9" /></mask>
    <g class="order"><g :mask="`url(#${uid}-g)`"><g :clip-path="`url(#${uid}-a)`" fill="none" stroke="var(--mark-grid)" stroke-width="3" stroke-linejoin="round"><rect x="6" y="6" width="36" height="36" rx="8" /><path d="M18 6v36M30 6v36M6 18h36M6 30h36" /></g></g></g>
    <g class="noise" fill="var(--mark-noise)"><rect x="9" y="9.5" width="7" height="3" rx="1.5" /><rect x="5.5" y="17" width="5" height="3" rx="1.5" /><rect x="11" y="24" width="5.5" height="3" rx="1.5" /></g>
    <path class="flow" pathLength="1" d="M5 39C19 39 24 10 43 9" fill="none" stroke="var(--mark-flow)" stroke-width="4.6" stroke-linecap="round" />
  </svg>
  <svg
    v-else :width="size" :height="size" viewBox="0 0 24 24" fill="none" class="brand-mark" :class="{ enter }"
    :role="title ? 'img' : undefined" :aria-label="title || undefined" :aria-hidden="title ? undefined : 'true'"
  >
    <clipPath :id="`${uid}-a`"><path d="M24 0V24H0V20.5C8 20.5 11 5 24 4Z" /></clipPath>
    <mask :id="`${uid}-g`" maskUnits="userSpaceOnUse" x="0" y="0" width="24" height="24"><rect width="24" height="24" fill="#fff" /><path d="M2.5 19.5C9.5 19.5 12 5 21.5 4.5" fill="none" stroke="#000" stroke-width="5.4" /></mask>
    <g class="order"><g :mask="`url(#${uid}-g)`"><g :clip-path="`url(#${uid}-a)`" fill="none" stroke="var(--mark-grid)" stroke-width="2.2" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="4.5" /><path d="M12 3v18M3 12h18" /></g></g></g>
    <g class="noise" fill="var(--mark-noise)"><rect x="4" y="5" width="4.5" height="2.2" rx="1.1" /><rect x="3" y="10" width="3.4" height="2.2" rx="1.1" /></g>
    <path class="flow" pathLength="1" d="M2.5 19.5C9.5 19.5 12 5 21.5 4.5" fill="none" stroke="var(--mark-flow)" stroke-width="2.8" stroke-linecap="round" />
  </svg>
</template>

<style scoped>
/* The mark's one moment, on a full load of the page only and never on a change of page (see
   the script): the flow draws itself and the grid appears as it passes, because order only
   exists after the transformation. CSS, so it starts with the first paint and waits for
   nothing; what it animates from is all in the keyframes, so without it the mark is simply
   complete. */
@media (prefers-reduced-motion: no-preference) {
  .enter .noise { animation: mark-noise 240ms cubic-bezier(0.16, 1, 0.3, 1) backwards; }
  .enter .flow { animation: mark-flow 620ms cubic-bezier(0.33, 0, 0.2, 1) backwards; }
  .enter .order { animation: mark-order 560ms cubic-bezier(0.33, 0, 0.2, 1) 60ms backwards; }
}
@keyframes mark-noise { from { opacity: 0; } }
/* the dash grows from the start of the path; its gap outlasts the path, or the pattern's next
   dash would show its round cap at the far end */
@keyframes mark-flow { from { stroke-dasharray: 0 2; } to { stroke-dasharray: 1 2; } }
@keyframes mark-order { from { clip-path: inset(0 100% 0 0); } to { clip-path: inset(0 0 0 0); } }
</style>
