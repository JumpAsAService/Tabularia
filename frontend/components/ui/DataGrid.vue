<script setup lang="ts">
import type { PreviewResult } from '~/composables/useApi'

defineProps<{ result: PreviewResult | null; loading?: boolean; error?: string }>()

function fmt(v: any): string {
  if (v === null || v === undefined) return '∅'
  if (typeof v === 'object') return JSON.stringify(v)
  return String(v)
}
</script>

<template>
  <div class="datagrid">
    <div v-if="loading" class="muted pad">Anteprima in corso…</div>
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
