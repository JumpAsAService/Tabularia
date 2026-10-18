<script setup lang="ts">
definePageMeta({ layout: 'docs' })
const desc = 'Install, operate and integrate Tabularia: deployment, configuration, connectors, dynamic inputs and outputs, API, dbt export, OpenLineage, SSO and audit.'
useSeoMeta({ title: 'Documentation', description: desc, ogTitle: 'Tabularia documentation', ogDescription: desc })
const { data: nav } = await useAsyncData('docs-nav-index', async () => {
  const all = await queryCollection('docs').select('path', 'title', 'description').all()
  const groups = buildDocsNav([...all, { path: '/docs/overview', title: 'Overview (README)' }])
  const desc = new Map(all.map(d => [d.path, d.description]))
  desc.set('/docs/overview', 'What Tabularia is, what it does and how it is built: the project README.')
  return groups.map(g => ({ ...g, items: g.items.map(i => ({ ...i, description: desc.get(i.path) || '' })) }))
})
</script>

<template>
  <main id="main" class="docs-index">
    <h1>Documentation</h1>
    <p class="docs-index__lead">
      How to install Tabularia on your servers, connect it to your data and your stack, and keep it governed.
      New here? Start with <NuxtLink to="/docs/getting-started">Getting started</NuxtLink>.
    </p>
    <div class="docs-index__groups">
      <section v-for="g in nav" :key="g.title" class="docs-index__group">
        <h2>{{ g.title }}</h2>
        <ul role="list">
          <li v-for="i in g.items" :key="i.path">
            <NuxtLink :to="i.path">
              <span class="docs-index__title">{{ i.title }}</span>
              <span v-if="i.description" class="docs-index__desc">{{ i.description }}</span>
            </NuxtLink>
          </li>
        </ul>
      </section>
    </div>
  </main>
</template>

<style scoped>
.docs-index { max-width: 1440px; margin: 0 auto; padding: 48px 16px 0; }
.docs-index h1 { font-size: var(--fs-h2); line-height: 1; letter-spacing: -0.035em; font-weight: 600; }
.docs-index__lead { margin-top: 20px; max-width: 60ch; font-size: var(--fs-statement); line-height: 1.3; color: var(--soft); }
.docs-index__lead a { color: var(--accent-ink); }
.docs-index__groups { margin-top: 56px; display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 8px; }
.docs-index__group { background: var(--tile); border-radius: var(--radius); padding: 24px; }
.docs-index__group h2 { font-size: 20px; font-weight: 600; letter-spacing: -0.01em; margin-bottom: 12px; }
.docs-index__group ul { list-style: none; margin: 0; padding: 0; display: grid; gap: 4px; }
.docs-index__group a {
  display: grid; gap: 2px; padding: 10px 12px; margin: 0 -12px; border-radius: 6px; text-decoration: none; color: var(--text);
  transition: background-color 160ms ease;
}
.docs-index__group a:hover { background: var(--tile-2); }
.docs-index__title { font-weight: 600; }
.docs-index__desc { font-size: 14px; color: var(--soft); line-height: 1.45; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
</style>
