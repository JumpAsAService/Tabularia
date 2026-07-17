<script setup lang="ts">
// Nodo di controllo (non un'operazione dell'engine): al run il gateway aggiorna
// la datasource indicata PRIMA degli output, così il flusso gira su dati freschi.
import { computed } from 'vue'
import { Handle, Position } from '@vue-flow/core'
import { RefreshCw } from 'lucide-vue-next'

const props = defineProps<{ id: string; data: any }>()
const summary = computed(() => props.data?.dsName?.trim() || 'scegli una datasource…')
</script>

<template>
  <div class="node node-ctl">
    <!-- handle di SEQUENZA (orizzontali): definiscono l'ordine di orchestrazione -->
    <Handle id="seq-in" type="target" :position="Position.Left" class="handle-seq" />
    <div class="node-title">
      <RefreshCw :size="13" class="node-icon" />
      Refresh datasource
    </div>
    <div class="node-body muted">{{ summary }}</div>
    <Handle id="seq-out" type="source" :position="Position.Right" class="handle-seq" />
  </div>
</template>

<style scoped>
.node-ctl { --node-accent: #38bdf8; min-width: 170px; }
.node-icon { color: var(--node-accent); }
.node-body { max-width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.handle-seq { background: var(--node-accent) !important; }
</style>
