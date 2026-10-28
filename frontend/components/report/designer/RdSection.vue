<script setup lang="ts">
// A part of the properties panel that folds: its title as a button, its content under it.
// Whether it is open is remembered for the session by its key, item after item.
import { computed } from 'vue'
import { ChevronRight } from 'lucide-vue-next'

const props = defineProps<{ title: string; skey: string; count?: number | string | null; bad?: boolean; closed?: boolean }>()
const state = useState<Record<string, boolean>>('report-designer-sections', () => ({}))
const open = computed({
  get: () => state.value[props.skey] ?? !props.closed,
  set: (v: boolean) => { state.value = { ...state.value, [props.skey]: v } },
})
const uid = `rsec-${Math.random().toString(36).slice(2, 9)}`
</script>

<template>
  <section class="rsec" :class="{ bad }">
    <div class="rsec-h">
      <h3 class="rsec-hh">
        <button type="button" class="rsec-t" :aria-expanded="open || bad" :aria-controls="uid" @click="open = !(open || bad)">
          <ChevronRight :size="13" class="rsec-chev" />
          <span>{{ title }}</span>
          <span v-if="count != null && count !== ''" class="rsec-n">{{ count }}</span>
        </button>
      </h3>
      <slot name="actions" />
    </div>
    <div v-show="open || bad" :id="uid" class="rsec-body"><slot /></div>
  </section>
</template>

<style scoped>
.rsec { border-top: 1px solid var(--border-soft); padding: 2px 0 6px; }
.rsec-h { display: flex; align-items: center; gap: 6px; }
.rsec-hh { margin: 0; flex: 1; min-width: 0; font: inherit; display: flex; }
.rsec-t {
  flex: 1; justify-content: flex-start; gap: 6px; border: 0; background: transparent; padding: 9px 2px 7px; box-shadow: none;
  font-size: 11px; font-weight: 600; letter-spacing: 0.05em; text-transform: uppercase; color: var(--muted);
}
.rsec-t:hover { color: var(--text); box-shadow: none; }
.rsec-t:active { transform: none; }
.rsec.bad .rsec-t { color: var(--danger); }
.rsec-chev { transition: transform 0.15s ease; }
.rsec-t[aria-expanded='true'] .rsec-chev { transform: rotate(90deg); }
.rsec-n { font-size: 10.5px; letter-spacing: 0; text-transform: none; padding: 0 6px; border-radius: 999px; background: var(--panel-2); color: var(--muted); font-weight: 500; }
.rsec-body { display: flex; flex-direction: column; gap: 10px; padding: 2px 0 6px; }
@media (prefers-reduced-motion: reduce) { .rsec-chev { transition: none; } }
</style>
