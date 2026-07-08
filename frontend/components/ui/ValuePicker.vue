<script setup lang="ts">
// Multi-select with search for the "is one of…" filter: shows the column's
// real distinct values (computed by the engine on the full dataset) and lets
// the user search, pick, or add free values (e.g. {{placeholders}}).
import { ref, computed } from 'vue'
import { X, Plus, LoaderCircle } from 'lucide-vue-next'

const props = defineProps<{
  modelValue: any[]
  options: any[] | null // null = still loading
  loading?: boolean
}>()
const emit = defineEmits<{ (e: 'update:modelValue', values: any[]): void }>()

const query = ref('')
const MAX_SHOWN = 50

const selectedSet = computed(() => new Set(props.modelValue.map((v) => String(v))))

const filtered = computed(() => {
  const opts = props.options ?? []
  const q = query.value.trim().toLowerCase()
  const hits = q ? opts.filter((o) => String(o).toLowerCase().includes(q)) : opts
  return hits.slice(0, MAX_SHOWN)
})

const hiddenCount = computed(() => {
  const opts = props.options ?? []
  const q = query.value.trim().toLowerCase()
  const total = q ? opts.filter((o) => String(o).toLowerCase().includes(q)).length : opts.length
  return Math.max(0, total - MAX_SHOWN)
})

// free value not among the options (placeholders, values beyond the fetch cap)
const canAddFree = computed(() => {
  const q = query.value.trim()
  return q.length > 0 && !(props.options ?? []).some((o) => String(o) === q) && !selectedSet.value.has(q)
})

function toggle(v: any) {
  const key = String(v)
  const next = selectedSet.value.has(key)
    ? props.modelValue.filter((x) => String(x) !== key)
    : [...props.modelValue, v]
  emit('update:modelValue', next)
}

function addFree() {
  const raw = query.value.trim()
  if (!raw) return
  // numbers stay numbers (the engine compares typed values)
  const parsed = /^-?\d+(\.\d+)?$/.test(raw) ? Number(raw) : raw
  emit('update:modelValue', [...props.modelValue, parsed])
  query.value = ''
}
</script>

<template>
  <div class="vpicker">
    <!-- selected values as removable chips -->
    <div v-if="modelValue.length" class="chips">
      <span v-for="v in modelValue" :key="String(v)" class="chip">
        {{ v }}
        <button class="chip-x" title="Remove" @click="toggle(v)"><X :size="11" /></button>
      </span>
    </div>

    <input
      v-model="query"
      type="text"
      placeholder="Search values…"
      @keydown.enter.prevent="canAddFree ? addFree() : filtered.length === 1 ? toggle(filtered[0]) : null"
    />

    <div class="optlist">
      <p v-if="loading" class="muted state"><LoaderCircle :size="13" class="spin" /> Loading distinct values…</p>
      <template v-else>
        <label v-for="o in filtered" :key="String(o)" class="opt">
          <input type="checkbox" :checked="selectedSet.has(String(o))" @change="toggle(o)" />
          <span class="optval">{{ o }}</span>
        </label>
        <button v-if="canAddFree" class="addfree" @click="addFree">
          <Plus :size="12" /> Add “{{ query.trim() }}”
        </button>
        <p v-if="hiddenCount" class="muted state">+{{ hiddenCount }} more — refine the search</p>
        <p v-if="!filtered.length && !canAddFree" class="muted state">No matching values.</p>
      </template>
    </div>
  </div>
</template>

<style scoped>
.vpicker { display: flex; flex-direction: column; gap: 6px; }
.chips { display: flex; flex-wrap: wrap; gap: 4px; }
.chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  background: rgba(79, 140, 255, 0.14);
  border: 1px solid rgba(79, 140, 255, 0.4);
  border-radius: 999px;
  padding: 1px 4px 1px 9px;
  font-size: 12px;
  max-width: 100%;
}
.chip-x { padding: 2px; border: none; background: transparent; border-radius: 50%; }
.chip-x:hover { background: rgba(255, 255, 255, 0.12); box-shadow: none; }
.optlist {
  max-height: 170px;
  overflow-y: auto;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--bg-soft);
  padding: 4px;
  display: flex;
  flex-direction: column;
  gap: 1px;
}
.opt {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 6px;
  border-radius: 6px;
  font-size: 13px;
  cursor: pointer;
}
.opt:hover { background: var(--panel-2); }
.opt input { width: auto; }
.optval { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.addfree { justify-content: flex-start; font-size: 12px; margin-top: 2px; }
.state { display: flex; align-items: center; gap: 6px; font-size: 12px; margin: 4px 6px; }
</style>
