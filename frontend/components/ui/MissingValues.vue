<script setup lang="ts">
// The values of a range (or list) whose file was not there: «2 of 7 values have
// no file: day=2026-10-15 (in/s_20261015.csv), …». Shown by «Check the files»
// before saving and under a refresh that skipped them (the run's notes).
import { computed } from 'vue'
import { TriangleAlert } from 'lucide-vue-next'
import type { MissingValue } from '~/composables/usePlaceholders'

const props = defineProps<{
  missing: MissingValue[]
  count?: number | null // all of them; `missing` may be only the first ones
  expansions?: number | null
  max?: number
}>()

const total = computed(() => props.count ?? props.missing.length)
const shown = computed(() => props.missing.slice(0, props.max ?? 8))
const params = (m: MissingValue) => Object.entries(m.params ?? {}).map(([k, v]) => `${k}=${v}`).join(', ')
</script>

<template>
  <div v-if="total" class="mv" data-missing>
    <p class="mv-head">
      <TriangleAlert :size="12" />
      <span v-if="expansions">{{ $t('placeholders.missingOf', { n: total, total: expansions }) }}</span>
      <span v-else>{{ $t('placeholders.missingCount', { n: total }) }}</span>
    </p>
    <ul>
      <li v-for="(m, i) in shown" :key="i">
        {{ params(m) ? `${params(m)} ` : '' }}<span class="mv-soft">({{ m.pattern }})</span>
      </li>
    </ul>
    <p v-if="total > shown.length" class="mv-soft">{{ $t('placeholders.missingMore', { n: total - shown.length }) }}</p>
  </div>
</template>

<style scoped>
.mv { font-size: 12px; color: var(--warning); }
.mv p { margin: 0; }
.mv-head { display: flex; align-items: center; gap: 6px; }
.mv-head svg { flex: none; }
.mv ul { list-style: none; margin: 4px 0 0; padding: 0 0 0 18px; display: grid; gap: 2px; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11.5px; color: var(--text); }
.mv li { overflow-wrap: anywhere; }
.mv-soft { color: var(--muted); }
</style>
