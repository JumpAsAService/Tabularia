<script setup lang="ts">
// The top of the inspector, for every node and box (docs/specs/bayes-graph-editor.md, «The
// inspector»): the label, the name (proposed from the label while the person has not written one),
// the box it sits in — the keyboard's way to what a drag into a box does — and for a box its key,
// the box it sits inside, and for the rows' box its series. Every field edits the graph at once; the
// mirror's findings on these fields are said under them.
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import Select from '~/components/ui/Select.vue'
import { bayesBoxSubtree, type BayesDoc } from '~/composables/bayesGraphEdits'
import { bayesFindingText } from '~/composables/bayesEditorText'
import type { BayesGraphFinding } from '~/composables/bayesGraph'

const props = defineProps<{
  doc: BayesDoc
  id: string
  findings: BayesGraphFinding[]
}>()
const emit = defineEmits<{
  (e: 'label', id: string, text: string): void
  (e: 'name', id: string, text: string): void
  (e: 'plate', id: string, plate: string | null): void
  (e: 'parent', id: string, parent: string | null): void
  (e: 'key', id: string, key: string): void
  (e: 'target', id: string, target: string): void
  (e: 'series', on: boolean): void
  (e: 'seriesPart', part: 'time' | 'by', value: string | null): void
  (e: 'escape'): void
}>()
const { t, te } = useI18n()

const NONE = '-'
const node = computed(() => props.doc.nodes.find(n => n.id === props.id))
const plate = computed(() => props.doc.plates.find(p => p.id === props.id))
const groups = computed(() => props.doc.plates.filter(p => p.kind === 'group'))
const labelOf = (id: string): string => props.doc.nodes.find(n => n.id === id)?.label ?? props.doc.plates.find(p => p.id === id)?.label ?? id
const say = (f: BayesGraphFinding): string => {
  const m = bayesFindingText(f, labelOf)
  return te(m.key) ? t(m.key, m.params) : f.code
}
const own = computed(() => props.findings.filter(f => f.node === props.id))
const labelFindings = computed(() => own.value.filter(f => f.slot === 'label'))
const nameFindings = computed(() => own.value.filter(f => f.code.startsWith('graph.name')))

/** «School, inside District». */
function boxWords (id: string): string {
  const p = props.doc.plates.find(x => x.id === id)
  if (!p) return id
  const parent = typeof p.parent === 'string' ? props.doc.plates.find(x => x.id === p.parent) : undefined
  return parent ? t('bayes.editor.inspector.boxInside', { box: p.label, parent: parent.label }) : p.label
}

const named = computed(() => !!node.value && node.value.kind !== 'likelihood' && node.value.kind !== 'correlated_effects')
const boxOptions = computed(() => [
  { value: NONE, label: t('bayes.editor.inspector.noBox') },
  ...props.doc.plates.filter(p => p.kind === 'rows' || p.kind === 'group').map(p => ({ value: p.id, label: boxWords(p.id) })),
])
const insideOptions = computed(() => {
  const own = plate.value ? bayesBoxSubtree(props.doc, plate.value.id) : new Set<string>()
  return [{ value: NONE, label: t('bayes.editor.inspector.noParent') }, ...groups.value.filter(g => !own.has(g.id)).map(g => ({ value: g.id, label: boxWords(g.id) }))]
})
const keyOptions = computed(() => [
  { value: NONE, label: t('bayes.editor.inspector.keyNone') },
  ...props.doc.nodes.filter(n => n.kind === 'data' && n.type === 'group_index').map(n => ({ value: n.id, label: String(n.name ?? n.id), hint: n.label })),
])
const targetOptions = computed(() => [{ value: NONE, label: t('bayes.editor.inspector.targetNone') }, ...groups.value.map(g => ({ value: g.id, label: boxWords(g.id) }))])
const series = computed(() => (plate.value?.kind === 'rows' ? (plate.value.series as { time: string, by: string | null } | undefined) ?? null : null))
const timeOptions = computed(() => [
  { value: NONE, label: t('bayes.editor.inspector.seriesNoTime') },
  ...props.doc.nodes.filter(n => n.kind === 'data' && n.type === 'time').map(n => ({ value: n.id, label: String(n.name ?? n.id), hint: n.label })),
])
const byOptions = computed(() => [{ value: NONE, label: t('bayes.editor.inspector.seriesSingle') }, ...groups.value.map(g => ({ value: g.id, label: boxWords(g.id) }))])
const opt = (v: unknown): string | null => (v === NONE || v == null ? null : String(v))
const fid = (what: string) => `bi-${props.id}-${what}`
</script>

