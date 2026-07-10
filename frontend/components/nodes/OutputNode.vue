<script setup lang="ts">
// Nodo Output (terminale, stile Tableau Prep): dove finisce il risultato della
// catena — datasource del catalogo Tabularia oppure tabella di un database.
import { computed } from 'vue'
import { Handle, Position } from '@vue-flow/core'
import { Database, HardDriveDownload } from 'lucide-vue-next'

const props = defineProps<{ id: string; data: any }>()

const isDb = computed(() => props.data?.destType === 'database')
const summary = computed(() => {
  if (isDb.value) {
    const t = props.data?.table?.trim()
    return t ? `tabella ${t} (${props.data?.mode === 'replace' ? 'sostituisci' : 'accoda'})` : 'scegli la tabella…'
  }
  const n = props.data?.name?.trim()
  return n ? `datasource “${n}”` : 'dai un nome alla datasource…'
})
</script>

<template>
  <div class="node node-output">
    <Handle id="left" type="target" :position="Position.Left" />
    <div class="node-title">
      <component :is="isDb ? Database : HardDriveDownload" :size="13" class="node-icon" />
      Output {{ isDb ? 'database' : 'datasource' }}
    </div>
    <div class="node-body muted">{{ summary }}</div>
    <!-- terminale: nessun handle di uscita -->
  </div>
</template>

<style scoped>
.node-output { --node-accent: #fbbf24; }
.node-icon { color: var(--node-accent); }
.node-body { max-width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
</style>
