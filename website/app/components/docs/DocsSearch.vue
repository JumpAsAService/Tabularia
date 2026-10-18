<script setup lang="ts">
// Search over the docs: a static index (search.json, built at generate time), loaded on first
// focus and searched in the browser with MiniSearch. Keyboard: "/" focuses, arrows move, Enter opens.
import MiniSearch from 'minisearch'

interface Section { id: string, title: string, titles: string[], level: number, content: string }

const config = useRuntimeConfig()
const router = useRouter()
const q = ref('')
const open = ref(false)
const active = ref(0)
const loading = ref(false)
const failed = ref(false)
const input = ref<HTMLInputElement | null>(null)
const listId = useId()
let engine: MiniSearch<Section> | null = null
let docs: Map<string, Section> = new Map()

async function load () {
  if (engine || loading.value) return
  loading.value = true
  try {
    const data: Section[] = await $fetch(`${config.app.baseURL}search.json`)
    docs = new Map(data.map(d => [d.id, d]))
    engine = new MiniSearch<Section>({
      fields: ['title', 'titles', 'content'],
      storeFields: ['id'],
      extractField: (d, f) => (f === 'titles' ? d.titles.join(' ') : (d as any)[f]),
      searchOptions: { boost: { title: 3, titles: 1.5 }, prefix: true, fuzzy: 0.15, combineWith: 'AND' },
    })
    engine.addAll(data)
  } catch {
    failed.value = true
  } finally {
    loading.value = false
  }
}

const results = computed(() => {
  const term = q.value.trim()
  if (!engine || term.length < 2) return []
  return engine.search(term).slice(0, 10).map((r) => {
    const d = docs.get(r.id)!
    const i = d.content.toLowerCase().indexOf(term.toLowerCase().split(/\s+/)[0] || '')
    const start = Math.max(0, i - 40)
    const snippet = (start > 0 ? '…' : '') + d.content.slice(start, start + 140).trim() + (d.content.length > start + 140 ? '…' : '')
    return { id: d.id, title: d.title, trail: d.titles.join(' › '), snippet }
  })
})

watch(q, () => { active.value = 0; open.value = true })

function go (id: string) {
  open.value = false
  q.value = ''
  router.push(id)
  input.value?.blur()
}
function onKey (e: KeyboardEvent) {
  if (e.key === 'ArrowDown') { e.preventDefault(); active.value = Math.min(active.value + 1, results.value.length - 1) }
  else if (e.key === 'ArrowUp') { e.preventDefault(); active.value = Math.max(active.value - 1, 0) }
  else if (e.key === 'Enter' && results.value[active.value]) { e.preventDefault(); go(results.value[active.value]!.id) }
  else if (e.key === 'Escape') { open.value = false; input.value?.blur() }
}
function onGlobalKey (e: KeyboardEvent) {
  const t = e.target as HTMLElement
  if (e.key === '/' && !['INPUT', 'TEXTAREA', 'SELECT'].includes(t.tagName) && !t.isContentEditable) {
    e.preventDefault(); input.value?.focus()
  }
}
onMounted(() => window.addEventListener('keydown', onGlobalKey))
onBeforeUnmount(() => window.removeEventListener('keydown', onGlobalKey))
</script>

<template>
  <div class="search" role="search" @focusout="(e: FocusEvent) => { if (!(e.currentTarget as HTMLElement).contains(e.relatedTarget as Node)) open = false }">
    <label :for="`${listId}-q`" class="sr-only">Search the documentation</label>
    <svg class="search__icon" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="11" cy="11" r="7" /><path d="m20 20-3.5-3.5" /></svg>
    <input
      :id="`${listId}-q`" ref="input" v-model="q" type="search" class="search__input" placeholder="Search the docs"
      autocomplete="off" spellcheck="false" role="combobox" :aria-expanded="open && q.length > 1" :aria-controls="`${listId}-list`"
      :aria-activedescendant="results.length ? `${listId}-r${active}` : undefined"
      @focus="load(); open = true" @keydown="onKey"
    >
    <kbd class="search__kbd" aria-hidden="true">/</kbd>
    <div v-if="open && q.trim().length > 1" class="search__panel">
      <p v-if="loading" class="search__note">Loading the index…</p>
      <p v-else-if="failed" class="search__note">The search index could not be loaded.</p>
      <p v-else-if="!results.length" class="search__note">Nothing found for «{{ q.trim() }}».</p>
      <ul v-else :id="`${listId}-list`" role="listbox" class="search__list" aria-label="Results">
        <li
          v-for="(r, i) in results" :id="`${listId}-r${i}`" :key="r.id" role="option" :aria-selected="i === active"
          class="search__item" :class="{ 'is-active': i === active }" @mousedown.prevent="go(r.id)" @mousemove="active = i"
        >
          <span class="search__title">{{ r.title }}</span>
          <span v-if="r.trail" class="search__trail">{{ r.trail }}</span>
          <span class="search__snippet">{{ r.snippet }}</span>
        </li>
      </ul>
    </div>
  </div>
</template>

<style scoped>
.search { position: relative; flex: 1 1 260px; max-width: 420px; }
.search__icon { position: absolute; left: 12px; top: 50%; transform: translateY(-50%); color: var(--muted); pointer-events: none; }
.search__input {
  width: 100%; height: 44px; padding: 0 40px 0 40px; border: 1px solid transparent; border-radius: var(--radius); /* taste-skill-ignore: a form field */
  background: var(--tile-2); color: var(--text); font-size: 15px; outline: none;
}
.search__input::placeholder { color: var(--muted); }
.search__input:focus { border-color: var(--accent); background: var(--ground); }
.search__input::-webkit-search-cancel-button { display: none; }
.search__kbd {
  position: absolute; right: 10px; top: 50%; transform: translateY(-50%);
  font-size: 13px; color: var(--muted); background: var(--tile); border-radius: 4px; padding: 0 6px; line-height: 22px;
}
.search__panel {
  position: absolute; z-index: 60; top: calc(100% + 6px); left: 0; width: min(560px, calc(100vw - 32px));
  max-height: min(70vh, 560px); overflow: auto; padding: 6px; border-radius: var(--radius);
  background: var(--tile-2); box-shadow: 0 18px 48px rgba(0, 0, 0, .35); /* taste-skill-ignore: a real overlay */
}
.search__note { padding: 12px 14px; color: var(--soft); font-size: 15px; }
.search__list { list-style: none; margin: 0; padding: 0; }
.search__item { display: grid; gap: 2px; padding: 10px 12px; border-radius: 6px; cursor: pointer; }
.search__item.is-active { background: var(--tile-3); }
.search__title { font-weight: 600; font-size: 15px; color: var(--text); }
.search__trail { font-size: 13px; color: var(--accent-ink); }
.search__snippet { font-size: 14px; color: var(--soft); line-height: 1.45; }
</style>
