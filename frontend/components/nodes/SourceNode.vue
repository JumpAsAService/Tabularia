<script setup lang="ts">
import { Handle, Position } from '@vue-flow/core'
import { FileText } from 'lucide-vue-next'

defineProps<{ id: string; data: any }>()
</script>

<template>
  <div class="node node-source">
    <div class="node-title"><FileText :size="13" /> Sorgente</div>
    <div class="node-body">
      <template v-if="data.parquetKey">
        <div>{{ data.filename }}</div>
        <div class="muted">{{ data.rows }} righe</div>
      </template>
      <span v-else class="muted">nessun dato — carica un file</span>
    </div>
    <Handle type="source" :position="Position.Right" />
  </div>
</template>
