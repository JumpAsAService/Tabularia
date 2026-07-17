<script setup lang="ts">
// Nodo Output (terminale, stile Tableau Prep): dove finisce il risultato della
// catena — datasource del catalogo, tabella di database, o file/dataset su S3.
import { computed } from 'vue'
import { Handle, Position } from '@vue-flow/core'
import { Database, HardDriveDownload, CloudUpload } from 'lucide-vue-next'

const props = defineProps<{ id: string; data: any }>()

const destType = computed(() => props.data?.destType ?? 'datasource')
const icon = computed(() =>
  destType.value === 'database' ? Database : destType.value === 's3' ? CloudUpload : HardDriveDownload,
)
const title = computed(() =>
  destType.value === 'database' ? 'Output database' : destType.value === 's3' ? 'Output S3' : 'Output datasource',
)
const summary = computed(() => {
  const d = props.data ?? {}
  if (destType.value === 'database') {
    const t = d.table?.trim()
    return t ? `tabella ${t} (${d.mode === 'replace' ? 'sostituisci' : 'accoda'})` : 'scegli la tabella…'
  }
  if (destType.value === 's3') {
    const k = d.s3Key?.trim()
    if (!k) return 'scegli chiave e bucket…'
    const parts = (d.partitionBy ?? []).length ? ` · ${d.partitionBy.length} partizioni` : ''
    return `${k} (${d.s3Format ?? 'parquet'})${parts}`
  }
  const n = d.name?.trim()
  return n ? `datasource “${n}”` : 'dai un nome alla datasource…'
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