<template>
  <div class="ident">
    <div class="fl">
      <label :for="fid('label')">{{ t('bayes.editor.inspector.label') }}</label>
      <input
        :id="fid('label')"
        class="nokey"
        type="text"
        :value="(node ?? plate)?.label ?? ''"
        :aria-invalid="labelFindings.length ? 'true' : 'false'"
        :aria-describedby="labelFindings.length ? fid('label-err') : undefined"
        @input="emit('label', id, ($event.target as HTMLInputElement).value)"
        @keydown.esc="emit('escape')"
      >
      <p v-if="labelFindings.length" :id="fid('label-err')" class="errline">{{ say(labelFindings[0]!) }}</p>
    </div>

    <div v-if="node" class="row2">
      <div v-if="named" class="fl">
        <label :for="fid('name')">{{ t('bayes.editor.inspector.name') }}</label>
        <input
          :id="fid('name')"
          class="nokey mono"
          type="text"
          spellcheck="false"
          autocapitalize="off"
          autocomplete="off"
          :value="node.name ?? ''"
          :placeholder="t('bayes.editor.inspector.namePlaceholder')"
          :aria-invalid="nameFindings.length ? 'true' : 'false'"
          :aria-describedby="nameFindings.length ? fid('name-err') : undefined"
          @input="emit('name', id, ($event.target as HTMLInputElement).value)"
          @keydown.esc="emit('escape')"
        >
      </div>
      <div class="fl">
        <label :for="fid('box')">{{ t('bayes.editor.inspector.box') }}</label>
        <Select
          v-if="node.kind !== 'likelihood'"
          :id="fid('box')"
          :model-value="typeof node.plate === 'string' && node.plate ? node.plate : NONE"
          :options="boxOptions"
          @update:model-value="(v: unknown) => emit('plate', id, opt(v))"
        />
        <p v-else :id="fid('box')" class="fact">{{ t('bayes.editor.inspector.likelihoodBox') }}</p>
      </div>
    </div>
    <p v-if="node && nameFindings.length" :id="fid('name-err')" class="errline">{{ say(nameFindings[0]!) }} {{ t('bayes.editor.inspector.nameRule') }}</p>

    <div v-if="node && node.kind === 'data' && node.type === 'group_index'" class="fl">
      <label :for="fid('target')">{{ t('bayes.editor.inspector.target') }}</label>
      <Select :id="fid('target')" :model-value="typeof node.target === 'string' && node.target ? node.target : NONE" :options="targetOptions" @update:model-value="(v: unknown) => emit('target', id, opt(v) ?? '')" />
    </div>

    <div v-if="plate && plate.kind === 'group'" class="row2">
      <div class="fl">
        <label :for="fid('key')">{{ t('bayes.editor.inspector.unitPer') }}</label>
        <Select :id="fid('key')" :model-value="typeof plate.key === 'string' && plate.key ? plate.key : NONE" :options="keyOptions" @update:model-value="(v: unknown) => emit('key', id, opt(v) ?? '')" />
      </div>
      <div class="fl">
        <label :for="fid('inside')">{{ t('bayes.editor.inspector.inside') }}</label>
        <Select :id="fid('inside')" :model-value="typeof plate.parent === 'string' && plate.parent ? plate.parent : NONE" :options="insideOptions" @update:model-value="(v: unknown) => emit('parent', id, opt(v))" />
      </div>
    </div>

    <template v-if="plate && plate.kind === 'rows'">
      <label class="swrow">
        <input type="checkbox" role="switch" :checked="!!series" @change="emit('series', ($event.target as HTMLInputElement).checked)">
        {{ t('bayes.editor.plate.series') }}
      </label>
      <p class="explain">{{ t(series ? 'bayes.editor.plate.seriesWhat' : 'bayes.editor.plate.rowsWhat') }}</p>
      <div v-if="series" class="row2">
        <div class="fl">
          <label :for="fid('time')">{{ t('bayes.editor.inspector.seriesTime') }}</label>
          <Select :id="fid('time')" :model-value="series.time || NONE" :options="timeOptions" @update:model-value="(v: unknown) => emit('seriesPart', 'time', opt(v))" />
        </div>
        <div class="fl">
          <label :for="fid('by')">{{ t('bayes.editor.inspector.seriesBy') }}</label>
          <Select :id="fid('by')" :model-value="series.by || NONE" :options="byOptions" @update:model-value="(v: unknown) => emit('seriesPart', 'by', opt(v))" />
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.ident { display: flex; flex-direction: column; gap: 10px; min-width: 0; }
.row2 { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 10px; }
.fl { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.fl > label { font-size: 12px; font-weight: 500; color: var(--muted); }
.fl :deep(.sel-trigger), .fl :deep(button[role="combobox"]) { width: 100%; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12.5px; }
.fact { margin: 0; padding: 6px 0; font-size: 12.5px; line-height: 1.4; color: var(--muted); }
.errline { margin: -4px 0 0; font-size: 12px; line-height: 1.4; color: var(--danger-text); }
.swrow { display: flex; align-items: center; gap: 9px; font-size: 13px; cursor: pointer; }
.explain { margin: 0; font-size: 12.5px; line-height: 1.5; color: var(--text); }
</style>
