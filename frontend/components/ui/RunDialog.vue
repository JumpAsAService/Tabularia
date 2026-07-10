<script setup lang="ts">
// Dialog di lancio del run: esegui e basta, oppure pubblica l'output come
// datasource nominata (riusabile come sorgente in altri flussi). Se il canvas
// ha nodi Output, il dialog mostra COSA verrà scritto (un run per output): la
// configurazione sta sui nodi, qui si conferma soltanto.
import { ref, watch } from 'vue'
import { Play, X, Database, HardDriveDownload } from 'lucide-vue-next'
import type { PublishSpec } from '~/composables/useRuns'

const props = defineProps<{
  open: boolean
  canPublish: boolean // richiede un flusso salvato (il run entra nella cronologia)
  projects: { id: number; name: string }[]
  defaultProjectId: number | null
  error?: string // errore di lancio (409 nome duplicato, 403…): il dialog resta aperto
  busy?: boolean
  outputs?: { id: string; label: string; detail: string }[] // nodi Output del canvas
}>()
const emit = defineEmits<{
  (e: 'confirm', publish: PublishSpec | null): void
  (e: 'cancel'): void
}>()

const publishEnabled = ref(false)
const name = ref('')
const projectId = ref<number | null>(null)
const description = ref('')

watch(
  () => props.open,
  (open) => {
    if (open) {
      publishEnabled.value = false
      name.value = ''
      description.value = ''
      projectId.value = props.defaultProjectId
    }
  },
)

const nameMissing = () => publishEnabled.value && (!name.value.trim() || projectId.value === null)

function confirm() {
  if (nameMissing()) return
  emit(
    'confirm',
    publishEnabled.value && props.canPublish
      ? { name: name.value.trim(), project_id: projectId.value!, description: description.value }
      : null,
  )
}
</script>

<template>
  <Teleport to="body">
    <div v-if="open" class="rd-backdrop" @mousedown.self="emit('cancel')">
      <div class="rd-card" @keydown.esc="emit('cancel')">
        <div class="rd-head">
          <h3><Play :size="15" /> Esegui flusso</h3>
          <button class="rd-x" @click="emit('cancel')"><X :size="14" /></button>
        </div>

        <p class="muted rd-sub">Il flusso viene eseguito sull'intero dataset dal worker.</p>

        <!-- con nodi Output nel canvas: un run per output, configurati sui nodi -->
        <template v-if="outputs?.length">
          <div class="rd-outputs">
            <div v-for="o in outputs" :key="o.id" class="rd-output">
              <HardDriveDownload :size="14" class="rd-oicon" />
              <div>
                <div>{{ o.label }}</div>
                <div v-if="o.detail" class="muted rd-odetail">{{ o.detail }}</div>
              </div>
            </div>
          </div>
          <p class="muted rd-hint">
            {{ outputs.length === 1 ? 'Verrà lanciato 1 run.' : `Verranno lanciati ${outputs.length} run, uno per output.` }}
          </p>
        </template>

        <template v-else>
          <label class="chk rd-toggle" :class="{ off: !canPublish }">
            <input v-model="publishEnabled" type="checkbox" :disabled="!canPublish" />
            <Database :size="14" />
            Pubblica l'output come datasource
          </label>
          <p v-if="!canPublish" class="muted rd-hint">
            Salva il flusso per pubblicare l'output e avere la cronologia dei run.
          </p>

          <template v-if="publishEnabled && canPublish">
            <label>Nome della datasource</label>
            <input v-model="name" type="text" placeholder="es. vendite_pulite_2024" @keyup.enter="confirm" />
            <label>Cartella</label>
            <Select
              v-model="projectId"
              :options="projects.map((p) => ({ value: p.id, label: p.name }))"
              placeholder="cartella…"
            />
            <label>Descrizione (opzionale)</label>
            <input v-model="description" type="text" placeholder="cosa contiene, per chi" />
          </template>
        </template>

        <p v-if="error" class="rd-err">{{ error }}</p>

        <div class="rd-actions">
          <button @click="emit('cancel')">Annulla</button>
          <button class="primary" :disabled="nameMissing() || busy" @click="confirm">
            <Play :size="14" /> Esegui
          </button>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.rd-backdrop {
  position: fixed;
  inset: 0;
  background: rgba(5, 7, 12, 0.6);
  backdrop-filter: blur(2px);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 2000;
}
.rd-card {
  width: 380px;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 14px;
  box-shadow: var(--shadow-2);
  padding: 18px 20px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.rd-head { display: flex; align-items: center; justify-content: space-between; }
.rd-head h3 { margin: 0; display: inline-flex; align-items: center; gap: 7px; font-size: 16px; }
.rd-x { padding: 3px 7px; }
.rd-sub { font-size: 12px; margin: 0 0 4px; }
.rd-toggle { display: flex; align-items: center; gap: 7px; font-size: 13px; margin-top: 4px; }
.rd-toggle input { width: auto; }
.rd-toggle.off { opacity: 0.55; }
.rd-hint { font-size: 12px; margin: 0; }
.rd-outputs { display: flex; flex-direction: column; gap: 6px; margin-top: 2px; }
.rd-output {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 7px 10px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--panel-2);
  font-size: 13px;
}
.rd-oicon { color: #fbbf24; margin-top: 2px; flex-shrink: 0; }
.rd-odetail { font-size: 11.5px; }
label { font-size: 12px; color: var(--muted); }
.rd-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 10px; }
.rd-err { color: var(--danger); font-size: 12px; margin: 4px 0 0; }
</style>
