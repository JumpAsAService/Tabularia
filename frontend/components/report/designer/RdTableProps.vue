<script setup lang="ts">
// A table's properties: its dataset, its columns (header, value, width, alignment, format,
// total, conditional rules), its groups (by, label, header and footer, page break, order),
// whether the rows are shown, its order and filter, zebra rows, a grand total, the header
// repeated on every page, and what can be done on screen (sorting a column, closing a
// group). A matrix adds the columns it spreads (one per value) and the value of its cells.
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { ArrowDown, ArrowUp, ChevronRight, Plus, Trash2 } from 'lucide-vue-next'
import Select from '~/components/ui/Select.vue'
import RdExprField from '~/components/report/designer/RdExprField.vue'
import RdFormatInput from '~/components/report/designer/RdFormatInput.vue'
import RdNum from '~/components/report/designer/RdNum.vue'
import RdSeg from '~/components/report/designer/RdSeg.vue'
import RdSection from '~/components/report/designer/RdSection.vue'
import RdStyleFields from '~/components/report/designer/RdStyleFields.vue'
import { useDesign } from '~/composables/reportDesignStore'
import type { DColumn, DGroup, DTable } from '~/composables/reportDesignModel'

const props = defineProps<{ item: DTable }>()
const store = useDesign()
const { t } = useI18n()

const fields = computed(() => store.fieldsOf(props.item.dataset))
const datasetOptions = computed(() => store.doc.value.datasets.map((d) => ({ value: d.name, label: d.name })))
const id = computed(() => props.item.id)
const has = (prefix: string) => store.problems.value.some((p) => p.item === id.value && p.path.startsWith(prefix))

function move<T>(list: T[], i: number, d: number) {
  const j = i + d
  if (j < 0 || j >= list.length) return
  const [x] = list.splice(i, 1)
  list.splice(j, 0, x)
}

// ── columns ──
const openCols = ref<Set<number>>(new Set())
const colOpen = (i: number) => openCols.value.has(i) || has(`columns[${i}]`)
function toggleCol(i: number) {
  const next = new Set(openCols.value)
  if (next.has(i)) next.delete(i)
  else next.add(i)
  openCols.value = next
}
function addColumn() {
  props.item.columns.push({ header: '', value: '' })
  openCols.value = new Set([...openCols.value, props.item.columns.length - 1])
}
function moveColumn(i: number, d: number) {
  move(props.item.columns, i, d)
  openCols.value = new Set()
}
const colWidth = computed(() => props.item.columns.reduce((s, c) => s + (c.width ?? 0), 0))
function addRule(c: DColumn) {
  if (!c.rules) c.rules = []
  c.rules.push({ when: '', style: { color: '#c92a2a', weight: 'bold' } })
}
const alignOptions = computed(() => [
  { value: null, label: t('reportDesigner.table.alignAuto') },
  { value: 'left', label: t('reportDesigner.style.left') },
  { value: 'center', label: t('reportDesigner.style.center') },
  { value: 'right', label: t('reportDesigner.style.right') },
])

// ── groups ──
function groups(): DGroup[] {
  if (!props.item.groups) props.item.groups = []
  return props.item.groups
}
function addGroup() {
  groups().push({ by: '', header: true, footer: true, page_break: 'none', sort: 'asc', keep_together: true })
}
const breakOptions = computed(() => [
  { value: 'none', label: t('reportDesigner.table.breakNone') },
  { value: 'between', label: t('reportDesigner.table.breakBetween') },
  { value: 'after', label: t('reportDesigner.table.breakAfter') },
])
const dirOptions = computed(() => [
  { value: 'asc', label: t('reportDesigner.table.asc') },
  { value: 'desc', label: t('reportDesigner.table.desc') },
])

// ── order ──
function sortKeys() {
  if (!props.item.sort) props.item.sort = []
  return props.item.sort
}

