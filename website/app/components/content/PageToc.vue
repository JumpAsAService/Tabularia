<script setup lang="ts">
// "On this page" under the title, for screens too narrow for the side list (lib/prose-blocks.mjs
// puts it there). A <details>: it opens and closes without JavaScript; with it, a link also closes it.
import type { ComputedRef } from 'vue'

type Link = { id: string, text: string, children?: Link[] }
const toc = inject<ComputedRef<Link[]> | null>('docs-toc', null)
const links = computed(() => toc?.value || [])
const box = ref<HTMLDetailsElement | null>(null)
// close first, then go: the list folding away would otherwise move the target mid-scroll
async function go (e: MouseEvent, id: string) {
  const el = document.getElementById(id)
  if (!el || e.metaKey || e.ctrlKey || e.shiftKey) return
  e.preventDefault()
  if (box.value) box.value.open = false
  await nextTick()
  history.pushState(history.state, '', `#${id}`)
  el.scrollIntoView({ block: 'start', behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' })
}
</script>

<template>
  <details v-if="links.length > 1" ref="box" class="page-toc">
    <summary>
      <span>On this page</span>
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="m6 9 6 6 6-6" /></svg>
    </summary>
    <nav aria-label="On this page">
      <ul role="list">
        <li v-for="l in links" :key="l.id">
          <a :href="`#${l.id}`" @click="go($event, l.id)">{{ l.text }}</a>
          <ul v-if="l.children?.length" role="list">
            <li v-for="c in l.children" :key="c.id"><a :href="`#${c.id}`" @click="go($event, c.id)">{{ c.text }}</a></li>
          </ul>
        </li>
      </ul>
    </nav>
  </details>
</template>

<style>
/* wide screens have the side list */
.prose .page-toc { display: none; }
@media (max-width: 1180px) {
  .prose .page-toc { display: block; background: var(--tile); border-radius: var(--radius); }
}
.prose .page-toc summary {
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
  min-height: 48px; padding: 0 16px; cursor: pointer; list-style: none;
  font-size: 16px; font-weight: 600; color: var(--text);
}
.prose .page-toc summary::-webkit-details-marker { display: none; }
.prose .page-toc summary svg { color: var(--soft); transition: transform 160ms ease; flex: none; }
.prose .page-toc[open] summary svg { transform: rotate(180deg); }
.prose .page-toc summary:focus-visible { outline-offset: -3px; border-radius: var(--radius); }
.prose .page-toc nav { padding: 0 16px 14px; max-height: 60vh; overflow-y: auto; }
.prose .page-toc ul { list-style: none; margin: 0; padding: 0; }
.prose .page-toc ul ul { padding-left: 14px; }
.prose .page-toc li + li { margin-top: 0; }
.prose .page-toc a {
  display: block; padding: 8px 0; font-size: 15px; line-height: 1.35;
  color: var(--soft); text-decoration: none;
}
.prose .page-toc a:hover { color: var(--text); }
</style>
