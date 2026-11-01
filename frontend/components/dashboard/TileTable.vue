<script setup lang="ts">
// A table or matrix tile: a page of rows, as the server wrote them (`g` a group's header, `d` a
// detail row, `f` a group's footer, `t` the grand total). A sort or a closed group is not done
// here: it goes up as a change of the view, and comes back as numbers, as in the viewer of reports.
// On a narrow tile the table scrolls in its own box, which the keyboard can reach.
//
// A group's name and a matrix's header are buttons that filter the other tiles, by the raw value the
// server gave (`path`, `key`), never the text; Ctrl or Cmd adds it, from the keyboard too. Where the
// name would have closed the group, the arrow beside it still does.
import { computed } from 'vue'
import { ArrowDown, ArrowUp, ArrowUpDown, Check, ChevronDown, ChevronLeft, ChevronRight } from 'lucide-vue-next'
import { valueText, type ClickSide, type ClickValue } from '~/composables/dashboardClicks'
import { cellStyleOf } from '~/composables/dashboardStyle'
import type { TileNumbers } from '~/composables/useDashboards'

const props = defineProps<{
  answer: TileNumbers
  panel: string
  label: string
  busy?: boolean
  /** the sides a click on this table can filter by, and what is chosen on each */
  filtersRows?: boolean
  filtersColumns?: boolean
  chosenRows?: ClickValue[]
  chosenColumns?: ClickValue[]
}>()
const emit = defineEmits<{
  (e: 'sort', column: number): void
  (e: 'toggle', key: unknown[], closed: boolean): void
  (e: 'page', page: number): void
  (e: 'pick', on: ClickSide, value: ClickValue, add: boolean): void
}>()

const columns = computed<any[]>(() => props.answer.columns ?? [])
const firstRow = computed(() => (props.answer.page ?? 0) * (props.answer.page_rows ?? 50) + 1)
const lastRow = computed(() => Math.min((props.answer.page + 1) * props.answer.page_rows, props.answer.total_rows))

const canSort = (column: number) => !!props.answer.hot?.sort && !(props.answer.hot?.still ?? []).includes(column)
const sortedDir = (column: number) => (props.answer.sorted?.column === column ? props.answer.sorted.dir : null)
const ariaSort = (column: number) => {
  const dir = sortedDir(column)
  return dir === 'asc' ? 'ascending' : dir === 'desc' ? 'descending' : canSort(column) ? 'none' : undefined
}

function cell(c: any, column: number) {
  const st = { ...(columns.value[column]?.style ?? {}), ...(c?.style ?? {}) }
  return { textAlign: columns.value[column]?.align ?? 'left', ...cellStyleOf(st, props.panel) }
}
// a report's widths are millimetres, and on a screen a guide: the table may still scroll
const widthOf = (c: any) => (typeof c.width === 'number' ? `${c.width}mm` : undefined)

const rowsOn = computed(() => new Set((props.chosenRows ?? []).map(valueText)))
const columnsOn = computed(() => new Set((props.chosenColumns ?? []).map(valueText)))
const rowOn = (path: any) => !!path && rowsOn.value.has(valueText(path))
const columnOn = (key: any) => !!key && columnsOn.value.has(valueText(key))
// a value of one level goes as itself: `click.t=North` in the address, as a chart's
const one = (path: any[]): ClickValue => (path.length === 1 ? path[0] : path)
// Firefox's click from the keyboard forgets Ctrl: the key that makes it says so first
let keyAdds = false
function noteKey(e: KeyboardEvent) {
  keyAdds = (e.key === 'Enter' || e.key === ' ') && (e.ctrlKey || e.metaKey)
}
function pick(on: ClickSide, path: any[], e: MouseEvent) {
  emit('pick', on, one(path), e.ctrlKey || e.metaKey || keyAdds)
  keyAdds = false
}
</script>

