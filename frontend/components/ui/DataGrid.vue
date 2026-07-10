<script setup lang="ts">
import { computed } from 'vue'
import type { PreviewResult } from '~/composables/useApi'

const props = defineProps<{ result: PreviewResult | null; loading?: boolean; error?: string }>()

function fmt(v: any): string {
  if (v === null || v === undefined) return '∅'
  if (typeof v === 'object') return JSON.stringify(v)
  return String(v)
}

// ── Skeleton dell'anteprima ──────────────────────────────────────────────
// stesse colonne dell'ultimo risultato (clamp 3–8) così il layout non salta;
// larghezze pseudo-casuali ma deterministiche per un look organico e stabile
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
    <div v-else-if="error" class="pad" style="color: var(--danger)">{{ error }}</div>
    <div v-else-if="!result" class="muted pad">
      Seleziona un nodo per vedere l'anteprima del risultato.
    </div>
    <template v-else>
      <div class="pad muted">
        {{ result.row_count }} righe{{ result.truncated ? ' (troncato)' : '' }} ·
        {{ result.columns.length }} colonne
      </div>
      <table class="data">
        <thead>
          <tr>
            <th v-for="c in result.columns" :key="c.name">
              {{ c.name }}<br /><span class="muted" style="font-weight: 400">{{ c.dtype }}</span>
            </th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(row, i) in result.rows" :key="i">
            <td
              v-for="c in result.columns"
              :key="c.name"
              :class="{ null: row[c.name] === null || row[c.name] === undefined }"
            >
              {{ fmt(row[c.name]) }}
            </td>
          </tr>
        </tbody>
      </table>
    </template>
  </div>
</template>

<style scoped>
.datagrid { height: 100%; overflow: auto; }
.pad { padding: 8px; }
</style>
