<script setup lang="ts">
// Dialog di schedulazione (cron) riusabile: refresh datasource ed esecuzione
// flussi. Mostra la descrizione testuale live del cron (stile crontab.guru) via
// cronstrue in italiano.
import { ref, watch, computed } from 'vue'
import { CalendarClock, X } from 'lucide-vue-next'
import cronstrue from 'cronstrue/i18n'

const props = defineProps<{
  open: boolean
  title: string // nome dell'oggetto (datasource/flusso)
  subtitle?: string // cosa fa lo schedule
  current: string | null // cron attuale (per prefill e per mostrare "Disattiva")
  busy?: boolean
}>()
const emit = defineEmits<{
  (e: 'save', cron: string): void
  (e: 'cancel'): void
}>()

const cronInput = ref('')
watch(
  () => props.open,
  (o) => { if (o) cronInput.value = props.current ?? '' },
)

const CRON_PRESETS = [
  { label: 'Ogni 15 min', cron: '*/15 * * * *' },
  { label: 'Ogni ora', cron: '0 * * * *' },
  { label: 'Ogni notte (03:00)', cron: '0 3 * * *' },
  { label: 'Ogni lunedì (06:00)', cron: '0 6 * * 1' },
]

const cronDescription = computed<{ text: string; ok: boolean }>(() => {
  const expr = cronInput.value.trim()
  if (!expr) return { text: '', ok: true }
  try {
    return { text: cronstrue.toString(expr, { locale: 'it', verbose: false }), ok: true }
  } catch {
    return { text: 'Espressione cron non valida', ok: false }
  }
})
</script>

<template>
  <Teleport to="body">
    <div v-if="open" class="sd-backdrop" @mousedown.self="emit('cancel')">
      <div class="sd-card" @keydown.esc="emit('cancel')">
        <div class="sd-head">
          <h3><CalendarClock :size="15" /> Schedulazione</h3>
          <button class="sd-x" @click="emit('cancel')"><X :size="14" /></button>
        </div>
        <p class="muted sd-sub">
          {{ subtitle }} <strong>{{ title }}</strong>. Orari in UTC.
        </p>

        <label>Espressione cron (minuto ora giorno mese giorno-settimana)</label>
        <input
          v-model="cronInput"
          type="text"
          spellcheck="false"
          placeholder="es. 0 3 * * *"
          @keyup.enter="emit('save', cronInput)"
        />
        <p v-if="cronDescription.text" class="sd-desc" :class="{ bad: !cronDescription.ok }">
          {{ cronDescription.ok ? '↳ ' : '' }}{{ cronDescription.text }}
        </p>
        <div class="sd-presets">
          <button v-for="p in CRON_PRESETS" :key="p.cron" class="preset" @click="cronInput = p.cron">
            {{ p.label }}
          </button>
        </div>

        <div class="sd-actions">
          <button v-if="current" class="danger" :disabled="busy" @click="emit('save', '')">Disattiva</button>
          <span class="sd-spacer" />
          <button :disabled="busy" @click="emit('cancel')">Annulla</button>
          <button class="primary" :disabled="busy || !cronInput.trim() || !cronDescription.ok" @click="emit('save', cronInput)">
            Salva
          </button>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.sd-backdrop {
  position: fixed; inset: 0; background: rgba(5, 7, 12, 0.6); backdrop-filter: blur(2px);
  display: flex; align-items: center; justify-content: center; z-index: 2000;
}
.sd-card {
  width: 440px; background: var(--panel); border: 1px solid var(--border); border-radius: 14px;
  box-shadow: var(--shadow-2); padding: 18px 20px; display: flex; flex-direction: column; gap: 8px;
}
.sd-head { display: flex; align-items: center; justify-content: space-between; }
.sd-head h3 { margin: 0; display: inline-flex; align-items: center; gap: 7px; font-size: 16px; }
.sd-x { padding: 3px 7px; }
.sd-sub { font-size: 12px; margin: 0 0 4px; }
label { font-size: 12px; color: var(--muted); }
input { font-family: ui-monospace, monospace; }
.sd-desc { font-size: 12.5px; margin: 2px 0 0; color: var(--accent-2); font-weight: 500; }
.sd-desc.bad { color: var(--danger); }
.sd-presets { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 4px; }
.preset { font-size: 12px; padding: 4px 9px; }
.sd-actions { display: flex; align-items: center; gap: 8px; margin-top: 12px; }
.sd-spacer { flex: 1; }
</style>
