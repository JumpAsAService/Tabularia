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

const state = ref<BackupState | null>(null)
const error = ref('')
const saving = ref(false)
const starting = ref(false)
const draft = ref({ enabled: true, schedule: '', keep: 14 })

const PRESETS = [
  { cron: '0 3 * * *', key: 'presetNightly' },
  { cron: '0 */6 * * *', key: 'presetSixHours' },
  { cron: '0 2 * * 0', key: 'presetWeekly' },
] as const

function fill(s: BackupState) {
  draft.value = { enabled: s.enabled, schedule: s.schedule, keep: s.keep }
}

async function load(quiet = false) {
  try {
    const s = await api.get()
    // the form is filled the first time only: a refresh must not undo what is being typed
    if (!state.value) fill(s)
    state.value = s
    error.value = ''
    emit('count', s.items.length)
  } catch (e) {
    if (!quiet) error.value = errMessage(e)
  }
}

// while a backup runs the page follows it closely; otherwise now and then
let timer: ReturnType<typeof setTimeout> | undefined
function next() {
  const s = state.value
  const alive = !!(s && (s.running_since || s.requested_at))
  timer = setTimeout(async () => {
    await load(true)
    next()
  }, alive ? 3000 : 30_000)
}
onMounted(async () => {
  await load()
  next()
})
onBeforeUnmount(() => clearTimeout(timer))

const editable = computed(() => !!state.value?.editable)

const changes = computed<BackupUpdate>(() => {
  const s = state.value
  if (!s) return {}
  const m = draft.value
  const out: BackupUpdate = {}
  if (m.enabled !== s.enabled) out.enabled = m.enabled
  if (m.schedule.trim() !== s.schedule) out.schedule = m.schedule.trim()
  if (Number(m.keep) !== s.keep) out.keep = Number(m.keep)
  return out
})
const toSave = computed(() => Object.keys(changes.value).length > 0)

async function save(body: BackupUpdate = changes.value) {
  saving.value = true
  try {
    const s = await api.save(body)
    state.value = s
    fill(s)
    toast.success(t('backups.saved'))
  } catch (e) {
    toast.error(errMessage(e))
  } finally {
    saving.value = false
  }
}

async function now() {
  starting.value = true
  try {
    state.value = await api.runNow()
    clearTimeout(timer)
    next()
  } catch (e) {
    toast.error(errMessage(e))
  } finally {
    starting.value = false
  }
}

function bytes(n: number | null | undefined): string {
  if (n == null) return '—'
  if (n < 1024 ** 2) return `${fmt.number(n / 1024, 1)} KiB`
  return n >= 1024 ** 3 ? `${fmt.number(n / 1024 ** 3, 1)} GiB` : `${fmt.number(n / 1024 ** 2, 1)} MiB`
}

// the moment a backup was made, from its name (tabularia-2026-10-22T03-00-00Z.dump)
function when(name: string): string {
  const m = name.match(/^tabularia-(\d{4}-\d{2}-\d{2})T(\d{2})-(\d{2})-(\d{2})Z\.dump$/)
  return m ? fmt.dateTime(`${m[1]}T${m[2]}:${m[3]}:${m[4]}Z`) : name
}

const failed = computed(() => {
  const s = state.value
  if (!s?.last_error_at) return false
  return !s.last_ok_at || new Date(s.last_error_at) > new Date(s.last_ok_at)
})

const situation = computed<{ tone: 'off' | 'ok' | 'error' | 'waiting'; text: string }>(() => {
  const s = state.value
  if (!s) return { tone: 'off', text: '' }
  if (s.running_since) return { tone: 'waiting', text: t('backups.stateRunning', { when: fmt.ago(s.running_since) }) }
  if (s.requested_at) return { tone: 'waiting', text: t('backups.stateRequested') }
  if (failed.value) return { tone: 'error', text: t('backups.stateFailed', { when: fmt.ago(s.last_error_at) }) }
  if (!s.enabled) return { tone: 'off', text: s.last_ok_at ? t('backups.stateOffLast', { when: fmt.ago(s.last_ok_at) }) : t('backups.stateOff') }
  if (s.last_ok_at) return { tone: 'ok', text: t('backups.stateOk', { when: fmt.ago(s.last_ok_at), size: bytes(s.last_size) }) }
  return { tone: 'waiting', text: t('backups.stateNone') }
})
</script>

