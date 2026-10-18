<script setup lang="ts">
// The no-lock-in argument as one full-width moment: a genuine set of six things, set as a list,
// with the brand's noise-to-rows band running underneath as the page's recurring thread.
const t = useCopy()
</script>

<template>
  <section id="open" class="open section" aria-labelledby="open-title" data-rise-group>
    <div class="open__head">
      <h2 id="open-title" class="section-title open__title" data-rise>{{ t.open.title }}</h2>
      <p class="open__lead" data-rise>{{ t.open.lead }}</p>
    </div>
    <ul role="list" class="open__list">
      <li v-for="it in t.open.items" :key="it.name" data-rise>
        <h3>{{ it.name }}</h3>
        <p>
          {{ it.text }}
          <NuxtLink v-if="it.link" to="/docs/dbt-export#limits" class="link-arrow">{{ it.link }}</NuxtLink>
        </p>
      </li>
    </ul>
  </section>
</template>

<style scoped>
.open { padding-inline: var(--gutter); }
.open__head { display: grid; gap: 20px; max-width: 760px; }
.open__title { max-width: 14ch; }
.open__lead { font-size: var(--fs-body); max-width: 60ch; }
.open__list { list-style: none; margin: 72px 0 0; padding: 0; display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 48px 40px; }
.open__list h3 { font-size: var(--fs-statement); line-height: 1.15; letter-spacing: -0.015em; font-weight: 600; }
.open__list p { margin-top: 10px; font-size: var(--fs-body); max-width: 36ch; }
.open__list .link-arrow { display: inline-block; margin-top: 4px; }
.open__band { margin: 80px calc(-1 * var(--margin)) 0; height: 140px; }
.open__band :deep(.noise-rows) { --np-ground: var(--ground); }
@media (max-width: 960px) {
  .open__list { grid-template-columns: 1fr 1fr; margin-top: 48px; }
}
@media (max-width: 600px) { .open__list { grid-template-columns: 1fr; gap: 32px; } .open__band { height: 100px; margin-top: 56px; } }
</style>
