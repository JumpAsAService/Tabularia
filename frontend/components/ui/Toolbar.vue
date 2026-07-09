<script setup lang="ts">
import { computed } from 'vue'
import {
  Table2,
  Upload,
  Plus,
  Play,
  Save,
  CheckCircle2,
  XCircle,
  LoaderCircle,
  ArrowLeft,
  LogOut,
} from 'lucide-vue-next'

const { logout } = useAuth()

const props = defineProps<{
  status: string
  statusKind?: 'info' | 'ok' | 'error' | 'busy'
  busy?: boolean
  canRun?: boolean
  // salvataggio flusso
  flowName?: string
  projects?: { id: number; name: string }[]
  projectId?: number | null
}>()
const emit = defineEmits<{
  (e: 'upload', file: File): void
  (e: 'add-op'): void
  (e: 'add-source'): void
  (e: 'run'): void
  (e: 'save'): void
  (e: 'update:flowName', name: string): void
  (e: 'update:projectId', id: number | null): void
}>()

function onFile(ev: Event) {
  const input = ev.target as HTMLInputElement
  const file = input.files?.[0]
  if (file) emit('upload', file)
  input.value = '' // permette di ricaricare lo stesso file
}

// icona di stato: spinner mentre lavora, check verde su successo, X rossa su errore
const statusIcon = computed(() => {
  switch (props.statusKind) {
    case 'ok': return CheckCircle2
    case 'error': return XCircle
    case 'busy': return LoaderCircle
    default: return null
  }
})
</script>

<template>
  <div class="toolbar-inner">
    <strong class="brand"><Table2 :size="16" /> Tabularia</strong>

    <!-- nome del flusso + destinazione + salva -->
    <input
      class="flowname"
      type="text"
      :value="flowName"
      placeholder="Nome flusso…"
      @input="emit('update:flowName', ($event.target as HTMLInputElement).value)"
    />
    <Select
      v-if="projects?.length && projectId === null"
      class="projsel"
      :model-value="projectId"
      :options="projects.map((p) => ({ value: p.id, label: p.name }))"
      placeholder="cartella…"
      @update:model-value="emit('update:projectId', $event)"
    />
    <button :disabled="busy" title="Salva flusso" @click="emit('save')"><Save :size="15" /> Salva</button>

    <span class="sep" />

    <label class="filebtn">
      <Upload :size="15" /> Carica file
      <input type="file" accept=".csv,.tsv,.txt,.json,.ndjson,.jsonl,.xlsx,.xls,.parquet" @change="onFile" />
    </label>

    <button @click="emit('add-source')"><Plus :size="15" /> Sorgente</button>
    <button @click="emit('add-op')"><Plus :size="15" /> Operazione</button>
    <button class="primary" :disabled="!canRun || busy" @click="emit('run')"><Play :size="15" /> Esegui</button>

    <span class="status muted" :class="statusKind">
      <component
        :is="statusIcon"
        v-if="statusIcon"
        :size="15"
        :class="{ spin: statusKind === 'busy' }"
      />
      {{ status }}
    </span>

    <!-- navigazione: DENTRO la barra, così non copre mai lo stato del run -->
    <span class="sep" />
    <NuxtLink to="/" class="navbtn"><ArrowLeft :size="13" /> Progetti</NuxtLink>
    <button class="navbtn" title="Esci" @click="logout"><LogOut :size="13" /></button>
  </div>
</template>

<style scoped>
.toolbar-inner {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 12px;
}
.brand { display: inline-flex; align-items: center; gap: 6px; }
.flowname { width: 170px; }
.projsel { width: 140px; }
.sep { width: 1px; align-self: stretch; background: var(--border); }
.status {
  margin-left: auto;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.status.ok { color: var(--accent-2); }
.status.error { color: var(--danger); }
.navbtn {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 12px;
  padding: 5px 10px;
  background: var(--panel-2);
  border: 1px solid var(--border);
  border-radius: 6px;
  color: var(--text);
  text-decoration: none;
  white-space: nowrap;
  cursor: pointer;
}
.navbtn:hover { border-color: var(--accent); }
.filebtn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  background: var(--panel-2);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 6px 12px;
  cursor: pointer;
  white-space: nowrap;
}
.filebtn:hover { border-color: var(--accent); }
.filebtn input { display: none; }
</style>
