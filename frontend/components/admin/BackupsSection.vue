<script setup lang="ts">
// Backups (Admin panel): the automatic backup of Tabularia's database into the object
// storage. On top how it is going (the last one, the next one, «Back up now»), then the
// schedule and how many stay — the installation's values until an administrator changes
// them — and the backups there, to download. The observer sees the state, without the
// error text, and downloads nothing.
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { CheckCircle2, DatabaseBackup, Download, LoaderCircle, Play, Power, TriangleAlert, XCircle } from 'lucide-vue-next'
import { errMessage } from '~/composables/useApi'
import { useFormat } from '~/composables/useFormat'
import { useBackups, type BackupState, type BackupUpdate } from '~/composables/useBackups'

const emit = defineEmits<{ (e: 'count', n: number): void }>()

const { t } = useI18n()
const fmt = useFormat()
const api = useBackups()
const toast = useToast()

const stato = ref<BackupState | null>(null)
const errore = ref('')
const salvando = ref(false)
const avviando = ref(false)
const modulo = ref({ enabled: true, schedule: '', keep: 14 })

const PRESETS = [
  { cron: '0 3 * * *', key: 'presetNightly' },
  { cron: '0 */6 * * *', key: 'presetSixHours' },
  { cron: '0 2 * * 0', key: 'presetWeekly' },
] as const

function riempi(s: BackupState) {
  modulo.value = { enabled: s.enabled, schedule: s.schedule, keep: s.keep }
}

async function carica(silenzioso = false) {
  try {
    const s = await api.get()
    // the form is filled the first time only: a refresh must not undo what is being typed
    if (!stato.value) riempi(s)
    stato.value = s
    errore.value = ''
    emit('count', s.items.length)
  } catch (e) {
    if (!silenzioso) errore.value = errMessage(e)
  }
}

// while a backup runs the page follows it closely; otherwise now and then
let timer: ReturnType<typeof setTimeout> | undefined
function prossimo() {
  const s = stato.value
  const vivo = !!(s && (s.running_since || s.requested_at))
  timer = setTimeout(async () => {
    await carica(true)
    prossimo()
  }, vivo ? 3000 : 30_000)
}
onMounted(async () => {
  await carica()
  prossimo()
})
onBeforeUnmount(() => clearTimeout(timer))

const modificabile = computed(() => !!stato.value?.editable)

const cambi = computed<BackupUpdate>(() => {
  const s = stato.value
  if (!s) return {}
  const m = modulo.value
  const out: BackupUpdate = {}
  if (m.enabled !== s.enabled) out.enabled = m.enabled
  if (m.schedule.trim() !== s.schedule) out.schedule = m.schedule.trim()
  if (Number(m.keep) !== s.keep) out.keep = Number(m.keep)
  return out
})
const daSalvare = computed(() => Object.keys(cambi.value).length > 0)

async function salva(body: BackupUpdate = cambi.value) {
  salvando.value = true
  try {
    const s = await api.save(body)
    stato.value = s
    riempi(s)
    toast.success(t('backups.saved'))
  } catch (e) {
    toast.error(errMessage(e))
  } finally {
    salvando.value = false
  }
}

async function adesso() {
  avviando.value = true
  try {
    stato.value = await api.runNow()
    clearTimeout(timer)
    prossimo()
  } catch (e) {
    toast.error(errMessage(e))
  } finally {
    avviando.value = false
  }
}

function bytes(n: number | null | undefined): string {
  if (n == null) return '—'
  if (n < 1024 ** 2) return `${fmt.number(n / 1024, 1)} KiB`
  return n >= 1024 ** 3 ? `${fmt.number(n / 1024 ** 3, 1)} GiB` : `${fmt.number(n / 1024 ** 2, 1)} MiB`
}

// the moment a backup was made, from its name (tabularia-2026-10-22T03-00-00Z.dump)
function quando(name: string): string {
  const m = name.match(/^tabularia-(\d{4}-\d{2}-\d{2})T(\d{2})-(\d{2})-(\d{2})Z\.dump$/)
  return m ? fmt.dateTime(`${m[1]}T${m[2]}:${m[3]}:${m[4]}Z`) : name
}

const fallito = computed(() => {
  const s = stato.value
  if (!s?.last_error_at) return false
  return !s.last_ok_at || new Date(s.last_error_at) > new Date(s.last_ok_at)
})

const situazione = computed<{ tono: 'off' | 'ok' | 'errore' | 'attesa'; testo: string }>(() => {
  const s = stato.value
  if (!s) return { tono: 'off', testo: '' }
  if (s.running_since) return { tono: 'attesa', testo: t('backups.stateRunning', { when: fmt.ago(s.running_since) }) }
  if (s.requested_at) return { tono: 'attesa', testo: t('backups.stateRequested') }
  if (fallito.value) return { tono: 'errore', testo: t('backups.stateFailed', { when: fmt.ago(s.last_error_at) }) }
  if (!s.enabled) return { tono: 'off', testo: s.last_ok_at ? t('backups.stateOffLast', { when: fmt.ago(s.last_ok_at) }) : t('backups.stateOff') }
  if (s.last_ok_at) return { tono: 'ok', testo: t('backups.stateOk', { when: fmt.ago(s.last_ok_at), size: bytes(s.last_size) }) }
  return { tono: 'attesa', testo: t('backups.stateNone') }
})
</script>

