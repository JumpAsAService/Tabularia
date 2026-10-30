<script setup lang="ts">
// The left panel of the designer, in two tabs. «Insert»: the elements of a report, to drag
// onto the page or to add with a click (under what the band has already), and a few
// ready-made texts (the page number, the date it was printed). «Data»: the datasets with
// their fields — dragged onto the page they become a text, onto a table a column — their
// calculated fields, and the report's parameters.
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  ChartColumn, ChevronRight, Database, Gauge, Grid3x3, Image, LoaderCircle, Minus, Pencil, Plus, RefreshCw, Square,
  SquareFunction, Table2, Trash2, Type, Variable,
} from 'lucide-vue-next'
import RdFieldIcon from '~/components/report/designer/RdFieldIcon.vue'
import RdRowCount from '~/components/report/designer/RdRowCount.vue'
import { DRAG_TYPE, dragged, useDesign, type DragPayload } from '~/composables/reportDesignStore'
import { countFor } from '~/composables/reportRowCount'
import {
  columnFor, fieldRef, newItem, type DDataset, type DField, type DText, type InsertKind,
} from '~/composables/reportDesignModel'

defineProps<{ refreshing?: string | null; fieldErrors?: Record<string, string>; sourceLabel: (d: DDataset) => string }>()
const emit = defineEmits<{
  (e: 'edit-dataset', name: string | null): void
  (e: 'remove-dataset', name: string): void
  (e: 'refresh-fields', name: string): void
  (e: 'edit-parameter', name: string | null): void
  (e: 'remove-parameter', name: string): void
}>()

const store = useDesign()
const { t } = useI18n()
const tab = store.sideTab

const ELEMENTS: { kind: InsertKind; icon: unknown }[] = [
  { kind: 'text', icon: Type }, { kind: 'image', icon: Image }, { kind: 'line', icon: Minus }, { kind: 'rect', icon: Square },
  { kind: 'table', icon: Table2 }, { kind: 'matrix', icon: Grid3x3 }, { kind: 'chart', icon: ChartColumn }, { kind: 'kpi', icon: Gauge },
]

// the words of the ready-made texts in the report's own language (not the interface's)
const WORDS: Record<string, [string, string, string]> = {
  en: ['Page', 'of', 'Printed'], it: ['Pagina', 'di', 'Stampato il'], de: ['Seite', 'von', 'Gedruckt am'],
  es: ['Página', 'de', 'Impreso el'], fr: ['Page', 'sur', 'Imprimé le'],
}
const snippets = computed(() => {
  const [page, of, printed] = WORDS[store.doc.value.locale] ?? WORDS.en
  return [
  { key: 'pageOf', label: '', value: `="${page} " & PAGE() & " ${of} " & PAGES()`, w: 40 },
  { key: 'printed', label: '', value: `="${printed} " & FORMAT(NOW(), "d MMM yyyy HH:mm")`, w: 60 },
  { key: 'reportName', label: '', value: '=REPORT()', w: 80 },
  ...store.doc.value.parameters.map((p) => ({
    key: '', label: t('reportDesigner.insert.paramValue', { name: p.label || p.name }),
    value: `="${(p.label || p.name).replaceAll('"', '""')}: " & PARAM("${p.name}")`, w: 60,
  })),
  ]
})

function start(e: DragEvent, payload: DragPayload) {
  dragged.value = payload
  e.dataTransfer?.setData(DRAG_TYPE, payload.kind)
  e.dataTransfer?.setData('text/plain', '')
  if (e.dataTransfer) e.dataTransfer.effectAllowed = 'copy'
}
function end() {
  dragged.value = null
}

/** An element added without a drop: in the band chosen when it can hold it, else the
 *  body, under what is there. */
function insert(kind: InsertKind) {
  const d = store.doc.value
  const spot = store.freeSpot(kind)
  const it = newItem(d, kind, spot)
  store.keepInside(spot.band, it)
  store.add(spot.band, it)
  store.settleRow(it)
  if (it.type === 'chart') store.chartPicker.value = it.id
}

function insertSnippet(value: string, w: number) {
  const spot = store.freeSpot('text', 6)
  const it = newItem(store.doc.value, 'text', spot) as DText
  it.value = value
  it.w = w
  it.h = 6
  store.keepInside(spot.band, it)
  store.add(spot.band, it)
  store.settleRow(it)
}

// ── data ──
const openSets = ref<Set<string>>(new Set())
function toggle(name: string) {
  const next = new Set(openSets.value)
  if (next.has(name)) next.delete(name)
  else next.add(name)
  openSets.value = next
}
const isOpen = (name: string) => openSets.value.has(name) || store.doc.value.datasets.length === 1
// a dataset just added shows its fields (they are what one works with next), and the only
// one, open until then, stays open when a second comes
watch(() => store.doc.value.datasets.map((d) => d.name), (now, before) => {
  if (!before) return
  const added = now.filter((n) => !before.includes(n))
  if (!added.length) return
  const next = new Set(openSets.value)
  if (before.length === 1 && now.includes(before[0])) next.add(before[0])
  for (const n of added) next.add(n)
  openSets.value = next
})

