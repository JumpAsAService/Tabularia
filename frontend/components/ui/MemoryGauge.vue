<script setup lang="ts">
// Indicatore RAM dell'host in tempo reale: scala Likert a 5 segmenti (verde →
// rosso) + memoria usata e disponibile. Sta nella topbar della shell e nella
// toolbar dell'editor: Polars lavora in RAM, e vedere quanta ne resta MENTRE
// costruisci un flusso evita di scoprire la saturazione a run fallito.
//
// Se node-exporter non risponde (503) il badge sparisce invece di mostrare un
// errore: è un'informazione accessoria, non deve disturbare il lavoro.
import { formatGB, useSystemMemory } from '~/composables/useSystemMemory'
import { MemoryStick } from 'lucide-vue-next'

// compact: nella toolbar dell'editor lo spazio è poco → si omette "liberi"
withDefaults(defineProps<{ compact?: boolean }>(), { compact: false })

const { memory, unavailable, level } = useSystemMemory()
</script>

<template>
  <span
    v-if="memory && level && !unavailable"
    class="memgauge"
    :title="`RAM host: ${formatGB(memory.used_bytes)} usata · ${formatGB(
      memory.available_bytes,
    )} disponibile · ${formatGB(memory.total_bytes)} totale (${memory.used_percent}% — ${
      level.label
    })`"
  >
    <MemoryStick :size="13" class="ico" :style="{ color: level.color }" />
    <span class="bars" aria-hidden="true">
      <i
        v-for="s in 5"
        :key="s"
        :class="{ on: s <= level.step }"
        :style="s <= level.step ? { background: level.color } : undefined"
      />
    </span>
    <span class="txt">{{ formatGB(memory.used_bytes) }} / {{ formatGB(memory.total_bytes) }}</span>
    <span v-if="!compact" class="free muted">· {{ formatGB(memory.available_bytes) }} liberi</span>
  </span>
</template>

<style scoped>
.memgauge {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  white-space: nowrap;
  cursor: default;
}
.ico { flex: none; }
.bars { display: inline-flex; align-items: flex-end; gap: 2px; height: 12px; }
/* segmenti crescenti: la scala si "legge" anche senza colore */
.bars i {
  width: 3px;
  border-radius: 1px;
  background: var(--border);
  transition: background 0.25s;
}
.bars i:nth-child(1) { height: 4px; }
.bars i:nth-child(2) { height: 6px; }
.bars i:nth-child(3) { height: 8px; }
.bars i:nth-child(4) { height: 10px; }
.bars i:nth-child(5) { height: 12px; }
.txt { font-variant-numeric: tabular-nums; }
.free { font-size: 11.5px; }
</style>
