<script setup lang="ts">
definePageMeta({ layout: 'docs' })

const route = useRoute()
const config = useRuntimeConfig()
const path = computed(() => route.path.replace(/\/+$/, '') || '/docs')

const { data: page } = await useAsyncData(`doc:${path.value}`, () =>
  path.value === '/docs/overview'
    ? queryCollection('readme').first()
    : queryCollection('docs').path(path.value).first(),
)
if (!page.value) {
  throw createError({ statusCode: 404, statusMessage: 'Page not found', fatal: true })
}

const { data: nav } = await useAsyncData('docs-nav', async () => {
  const all = await queryCollection('docs').select('path', 'title').all()
  return buildDocsNav([...all, { path: '/docs/overview', title: 'Overview (README)' }])
})

const flat = computed(() => flattenNav(nav.value || []))
const index = computed(() => flat.value.findIndex(i => i.path === path.value))
const prev = computed(() => (index.value > 0 ? flat.value[index.value - 1] : null))
const next = computed(() => (index.value >= 0 && index.value < flat.value.length - 1 ? flat.value[index.value + 1] : null))
const group = computed(() => (nav.value || []).find(g => g.items.some(i => i.path === path.value))?.title)

const toc = computed(() => (page.value as any)?.body?.toc?.links || [])

// the file this page was generated from, for "Edit this page" (null when it is not on GitHub)
const editUrl = computed(() => {
  const source = (page.value as any)?.meta?.source
  return source ? `${config.public.repo}/blob/${config.public.branch}/${source}` : null
})

const isOverview = computed(() => path.value === '/docs/overview')
const title = computed(() => (isOverview.value ? 'Overview' : (page.value as any)?.title || 'Documentation'))
const description = computed(() => (isOverview.value
  ? 'What Tabularia is and how it is built: a self-hosted, open-source visual data-preparation platform on pluggable engines.'
  : (page.value as any)?.description || 'Tabularia documentation'))
useSeoMeta({
  title,
  description,
  ogTitle: computed(() => `${title.value} · Tabularia docs`),
  ogDescription: description,
})

const navOpen = ref(false)
watch(() => route.path, () => { navOpen.value = false })

// the sidebar's sections fold: the one of the current page is open, the others as the reader left them
const opened = ref<Record<string, boolean>>({})
function isOpen (title: string) { return opened.value[title] ?? title === group.value }
function toggle (title: string) { opened.value = { ...opened.value, [title]: !isOpen(title) } }
watch(group, (g) => { if (g) opened.value = { ...opened.value, [g]: true } })
</script>

<template>
  <div class="docs">
    <button type="button" class="docs__navtoggle" :aria-expanded="navOpen" aria-controls="docs-nav" @click="navOpen = !navOpen">
      <span>{{ group || 'Documentation' }}</span>
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="m6 9 6 6 6-6" /></svg>
    </button>
    <nav id="docs-nav" class="docs__nav" :class="{ 'is-open': navOpen }" aria-label="Documentation">
      <div v-for="g in nav" :key="g.title" class="docs__group">
        <button
          type="button" class="docs__grouptoggle" :aria-expanded="isOpen(g.title)" :aria-controls="`nav-${g.icon}`"
          @click="toggle(g.title)"
        >
          <DocsIcon :name="g.icon" :size="16" />
          <span>{{ g.title }}</span>
          <svg class="docs__chevron" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="m6 9 6 6 6-6" /></svg>
        </button>
        <ul v-show="isOpen(g.title)" :id="`nav-${g.icon}`" role="list">
          <li v-for="item in g.items" :key="item.path">
            <NuxtLink :to="item.path" class="docs__link" :aria-current="item.path === path ? 'page' : undefined">{{ item.title }}</NuxtLink>
          </li>
        </ul>
      </div>
    </nav>

    <main id="main" class="docs__main">
      <p v-if="group && !isOverview" class="docs__crumbs">
        <NuxtLink to="/docs">Documentation</NuxtLink>
        <span aria-hidden="true">/</span>
        <span>{{ group }}</span>
      </p>
      <article>
        <ContentRenderer v-if="page" :value="page" class="prose" />
      </article>
      <p v-if="editUrl" class="docs__edit"><a :href="editUrl">Edit this page on GitHub</a></p>
      <nav class="docs__pager" aria-label="Previous and next page">
        <NuxtLink v-if="prev" :to="prev.path" class="docs__pagelink docs__pagelink--prev">
          <span class="docs__pagedir">Previous</span>
          <span class="docs__pagetitle">{{ prev.title }}</span>
        </NuxtLink>
        <span v-else />
        <NuxtLink v-if="next" :to="next.path" class="docs__pagelink docs__pagelink--next">
          <span class="docs__pagedir">Next</span>
          <span class="docs__pagetitle">{{ next.title }}</span>
        </NuxtLink>
      </nav>
    </main>

    <aside v-if="toc.length" class="docs__toc" aria-label="On this page">
      <p class="docs__grouptitle">On this page</p>
      <ul role="list">
        <li v-for="l in toc" :key="l.id">
          <a :href="`#${l.id}`">{{ l.text }}</a>
          <ul v-if="l.children?.length" role="list">
            <li v-for="c in l.children" :key="c.id"><a :href="`#${c.id}`">{{ c.text }}</a></li>
          </ul>
        </li>
      </ul>
    </aside>
  </div>