/** A field added with a click: a column of the selected table when it reads this
 *  dataset, else a text in the body. */
function useField(ds: DDataset, f: DField) {
  const sel = store.selected.value?.item
  if (sel?.type === 'table' && (sel.dataset === ds.name || !sel.dataset)) {
    sel.dataset = ds.name
    if (sel.matrix) sel.columns.splice(0, sel.columns.length ? 1 : 0, { header: f.name, value: '' })
    else sel.columns.push(columnFor(f))
    return
  }
  const spot = store.freeSpot('text', 7)
  const spotBand = spot.band === 'body' ? spot : { ...spot, band: 'body' as const }
  const it = newItem(store.doc.value, 'text', spotBand) as DText
  it.value = `=${fieldRef(f.name)}`
  it.dataset = ds.name
  store.keepInside('body', it)
  store.add('body', it)
  store.settleRow(it)
}

const fieldTitle = computed(() => (store.selected.value?.item.type === 'table'
  ? t('reportDesigner.data.fieldToTable') : t('reportDesigner.data.fieldToText')))
</script>

<template>
  <aside class="rds" :aria-label="$t('reportDesigner.sidebar')">
    <div class="rds-tabs" role="tablist">
      <button type="button" role="tab" :aria-selected="tab === 'insert'" :class="{ on: tab === 'insert' }" @click="tab = 'insert'">
        <Plus :size="13" /> {{ $t('reportDesigner.insert.tab') }}
      </button>
      <button type="button" role="tab" :aria-selected="tab === 'data'" :class="{ on: tab === 'data' }" @click="tab = 'data'">
        <Database :size="13" /> {{ $t('reportDesigner.data.tab') }}
        <span v-if="store.doc.value.datasets.length" class="rds-count">{{ store.doc.value.datasets.length }}</span>
      </button>
    </div>

    <div v-if="tab === 'insert'" class="rds-body" role="tabpanel">
      <p class="rds-help">{{ $t('reportDesigner.insert.help') }}</p>
      <div class="rds-tiles">
        <button
          v-for="el in ELEMENTS"
          :key="el.kind"
          type="button"
          class="rds-tile"
          draggable="true"
          :data-insert="el.kind"
          :title="$t(`reportDesigner.insert.hint.${el.kind}`)"
          @dragstart="start($event, { kind: 'insert', type: el.kind })"
          @dragend="end"
          @click="insert(el.kind)"
        >
          <component :is="el.icon" :size="18" />
          <span>{{ $t(`reportDesigner.items.${el.kind}`) }}</span>
        </button>
      </div>

      <h3 class="rds-h">{{ $t('reportDesigner.insert.readyMade') }}</h3>
      <ul class="rds-snips">
        <li v-for="(s, i) in snippets" :key="i">
          <button
            type="button"
            class="rds-snip"
            draggable="true"
            :data-snippet="s.key || 'param'"
            @dragstart="start($event, { kind: 'snippet', value: s.value, w: s.w })"
            @dragend="end"
            @click="insertSnippet(s.value, s.w)"
          >
            <Type :size="13" />
            <span class="rds-snipname">{{ s.key ? $t(`reportDesigner.insert.snip.${s.key}`) : s.label }}</span>
          </button>
        </li>
      </ul>
    </div>

    <div v-else class="rds-body" role="tabpanel">
      <div class="rds-head">
        <h3 class="rds-h">{{ $t('reportDesigner.data.datasets') }}</h3>
        <button type="button" class="mini" @click="emit('edit-dataset', null)"><Plus :size="12" /> {{ $t('reportDesigner.data.addDataset') }}</button>
      </div>
      <p v-if="!store.doc.value.datasets.length" class="rds-empty">{{ $t('reportDesigner.data.noDatasets') }}</p>
      <ul class="rds-sets">
        <li v-for="ds in store.doc.value.datasets" :key="ds.name" class="rds-set" :class="{ bad: store.problems.value.some((p) => p.dataset === ds.name) || store.liveOf('dataset', ds.name).length > 0 }">
          <div class="rds-sethead">
            <button type="button" class="rds-chev" :aria-expanded="isOpen(ds.name)" :aria-label="$t('reportDesigner.data.fieldsOf', { name: ds.name })" @click="toggle(ds.name)">
              <ChevronRight :size="13" />
            </button>
            <div class="rds-setname">
              <span class="rds-mono" :title="ds.name">{{ ds.name }}</span>
              <span class="rds-src">{{ sourceLabel(ds) }}</span>
            </div>
            <button type="button" class="rds-icon" :title="$t('reportDesigner.data.refreshFields')" :aria-label="$t('reportDesigner.data.refreshFields')" :disabled="refreshing === ds.name" @click="emit('refresh-fields', ds.name)">
              <LoaderCircle v-if="refreshing === ds.name" :size="13" class="spin" /><RefreshCw v-else :size="13" />
            </button>
            <button type="button" class="rds-icon" :title="$t('reportDesigner.data.editDataset')" :aria-label="$t('reportDesigner.data.editDataset')" @click="emit('edit-dataset', ds.name)">
              <Pencil :size="13" />
            </button>
            <button type="button" class="rds-icon danger" :title="$t('reportDesigner.data.removeDataset')" :aria-label="$t('reportDesigner.data.removeDataset')" @click="emit('remove-dataset', ds.name)">
              <Trash2 :size="13" />
            </button>
          </div>
          <RdRowCount class="rds-rows" :count="countFor(store.rowCounts.value[ds.name], ds.source)" />
          <template v-if="isOpen(ds.name)">
            <p v-if="fieldErrors?.[ds.name] && !(ds.fields ?? []).length" class="rds-nofields bad" role="alert">{{ fieldErrors[ds.name] }}</p>
            <p v-else-if="!(ds.fields ?? []).length && !(ds.calculated ?? []).length" class="rds-nofields">
              {{ $t('reportDesigner.data.noFields') }}
            </p>
            <ul class="rds-fields">
              <li v-for="f in ds.fields ?? []" :key="`f-${f.name}`">
                <button
                  type="button"
                  class="rds-field"
                  draggable="true"
                  :data-field="f.name"
                  :title="`${f.name} · ${f.type}\n${fieldTitle}`"
                  @dragstart="start($event, { kind: 'field', dataset: ds.name, field: f })"
                  @dragend="end"
                  @click="useField(ds, f)"
                >
                  <RdFieldIcon :type="f.type" />
                  <span class="rds-fname">{{ f.name }}</span>
                  <span class="rds-ftype">{{ f.type }}</span>
                </button>
              </li>
              <li v-for="c in ds.calculated ?? []" :key="`c-${c.name}`">
                <button
                  type="button"
                  class="rds-field calc"
                  draggable="true"
                  :data-field="c.name"
                  :title="`${c.name} = ${c.expr}\n${fieldTitle}`"
                  @dragstart="start($event, { kind: 'field', dataset: ds.name, field: { name: c.name, type: 'calculated' } })"
                  @dragend="end"
                  @click="useField(ds, { name: c.name, type: 'calculated' })"
                >
                  <SquareFunction :size="13" />
                  <span class="rds-fname">{{ c.name }}</span>
                  <span class="rds-ftype">{{ $t('reportDesigner.data.calculated') }}</span>
                </button>
              </li>
            </ul>
          </template>
        </li>
      </ul>

      <div class="rds-head params">
        <h3 class="rds-h">{{ $t('reportDesigner.data.parameters') }}</h3>
        <button type="button" class="mini" @click="emit('edit-parameter', null)"><Plus :size="12" /> {{ $t('reportDesigner.data.addParameter') }}</button>
      </div>
      <p v-if="!store.doc.value.parameters.length" class="rds-empty">{{ $t('reportDesigner.data.noParameters') }}</p>
      <ul class="rds-params">
        <li v-for="p in store.doc.value.parameters" :key="p.name" class="rds-param" :class="{ bad: store.problems.value.some((x) => x.parameter === p.name) }">
          <Variable :size="13" class="rds-pi" />
          <button type="button" class="rds-pname" @click="emit('edit-parameter', p.name)">
            <span class="rds-mono" :title="p.label ? `${p.name} · ${p.label}` : p.name">{{ p.name }}</span>
            <span class="rds-src">{{ $t(`reportDesigner.param.types.${p.type}`) }}<template v-if="p.multi"> · {{ $t('reportDesigner.param.multiShort') }}</template><template v-if="p.required"> · {{ $t('reportDesigner.param.requiredShort') }}</template></span>
          </button>
          <button type="button" class="rds-icon danger" :title="$t('reportDesigner.data.removeParameter')" :aria-label="$t('reportDesigner.data.removeParameter')" @click="emit('remove-parameter', p.name)">
            <Trash2 :size="13" />
          </button>
        </li>
      </ul>
    </div>
  </aside>