// ── matrix ──
function setMatrix(on: boolean) {
  const tb = props.item
  if (on) {
    tb.matrix = { columns_by: '', value: '', total: true }
    if (!tb.columns.length) tb.columns.push({ header: '', value: '' })
    if (!tb.groups?.length) groups().push({ by: '', header: false, footer: false })
    tb.detail = false
    if (tb.interactive?.sort) tb.interactive.sort = false
  } else {
    // a table again: its rows shown, and nothing the matrix left empty (a group without
    // its key, the rows' column without a value) for the save to refuse
    tb.matrix = null
    tb.detail = true
    tb.groups = (tb.groups ?? []).filter((g) => g.by.trim())
    const first = tb.columns[0]
    if (first && !first.value.trim()) {
      const inner = tb.groups[tb.groups.length - 1]?.by
      if (inner) first.value = inner
      else tb.columns.splice(0, 1)
    }
  }
}
function setRowHeader(v: string) {
  const first = props.item.columns[0]
  if (first) first.header = v
  else props.item.columns.push({ header: v, value: '' })
}
function interactive() {
  if (!props.item.interactive) props.item.interactive = {}
  return props.item.interactive
}
</script>

<template>
  <RdSection :title="$t('reportDesigner.table.data')" skey="table-data" :bad="has('dataset') || has('filter') || has('sort')">
    <label class="rp-f">
      <span>{{ $t('reportDesigner.props.dataset') }}</span>
      <Select :model-value="item.dataset" :options="datasetOptions" :placeholder="$t('reportDesigner.props.chooseDataset')" :aria-label="$t('reportDesigner.props.dataset')" @update:model-value="item.dataset = $event" />
    </label>
    <p v-if="store.problemAt(id, 'dataset')" class="rp-err" role="alert">{{ store.problemAt(id, 'dataset')?.message }}</p>
    <label class="rp-check">
      <input type="checkbox" :checked="!!item.matrix" @change="setMatrix(($event.target as HTMLInputElement).checked)" />
      {{ $t('reportDesigner.table.isMatrix') }}
    </label>
    <RdExprField :obj="item" prop="filter" :label="$t('reportDesigner.table.filter')" path="filter" :item="id" :fields="fields" optional
                 :placeholder="$t('reportDesigner.table.filterPh')" />
    <div class="rp-list">
      <div class="rp-lhead">
        <span>{{ $t('reportDesigner.table.sort') }}</span>
        <button type="button" class="mini" @click="sortKeys().push({ by: '', dir: 'asc' })"><Plus :size="12" /> {{ $t('reportDesigner.table.addSort') }}</button>
      </div>
      <div v-for="(s, i) in item.sort ?? []" :key="i" class="rp-rule">
        <div class="rp-rulehead">
          <RdExprField :obj="s" prop="by" :label="$t('reportDesigner.table.sortBy', { n: i + 1 })" :path="`sort[${i}].by`" :item="id" :fields="fields" />
          <button type="button" class="rp-icon danger" :aria-label="$t('reportDesigner.props.remove')" :title="$t('reportDesigner.props.remove')" @click="item.sort!.splice(i, 1)"><Trash2 :size="13" /></button>
        </div>
        <RdSeg :model-value="s.dir ?? 'asc'" :options="dirOptions" :label="$t('reportDesigner.table.direction')" @update:model-value="s.dir = $event" />
      </div>
    </div>
  </RdSection>

  <RdSection v-if="item.matrix" :title="$t('reportDesigner.table.matrix')" skey="table-matrix" :bad="has('matrix')">
    <RdExprField :obj="item.matrix" prop="columns_by" :label="$t('reportDesigner.table.columnsBy')" path="matrix.columns_by" :item="id" :fields="fields"
                 :hint="$t('reportDesigner.table.columnsByHint')" />
    <RdExprField :obj="item.matrix" prop="value" :label="$t('reportDesigner.table.cellValue')" path="matrix.value" :item="id" :fields="fields"
                 :placeholder="'SUM([amount])'" :hint="$t('reportDesigner.table.cellValueHint')" />
    <div class="rp-row">
      <RdFormatInput v-model="item.matrix.format" :label="$t('reportDesigner.style.format')" :item="id" path="matrix.format" />
      <label class="rp-f narrow">
        <span>{{ $t('reportDesigner.table.cellWidth') }}</span>
        <RdNum v-model="item.matrix.width" unit="mm" :min="1" nullable :placeholder="$t('reportDesigner.table.auto')" />
      </label>
    </div>
    <label class="rp-check"><input type="checkbox" :checked="item.matrix.total !== false" @change="item.matrix!.total = ($event.target as HTMLInputElement).checked" /> {{ $t('reportDesigner.table.matrixTotal') }}</label>
    <label class="rp-f">
      <span>{{ $t('reportDesigner.table.rowHeader') }}</span>
      <input type="text" :value="item.columns[0]?.header ?? ''" maxlength="500" @input="setRowHeader(($event.target as HTMLInputElement).value)" />
    </label>
  </RdSection>

  <RdSection v-else :title="$t('reportDesigner.table.columns')" skey="table-columns" :count="item.columns.length" :bad="has('columns')">
    <template #actions>
      <button type="button" class="mini rp-add" @click="addColumn"><Plus :size="12" /> {{ $t('reportDesigner.table.addColumn') }}</button>
    </template>
    <p v-if="!item.columns.length" class="rp-help">{{ $t('reportDesigner.table.noColumns') }}</p>
    <p v-if="store.problemAt(id, 'columns')" class="rp-err" role="alert">{{ store.problemAt(id, 'columns')?.message }}</p>
    <div v-for="(c, i) in item.columns" :key="i" class="rp-card" :class="{ bad: has(`columns[${i}]`) }">
      <div class="rp-chead">
        <button type="button" class="rp-ctitle" :aria-expanded="colOpen(i)" @click="toggleCol(i)">
          <ChevronRight :size="13" class="rp-chev" />
          <span class="rp-cname">{{ c.header || $t('reportDesigner.table.columnN', { n: i + 1 }) }}</span>
          <span class="rp-cexpr">{{ c.value }}</span>
        </button>
        <button type="button" class="rp-icon" :disabled="i === 0" :aria-label="$t('reportDesigner.props.moveUp')" :title="$t('reportDesigner.props.moveUp')" @click="moveColumn(i, -1)"><ArrowUp :size="13" /></button>
        <button type="button" class="rp-icon" :disabled="i === item.columns.length - 1" :aria-label="$t('reportDesigner.props.moveDown')" :title="$t('reportDesigner.props.moveDown')" @click="moveColumn(i, 1)"><ArrowDown :size="13" /></button>
        <button type="button" class="rp-icon danger" :aria-label="$t('reportDesigner.props.remove')" :title="$t('reportDesigner.props.remove')" @click="item.columns.splice(i, 1)"><Trash2 :size="13" /></button>
      </div>
      <div v-if="colOpen(i)" class="rp-cbody">
        <label class="rp-f">
          <span>{{ $t('reportDesigner.table.header') }}</span>
          <input v-model="c.header" type="text" maxlength="500" />
        </label>
        <RdExprField :obj="c" prop="value" :label="$t('reportDesigner.table.value')" :path="`columns[${i}].value`" :item="id" :fields="fields" placeholder="[amount]" />
        <div class="rp-row">
          <label class="rp-f narrow">
            <span>{{ $t('reportDesigner.table.width') }}</span>
            <RdNum v-model="c.width" unit="mm" :min="1" nullable :placeholder="$t('reportDesigner.table.auto')" />
          </label>
          <label class="rp-f">
            <span>{{ $t('reportDesigner.style.align') }}</span>
            <Select :model-value="c.align ?? null" :options="alignOptions" :aria-label="$t('reportDesigner.style.align')" @update:model-value="c.align = $event" />
          </label>
        </div>
        <RdFormatInput v-model="c.format" :label="$t('reportDesigner.style.format')" :item="id" :path="`columns[${i}].format`" />
        <RdExprField :obj="c" prop="total" :label="$t('reportDesigner.table.total')" :path="`columns[${i}].total`" :item="id" :fields="fields" optional
                     placeholder="SUM([amount])" :hint="$t('reportDesigner.table.totalHint')" />
        <details class="rp-more">
          <summary>{{ $t('reportDesigner.table.cellStyle') }}</summary>
          <RdStyleFields :obj="c" :parts="['font', 'color', 'fill']" :item="id" :path="`columns[${i}]`" />
        </details>
        <div class="rp-list">
          <div class="rp-lhead">
            <span>{{ $t('reportDesigner.table.rules') }}</span>
            <button type="button" class="mini" @click="addRule(c)"><Plus :size="12" /> {{ $t('reportDesigner.table.addRule') }}</button>
          </div>
          <div v-for="(r, j) in c.rules ?? []" :key="j" class="rp-rule">
            <div class="rp-rulehead">
              <RdExprField :obj="r" prop="when" :label="$t('reportDesigner.table.when')" :path="`columns[${i}].rules[${j}].when`" :item="id" :fields="fields" placeholder="[amount] < 0" />
              <button type="button" class="rp-icon danger" :aria-label="$t('reportDesigner.props.remove')" :title="$t('reportDesigner.props.remove')" @click="c.rules!.splice(j, 1)"><Trash2 :size="13" /></button>
            </div>
            <RdStyleFields :obj="r" :parts="['color', 'fill']" :item="id" :path="`columns[${i}].rules[${j}]`" />
            <label class="rp-check">
              <input type="checkbox" :checked="r.style?.weight === 'bold'" @change="r.style = { ...(r.style ?? {}), weight: ($event.target as HTMLInputElement).checked ? 'bold' : null }" />
              {{ $t('reportDesigner.style.bold') }}
            </label>
          </div>
        </div>
      </div>
    </div>
    <p v-if="colWidth > item.w + 0.05" class="rp-err">{{ $t('reportDesigner.table.tooWide', { cols: colWidth.toFixed(1), w: item.w.toFixed(1) }) }}</p>
  </RdSection>

  <RdSection :title="$t('reportDesigner.table.groups')" skey="table-groups" :count="(item.groups ?? []).length || null" :bad="has('groups')">
    <template #actions>
      <button type="button" class="mini rp-add" :disabled="(item.groups ?? []).length >= 6" @click="addGroup"><Plus :size="12" /> {{ $t('reportDesigner.table.addGroup') }}</button>
    </template>
    <p v-if="!(item.groups ?? []).length" class="rp-help">{{ $t('reportDesigner.table.noGroups') }}</p>
    <div v-for="(g, i) in item.groups ?? []" :key="i" class="rp-card" :class="{ bad: has(`groups[${i}]`) }">
      <div class="rp-chead">
        <span class="rp-glevel">{{ $t('reportDesigner.table.level', { n: i + 1 }) }}</span>
        <span class="rp-cexpr">{{ g.by }}</span>
        <button type="button" class="rp-icon" :disabled="i === 0" :aria-label="$t('reportDesigner.props.moveUp')" :title="$t('reportDesigner.props.moveUp')" @click="move(item.groups!, i, -1)"><ArrowUp :size="13" /></button>
        <button type="button" class="rp-icon" :disabled="i === (item.groups ?? []).length - 1" :aria-label="$t('reportDesigner.props.moveDown')" :title="$t('reportDesigner.props.moveDown')" @click="move(item.groups!, i, 1)"><ArrowDown :size="13" /></button>
        <button type="button" class="rp-icon danger" :aria-label="$t('reportDesigner.props.remove')" :title="$t('reportDesigner.props.remove')" @click="item.groups!.splice(i, 1)"><Trash2 :size="13" /></button>
      </div>
      <div class="rp-cbody">
        <RdExprField :obj="g" prop="by" :label="$t('reportDesigner.table.groupBy')" :path="`groups[${i}].by`" :item="id" :fields="fields" placeholder="[region]" />
        <RdExprField :obj="g" prop="label" :label="$t('reportDesigner.table.groupLabel')" :path="`groups[${i}].label`" :item="id" :fields="fields" optional
                     :placeholder="$t('reportDesigner.table.groupLabelPh')" />
        <div class="rp-f">
          <span>{{ $t('reportDesigner.table.order') }}</span>
          <RdSeg :model-value="g.sort ?? 'asc'" :options="dirOptions" :label="$t('reportDesigner.table.order')" @update:model-value="g.sort = $event" />
        </div>
        <label class="rp-f">
          <span>{{ $t('reportDesigner.table.pageBreak') }}</span>
          <Select :model-value="g.page_break ?? 'none'" :options="breakOptions" :aria-label="$t('reportDesigner.table.pageBreak')" @update:model-value="g.page_break = $event" />
        </label>
        <div class="rp-checks">
          <label class="rp-check"><input type="checkbox" :checked="g.header !== false" @change="g.header = ($event.target as HTMLInputElement).checked" /> {{ $t('reportDesigner.table.groupHeader') }}</label>
          <label class="rp-check"><input type="checkbox" :checked="g.footer !== false" @change="g.footer = ($event.target as HTMLInputElement).checked" /> {{ $t('reportDesigner.table.groupFooter') }}</label>
          <label class="rp-check"><input type="checkbox" :checked="g.keep_together !== false" @change="g.keep_together = ($event.target as HTMLInputElement).checked" /> {{ $t('reportDesigner.table.keepTogether') }}</label>
        </div>
      </div>
    </div>
  </RdSection>

  <RdSection :title="$t('reportDesigner.table.layout')" skey="table-layout">
    <div class="rp-checks">
      <label v-if="!item.matrix" class="rp-check"><input type="checkbox" :checked="item.detail !== false" @change="item.detail = ($event.target as HTMLInputElement).checked" /> {{ $t('reportDesigner.table.detail') }}</label>
      <label class="rp-check"><input type="checkbox" :checked="!!item.grand_total" @change="item.grand_total = ($event.target as HTMLInputElement).checked" /> {{ $t('reportDesigner.table.grandTotal') }}</label>
      <label class="rp-check"><input type="checkbox" :checked="item.repeat_header !== false" @change="item.repeat_header = ($event.target as HTMLInputElement).checked" /> {{ $t('reportDesigner.table.repeatHeader') }}</label>
      <label class="rp-check"><input type="checkbox" :checked="!!item.zebra" @change="item.zebra = ($event.target as HTMLInputElement).checked" /> {{ $t('reportDesigner.table.zebra') }}</label>
    </div>
    <label class="rp-f">
      <span>{{ $t('reportDesigner.table.title') }}</span>
      <input type="text" :value="item.title ?? ''" maxlength="100" :placeholder="item.id" @input="item.title = ($event.target as HTMLInputElement).value || null" />
    </label>
    <p class="rp-help">{{ $t('reportDesigner.table.titleHelp') }}</p>
  </RdSection>

  <RdSection :title="$t('reportDesigner.table.onScreen')" skey="table-screen">
    <p class="rp-help">{{ $t('reportDesigner.table.onScreenHelp') }}</p>
    <div class="rp-checks">
      <!-- a matrix is not sorted by a column (the engine leaves it as it is) -->
      <label v-if="!item.matrix" class="rp-check"><input type="checkbox" :checked="!!item.interactive?.sort" @change="interactive().sort = ($event.target as HTMLInputElement).checked" /> {{ $t('reportDesigner.table.interactiveSort') }}</label>
      <label class="rp-check"><input type="checkbox" :checked="!!item.interactive?.toggle" @change="interactive().toggle = ($event.target as HTMLInputElement).checked" /> {{ $t('reportDesigner.table.interactiveToggle') }}</label>
    </div>
  </RdSection>
</template>

<style scoped src="~/assets/reportprops.css" />
