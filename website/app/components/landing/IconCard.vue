<script setup lang="ts">
// One point of a section as a card with its Lucide icon (the same icon set as the product).
// When the card comes into view (useReveal adds .is-in) the icon draws itself stroke by
// stroke, one card after the other (`index`); on hover it nudges and the card steps up one
// tone. Without script, and under reduced motion, the icon is simply there, drawn.
import type { Component } from 'vue'

defineProps<{ icon: Component, title: string, index?: number }>()
</script>

<template>
  <li class="icard" data-rise :style="{ '--i': index ?? 0 }">
    <span class="icard__icon" aria-hidden="true"><component :is="icon" :size="22" :stroke-width="1.75" /></span>
    <h3 class="icard__title">{{ title }}</h3>
    <div class="icard__body"><slot /></div>
  </li>
</template>

<style scoped>
.icard {
  display: grid; align-content: start; gap: 14px; padding: 26px 26px 28px; border-radius: var(--radius);
  background: var(--tile); transition: background-color 200ms ease;
}
.icard:hover { background: var(--tile-2); }
.icard__icon {
  display: grid; place-items: center; width: 46px; height: 46px; border-radius: 10px;
  background: var(--chip-bg); color: var(--chip-ink);
}
.icard__title { font-size: 21px; line-height: 1.2; letter-spacing: -0.015em; font-weight: 600; }
.icard__body { font-size: var(--fs-body); line-height: 1.55; }
.icard__body :deep(p) { max-width: 40ch; }

@media (prefers-reduced-motion: no-preference) {
  /* the icon draws itself when its card comes into view; every Lucide stroke is shorter than 100 units */
  :global(.rise-ready) .icard.is-in .icard__icon :deep(svg > *) {
    stroke-dasharray: 100; stroke-dashoffset: 100;
    animation: draw 1300ms var(--ease-out) forwards; animation-delay: calc(220ms + var(--i) * 110ms);
  }
  .icard__icon :deep(svg) { transition: transform 420ms var(--ease-out); }
  .icard:hover .icard__icon :deep(svg) { transform: translateY(-2px) rotate(-8deg) scale(1.08); }
}
@keyframes draw { to { stroke-dashoffset: 0; } }
</style>
