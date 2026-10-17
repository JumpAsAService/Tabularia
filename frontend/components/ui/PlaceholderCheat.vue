<script setup lang="ts">
// The cheat sheet of the placeholders, the same for every form of a kind: an
// input (datasource) also has {last_refresh} and the range variables, an output
// has the run's number and the flow's name. The codes are literals here, never in
// the messages: vue-i18n reads braces as its own parameters.
import { INPUT_PLACEHOLDERS, OUTPUT_PLACEHOLDERS, RANGE_EXAMPLE } from '~/composables/usePlaceholders'

defineProps<{ kind: 'input' | 'output' }>()
</script>

<template>
  <div class="pc" role="note" :data-cheat="kind">
    <span class="pc-soft">{{ $t('placeholders.cheatTitle') }}:</span>
    <code v-for="p in kind === 'input' ? INPUT_PLACEHOLDERS : OUTPUT_PLACEHOLDERS" :key="p">{{ p }}</code>
    <span class="pc-soft">{{ $t('placeholders.offsetsHint') }}</span>
    <span v-if="kind === 'input'" class="pc-soft">
      {{ $t('placeholders.rangeHint', { example: RANGE_EXAMPLE, day: '{day}', day_end: '{day_end}' }) }}
    </span>
    <span v-else class="pc-soft">
      {{ $t('placeholders.outputHint', { run_id: '{run_id}', flow: '{flow}', date: '{date}', datetime: '{datetime}' }) }}
    </span>
  </div>
</template>

<style scoped>
.pc { display: flex; flex-wrap: wrap; gap: 4px 6px; align-items: center; font-size: 11.5px; }
.pc code { font-size: 11px; padding: 0 4px; border: 1px solid var(--border-soft); border-radius: 4px; background: var(--bg-soft); }
.pc-soft { opacity: 0.7; font-size: 11.5px; color: var(--muted); }
</style>
