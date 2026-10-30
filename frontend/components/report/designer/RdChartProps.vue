<script setup lang="ts">
// A chart's properties: its kind (the 14), its dataset, the category along its axis, the
// series (an aggregate each, with its name, drawn as bars or a line, on the left or the
// right axis), the split into one series per value, and the options — stacked, labels,
// legend, order, the top N, and the scale, target and bands of a gauge or a bullet.
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { Plus, Trash2 } from 'lucide-vue-next'
import Select from '~/components/ui/Select.vue'
import RdChartGlyph from '~/components/report/designer/RdChartGlyph.vue'
import RdExprField from '~/components/report/designer/RdExprField.vue'
import RdFormatInput from '~/components/report/designer/RdFormatInput.vue'
import RdNum from '~/components/report/designer/RdNum.vue'
import RdSeg from '~/components/report/designer/RdSeg.vue'
import RdSection from '~/components/report/designer/RdSection.vue'
import { useDesign } from '~/composables/reportDesignStore'
import { CHART_KINDS, type DChart, type DChartOptions } from '~/composables/reportDesignModel'

const props = defineProps<{ item: DChart }>()
const store = useDesign()
const { t } = useI18n()

const fields = computed(() => store.fieldsOf(props.item.dataset))
const datasetOptions = computed(() => store.doc.value.datasets.map((d) => ({ value: d.name, label: d.name })))
const id = computed(() => props.item.id)
const has = (prefix: string) => store.problems.value.some((p) => p.item === id.value && p.path.startsWith(prefix))

const scaled = computed(() => ['gauge', 'bullet'].includes(props.item.kind))
const axes = computed(() => ['bar', 'column', 'line', 'area', 'combo', 'waterfall', 'scatter'].includes(props.item.kind))
const stackable = computed(() => ['bar', 'column', 'area', 'combo'].includes(props.item.kind))
// a funnel and a treemap lay out by value whatever the order, a gauge has one value
const ordered = computed(() => !['funnel', 'treemap', 'gauge'].includes(props.item.kind))

function opts(): DChartOptions {
  if (!props.item.options) props.item.options = {}
  return props.item.options
}
const o = computed<DChartOptions>(() => props.item.options ?? {})

const legendOptions = computed(() => [
  { value: null, label: t('reportDesigner.chart.legendAuto') },
  { value: true, label: t('reportDesigner.chart.legendShow') },
  { value: false, label: t('reportDesigner.chart.legendHide') },
])
// the engine's order (docs/specs/reports.md, `options.sort`): the rows', or the first series' value
const sortOptions = computed(() => [
  { value: 'none', label: t('reportDesigner.chart.sortNone') },
  { value: 'asc', label: t('reportDesigner.chart.sortAsc') },
  { value: 'desc', label: t('reportDesigner.chart.sortDesc') },
])
const asOptions = computed(() => [
  { value: 'bar', label: t('reportDesigner.chart.asBar') },
  { value: 'line', label: t('reportDesigner.chart.asLine') },
])
const axisOptions = computed(() => [
  { value: 'left', label: t('reportDesigner.chart.axisLeft') },
  { value: 'right', label: t('reportDesigner.chart.axisRight') },
])

// the target: a number, or an expression (an aggregate)
const targetText = computed(() => (o.value.target == null ? '' : String(o.value.target)))
function setTarget(v: string) {
  const s = v.trim()
  if (!s) opts().target = null
  else opts().target = /^-?\d+(\.\d+)?$/.test(s) ? Number(s) : s
  store.clearProblem(id.value, 'options.target')
}
const bandsText = computed(() => (o.value.bands ?? []).join(', '))
function setBands(v: string) {
  const list = v.split(/[;,\s]+/).map((x) => x.trim()).filter(Boolean).map(Number).filter((n) => Number.isFinite(n))
  opts().bands = list.slice(0, 10)
}
function addSeries() {
  props.item.series.push({ value: '', name: '', ...(props.item.kind === 'combo' ? { as: 'line' as const } : {}) })
}
</script>

