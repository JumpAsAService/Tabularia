<script setup lang="ts">
import { Handle, Position } from '@vue-flow/core'
import { FileText } from 'lucide-vue-next'

defineProps<{ id: string; data: any }>()
</script>

<template>
  <div class="node node-source">
    <!-- ingresso di SEQUENZA: collega qui un «Refresh datasource» perché venga
         aggiornata PRIMA che la sorgente ne legga i dati -->
    <Handle id="seq-in" type="target" :position="Position.Left" class="handle-seq" />
    <div class="node-title"><FileText :size="13" /> {{ $t('sourceNode.title') }}</div>
    <div class="node-body">
      <template v-if="data.parquetKey">
        <div>{{ data.filename }}</div>
        <div class="muted">{{ $t('sourceNode.rowsCount', { n: data.rows }) }}</div>
      </template>
      <span v-else class="muted">{{ $t('sourceNode.noData') }}</span>
    </div>
    <Handle type="source" :position="Position.Right" />
  </div>
</template>

<style scoped>
/* handle di sequenza (orchestrazione), stesso viola degli archi di controllo */
.handle-seq { background: #c084fc !important; }
</style>
