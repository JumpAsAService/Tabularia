<script setup lang="ts">
// The host's RAM indicator in real time: a 5-segment Likert scale (green → red) + memory
// used and available. It sits in the shell's topbar and in the editor's toolbar: Polars
// works in RAM, and seeing how much is left WHILE you build a flow avoids finding out the
// saturation with a failed run.
//
// If node-exporter does not answer (503) the badge disappears instead of showing an
// error: it is an extra, it must not disturb the work.
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { formatGB, useSystemMemory } from '~/composables/useSystemMemory'
import { MemoryStick } from 'lucide-vue-next'

// compact: in the editor's toolbar there is little room → "free" is left out
const props = withDefaults(defineProps<{ compact?: boolean }>(), { compact: false })

// in the editor (compact) the data must be fresh: there the memory is watched while a flow runs
const { memory, unavailable, level } = useSystemMemory({ fast: props.compact })
const { t } = useI18n()

// In the top bar the numbers give their place to the navigation (see the style): they
// stay here, in the tooltip and in the name for the screen readers, in the chosen
// language.
const summary = computed(() =>
  memory.value && level.value
    ? t('memoryGauge.summary', {
        used: formatGB(memory.value.used_bytes),
        available: formatGB(memory.value.available_bytes),
        total: formatGB(memory.value.total_bytes),
        pct: memory.value.used_percent,
        level: t(`memoryGauge.level${level.value.step}`),
      })
    : '',
)
</script>

<template>
  <span
    v-if="memory && level && !unavailable"
    class="memgauge"
    :class="{ 'in-topbar': !compact }"
    role="img"
    :title="summary"
    :aria-label="summary"
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
    <span v-if="!compact" class="free muted">· {{ $t('memoryGauge.free', { gb: formatGB(memory.available_bytes) }) }}</span>
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
/* growing segments: the scale "reads" without colour too */
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
/* In the top bar the navigation takes precedence: under 1800px icon and notches stay (the
   numbers in the tooltip), on the phone the indicator disappears — it is an extra. In the
   editor's toolbar (compact) nothing changes. */
/* `in-topbar`, not `shell`: the root of this component also carries the scope of
   the component that uses it, and AppShell's `.shell` rule (min-height: 100vh)
   turned the gauge into an invisible full-height strip that swallowed clicks. */
@media (max-width: 1800px) {
  .memgauge.in-topbar .txt, .memgauge.in-topbar .free { display: none; }
}
@media (max-width: 760px) {
  .memgauge.in-topbar { display: none; }
}
</style>