<template>
  <div class="tt" :class="{ busy }" @keydown.capture="noteKey">
    <p v-if="answer.empty" class="nodata">{{ $t('dashboard.noData') }}</p>
    <template v-else>
      <div class="scroll" tabindex="0" role="region" :aria-label="$t('dashboard.tableLabel', { name: label })">
        <table>
          <colgroup><col v-for="(c, i) in columns" :key="i" :style="{ width: widthOf(c) }" /></colgroup>
          <thead>
            <tr v-for="(row, r) in answer.head" :key="r">
              <th
                v-for="(h, i) in row"
                :key="i"
                scope="col"
                :colspan="h.span > 1 ? h.span : undefined"
                :rowspan="h.rspan > 1 ? h.rspan : undefined"
                :style="{ textAlign: h.align }"
                :aria-sort="!answer.matrix ? ariaSort(i) : undefined"
                :class="{ picked: columnOn(h.key) }"
              >
                <button v-if="!answer.matrix && canSort(i)" type="button" class="sortbtn" :class="{ on: sortedDir(i), right: h.align === 'right' }" :title="$t('dashboard.sortBy', { name: h.t })" @click="emit('sort', i)">
                  <span class="hname">{{ h.t }}</span>
                  <ArrowUp v-if="sortedDir(i) === 'asc'" :size="12" aria-hidden="true" />
                  <ArrowDown v-else-if="sortedDir(i) === 'desc'" :size="12" aria-hidden="true" />
                  <ArrowUpDown v-else :size="12" class="idle" aria-hidden="true" />
                </button>
                <button v-else-if="answer.matrix && h.key && filtersColumns" type="button" class="pickbtn" :class="{ right: h.align === 'right' }" :aria-pressed="columnOn(h.key)" :title="$t('dashboard.filterOthers', { name: h.t })" @click="pick('columns', h.key, $event)">
                  <Check v-if="columnOn(h.key)" :size="12" aria-hidden="true" />
                  <span class="hname">{{ h.t }}</span>
                </button>
                <template v-else>{{ h.t }}</template>
              </th>
            </tr>
          </thead>
          <tbody>
            <template v-for="(row, r) in answer.rows" :key="r">
              <tr v-if="row.kind === 'g'" class="g" :class="{ picked: filtersRows && rowOn(row.path) }">
                <th :colspan="columns.length" scope="rowgroup" :style="{ paddingLeft: `${9 + row.level * 14}px` }">
                  <template v-if="filtersRows">
                    <button v-if="row.key && answer.hot?.toggle" type="button" class="groupbtn arrow" :aria-expanded="!row.closed" :title="$t(row.closed ? 'dashboard.openGroup' : 'dashboard.closeGroup', { name: row.label })" :aria-label="$t(row.closed ? 'dashboard.openGroup' : 'dashboard.closeGroup', { name: row.label })" @click="emit('toggle', row.key, row.closed)">
                      <ChevronRight v-if="row.closed" :size="13" aria-hidden="true" />
                      <ChevronDown v-else :size="13" aria-hidden="true" />
                    </button>
                    <button type="button" class="pickbtn" :aria-pressed="rowOn(row.path)" :title="$t('dashboard.filterOthers', { name: row.label })" @click="pick('rows', row.path, $event)">
                      <Check v-if="rowOn(row.path)" :size="12" aria-hidden="true" />
                      <span>{{ row.label }}</span>
                    </button>
                  </template>
                  <button v-else-if="row.key && answer.hot?.toggle" type="button" class="groupbtn" :aria-expanded="!row.closed" :title="$t(row.closed ? 'dashboard.openGroup' : 'dashboard.closeGroup', { name: row.label })" @click="emit('toggle', row.key, row.closed)">
                    <ChevronRight v-if="row.closed" :size="13" aria-hidden="true" />
                    <ChevronDown v-else :size="13" aria-hidden="true" />
                    <span>{{ row.label }}</span>
                  </button>
                  <span v-else>{{ row.label }}</span>
                </th>
              </tr>
              <tr v-else-if="row.kind === 'd'" :class="{ zebra: row.zebra, picked: filtersRows && rowOn(row.path) }">
                <td v-for="(c, i) in row.cells" :key="i" :style="cell(c, i)">
                  <!-- a matrix's innermost group is a row: its name is what can be clicked -->
                  <button v-if="i === 0 && row.path && filtersRows" type="button" class="pickbtn" :aria-pressed="rowOn(row.path)" :title="$t('dashboard.filterOthers', { name: c.text })" @click="pick('rows', row.path, $event)">
                    <Check v-if="rowOn(row.path)" :size="12" aria-hidden="true" />
                    <span>{{ c.text }}</span>
                  </button>
                  <template v-else>{{ c.text }}</template>
                </td>
              </tr>
              <tr v-else :class="[row.kind === 't' ? 'total' : 'foot', { picked: filtersRows && rowOn(row.path) }]">
                <!-- no room for its label when the first column has a total of its own -->
                <th v-if="row.span" :colspan="row.span" scope="row" :style="{ paddingLeft: `${9 + (row.level ?? 0) * 14}px` }">
                  <button v-if="row.path && filtersRows" type="button" class="pickbtn" :aria-pressed="rowOn(row.path)" :title="$t('dashboard.filterOthers', { name: row.label })" @click="pick('rows', row.path, $event)">
                    <Check v-if="rowOn(row.path)" :size="12" aria-hidden="true" />
                    <span>{{ row.label }}</span>
                  </button>
                  <template v-else>{{ row.label }}</template>
                </th>
                <td v-for="(c, i) in row.cells.slice(row.span)" :key="i" :style="cell(c, i + row.span)">{{ c.text }}</td>
              </tr>
            </template>
          </tbody>
        </table>
      </div>
      <div v-if="answer.pages > 1" class="pages">
        <span class="range">{{ $t('pager.range', { from: firstRow, to: lastRow, total: answer.total_rows }) }}</span>
        <button type="button" class="mini" :disabled="answer.page <= 0 || busy" :title="$t('pager.prev')" :aria-label="$t('pager.prev')" @click="emit('page', answer.page - 1)"><ChevronLeft :size="14" aria-hidden="true" /></button>
        <button type="button" class="mini" :disabled="answer.page >= answer.pages - 1 || busy" :title="$t('pager.next')" :aria-label="$t('pager.next')" @click="emit('page', answer.page + 1)"><ChevronRight :size="14" aria-hidden="true" /></button>
      </div>
    </template>
  </div>