<template>
  <div class="card">
    <h4><DatabaseBackup :size="14" /> {{ $t('backups.title') }}</h4>
    <p class="hint muted small">{{ $t('backups.lead') }}</p>
    <p v-if="error" class="page-error" role="alert">{{ error }}</p>

    <template v-if="state">
      <p v-if="!state.available" class="note" role="note">
        {{ state.disabled_reason === 'kubernetes' ? $t('backups.kubernetesOnly') : state.disabled_reason === 'postgres' ? $t('backups.notPostgres') : $t('backups.noPgDump') }}
      </p>
      <template v-else>
        <div class="state" :class="situation.tone" role="status">
          <LoaderCircle v-if="state.running_since || state.requested_at" :size="15" class="spin" />
          <component :is="situation.tone === 'ok' ? CheckCircle2 : situation.tone === 'off' ? Power : situation.tone === 'error' ? XCircle : TriangleAlert" v-else :size="15" />
          <span>{{ situation.text }}</span>
          <button
            v-if="editable"
            type="button"
            class="primary mini"
            :disabled="starting || !!state.running_since || !!state.requested_at"
            @click="now"
          >
            <Play :size="12" /> {{ $t('backups.runNow') }}
          </button>
        </div>
        <p v-if="failed && state.last_error" class="last-error"><code>{{ state.last_error }}</code></p>
        <dl class="numbers">
          <div><dt>{{ $t('backups.next') }}</dt><dd>{{ state.enabled && state.next_at ? fmt.dateTime(state.next_at) : '—' }}</dd></div>
          <div><dt>{{ $t('backups.last') }}</dt><dd>{{ state.last_ok_at ? fmt.dateTime(state.last_ok_at) : $t('backups.never') }}</dd></div>
          <div><dt>{{ $t('backups.kept') }}</dt><dd>{{ $t('backups.keptOf', { n: state.items.length, keep: state.keep }) }}</dd></div>
        </dl>
        <p v-if="state.bucket" class="dove small">
          {{ $t('backups.where') }} <code>{{ state.bucket }}/{{ state.prefix }}</code>
        </p>
        <p v-if="state.same_as_data" class="notice" role="note">
          <TriangleAlert :size="13" /> {{ $t('backups.sameBucket') }}
        </p>
        <p v-if="state.storage_error" class="page-error">{{ state.storage_error }}</p>
      </template>
    </template>
  </div>

  <form v-if="state?.available" class="card" @submit.prevent="save()">
    <h4>{{ $t('backups.settingsTitle') }}</h4>
    <p class="hint muted small">{{ $t('backups.settingsLead') }}</p>

    <label class="tick">
      <input v-model="draft.enabled" type="checkbox" :disabled="!editable || saving" />
      <span>{{ $t('backups.enabled') }}</span>
      <button v-if="editable && state.changed.enabled" type="button" class="link" @click="save({ reset: ['enabled'] })">
        {{ $t('backups.reset', { value: state.defaults.enabled ? $t('backups.on') : $t('backups.off') }) }}
      </button>
    </label>

    <div class="fields">
      <label class="field">
        <span>{{ $t('backups.schedule') }}</span>
        <input v-model="draft.schedule" type="text" spellcheck="false" autocomplete="off" placeholder="0 3 * * *" :disabled="!editable || saving" />
        <small>
          {{ $t('backups.scheduleHint', { tz: state.timezone }) }}
          <button v-if="editable && state.changed.schedule" type="button" class="link" @click="save({ reset: ['schedule'] })">
            {{ $t('backups.reset', { value: state.defaults.schedule }) }}
          </button>
        </small>
        <span v-if="editable" class="preset">
          <button v-for="p in PRESETS" :key="p.cron" type="button" class="chip" :disabled="saving" @click="draft.schedule = p.cron">
            {{ $t(`backups.${p.key}`) }}
          </button>
        </span>
      </label>
      <label class="field">
        <span>{{ $t('backups.keep') }}</span>
        <input v-model.number="draft.keep" type="number" min="1" max="1000" :disabled="!editable || saving" />
        <small>
          {{ $t('backups.keepHint') }}
          <button v-if="editable && state.changed.keep" type="button" class="link" @click="save({ reset: ['keep'] })">
            {{ $t('backups.reset', { value: state.defaults.keep }) }}
          </button>
        </small>
      </label>
    </div>

    <div v-if="editable" class="actions">
      <button type="submit" class="primary" :disabled="saving || !toSave">
        <LoaderCircle v-if="saving" :size="13" class="spin" /> {{ $t('backups.save') }}
      </button>
    </div>
    <p v-else class="note">{{ $t('backups.readOnly') }}</p>
  </form>

  <div v-if="state?.available" class="card">
    <h4>{{ $t('backups.listTitle') }}</h4>
    <p class="hint muted small">{{ $t('backups.listLead') }}</p>
    <p v-if="!state.items.length" class="muted small">{{ $t('backups.listEmpty') }}</p>
    <table v-else class="listing">
      <thead>
        <tr><th>{{ $t('backups.colWhen') }}</th><th>{{ $t('backups.colName') }}</th><th class="num">{{ $t('backups.colSize') }}</th><th v-if="editable" /></tr>
      </thead>
      <tbody>
        <tr v-for="b in state.items" :key="b.name">
          <td>{{ when(b.name) }}</td>
          <td><code>{{ b.name }}</code></td>
          <td class="num">{{ bytes(b.size) }}</td>
          <td v-if="editable" class="num">
            <a class="download" :href="api.downloadUrl(b.name)" :download="b.name"><Download :size="13" /> {{ $t('backups.download') }}</a>
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
.page-error { color: var(--danger); font-size: 13px; margin: 0; }
.note { margin: 0; font-size: 12.5px; padding: 8px 10px; border-radius: 8px; background: var(--bg-soft); border: 1px solid var(--border); }

