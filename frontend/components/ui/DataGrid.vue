<script setup lang="ts">
import { computed, ref } from 'vue'
import { CircleAlert, MousePointerClick } from 'lucide-vue-next'
import type { PreviewResult } from '~/composables/useApi'
import { readableNumber } from '~/composables/readableNumber'

// note: something to say instead of a table that is not an error (no preview after a Script node)
// readable: for who reads an answer, not who checks a step (the assistant's tables): the exact
// value stays in the cell's tooltip and in the file
const props = defineProps<{ result: PreviewResult | null; loading?: boolean; error?: string; note?: string; readable?: boolean }>()

// numbers on the right with tabular digits; the VALUES stay raw: in a step's preview one
// wants to see the exact data, not a formatted version of it
const isNum = (dtype: string) => /Int|Float|Decimal/i.test(dtype)

function fmt(v: any, dtype = ''): string {
  if (v === null || v === undefined) return '∅'
  if (typeof v === 'object') return JSON.stringify(v)
  return props.readable && typeof v === 'number' ? readableNumber(v, dtype) : String(v)
}

// a long text is cut at the column's cap (CSS); the whole value is in the cell's tooltip
const LONG = 48
function tip(v: any, dtype: string): string | undefined {
  if (props.readable && typeof v === 'number') return String(v)
  if (v === null || v === undefined) return undefined
  const s = fmt(v, dtype)
  return s.length > LONG ? s : undefined
}

// the column under the pointer, tinted like the row: a <col> carries it, so one class
// changes and no cell is re-rendered
const hoverCol = ref(-1)
function onOver(e: MouseEvent) {
  const cell = (e.target as HTMLElement).closest?.('td, th') as HTMLTableCellElement | null
  hoverCol.value = cell ? cell.cellIndex : -1
}

// ── The preview's skeleton ──────────────────────────────────────────────
// the same columns as the last result (clamp 3–8) so the layout does not jump;
// pseudo-random but deterministic widths for an organic, stable look
const SK_WIDTHS = [64, 40, 88, 52, 72, 46, 60, 78]
const skCols = computed(() => Math.min(8, Math.max(3, props.result?.columns.length ?? 5)))
const skW = (r: number, c: number) => `${SK_WIDTHS[(r * 3 + c) % SK_WIDTHS.length]}px`
</script>

<template>
  <div class="datagrid">
    <div v-if="loading" aria-busy="true">
      <div class="pad"><span class="sk" style="width: 150px" /></div>
      <table class="data">
        <thead>
          <tr>
            <th v-for="c in skCols" :key="c">
              <span class="sk" :style="{ width: skW(0, c), height: '11px' }" /><br />
              <span class="sk" :style="{ width: '34px', height: '8px' }" />
            </th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in 8" :key="r">
            <td v-for="c in skCols" :key="c"><span class="sk" :style="{ width: skW(r, c) }" /></td>
          </tr>
        </tbody>
      </table>
    </div>
    <div v-else-if="error" class="dg-state dg-error" role="alert">
      <CircleAlert :size="16" class="dg-state-icon" aria-hidden="true" />
      <span>{{ error }}</span>
    </div>
    <div v-else-if="note && !result" class="dg-state muted">{{ note }}</div>
    <div v-else-if="!result" class="dg-state dg-empty muted">
      <MousePointerClick :size="16" class="dg-state-icon" aria-hidden="true" />
      <span>{{ $t('dataGrid.selectNode') }}</span>
    </div>
    <template v-else>
      <div v-if="note" class="pad" style="color: var(--muted); padding-bottom: 0">{{ note }}</div>
      <div class="pad muted dg-summary">
        {{ $t(result.truncated ? 'dataGrid.summaryTruncated' : 'dataGrid.summary',
              { rows: result.row_count, cols: result.columns.length }) }}
      </div>
      <table class="data" @mouseover="onOver" @mouseleave="hoverCol = -1">
        <colgroup>
          <col v-for="(c, j) in result.columns" :key="c.name" :class="{ hc: j === hoverCol }" />
        </colgroup>
        <thead>
          <tr>
            <th v-for="c in result.columns" :key="c.name" :class="{ num: isNum(c.dtype) }">
              <span class="cn">{{ c.name }}</span><span class="ct">{{ c.dtype }}</span>
            </th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(row, i) in result.rows" :key="i">
            <td
              v-for="c in result.columns"
              :key="c.name"
              :class="{ null: row[c.name] === null || row[c.name] === undefined, num: isNum(c.dtype) }"
              :title="tip(row[c.name], c.dtype)"
            >
              {{ fmt(row[c.name], c.dtype) }}
            </td>
          </tr>
        </tbody>
      </table>
    </template>
  </div>
</template>

<style scoped>
.data th.num, .data td.num { text-align: right; }
.datagrid { height: 100%; overflow: auto; }
.pad { padding: 8px; }
.dg-summary { font-variant-numeric: tabular-nums; font-size: 12px; padding: 7px 9px; }
/* empty, note and error: said in the panel's place of the table, with an icon */
.dg-state { display: flex; align-items: flex-start; gap: 8px; margin: 12px; font-size: 12.5px; line-height: 1.5; }
.dg-state-icon { flex: none; margin-top: 2px; }
.dg-error {
  padding: 10px 12px;
  border: 1px solid color-mix(in srgb, var(--danger) 45%, transparent);
  border-radius: 8px;
  background: var(--tint-danger);
  color: var(--danger-text);
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
</style>