</template>

<style scoped>
.tt { display: flex; flex-direction: column; flex: 1; min-height: 0; min-width: 0; }
.scroll { flex: 1; min-height: 0; overflow: auto; border-radius: 6px; }
.scroll:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
table { border-collapse: separate; border-spacing: 0; width: 100%; font-size: 12px; font-variant-numeric: tabular-nums; }
thead th {
  position: sticky; top: 0; z-index: 1; background: var(--panel-2); color: var(--accent-hi); font-weight: 600;
  padding: 5px 9px; border-bottom: 1px solid var(--border); white-space: nowrap;
}
/* a matrix's levels of columns: the second line sits under the first */
thead tr + tr th { top: 26px; }
td, tbody th { padding: 4px 9px; border-bottom: 1px solid var(--border-soft); white-space: nowrap; font-weight: 400; color: var(--text); text-align: left; }
tr.zebra td { background: var(--row-even); }
tbody tr:hover td { background: var(--row-hover); }
tr.g th { background: var(--panel-2); font-weight: 600; }
tr.foot th, tr.foot td { font-weight: 600; }
tr.total th, tr.total td { font-weight: 700; border-top: 1px solid var(--border); border-bottom: 0; position: sticky; bottom: 0; background: var(--panel); }
.sortbtn, .groupbtn {
  all: unset; display: inline-flex; align-items: center; gap: 4px; cursor: pointer; border-radius: 4px; box-sizing: border-box; max-width: 100%;
}
.sortbtn.right { justify-content: flex-end; width: 100%; }
.sortbtn .idle { opacity: 0; transition: opacity 0.12s; }
.sortbtn:hover .idle, .sortbtn:focus-visible .idle { opacity: 0.7; }
.sortbtn:focus-visible, .groupbtn:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.groupbtn:hover { color: var(--accent-hi); }
.pickbtn {
  all: unset; display: inline-flex; align-items: center; gap: 4px; cursor: pointer; border-radius: 4px; box-sizing: border-box; max-width: 100%;
  border-bottom: 1px dotted transparent;
}
.pickbtn.right { justify-content: flex-end; width: 100%; }
.pickbtn:hover { color: var(--accent-hi); border-bottom-color: currentColor; }
.pickbtn:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.pickbtn[aria-pressed='true'] { font-weight: 700; }
/* chosen: a tick and a bar on its edge, on any theme and without telling colours apart */
tr.picked > *:first-child { box-shadow: inset 3px 0 0 var(--text); }
th.picked { box-shadow: inset 0 -3px 0 var(--text); }
.pages { display: flex; align-items: center; justify-content: flex-end; gap: 6px; padding-top: 6px; font-size: 12px; color: var(--muted); }
.range { font-variant-numeric: tabular-nums; }
.pages .mini { padding: 2px 6px; }
.pages .mini:disabled { opacity: 0.4; cursor: default; }
.nodata { margin: auto; font-size: 12.5px; color: var(--muted); }
@media (prefers-reduced-motion: reduce) { .sortbtn .idle { transition: none; } }
</style>
