<script setup lang="ts">
// Dialog di schedulazione (cron) riusabile: refresh datasource ed esecuzione
// flussi. Mostra la descrizione testuale live del cron (stile crontab.guru) via
// cronstrue in italiano.
import { ref, watch, computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { CalendarClock, X } from 'lucide-vue-next'
import cronstrue from 'cronstrue/i18n'

const { t } = useI18n()

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

const api = useApi()
const cronInput = ref('')
// fuso del deployment in cui gli orari cron vengono interpretati (dal gateway);
// caricato una volta alla prima apertura
const tz = ref('UTC')
let tzLoaded = false
watch(
  () => props.open,
  async (o) => {
    if (!o) return
    cronInput.value = props.current ?? ''
    if (!tzLoaded) {
      tzLoaded = true
      try { tz.value = (await api.appInfo()).timezone } catch { /* fallback UTC */ }
    }
  },
)

const CRON_PRESETS = [
  { label: t('scheduleDialog.presetEvery15Min'), cron: '*/15 * * * *' },
  { label: t('scheduleDialog.presetHourly'), cron: '0 * * * *' },
  { label: t('scheduleDialog.presetNightly'), cron: '0 3 * * *' },
  { label: t('scheduleDialog.presetWeeklyMonday'), cron: '0 6 * * 1' },
]

const cronDescription = computed<{ text: string; ok: boolean }>(() => {
  const expr = cronInput.value.trim()
  if (!expr) return { text: '', ok: true }
  try {
    return { text: cronstrue.toString(expr, { locale: 'it', verbose: false }), ok: true }
  } catch {
    return { text: t('scheduleDialog.invalidCron'), ok: false }
  }
})
</script>

<template>
  <Teleport to="body">
    <div v-if="open" class="sd-backdrop" @mousedown.self="emit('cancel')">
      <div class="sd-card" @keydown.esc="emit('cancel')">
        <div class="sd-head">
          <h3><CalendarClock :size="15" /> {{ $t('scheduleDialog.heading') }}</h3>
          <button class="sd-x" @click="emit('cancel')"><X :size="14" /></button>
        </div>
        <p class="muted sd-sub">
          {{ subtitle }} <strong>{{ title }}</strong>. {{ $t('scheduleDialog.timezoneNote') }} <strong>{{ tz }}</strong>.
        </p>

        <label>{{ $t('scheduleDialog.cronLabel') }}</label>
        <input
          v-model="cronInput"
          type="text"
          spellcheck="false"
          :placeholder="$t('scheduleDialog.cronPlaceholder')"
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
          <button v-if="current" class="danger" :disabled="busy" @click="emit('save', '')">{{ $t('scheduleDialog.disable') }}</button>
          <span class="sd-spacer" />
          <button :disabled="busy" @click="emit('cancel')">{{ $t('scheduleDialog.cancel') }}</button>
          <button class="primary" :disabled="busy || !cronInput.trim() || !cronDescription.ok" @click="emit('save', cronInput)">
            {{ $t('scheduleDialog.save') }}
          </button>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.sd-backdrop {
  position: fixed; inset: 0; background: var(--scrim); backdrop-filter: blur(2px);
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