<template>
  <RdSection :title="$t('reportDesigner.chart.kind')" skey="chart-kind">
    <div class="rcp-kinds" role="radiogroup" :aria-label="$t('reportDesigner.chart.kind')">
      <button
        v-for="k in CHART_KINDS"
        :key="k"
        type="button"
        role="radio"
        :aria-checked="item.kind === k"
        :class="{ on: item.kind === k }"
        :title="$t(`reportDesigner.kinds.${k}`)"
        @click="item.kind = k"
      >
        <RdChartGlyph :kind="k" :size="28" />
        <span>{{ $t(`reportDesigner.kinds.${k}`) }}</span>
      </button>
    </div>
  </RdSection>

  <RdSection :title="$t('reportDesigner.chart.data')" skey="chart-data" :bad="has('dataset') || has('category') || has('split_by') || has('series')">
    <label class="rp-f">
      <span>{{ $t('reportDesigner.props.dataset') }}</span>
      <Select :model-value="item.dataset" :options="datasetOptions" :placeholder="$t('reportDesigner.props.chooseDataset')" :aria-label="$t('reportDesigner.props.dataset')" @update:model-value="item.dataset = $event" />
    </label>
    <p v-if="store.problemAt(id, 'dataset')" class="rp-err" role="alert">{{ store.problemAt(id, 'dataset')?.message }}</p>
    <label class="rp-f">
      <span>{{ $t('reportDesigner.chart.title') }}</span>
      <input type="text" :value="item.title ?? ''" maxlength="200" @input="item.title = ($event.target as HTMLInputElement).value || null" />
    </label>
    <RdExprField :obj="item" prop="category" :label="scaled ? $t('reportDesigner.chart.categoryOptional') : $t('reportDesigner.chart.category')" path="category" :item="id" :fields="fields" optional
                 placeholder="[month]" :hint="$t('reportDesigner.chart.categoryHint')" />
    <div class="rp-list">
      <div class="rp-lhead">
        <span>{{ $t('reportDesigner.chart.series') }}</span>
        <button type="button" class="mini" :disabled="item.series.length >= 12" @click="addSeries"><Plus :size="12" /> {{ $t('reportDesigner.chart.addSeries') }}</button>
      </div>
      <p v-if="store.problemAt(id, 'series')" class="rp-err" role="alert">{{ store.problemAt(id, 'series')?.message }}</p>
      <div v-for="(s, i) in item.series" :key="i" class="rp-rule">
        <div class="rp-rulehead">
          <RdExprField :obj="s" prop="value" :label="$t('reportDesigner.chart.value')" :path="`series[${i}].value`" :item="id" :fields="fields" placeholder="SUM([amount])" />
          <button type="button" class="rp-icon danger" :disabled="item.series.length < 2" :aria-label="$t('reportDesigner.props.remove')" :title="$t('reportDesigner.props.remove')" @click="item.series.splice(i, 1)"><Trash2 :size="13" /></button>
        </div>
        <label class="rp-f">
          <span>{{ $t('reportDesigner.chart.seriesName') }}</span>
          <input v-model="s.name" type="text" maxlength="200" />
        </label>
        <div v-if="item.kind === 'combo' || axes" class="rp-row">
          <div v-if="item.kind === 'combo'" class="rp-f">
            <span>{{ $t('reportDesigner.chart.drawAs') }}</span>
            <RdSeg :model-value="s.as ?? (i ? 'line' : 'bar')" :options="asOptions" :label="$t('reportDesigner.chart.drawAs')" @update:model-value="s.as = $event" />
          </div>
          <div v-if="axes" class="rp-f">
            <span>{{ $t('reportDesigner.chart.axis') }}</span>
            <RdSeg :model-value="s.axis ?? 'left'" :options="axisOptions" :label="$t('reportDesigner.chart.axis')" @update:model-value="s.axis = $event" />
          </div>
        </div>
        <RdFormatInput v-if="item.kind === 'combo'" v-model="s.format" :label="$t('reportDesigner.chart.seriesFormat')" :item="id" :path="`series[${i}].format`" />
      </div>
    </div>
    <RdExprField :obj="item" prop="split_by" :label="$t('reportDesigner.chart.splitBy')" path="split_by" :item="id" :fields="fields" optional
                 :placeholder="'[region]'" :hint="$t('reportDesigner.chart.splitByHint')" />
  </RdSection>

  <RdSection :title="$t('reportDesigner.chart.options')" skey="chart-options" :bad="has('options')">
    <div class="rp-checks">
      <label v-if="stackable" class="rp-check"><input type="checkbox" :checked="!!o.stacked" @change="opts().stacked = ($event.target as HTMLInputElement).checked" /> {{ $t('reportDesigner.chart.stacked') }}</label>
      <label class="rp-check"><input type="checkbox" :checked="!!o.labels" @change="opts().labels = ($event.target as HTMLInputElement).checked" /> {{ $t('reportDesigner.chart.labels') }}</label>
    </div>
    <label class="rp-f">
      <span>{{ $t('reportDesigner.chart.legend') }}</span>
      <Select :model-value="o.legend ?? null" :options="legendOptions" :aria-label="$t('reportDesigner.chart.legend')" @update:model-value="opts().legend = $event" />
    </label>
    <label v-if="ordered" class="rp-f">
      <span>{{ $t('reportDesigner.chart.sort') }}</span>
      <Select :model-value="o.sort ?? 'none'" :options="sortOptions" :aria-label="$t('reportDesigner.chart.sort')" @update:model-value="opts().sort = $event" />
    </label>
    <div class="rp-row">
      <label class="rp-f narrow">
        <span>{{ $t('reportDesigner.chart.top') }}</span>
        <RdNum :model-value="o.top ?? null" :decimals="0" :step="1" :min="1" :max="500" nullable :placeholder="$t('reportDesigner.chart.all')" @update:model-value="opts().top = $event" />
      </label>
      <RdFormatInput :model-value="o.format" :label="$t('reportDesigner.style.format')" :item="id" path="options.format" @update:model-value="opts().format = $event" />
    </div>
    <p v-if="ordered" class="rp-help">{{ $t('reportDesigner.chart.sortHint') }}</p>
    <template v-if="scaled">
      <div class="rp-row">
        <label class="rp-f">
          <span>{{ $t('reportDesigner.chart.min') }}</span>
          <RdNum :model-value="o.min ?? null" :decimals="4" :step="1" nullable :placeholder="$t('reportDesigner.table.auto')" @update:model-value="opts().min = $event" />
        </label>
        <label class="rp-f">
          <span>{{ $t('reportDesigner.chart.max') }}</span>
          <RdNum :model-value="o.max ?? null" :decimals="4" :step="1" nullable :placeholder="$t('reportDesigner.table.auto')" @update:model-value="opts().max = $event" />
        </label>
      </div>
      <label class="rp-f">
        <span>{{ $t('reportDesigner.chart.target') }}</span>
        <input type="text" class="rcp-mono" :value="targetText" spellcheck="false" :placeholder="$t('reportDesigner.chart.targetPh')" @input="setTarget(($event.target as HTMLInputElement).value)" />
      </label>
      <p v-if="store.problemAt(id, 'options.target')" class="rp-err" role="alert">{{ store.problemAt(id, 'options.target')?.message }}</p>
      <p v-else-if="store.liveAt(id, 'options.target')" class="rp-err">{{ store.liveAt(id, 'options.target')?.message }}</p>
      <label class="rp-f">
        <span>{{ $t('reportDesigner.chart.bands') }}</span>
        <input type="text" class="rcp-mono" :value="bandsText" spellcheck="false" placeholder="50, 80" @change="setBands(($event.target as HTMLInputElement).value)" />
      </label>
      <p class="rp-help">{{ $t('reportDesigner.chart.bandsHelp') }}</p>
    </template>
  </RdSection>
</template>

<style scoped src="~/assets/reportprops.css" />
<style scoped>
.rcp-kinds { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 3px; }
.rcp-kinds button {
  flex-direction: column; gap: 3px; padding: 6px 2px 5px; font-size: 10.5px; color: var(--muted); min-width: 0;
  background: transparent; border-color: transparent; box-shadow: none;
}
.rcp-kinds button span { max-width: 100%; line-height: 1.15; text-align: center; overflow-wrap: anywhere; }
.rcp-kinds button :deep(svg) { color: var(--accent); }
.rcp-kinds button:hover { background: var(--panel-2); color: var(--text); box-shadow: none; }
.rcp-kinds button.on { background: var(--tint-accent); color: var(--accent-hi); border-color: color-mix(in srgb, var(--accent) 45%, transparent); }
.rcp-mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
</style>
