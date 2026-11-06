<script setup lang="ts">
// A group of "#### Windows" / "#### Linux" / "#### macOS" blocks of a docs page, as tabs
// (lib/os-tabs.mjs builds the group). Without JavaScript every block stays visible under
// its heading; with it, one block shows at a time, the one of the visitor's system, and a
// choice made here applies to every group of the site.
import { panelFor, panelSystems, useOsChoice } from '~/composables/useOsChoice'

const props = defineProps<{ systems?: string }>()
const LABELS: Record<string, string> = { 'windows': 'Windows', 'linux': 'Linux', 'macos': 'macOS', 'linux-macos': 'Linux and macOS' }
const keys = computed(() => (props.systems || '').split(/\s+/).filter(k => k in LABELS))
const covers = computed(() => [...new Set(keys.value.flatMap(panelSystems))].join(' '))
const uid = useId()
const tabId = (k: string) => `${uid}-tab-${k}`
const panelId = (k: string) => `${uid}-panel-${k}`

const { chosen, choose } = useOsChoice()
const ready = ref(false)
const active = computed(() => (ready.value ? panelFor(keys.value, chosen.value) : ''))
provide('os-tabs', { ready, active, tabId, panelId })
onMounted(() => { ready.value = true })

const list = ref<HTMLElement | null>(null)
// Every group of the page follows the choice, so the groups above this one may change
// height: keep this tab where it was on screen.
async function select (k: string, focus = false) {
  const tab = list.value?.querySelector<HTMLElement>(`[data-key="${k}"]`)
  const before = tab?.getBoundingClientRect().top
  choose(k)
  await nextTick()
  if (tab && before !== undefined) {
    const shift = tab.getBoundingClientRect().top - before
    if (Math.abs(shift) > 1) window.scrollBy({ top: shift, behavior: 'instant' })
    if (focus) tab.focus()
  }
}
function onKey (e: KeyboardEvent) {
  const i = keys.value.indexOf(active.value)
  const n = keys.value.length
  const next = { ArrowRight: i + 1, ArrowLeft: i - 1 + n, Home: 0, End: n - 1 }[e.key]
  if (next === undefined || i < 0) return
  e.preventDefault()
  select(keys.value[next % n]!, true)
}
</script>

<template>
  <div class="os-tabs" :class="{ 'is-ready': ready }" :data-covers="covers">
    <div ref="list" class="os-tablist" role="tablist" aria-label="Operating system" @keydown="onKey">
      <button
        v-for="k in keys" :id="tabId(k)" :key="k" type="button" role="tab" class="os-tab"
        :data-key="k" :data-for="panelSystems(k).join(' ')"
        :aria-selected="active === k ? 'true' : 'false'" :aria-controls="panelId(k)"
        :tabindex="active === k || !ready ? 0 : -1" @click="select(k)"
      >
        {{ LABELS[k] }}
      </button>
    </div>
    <slot />
  </div>
</template>

<style>
/* Without JavaScript <html> has no data-os: no tab bar, every block under its heading. */
.prose .os-tabs { margin-top: 1.6em; }
.os-tablist { display: none; }
.prose .os-panel > :first-child { margin-top: 0; }
.prose .os-panel + .os-panel { margin-top: 1.6em; }

html[data-os] .os-tablist {
  display: flex; flex-wrap: wrap; gap: 4px;
  border-bottom: 1px solid var(--tile-3);
}
html[data-os] .os-tab {
  appearance: none; background: none; border: 0; cursor: pointer;
  font: inherit; font-size: 15px; font-weight: 600; line-height: 1.3;
  color: var(--soft);
  padding: 10px 14px 9px; margin-bottom: -1px; min-height: 44px;
  border-bottom: 2px solid transparent; border-radius: 6px 6px 0 0;
}
html[data-os] .os-tab:hover { color: var(--text); background: var(--tile); }
html[data-os] .os-tab[aria-selected="true"] { color: var(--text); border-bottom-color: var(--accent); }
html[data-os] .os-tab:focus-visible { outline-offset: -3px; }
html[data-os] .prose .os-panel { margin-top: 0; padding-top: 1.1em; }
html[data-os] .prose .os-panel > h4:first-child + * { margin-top: 0; }
html[data-os] .os-panel:focus-visible { outline-offset: 4px; }
/* the tab names the block: its heading stays for screen readers and in-page links */
html[data-os] .os-panel > h4:first-child {
  position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden;
  clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0;
}

/* Before the page is interactive, the head script's data-os already picks the block. */
html[data-os="windows"] .os-tabs:not(.is-ready)[data-covers~="windows"] > .os-panel:not([data-for~="windows"]),
html[data-os="linux"] .os-tabs:not(.is-ready)[data-covers~="linux"] > .os-panel:not([data-for~="linux"]),
html[data-os="macos"] .os-tabs:not(.is-ready)[data-covers~="macos"] > .os-panel:not([data-for~="macos"]) { display: none; }
html[data-os="windows"] .os-tabs:not(.is-ready) .os-tab[data-for~="windows"],
html[data-os="linux"] .os-tabs:not(.is-ready) .os-tab[data-for~="linux"],
html[data-os="macos"] .os-tabs:not(.is-ready) .os-tab[data-for~="macos"] { color: var(--text); border-bottom-color: var(--accent); }

@media print {
  html[data-os] .os-tablist { display: none; }
  .os-panel[hidden] { display: block; }
  html[data-os] .os-panel > h4:first-child { position: static; width: auto; height: auto; margin: 0 0 .6em; clip: auto; overflow: visible; }
}
</style>
