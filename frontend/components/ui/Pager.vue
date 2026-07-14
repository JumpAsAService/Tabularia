<script setup lang="ts">
// Controlli di paginazione: "X–Y di Z" + precedente/successivo. Si mostra solo
// se il totale supera una pagina.
import { computed } from 'vue'
import { ChevronLeft, ChevronRight } from 'lucide-vue-next'

const props = defineProps<{ offset: number; pageSize: number; total: number; loading?: boolean }>()
const emit = defineEmits<{ prev: []; next: [] }>()

const from = computed(() => (props.total === 0 ? 0 : props.offset + 1))
const to = computed(() => Math.min(props.offset + props.pageSize, props.total))
const hasPrev = computed(() => props.offset > 0)
const hasNext = computed(() => props.offset + props.pageSize < props.total)
</script>

<template>
  <div v-if="total > pageSize" class="pager">
    <span class="muted">{{ from }}–{{ to }} di {{ total }}</span>
    <button class="mini" :disabled="!hasPrev || loading" title="Precedente" @click="emit('prev')">
      <ChevronLeft :size="15" />
    </button>
    <button class="mini" :disabled="!hasNext || loading" title="Successiva" @click="emit('next')">
      <ChevronRight :size="15" />
    </button>
  </div>
</template>

<style scoped>
.pager { display: flex; align-items: center; justify-content: flex-end; gap: 8px; margin-top: 14px; font-size: 13px; }
.mini { padding: 5px 8px; }
.mini:disabled { opacity: 0.4; cursor: default; }
</style>