<template>
  <div class="card">
    <h4><DatabaseBackup :size="14" /> {{ $t('backups.title') }}</h4>
    <p class="hint muted small">{{ $t('backups.lead') }}</p>
    <p v-if="errore" class="errore-pagina" role="alert">{{ errore }}</p>

    <template v-if="stato">
      <p v-if="!stato.available" class="nota" role="note">
        {{ stato.disabled_reason === 'kubernetes' ? $t('backups.kubernetesOnly') : stato.disabled_reason === 'postgres' ? $t('backups.notPostgres') : $t('backups.noPgDump') }}
      </p>
      <template v-else>
        <div class="stato" :class="situazione.tono" role="status">
          <LoaderCircle v-if="stato.running_since || stato.requested_at" :size="15" class="spin" />
          <component :is="situazione.tono === 'ok' ? CheckCircle2 : situazione.tono === 'off' ? Power : situazione.tono === 'errore' ? XCircle : TriangleAlert" v-else :size="15" />
          <span>{{ situazione.testo }}</span>
          <button
            v-if="modificabile"
            type="button"
            class="primary mini"
            :disabled="avviando || !!stato.running_since || !!stato.requested_at"
            @click="adesso"
          >
            <Play :size="12" /> {{ $t('backups.runNow') }}
          </button>
        </div>
        <p v-if="fallito && stato.last_error" class="ultimo-errore"><code>{{ stato.last_error }}</code></p>
        <dl class="numeri">
          <div><dt>{{ $t('backups.next') }}</dt><dd>{{ stato.enabled && stato.next_at ? fmt.dateTime(stato.next_at) : '—' }}</dd></div>
          <div><dt>{{ $t('backups.last') }}</dt><dd>{{ stato.last_ok_at ? fmt.dateTime(stato.last_ok_at) : $t('backups.never') }}</dd></div>
          <div><dt>{{ $t('backups.kept') }}</dt><dd>{{ $t('backups.keptOf', { n: stato.items.length, keep: stato.keep }) }}</dd></div>
        </dl>
        <p v-if="stato.bucket" class="dove small">
          {{ $t('backups.where') }} <code>{{ stato.bucket }}/{{ stato.prefix }}</code>
        </p>
        <p v-if="stato.same_as_data" class="avviso" role="note">
          <TriangleAlert :size="13" /> {{ $t('backups.sameBucket') }}
        </p>
        <p v-if="stato.storage_error" class="errore-pagina">{{ stato.storage_error }}</p>
      </template>
    </template>
  </div>

  <form v-if="stato?.available" class="card" @submit.prevent="salva()">
    <h4>{{ $t('backups.settingsTitle') }}</h4>
    <p class="hint muted small">{{ $t('backups.settingsLead') }}</p>

    <label class="spunta">
      <input v-model="modulo.enabled" type="checkbox" :disabled="!modificabile || salvando" />
      <span>{{ $t('backups.enabled') }}</span>
      <button v-if="modificabile && stato.changed.enabled" type="button" class="link" @click="salva({ reset: ['enabled'] })">
        {{ $t('backups.reset', { value: stato.defaults.enabled ? $t('backups.on') : $t('backups.off') }) }}
      </button>
    </label>

    <div class="campi">
      <label class="campo">
        <span>{{ $t('backups.schedule') }}</span>
        <input v-model="modulo.schedule" type="text" spellcheck="false" autocomplete="off" placeholder="0 3 * * *" :disabled="!modificabile || salvando" />
        <small>
          {{ $t('backups.scheduleHint', { tz: stato.timezone }) }}
          <button v-if="modificabile && stato.changed.schedule" type="button" class="link" @click="salva({ reset: ['schedule'] })">
            {{ $t('backups.reset', { value: stato.defaults.schedule }) }}
          </button>
        </small>
        <span v-if="modificabile" class="preset">
          <button v-for="p in PRESETS" :key="p.cron" type="button" class="chip" :disabled="salvando" @click="modulo.schedule = p.cron">
            {{ $t(`backups.${p.key}`) }}
          </button>
        </span>
      </label>
      <label class="campo">
        <span>{{ $t('backups.keep') }}</span>
        <input v-model.number="modulo.keep" type="number" min="1" max="1000" :disabled="!modificabile || salvando" />
        <small>
          {{ $t('backups.keepHint') }}
          <button v-if="modificabile && stato.changed.keep" type="button" class="link" @click="salva({ reset: ['keep'] })">
            {{ $t('backups.reset', { value: stato.defaults.keep }) }}
          </button>
        </small>
      </label>
    </div>

    <div v-if="modificabile" class="azioni">
      <button type="submit" class="primary" :disabled="salvando || !daSalvare">
        <LoaderCircle v-if="salvando" :size="13" class="spin" /> {{ $t('backups.save') }}
      </button>
    </div>
    <p v-else class="nota">{{ $t('backups.readOnly') }}</p>
  </form>

  <div v-if="stato?.available" class="card">
    <h4>{{ $t('backups.listTitle') }}</h4>
    <p class="hint muted small">{{ $t('backups.listLead') }}</p>
    <p v-if="!stato.items.length" class="muted small">{{ $t('backups.listEmpty') }}</p>
    <table v-else class="elenco">
      <thead>
        <tr><th>{{ $t('backups.colWhen') }}</th><th>{{ $t('backups.colName') }}</th><th class="num">{{ $t('backups.colSize') }}</th><th v-if="modificabile" /></tr>
      </thead>
      <tbody>
        <tr v-for="b in stato.items" :key="b.name">
          <td>{{ quando(b.name) }}</td>
          <td><code>{{ b.name }}</code></td>
          <td class="num">{{ bytes(b.size) }}</td>
          <td v-if="modificabile" class="num">
            <a class="scarica" :href="api.downloadUrl(b.name)" :download="b.name"><Download :size="13" /> {{ $t('backups.download') }}</a>
          </td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<style scoped>
