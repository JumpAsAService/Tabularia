<script setup lang="ts">
// Nodo di controllo: al run il gateway esegue un ALTRO flusso salvato (i suoi
// nodi Output), dopo gli output di questo. Guardia anti-ciclo lato gateway.
import { computed } from 'vue'
import { PlayCircle } from 'lucide-vue-next'

const props = defineProps<{ id: string; data: any }>()
const summary = computed(() => props.data?.flowName?.trim() || 'scegli un flusso…')
</script>

<template>
  <div class="node node-ctl">
    <div class="node-title">
      <PlayCircle :size="13" class="node-icon" />
      Esegui flusso
    </div>
    <div class="node-body muted">{{ summary }}</div>
  </div>
</template>

<style scoped>
.node-ctl { --node-accent: #c084fc; min-width: 170px; }
.node-icon { color: var(--node-accent); }
.node-body { max-width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
</style>
