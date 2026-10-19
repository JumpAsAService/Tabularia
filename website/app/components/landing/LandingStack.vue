<script setup lang="ts">
// The systems Tabularia talks to: the claim and Marquez receiving a run beside it, then the six
// integrations as cards with their icons (the product's own icon set).
import { Activity, KeyRound, Library, ShieldCheck, Sparkles, Workflow } from 'lucide-vue-next'

const t = useCopy()
// in the order of t.stack.items
const ICONS = [Workflow, Library, ShieldCheck, KeyRound, Activity, Sparkles]
const base = useRuntimeConfig().app.baseURL
</script>

<template>
  <section id="stack" class="stack section" aria-labelledby="stack-title" data-rise-group>
    <div class="stack__text">
      <h2 id="stack-title" class="section-title" data-rise>{{ t.stack.title }}</h2>
      <p class="stack__lead" data-rise>{{ t.stack.lead }}</p>
      <p data-rise><NuxtLink to="/docs/api" class="link-arrow">API, tokens and webhooks</NuxtLink></p>
    </div>
    <figure class="stack__pic" data-rise>
      <img :src="`${base}shots/lineage-marquez.jpg`" width="900" height="580" loading="lazy" decoding="async" :alt="t.stack.pictureAlt">
      <figcaption>{{ t.stack.pictureCaption }}</figcaption>
    </figure>
    <ul role="list" class="stack__list">
      <LandingIconCard v-for="(it, i) in t.stack.items" :key="it.name" :icon="ICONS[i] ?? Workflow" :title="it.name" :index="i">
        <p>{{ it.text }}</p>
      </LandingIconCard>
    </ul>
  </section>
</template>

<style scoped>
.stack { display: grid; grid-template-columns: minmax(0, 5fr) minmax(0, 6fr); gap: 64px; padding-inline: var(--gutter); align-items: center; }
.stack__text { display: grid; gap: 20px; align-content: center; }
.stack__lead { font-size: var(--fs-body); color: var(--soft); max-width: 44ch; }
.stack__list {
  grid-column: 1 / -1; list-style: none; margin: 0; padding: 0;
  display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: var(--gap);
}
.stack__pic { margin: 0; }
.stack__pic img { width: 100%; height: auto; border-radius: var(--radius); background: #0B0E14; }
.stack__pic figcaption { margin-top: 12px; font-size: var(--fs-small); color: var(--soft); }
@media (max-width: 960px) {
  .stack { grid-template-columns: 1fr; gap: 40px; }
  .stack__list { grid-template-columns: 1fr 1fr; }
  /* on a phone the graph is shown at a readable size, cropped around the flow's job */
  .stack__pic { overflow: hidden; border-radius: var(--radius); }
  .stack__pic img { width: 190%; max-width: none; margin-left: -48%; border-radius: 0; }
}
@media (max-width: 600px) { .stack__list { grid-template-columns: 1fr; } }
</style>
