<script setup lang="ts">
const t = useCopy()
const base = useRuntimeConfig().app.baseURL
</script>

<template>
  <section id="stack" class="stack section" aria-labelledby="stack-title" data-rise-group>
    <div class="stack__text">
      <h2 id="stack-title" class="section-title" data-rise>{{ t.stack.title }}</h2>
      <p class="stack__lead" data-rise>{{ t.stack.lead }}</p>
      <dl class="stack__list">
        <div v-for="it in t.stack.items" :key="it.name" data-rise>
          <dt>{{ it.name }}</dt>
          <dd>{{ it.text }}</dd>
        </div>
      </dl>
      <p data-rise><NuxtLink to="/docs/api" class="link-arrow">API, tokens and webhooks</NuxtLink></p>
    </div>
    <figure class="stack__pic" data-rise>
      <img :src="`${base}shots/lineage-marquez.jpg`" width="900" height="580" loading="lazy" decoding="async" :alt="t.stack.pictureAlt">
      <figcaption>{{ t.stack.pictureCaption }}</figcaption>
    </figure>
  </section>
</template>

<style scoped>
.stack { display: grid; grid-template-columns: minmax(0, 5fr) minmax(0, 6fr); gap: 64px; padding-inline: var(--gutter); align-items: start; }
.stack__text { display: grid; gap: 20px; }
.stack__lead { font-size: var(--fs-body); color: var(--soft); max-width: 44ch; }
.stack__list { margin: 12px 0 0; display: grid; gap: 22px; }
.stack__list dt { font-weight: 600; font-size: var(--fs-body); }
.stack__list dd { margin: 4px 0 0; font-size: var(--fs-body); max-width: 52ch; }
.stack__pic { margin: 0; position: sticky; top: 32px; }
.stack__pic img { width: 100%; height: auto; border-radius: var(--radius); background: #0B0E14; }
.stack__pic figcaption { margin-top: 12px; font-size: var(--fs-small); color: var(--soft); }
@media (max-width: 960px) {
  .stack { grid-template-columns: 1fr; gap: 40px; }
  .stack__pic { position: static; }
  /* on a phone the graph is shown at a readable size, cropped around the flow's job */
  .stack__pic { overflow: hidden; border-radius: var(--radius); }
  .stack__pic img { width: 190%; max-width: none; margin-left: -48%; border-radius: 0; }
}
</style>