</template>

<style scoped>
.rds { display: flex; flex-direction: column; min-height: 0; background: var(--panel); }
.rds-tabs { display: flex; gap: 2px; padding: 8px 8px 0; border-bottom: 1px solid var(--border-soft); }
.rds-tabs button {
  flex: 1; border: 0; border-bottom: 2px solid transparent; border-radius: 6px 6px 0 0; background: transparent;
  color: var(--muted); font-size: 12.5px; padding: 7px 8px; box-shadow: none;
}
.rds-tabs button:hover { color: var(--text); box-shadow: none; }
.rds-tabs button.on { color: var(--text); background: var(--tint-accent); border-bottom-color: var(--accent); }
.rds-tabs button:active { transform: none; }
.rds-count { font-size: 10.5px; padding: 0 6px; border-radius: 999px; background: var(--panel-2); color: var(--muted); }
.rds-body { flex: 1; min-height: 0; overflow: auto; padding: 12px; display: flex; flex-direction: column; gap: 10px; }
.rds-help { margin: 0; font-size: 12px; color: var(--muted); line-height: 1.45; }
.rds-tiles { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; }
.rds-tile {
  flex-direction: column; gap: 6px; padding: 12px 6px 10px; font-size: 12px; background: var(--bg-soft);
  border-color: var(--border-soft); cursor: grab; color: var(--text);
}
.rds-tile :deep(svg) { color: var(--accent); }
.rds-tile:active { cursor: grabbing; }
.rds-h { margin: 4px 0 0; font-size: 10.5px; font-weight: 600; letter-spacing: 0.05em; text-transform: uppercase; color: var(--muted); }
.rds-snips, .rds-sets, .rds-fields, .rds-params { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 2px; }
.rds-snip {
  width: 100%; justify-content: flex-start; background: transparent; border-color: transparent; padding: 5px 8px;
  font-size: 12.5px; cursor: grab; box-shadow: none;
}
.rds-snip :deep(svg) { color: var(--muted); flex: none; }
.rds-snip:hover { background: var(--panel-2); border-color: transparent; box-shadow: none; }
.rds-snipname { min-width: 0; overflow-wrap: anywhere; text-align: left; line-height: 1.25; }
.rds-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.rds-head .rds-h { margin: 0; }
.rds-head.params { margin-top: 10px; }
.rds-head .mini { display: inline-flex; align-items: center; gap: 4px; flex-shrink: 0; white-space: nowrap; }
.rds-empty { margin: 0; font-size: 12px; color: var(--muted); line-height: 1.45; }
.rds-set { border: 1px solid var(--border-soft); border-radius: 8px; padding: 4px; background: var(--bg-soft); }
.rds-set.bad, .rds-param.bad { border-color: color-mix(in srgb, var(--danger) 60%, var(--border)); }
.rds-sethead { display: flex; align-items: center; gap: 2px; }
.rds-chev, .rds-icon {
  border: 0; background: transparent; padding: 4px; min-height: 0; box-shadow: none; color: var(--muted); border-radius: 6px;
}
.rds-chev:hover, .rds-icon:hover { color: var(--text); background: var(--panel-2); box-shadow: none; }
.rds-icon.danger:hover { color: var(--danger); }
.rds-chev svg { transition: transform 0.15s ease; }
.rds-chev[aria-expanded='true'] svg { transform: rotate(90deg); }
.rds-setname { flex: 1; min-width: 0; display: flex; flex-direction: column; padding: 2px 4px; }
.rds-mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.rds-src { font-size: 11px; color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.rds-rows { margin: 0 8px 4px 26px; }
.rds-nofields { margin: 4px 8px 6px 26px; font-size: 11.5px; color: var(--muted); line-height: 1.4; overflow-wrap: anywhere; }
.rds-nofields.bad { color: var(--danger); }
.rds-fields { margin: 2px 0 2px 18px; padding-left: 6px; border-left: 1px solid var(--border-soft); }
.rds-field {
  width: 100%; justify-content: flex-start; gap: 6px; padding: 3px 6px; border: 0; background: transparent;
  font-size: 12px; cursor: grab; box-shadow: none; border-radius: 6px;
}
.rds-field:hover { background: var(--panel-2); box-shadow: none; }
.rds-field:active { transform: none; }
.rds-field :deep(svg) { color: var(--accent-hi); flex: none; }
.rds-field.calc :deep(svg) { color: var(--violet); }
.rds-fname { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; }
.rds-ftype { margin-left: auto; font-size: 10.5px; color: var(--muted); white-space: nowrap; }
.rds-param { display: flex; align-items: center; gap: 4px; padding: 3px 4px 3px 8px; border: 1px solid var(--border-soft); border-radius: 8px; background: var(--bg-soft); }
.rds-pi { color: var(--warning); flex: none; }
.rds-pname { flex: 1; min-width: 0; border: 0; background: transparent; padding: 2px 4px; display: flex; flex-direction: column; align-items: flex-start; box-shadow: none; }
.rds-pname:hover { box-shadow: none; }
.rds-pname:hover .rds-mono { color: var(--accent); }
</style>
