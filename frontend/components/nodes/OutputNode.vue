<script setup lang="ts">
// Nodo Output (terminale, stile Tableau Prep): dove finisce il risultato della
// catena — datasource del catalogo, tabella di database, o file/dataset su S3.
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { Handle, Position } from '@vue-flow/core'
import { Database, HardDriveDownload, CloudUpload } from 'lucide-vue-next'

const props = defineProps<{ id: string; data: any }>()
const { t } = useI18n()

const destType = computed(() => props.data?.destType ?? 'datasource')
const icon = computed(() =>
  destType.value === 'database' ? Database : destType.value === 's3' ? CloudUpload : HardDriveDownload,
)
const title = computed(() =>
  destType.value === 'database'
    ? t('outputNode.titleDatabase')
    : destType.value === 's3'
      ? t('outputNode.titleS3')
      : t('outputNode.titleDatasource'),
)
const summary = computed(() => {
  const d = props.data ?? {}
  if (destType.value === 'database') {
    const tbl = d.table?.trim()
    return tbl
      ? t('outputNode.tableSummary', { table: tbl, mode: d.mode === 'replace' ? t('outputNode.modeReplace') : t('outputNode.modeAppend') })
      : t('outputNode.chooseTable')
  }
  if (destType.value === 's3') {
    const k = d.s3Key?.trim()
    if (!k) return t('outputNode.chooseKeyBucket')
    const parts = (d.partitionBy ?? []).length ? t('outputNode.partitionsSuffix', { n: d.partitionBy.length }) : ''
    return `${k} (${d.s3Format ?? 'parquet'})${parts}`
  }
  const n = d.name?.trim()
  return n ? t('outputNode.datasourceSummary', { name: n }) : t('outputNode.nameDatasource')
})
</script>

<template>
  <div class="node node-output">
    <!-- input DATI: la catena di trasformazione (a sinistra, in alto) -->
    <Handle id="left" type="target" :position="Position.Left" class="h-left-top" />
    <!-- input di SEQUENZA: ordine di orchestrazione (a sinistra, sotto) -->
    <Handle id="seq-in" type="target" :position="Position.Left" class="handle-seq h-left-bottom" />
    <div class="node-title">
      <component :is="icon" :size="13" class="node-icon" />
      {{ title }}
    </div>
    <div class="node-body muted">{{ summary }}</div>
    <!-- uscita di SEQUENZA verso il prossimo nodo di controllo/output -->
    <Handle id="seq-out" type="source" :position="Position.Right" class="handle-seq" />
  </div>
</template>

<style scoped>
.node-output { --node-accent: #fbbf24; }
.node-icon { color: var(--node-accent); }
.node-body { max-width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.handle-seq { background: var(--node-accent) !important; }
</style>