</template>

<style scoped>
.docs {
  display: grid; grid-template-columns: 264px minmax(0, 1fr) 240px; gap: 48px;
  max-width: 1440px; margin: 0 auto; padding: 0 16px;
}
.docs__navtoggle { display: none; }
.docs__nav {
  position: sticky; top: 64px; align-self: start; max-height: calc(100vh - 64px); overflow-y: auto;
  padding: 28px 8px 48px 0;
}
.docs__group + .docs__group { margin-top: 4px; }
.docs__grouptitle { font-size: 14px; font-weight: 600; color: var(--text); margin: 0 0 6px 10px; }
.docs__grouptoggle {
  display: flex; align-items: center; gap: 10px; width: 100%; min-height: 36px; padding: 6px 10px;
  border: 0; border-radius: 6px; background: none; color: var(--text); font: inherit; font-size: 14px; font-weight: 600;
  text-align: left; cursor: pointer;
}
.docs__grouptoggle:hover { background: var(--tile); }
.docs__grouptoggle > svg:first-child { color: var(--soft); flex: none; }
.docs__chevron { margin-left: auto; color: var(--soft); transition: transform 160ms ease; }
.docs__grouptoggle[aria-expanded="false"] .docs__chevron { transform: rotate(-90deg); }
.docs__nav .docs__group ul { margin: 2px 0 10px; padding-left: 16px; border-left: 1px solid var(--tile-3); margin-left: 18px; }
.docs__crumbs { display: flex; gap: 8px; font-size: 14px; color: var(--soft); margin-bottom: 16px; }
.docs__crumbs a { color: var(--soft); text-decoration: none; }
.docs__crumbs a:hover { color: var(--text); }
.docs__nav ul, .docs__toc ul { list-style: none; margin: 0; padding: 0; }
.docs__link {
  display: block; padding: 6px 10px; border-radius: 6px; font-size: 15px; line-height: 1.35;
  color: var(--soft); text-decoration: none;
}
.docs__link:hover { color: var(--text); background: var(--tile); }
.docs__link[aria-current="page"] { color: var(--text); background: var(--tile-2); font-weight: 600; }

.docs__main { padding: 40px 0 0; min-width: 0; }
.docs__edit { margin-top: 56px; font-size: 15px; }
.docs__edit a { color: var(--accent-ink); }
.docs__pager { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: 24px; max-width: 76ch; }
.docs__pagelink {
  display: grid; gap: 4px; padding: 16px 20px; border-radius: var(--radius); background: var(--tile);
  text-decoration: none; color: var(--text); transition: background-color 160ms ease, transform 160ms var(--ease-out);
}
.docs__pagelink:hover { background: var(--tile-2); }
.docs__pagelink:active { transform: scale(.99); }
.docs__pagelink--next { text-align: right; }
.docs__pagedir { font-size: 14px; color: var(--soft); }
.docs__pagetitle { font-weight: 600; }

.docs__toc {
  position: sticky; top: 64px; align-self: start; max-height: calc(100vh - 64px); overflow-y: auto;
  padding: 40px 0 48px; font-size: 14px;
}
.docs__toc a { display: block; padding: 4px 0; color: var(--soft); text-decoration: none; line-height: 1.35; }
.docs__toc a:hover { color: var(--text); }
.docs__toc ul ul { padding-left: 12px; }

@media (max-width: 1180px) {
  .docs { grid-template-columns: 240px minmax(0, 1fr); gap: 40px; }
  .docs__toc { display: none; }
}
@media (max-width: 860px) {
  .docs { grid-template-columns: minmax(0, 1fr); gap: 0; padding: 0 16px; }
  .docs__navtoggle {
    display: flex; align-items: center; justify-content: space-between; width: 100%; margin-top: 12px;
    min-height: 48px; padding: 0 16px; border: 0; border-radius: var(--radius); background: var(--tile); color: var(--text);
    font-weight: 600; cursor: pointer;
  }
  .docs__navtoggle[aria-expanded="true"] svg { transform: rotate(180deg); }
  .docs__nav { display: none; position: static; max-height: none; padding: 12px 0 8px; }
  .docs__nav.is-open { display: block; }
  .docs__main { padding-top: 24px; }
}
</style>
