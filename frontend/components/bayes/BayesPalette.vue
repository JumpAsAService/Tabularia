<script setup lang="ts">
// The palette of the editor of Bayesian models (docs/specs/bayes-graph-editor.md, «The palette»): the
// kinds of node grouped — Data, Unknowns, Values, Observed data, Boxes — each with its icon, its name
// and one sentence for a reader new to Bayes. An item is dragged onto the canvas (its kind travels in
// the dataTransfer as application/x-bayes-kind, read by BayesCanvas.vue's drop), or added with Enter
// or a click: then it lands in the selected box, or at the canvas' centre. `n` on the canvas puts the
// focus on the first item (focusFirst); ↑ and ↓ walk the items.
import { computed, nextTick, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { AudioWaveform, Boxes, ChartScatter, CircleDot, Clock, Database, GripVertical, Hash, LayoutTemplate, List, Search, Sigma, SquareFunction, Users, Variable, X } from 'lucide-vue-next'
import { BAYES_PALETTE, type BayesPaletteKind } from '~/composables/bayesGraphEdits'

// `templates`: the gallery of the templates (lot G3), from the palette's foot; hidden for a version looked at
defineProps<{ templates?: boolean }>()
const emit = defineEmits<{ (e: 'add', kind: BayesPaletteKind): void, (e: 'templates'): void }>()
const { t } = useI18n()

const ICONS: Record<BayesPaletteKind, unknown> = {
  real: Database, int: Hash, group_index: Users, category: List, time: Clock,
  parameter: CircleDot, correlated_effects: Variable, state: AudioWaveform,
  transformed: SquareFunction, linear_predictor: Sigma, likelihood: ChartScatter, group: Boxes,
}

const query = ref('')
const fold = (s: string) => s.normalize('NFKD').replace(/\p{M}/gu, '').toLowerCase()
const groups = computed(() => {
  const q = fold(query.value.trim())
  return BAYES_PALETTE.map(g => ({
    id: g.group,
    items: g.items
      .map(k => ({ kind: k, name: t(`bayes.editor.palette.item.${k}.name`), what: t(`bayes.editor.palette.item.${k}.what`) }))
      .filter(x => !q || fold(x.name).includes(q) || fold(x.what).includes(q)),
  })).filter(g => g.items.length)
})

function onDragStart (ev: DragEvent, kind: BayesPaletteKind) {
  ev.dataTransfer?.setData('application/x-bayes-kind', kind)
  if (ev.dataTransfer) ev.dataTransfer.effectAllowed = 'copy'
}

const root = ref<HTMLElement | null>(null)
const items = (): HTMLElement[] => Array.from(root.value?.querySelectorAll<HTMLElement>('.item') ?? [])
function walk (ev: KeyboardEvent, by: number) {
  const list = items()
  const i = list.indexOf(ev.currentTarget as HTMLElement)
  const next = list[(i + by + list.length) % list.length]
  if (next) { ev.preventDefault(); next.focus() }
}

/** The first item takes the focus (`n` on the canvas). */
async function focusFirst () {
  query.value = ''
  await nextTick()
  items()[0]?.focus()
}
defineExpose({ focusFirst })
</script>

<template>
  <aside ref="root" class="pal" :aria-label="t('bayes.editor.palette.title')">
    <div class="searchbar">
      <Search :size="14" class="sic" aria-hidden="true" />
      <input
        v-model="query"
        class="nokey"
        type="text"
        spellcheck="false"
        :placeholder="t('bayes.editor.palette.search')"
        :aria-label="t('bayes.editor.palette.search')"
        @keydown.down.prevent="items()[0]?.focus()"
        @keydown.esc="query = ''"
      >
      <button v-if="query" type="button" class="clear" :aria-label="t('bayes.editor.palette.clear')" :title="t('bayes.editor.palette.clear')" @click="query = ''">
        <X :size="13" aria-hidden="true" />
      </button>
    </div>
    <p v-if="!groups.length" class="none">{{ t('bayes.editor.palette.none', { q: query.trim() }) }}</p>
    <section v-for="g in groups" :key="g.id" class="grp" :aria-labelledby="`pal-${g.id}`">
      <h2 :id="`pal-${g.id}`" class="gt">{{ t(`bayes.editor.palette.group.${g.id}`) }}</h2>
      <div
        v-for="x in g.items"
        :key="x.kind"
        class="item"
        draggable="true"
        role="button"
        tabindex="0"
        :data-kind="x.kind"
        :title="t('bayes.editor.palette.hint')"
        :aria-label="t('bayes.editor.palette.add', { name: x.name })"
        :aria-describedby="`pal-w-${x.kind}`"
        @dragstart="onDragStart($event, x.kind)"
        @click="emit('add', x.kind)"
        @keydown.enter.prevent="emit('add', x.kind)"
        @keydown.space.prevent="emit('add', x.kind)"
        @keydown.down="walk($event, 1)"
        @keydown.up="walk($event, -1)"
      >
        <component :is="ICONS[x.kind]" :size="15" class="ic" aria-hidden="true" />
        <span class="tx">
          <span class="nm">{{ x.name }}</span>
          <span :id="`pal-w-${x.kind}`" class="wh">{{ x.what }}</span>
        </span>
        <GripVertical :size="13" class="grip" aria-hidden="true" />
      </div>
    </section>
    <button v-if="templates" type="button" class="tpl" :title="t('bayes.editor.gallery.fromPalette')" @click="emit('templates')">
      <LayoutTemplate :size="14" aria-hidden="true" /><span class="tpn">{{ t('bayes.editor.gallery.fromPalette') }}</span>
    </button>
  </aside>
</template>

<style scoped>
.pal { height: 100%; overflow-y: auto; padding: 0 8px 14px; background: var(--panel); display: flex; flex-direction: column; gap: 2px; }
.searchbar {
  position: sticky; top: 0; z-index: 1; display: flex; align-items: center; gap: 6px;
  margin: 0 -8px 4px; padding: 10px 10px 9px; background: var(--panel); border-bottom: 1px solid var(--border-soft);
}
.sic { flex: none; color: var(--muted); }
.searchbar input {
  flex: 1; min-width: 0; padding: 6px 8px; font: inherit; font-size: 13px; color: var(--text);
  background: var(--bg-soft); border: 1px solid var(--control-border); border-radius: 8px;
}
.searchbar input::placeholder { color: var(--muted); }
.clear { flex: none; display: grid; place-items: center; width: 22px; height: 22px; padding: 0; border: 0; border-radius: 6px; background: none; color: var(--muted); }
@media (hover: hover) and (pointer: fine) { .clear:hover { color: var(--text); background: var(--panel-2); } }
.none { margin: 8px 6px; font-size: 12px; line-height: 1.45; color: var(--muted); }
.grp { display: flex; flex-direction: column; gap: 1px; }
.gt { margin: 0; padding: 10px 6px 4px; font-size: 10.5px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.08em; color: var(--muted); }
.item {
  position: relative; display: flex; align-items: flex-start; gap: 9px; padding: 6px 6px 7px 8px;
  border: 1px solid transparent; border-radius: 8px; cursor: grab; user-select: none;
  transition: background var(--dur-tint), border-color var(--dur-tint);
}
.item:active { cursor: grabbing; }
.item:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
@media (hover: hover) and (pointer: fine) { .item:hover { background: var(--panel-2); border-color: var(--border); } }
.ic { flex: none; margin-top: 2px; color: var(--muted); }
.tx { display: flex; flex-direction: column; gap: 1px; min-width: 0; flex: 1; }
.nm { font-size: 13px; font-weight: 500; color: var(--text); line-height: 1.3; }
.wh { font-size: 11.5px; line-height: 1.35; color: var(--muted); }
.tpl { margin: 12px 0 0; justify-content: flex-start; gap: 8px; font-size: 12.5px; }
.grip { flex: none; margin-top: 3px; color: var(--border); transition: color var(--dur-tint); }
@media (hover: hover) and (pointer: fine) { .item:hover .grip { color: var(--muted); } }
/* at 1280 px the palette is 168 px: the sentence stays, the grip gives way */
@media (max-width: 1399px) { .grip { display: none; } .item { gap: 7px; } }
/* below 1100 px (not a target of the first release) the palette folds to its icons, named by their title */
@media (max-width: 1099px) {
  .searchbar input, .gt, .tx, .none, .tpn { display: none; }
  .tpl { justify-content: center; padding: 6px 0; }
  .searchbar { justify-content: center; }
  .item { justify-content: center; padding: 8px 0; }
  .ic { margin: 0; }
}
</style>
