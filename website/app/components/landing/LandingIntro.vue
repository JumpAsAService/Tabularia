<script setup lang="ts">
const t = useCopy()
const lead = t.intro.find(p => p.strong)!
const rest = t.intro.filter(p => !p.strong)
</script>

<template>
  <section class="intro" aria-label="Why Tabularia" data-rise-group>
    <p class="intro__lead" data-rise>{{ lead.text }}</p>
    <div class="intro__rest">
      <p v-for="(p, i) in rest" :key="i" data-rise>{{ p.text }}</p>
      <p data-rise><NuxtLink to="/docs/overview" class="link-arrow">{{ t.introLink }}</NuxtLink></p>
    </div>
  </section>
</template>

<style scoped>
.intro {
  padding: clamp(40px, 6vh, 72px) var(--gutter) 0;
  display: grid; grid-template-columns: minmax(0, 5fr) minmax(0, 6fr); gap: clamp(32px, 5vw, 80px); align-items: start;
}
.intro__lead { font-size: var(--fs-statement); line-height: 1.25; letter-spacing: -0.01em; font-weight: 500; max-width: 26ch; }
.intro__rest { display: grid; gap: 1.1em; }
.intro__rest p { font-size: var(--fs-body); line-height: 1.55; max-width: 56ch; }
@media (max-width: 860px) { .intro { grid-template-columns: 1fr; gap: 24px; } }
</style>
