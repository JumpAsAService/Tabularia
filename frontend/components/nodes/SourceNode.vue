<script setup lang="ts">
import { Handle, Position } from '@vue-flow/core'
import { FileText, FlaskConical, Filter, FileSpreadsheet } from 'lucide-vue-next'
import { useI18n } from 'vue-i18n'
import { computed, inject, type ComputedRef } from 'vue'
import ContractBadge from '~/components/ui/ContractBadge.vue'
import type { ContractSummary } from '~/composables/useContracts'
import { sampleLabel, sourceFilterCount } from '~/composables/useFlowModel'

const props = defineProps<{ id: string; data: any }>()
const { t } = useI18n()
// the data contract state of the datasource read: whoever consumes data sees on the node
// whether whoever produces it is keeping the promise
const contracts = inject<ComputedRef<Map<number, ContractSummary>> | null>('datasourceContracts', null)
const contract = computed(() => (props.data?.datasourceId != null ? contracts?.value.get(props.data.datasourceId) ?? null : null))
</script>

<template>
  <div class="node node-source">
    <!-- SEQUENCE entry: connect a «Refresh datasource» here so that it is updated BEFORE the
         source reads its data -->
    <Handle id="seq-in" type="target" :position="Position.Left" class="handle-seq" />
    <div class="node-title"><FileText :size="13" /> {{ $t('sourceNode.title') }}</div>
    <div class="node-body">
      <template v-if="data.parquetKey">
        <div class="srcname">{{ data.filename }} <ContractBadge :contract="contract" :size="12" /></div>
        <div v-if="data.sheet" class="sheet"><FileSpreadsheet :size="11" /> {{ data.sheet }}</div>
        <div class="muted">{{ $t('sourceNode.rowsCount', { n: data.rows }, data.rows ?? 2) }}</div>
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
.srcname { display: flex; align-items: center; gap: 5px; }
.sheet { display: inline-flex; align-items: center; gap: 4px; font-size: 11px; color: var(--muted); }
/* sequence handle (orchestration), the same violet as the control edges */
.handle-seq { background: #c084fc !important; }
.filters { display: inline-flex; align-items: center; gap: 4px; margin-top: 3px; font-size: 11px; color: color-mix(in srgb, #60a5fa var(--enc-text-mix), var(--text)); font-weight: 500; }
.sample { display: inline-flex; align-items: center; gap: 4px; margin-top: 3px; font-size: 11px; color: color-mix(in srgb, #fbbf24 var(--enc-text-mix), var(--text)); font-weight: 500; }
</style>