.state { display: flex; align-items: center; gap: 8px; padding: 10px 12px; border-radius: 8px; border: 1px solid var(--border); background: var(--bg-soft); font-size: 13px; }
.state span { flex: 1; }
.state.ok { border-color: color-mix(in srgb, var(--accent-2) 45%, var(--border)); }
.state.ok svg { color: var(--accent-2); }
.state.error { border-color: color-mix(in srgb, var(--danger) 50%, var(--border)); }
.state.error svg { color: var(--danger); }
.state.waiting svg { color: var(--warning); }
.state.off svg { color: var(--muted); }
button.mini { padding: 2px 10px; min-height: 26px; font-size: 12px; display: inline-flex; align-items: center; gap: 5px; }

.numbers { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; margin: 0; }
.numbers div { padding: 8px 10px; border-radius: 8px; border: 1px solid var(--border-soft); }
.numbers dt { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; }
.numbers dd { margin: 2px 0 0; font-size: 14px; font-variant-numeric: tabular-nums; }
.last-error { margin: 0; font-size: 12px; color: var(--muted); }
.last-error code { display: block; font-size: 11.5px; white-space: pre-wrap; overflow-wrap: anywhere; color: var(--text); }
.dove { margin: 0; color: var(--muted); }
.dove code { color: var(--text); overflow-wrap: anywhere; }
.notice { margin: 0; display: flex; gap: 6px; align-items: flex-start; font-size: 12.5px; color: var(--warning); line-height: 1.45; }

.fields { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px 14px; }
.field { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); min-width: 0; }
.field small { font-size: 11.5px; line-height: 1.45; }
.field input[type='text'] { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.preset { display: flex; flex-wrap: wrap; gap: 6px; }
.chip { padding: 2px 9px; min-height: 24px; font-size: 11.5px; border-radius: 999px; }
.link { border: 0; background: none; padding: 0; min-height: 0; font-size: 11.5px; color: var(--accent); cursor: pointer; text-decoration: underline; }
.tick { display: inline-flex; align-items: center; gap: 8px; font-size: 13px; flex-wrap: wrap; }
.actions { display: flex; gap: 8px; flex-wrap: wrap; }

.listing { width: 100%; border-collapse: collapse; font-size: 12.5px; }
.listing th { text-align: left; font-weight: 500; font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; padding: 4px 8px; border-bottom: 1px solid var(--border); }
.listing td { padding: 6px 8px; border-bottom: 1px solid var(--border-soft); }
.listing td code { font-size: 11.5px; overflow-wrap: anywhere; }
.listing .num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
.download { display: inline-flex; align-items: center; gap: 4px; }

@media (max-width: 720px) {
  .fields, .numbers { grid-template-columns: 1fr; }
  .listing th:nth-child(2), .listing td:nth-child(2) { display: none; }
}
</style>