.card { display: flex; flex-direction: column; gap: 10px; padding: 16px; background: var(--panel); border: 1px solid var(--border); border-radius: var(--radius); }
.card h4 { margin: 0; display: inline-flex; align-items: center; gap: 7px; }
.hint { margin: 0; line-height: 1.5; }
.small { font-size: 12px; }
.errore-pagina { color: var(--danger); font-size: 13px; margin: 0; }
.nota { margin: 0; font-size: 12.5px; padding: 8px 10px; border-radius: 8px; background: var(--bg-soft); border: 1px solid var(--border); }

.stato { display: flex; align-items: center; gap: 8px; padding: 10px 12px; border-radius: 8px; border: 1px solid var(--border); background: var(--bg-soft); font-size: 13px; }
.stato span { flex: 1; }
.stato.ok { border-color: color-mix(in srgb, var(--accent-2) 45%, var(--border)); }
.stato.ok svg { color: var(--accent-2); }
.stato.errore { border-color: color-mix(in srgb, var(--danger) 50%, var(--border)); }
.stato.errore svg { color: var(--danger); }
.stato.attesa svg { color: var(--warning); }
.stato.off svg { color: var(--muted); }
button.mini { padding: 2px 10px; min-height: 26px; font-size: 12px; display: inline-flex; align-items: center; gap: 5px; }

.numeri { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; margin: 0; }
.numeri div { padding: 8px 10px; border-radius: 8px; border: 1px solid var(--border-soft); }
.numeri dt { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; }
.numeri dd { margin: 2px 0 0; font-size: 14px; font-variant-numeric: tabular-nums; }
.ultimo-errore { margin: 0; font-size: 12px; color: var(--muted); }
.ultimo-errore code { display: block; font-size: 11.5px; white-space: pre-wrap; overflow-wrap: anywhere; color: var(--text); }
.dove { margin: 0; color: var(--muted); }
.dove code { color: var(--text); overflow-wrap: anywhere; }
.avviso { margin: 0; display: flex; gap: 6px; align-items: flex-start; font-size: 12.5px; color: var(--warning); line-height: 1.45; }

.campi { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px 14px; }
.campo { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); min-width: 0; }
.campo small { font-size: 11.5px; line-height: 1.45; }
.campo input[type='text'] { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.preset { display: flex; flex-wrap: wrap; gap: 6px; }
.chip { padding: 2px 9px; min-height: 24px; font-size: 11.5px; border-radius: 999px; }
.link { border: 0; background: none; padding: 0; min-height: 0; font-size: 11.5px; color: var(--accent); cursor: pointer; text-decoration: underline; }
.spunta { display: inline-flex; align-items: center; gap: 8px; font-size: 13px; flex-wrap: wrap; }
.azioni { display: flex; gap: 8px; flex-wrap: wrap; }

.elenco { width: 100%; border-collapse: collapse; font-size: 12.5px; }
.elenco th { text-align: left; font-weight: 500; font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; padding: 4px 8px; border-bottom: 1px solid var(--border); }
.elenco td { padding: 6px 8px; border-bottom: 1px solid var(--border-soft); }
.elenco td code { font-size: 11.5px; overflow-wrap: anywhere; }
.elenco .num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
.scarica { display: inline-flex; align-items: center; gap: 4px; }

@media (max-width: 720px) {
  .campi, .numeri { grid-template-columns: 1fr; }
  .elenco th:nth-child(2), .elenco td:nth-child(2) { display: none; }
}
</style>
