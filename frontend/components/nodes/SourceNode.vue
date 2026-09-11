<script setup lang="ts">
import { Handle, Position } from '@vue-flow/core'
import { FileText, FlaskConical, Filter } from 'lucide-vue-next'
import { useI18n } from 'vue-i18n'
import { sampleLabel, sourceFilterCount } from '~/composables/useFlowModel'

defineProps<{ id: string; data: any }>()
const { t } = useI18n()
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
        <div v-if="sourceFilterCount(data)" class="filters" :title="$t('sourceNode.filtersTitle')">
          <Filter :size="11" /> {{ $t('sourceNode.filtersBadge', { n: sourceFilterCount(data) }) }}
        </div>
        <div v-if="sampleLabel(data, t)" class="sample" :title="$t('sourceNode.sampleTitle')">
          <FlaskConical :size="11" /> {{ sampleLabel(data, t) }}
        </div>
      </template>
      <span v-else class="muted">{{ $t('sourceNode.noData') }}</span>
    </div>
    <Handle type="source" :position="Position.Right" />
  </div>
</template>

<style scoped>
/* handle di sequenza (orchestrazione), stesso viola degli archi di controllo */
.handle-seq { background: #c084fc !important; }
.filters { display: inline-flex; align-items: center; gap: 4px; margin-top: 3px; font-size: 11px; color: #60a5fa; font-weight: 500; }
.sample { display: inline-flex; align-items: center; gap: 4px; margin-top: 3px; font-size: 11px; color: #fbbf24; font-weight: 500; }
</style>
