<script setup lang="ts">
definePageMeta({ layout: 'docs' })
const desc = 'Install, use and integrate Tabularia: capabilities, sources, connections, integrations, security and governance, deployment and configuration.'
useSeoMeta({ title: 'Documentation', description: desc, ogTitle: 'Tabularia documentation', ogDescription: desc })
const { data: nav } = await useAsyncData('docs-nav-index', async () => {
  const all = await queryCollection('docs').select('path', 'title').all()
  return buildDocsNav([...all, { path: '/docs/overview', title: 'Overview (README)' }])
})

// the path a newcomer follows, in order
const START = [
  { to: '/docs/getting-started', step: '1', title: 'Install it', text: 'One machine with Docker Compose, or Kubernetes with the Helm chart.' },
  { to: '/docs/concepts', step: '2', title: 'Learn the concepts', text: 'Folders, connections, datasources, flows, runs: the ideas everything is built on.' },
  { to: '/docs/capabilities/flows', step: '3', title: 'Build a first flow', text: 'From a source to a published datasource, step by step, on the sample data.' },
]
// the sections shown as tiles: Getting started is the path above
const sections = computed(() => (nav.value || []).filter(g => g.icon !== 'start'))
</script>

<template>
  <main id="main" class="docs-index">
    <header class="docs-index__head">
      <h1>Documentation</h1>
      <p class="docs-index__lead">
        How to install Tabularia on your own infrastructure, connect it to your data, build and orchestrate
        your work on it, and keep it governed.
      </p>
    </header>

    <section class="docs-index__start" aria-labelledby="start-title">
      <h2 id="start-title" class="docs-index__eyebrow">Start here</h2>
      <ol role="list">
        <li v-for="s in START" :key="s.to">
          <NuxtLink :to="s.to">
            <span class="docs-index__step" aria-hidden="true">{{ s.step }}</span>
            <span class="docs-index__steptitle">{{ s.title }}</span>
            <span class="docs-index__steptext">{{ s.text }}</span>
          </NuxtLink>
        </li>
      </ol>
    </section>

    <div class="docs-index__groups">
      <section v-for="g in sections" :key="g.title" class="docs-index__group" :aria-labelledby="`g-${g.icon}`">
        <div class="docs-index__grouphead">
          <span class="docs-index__icon"><DocsIcon :name="g.icon" :size="20" /></span>
          <h2 :id="`g-${g.icon}`">{{ g.title }}</h2>
        </div>
        <p class="docs-index__intro">{{ g.intro }}</p>
        <ul role="list">
          <li v-for="i in g.items" :key="i.path">
            <NuxtLink :to="i.path">{{ i.title }}</NuxtLink>
          </li>
        </ul>
      </section>
    </div>
  </main>
</template>

<style scoped>
.docs-index { max-width: 1240px; margin: 0 auto; padding: 56px 16px 0; }
.docs-index h1 { font-size: var(--fs-h2); line-height: 1; letter-spacing: -0.035em; font-weight: 600; }
.docs-index__lead { margin-top: 20px; max-width: 58ch; font-size: var(--fs-statement); line-height: 1.3; color: var(--soft); }

.docs-index__start { margin-top: 56px; }
.docs-index__eyebrow { font-size: 14px; font-weight: 600; letter-spacing: 0.02em; color: var(--soft); margin-bottom: 12px; }
.docs-index__start ol { list-style: none; margin: 0; padding: 0; display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; }
.docs-index__start a {
  display: grid; grid-template-columns: auto 1fr; column-gap: 14px; row-gap: 4px; height: 100%;
  padding: 20px 22px; border-radius: var(--radius); background: var(--tile); color: var(--text); text-decoration: none;
  transition: background-color 160ms ease;
}
.docs-index__start a:hover { background: var(--tile-2); }
.docs-index__step {
  grid-row: span 2; display: grid; place-items: center; width: 32px; height: 32px; border-radius: 50%;
  background: var(--chip-bg); color: var(--chip-ink); font-family: var(--mono); font-size: 14px; font-weight: 600;
}
.docs-index__steptitle { font-weight: 600; font-size: 17px; }
.docs-index__steptext { font-size: 15px; line-height: 1.45; color: var(--soft); }

.docs-index__groups { margin-top: 40px; display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); gap: 8px; }
.docs-index__group { background: var(--tile); border-radius: var(--radius); padding: 24px 24px 18px; display: flex; flex-direction: column; }
.docs-index__grouphead { display: flex; align-items: center; gap: 12px; }
.docs-index__icon {
  display: grid; place-items: center; width: 36px; height: 36px; flex: none; border-radius: 8px;
  background: var(--chip-bg); color: var(--chip-ink);
}
.docs-index__group h2 { font-size: 20px; font-weight: 600; letter-spacing: -0.01em; }
.docs-index__intro { margin-top: 12px; font-size: 15px; line-height: 1.5; color: var(--soft); }
.docs-index__group ul {
  list-style: none; margin: 14px 0 0; padding: 12px 0 0; border-top: 1px solid var(--tile-3);
  display: grid; gap: 0;
}
.docs-index__group a {
  display: block; padding: 6px 10px; margin: 0 -10px; border-radius: 6px; font-size: 15px; line-height: 1.4;
  color: var(--text); text-decoration: none; transition: background-color 160ms ease;
}
.docs-index__group a:hover { background: var(--tile-2); }

@media (max-width: 860px) {
  .docs-index { padding-top: 40px; }
  .docs-index__start ol { grid-template-columns: minmax(0, 1fr); }
  .docs-index__groups { grid-template-columns: minmax(0, 1fr); }
}
</style>
