<script setup lang="ts">
// The no-lock-in argument as one full-width moment: a genuine set of six things, each a card
// with its icon (the product's own icon set), drawn as the card comes into view.
import { Braces, Database, GitFork, Network, PackageOpen, Server } from 'lucide-vue-next'

const t = useCopy()
// in the order of t.open.items
const ICONS = [GitFork, Server, Database, Braces, Network, PackageOpen]
</script>

<template>
  <section id="open" class="open section" aria-labelledby="open-title" data-rise-group>
    <div class="open__head">
      <h2 id="open-title" class="section-title open__title" data-rise>{{ t.open.title }}</h2>
      <p class="open__lead" data-rise>{{ t.open.lead }}</p>
    </div>
    <ul role="list" class="open__list">
      <LandingIconCard v-for="(it, i) in t.open.items" :key="it.name" :icon="ICONS[i] ?? Braces" :title="it.name" :index="i">
        <p>
          {{ it.text }}
          <NuxtLink v-if="it.link" to="/docs/dbt-export#limits" class="link-arrow">{{ it.link }}</NuxtLink>
        </p>
      </LandingIconCard>
    </ul>
  </section>
</template>

<style scoped>
.open { padding-inline: var(--gutter); }
.open__head { display: grid; gap: 20px; max-width: 760px; }
.open__title { max-width: 14ch; }
.open__lead { font-size: var(--fs-body); max-width: 60ch; }
.open__list { list-style: none; margin: 64px 0 0; padding: 0; display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: var(--gap); }
.open__list .link-arrow { display: inline-block; margin-top: 4px; }
.open__band { margin: 80px calc(-1 * var(--margin)) 0; height: 140px; }
.open__band :deep(.noise-rows) { --np-ground: var(--ground); }
@media (max-width: 960px) {
  .open__list { grid-template-columns: 1fr 1fr; margin-top: 48px; }
}
@media (max-width: 600px) { .open__list { grid-template-columns: 1fr; } .open__band { height: 100px; margin-top: 56px; } }
</style>
