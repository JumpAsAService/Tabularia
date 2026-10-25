<script setup lang="ts">
// SIEM (Admin panel): the audit log forwarded to Splunk (HTTP Event Collector). At the
// top the state (on, caught up or behind, the last error), below the destination. The
// token is written and not read back. The observer sees the state without the full
// address nor Splunk's errors, and touches nothing.
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { CheckCircle2, LoaderCircle, Power, Radar, Send, TriangleAlert, XCircle } from 'lucide-vue-next'
import { errMessage } from '~/composables/useApi'
import { useFormat } from '~/composables/useFormat'
import { useSiem, type SiemState, type SiemTest } from '~/composables/useSiem'

const emit = defineEmits<{ (e: 'state', isOn: boolean): void }>()

const { t } = useI18n()
const fmt = useFormat()
const api = useSiem()
const toast = useToast()

const state = ref<SiemState | null>(null)
const error = ref('')
const saving = ref(false)
const testing = ref(false)
const test = ref<SiemTest | null>(null)
// the form: what is written, apart from what is saved
const modulo = ref({ url: '', token: '', index: '', sourcetype: '', source: '', host: '', verify_tls: true })

function fill(s: SiemState) {
  modulo.value = {
    url: s.url, token: '', index: s.index ?? '', sourcetype: s.sourcetype ?? '', source: s.source ?? '',
    host: s.host ?? '', verify_tls: s.verify_tls,
  }
}

async function load(quiet = false) {
  try {
    const s = await api.get()
    // the form fills only the first time: the state's periodic update must not delete what
    // is being written
    if (!state.value) fill(s)
    state.value = s
    error.value = ''
    emit('state', s.enabled)
  } catch (e) {
    if (!quiet) error.value = errMessage(e)
  }
}

// the state changes by itself (the items go out every few seconds): it is read again
let timer: ReturnType<typeof setInterval> | undefined
onMounted(() => {
  load()
  timer = setInterval(() => load(true), 10_000)
})
onBeforeUnmount(() => clearInterval(timer))

const modificabile = computed(() => !!state.value?.editable)

// what changed compared with the saved one
const changes = computed(() => {
  const s = state.value
  if (!s) return {}
  const m = modulo.value
  const out: Record<string, string | boolean> = {}
  if (m.url.trim() !== s.url) out.url = m.url.trim()
  if (m.token.trim()) out.token = m.token.trim()
  for (const k of ['index', 'sourcetype', 'source', 'host'] as const) {
    if (m[k].trim() !== (s[k] ?? '')) out[k] = m[k].trim()
  }
  if (m.verify_tls !== s.verify_tls) out.verify_tls = m.verify_tls
  return out
})
const toSave = computed(() => Object.keys(changes.value).length > 0)

async function save(extra: Record<string, unknown> = {}) {
  saving.value = true
  try {
    const s = await api.save({ ...changes.value, ...extra })
    state.value = s
    fill(s)
    emit('state', s.enabled)
    toast.success(t('siem.saved'))
  } catch (e) {
    toast.error(errMessage(e))
  } finally {
    saving.value = false
  }
}

function turnOn(isOn: boolean) {
  if (!isOn && !confirm(t('siem.confirmOff'))) return
  save({ enabled: isOn })
}

function removeToken() {
  if (!confirm(t('siem.confirmRemoveToken'))) return
  save({ token: '', enabled: false })
}

async function send() {
  testing.value = true
  test.value = null
  try {
    if (toSave.value) await save()
    test.value = await api.test()
  } catch (e) {
    test.value = { ok: false, status_code: null, message: errMessage(e) }
  } finally {
    testing.value = false
  }
}

// the state in one sentence, with its colour
const situation = computed<{ tone: 'off' | 'ok' | 'error' | 'waiting'; text: string }>(() => {
  const s = state.value
  if (!s || !s.enabled) return { tone: 'off', text: t('siem.stateOff') }
  if (s.failures > 0) {
    return {
      tone: 'error',
      text: t('siem.stateFailing', { n: s.failures, when: fmt.ago(s.last_error_at), next: fmt.ago(s.next_attempt_at) }),
    }
  }
  if (s.backlog > 0 && s.oldest_pending_at && Date.now() - new Date(s.oldest_pending_at).getTime() > 60_000) {
    return { tone: 'waiting', text: t('siem.stateBehind', { n: fmt.count(s.backlog), when: fmt.ago(s.oldest_pending_at) }) }
  }
  return { tone: 'ok', text: s.last_ok_at ? t('siem.stateOk', { when: fmt.ago(s.last_ok_at) }) : t('siem.stateWaiting') }
})
</script>

<template>
  <div class="card">
    <h4><Radar :size="14" /> {{ $t('siem.title') }}</h4>
    <p class="hint muted small">{{ $t('siem.lead') }}</p>
    <p v-if="error" class="page-error" role="alert">{{ error }}</p>

    <template v-if="state">
      <div class="state" :class="situation.tone" role="status">
        <component :is="situation.tone === 'ok' ? CheckCircle2 : situation.tone === 'off' ? Power : situation.tone === 'error' ? XCircle : TriangleAlert" :size="15" />
        <span>{{ situation.text }}</span>
        <button v-if="modificabile && state.enabled" type="button" class="mini" :disabled="saving" @click="turnOn(false)">
          {{ $t('siem.turnOff') }}
        </button>
        <button
          v-else-if="modificabile"
          type="button"
          class="primary mini"
          :disabled="saving || !state.url || !state.token_set || toSave"
          :title="!state.url || !state.token_set ? $t('siem.needsUrlToken') : toSave ? $t('siem.saveFirst') : ''"
          @click="turnOn(true)"
        >
          {{ $t('siem.turnOn') }}
        </button>
      </div>
      <dl class="numbers">
        <div><dt>{{ $t('siem.sent') }}</dt><dd>{{ fmt.count(state.sent_total) }}</dd></div>
        <div><dt>{{ $t('siem.pending') }}</dt><dd>{{ fmt.count(state.backlog) }}</dd></div>
        <div><dt>{{ $t('siem.lastDelivery') }}</dt><dd>{{ state.last_ok_at ? fmt.ago(state.last_ok_at) : $t('siem.never') }}</dd></div>
      </dl>
      <!-- the error in evidence only while the attempts fail; solved, it stays a discreet line -->
      <p v-if="state.last_error && state.failures > 0" class="last-error">
        {{ $t('siem.lastError', { when: fmt.ago(state.last_error_at) }) }} <code>{{ state.last_error }}</code>
      </p>
      <p v-else-if="state.last_error_at" class="last-error" :title="state.last_error ?? undefined">
        {{ $t('siem.lastErrorResolved', { when: fmt.ago(state.last_error_at) }) }}
      </p>
    </template>
  </div>

  <form v-if="state" class="card" @submit.prevent="save()">
    <h4>{{ $t('siem.destinationTitle') }}</h4>
    <p class="hint muted small">{{ $t('siem.destinationLead') }}</p>

    <div class="fields">
      <label class="field largo">
        <span>{{ $t('siem.url') }}</span>
        <input v-model="modulo.url" type="url" autocomplete="off" placeholder="https://splunk.example.com:8088" :disabled="!modificabile || saving" />
        <small>{{ $t('siem.urlHint') }}</small>
      </label>
      <label class="field largo">
        <span>{{ $t('siem.token') }}</span>
        <input
          v-model="modulo.token"
          type="password"
          autocomplete="new-password"
          :placeholder="state.token_set ? $t('siem.tokenSet') : 'xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx'"
          :disabled="!modificabile || saving"
        />
        <small>
          {{ state.token_set ? $t('siem.tokenSetHint') : $t('siem.tokenHint') }}
          <button v-if="modificabile && state.token_set" type="button" class="link" @click="removeToken">{{ $t('siem.removeToken') }}</button>
        </small>
      </label>
      <label class="field">
        <span>{{ $t('siem.index') }}</span>
        <input v-model="modulo.index" type="text" :placeholder="$t('siem.indexPlaceholder')" :disabled="!modificabile || saving" />
      </label>
      <label class="field">
        <span>{{ $t('siem.sourcetype') }}</span>
        <input v-model="modulo.sourcetype" type="text" placeholder="tabularia:audit" :disabled="!modificabile || saving" />
      </label>
      <label class="field">
        <span>{{ $t('siem.source') }}</span>
        <input v-model="modulo.source" type="text" placeholder="tabularia" :disabled="!modificabile || saving" />
      </label>
      <label class="field">
        <span>{{ $t('siem.host') }}</span>
        <input v-model="modulo.host" type="text" placeholder="tabularia" :disabled="!modificabile || saving" />
      </label>
    </div>

    <label class="tick">
      <input v-model="modulo.verify_tls" type="checkbox" :disabled="!modificabile || saving" />
      <span>{{ $t('siem.verifyTls') }}</span>
    </label>
    <p v-if="!modulo.verify_tls" class="notice" role="note"><TriangleAlert :size="13" /> {{ $t('siem.verifyTlsOff') }}</p>
    <p v-else class="hint muted small">{{ $t('siem.verifyTlsHint') }}</p>

    <div v-if="modificabile" class="actions">
      <button type="submit" class="primary" :disabled="saving || !toSave">
        <LoaderCircle v-if="saving" :size="13" class="spin" /> {{ $t('siem.save') }}
      </button>
      <button type="button" :disabled="testing || saving || !(modulo.url && (state.token_set || modulo.token))" @click="send">
        <LoaderCircle v-if="testing" :size="13" class="spin" /><Send v-else :size="13" /> {{ $t('siem.test') }}
      </button>
    </div>
    <p v-if="test" class="outcome" :class="test.ok ? 'ok' : 'ko'" role="status">
      <component :is="test.ok ? CheckCircle2 : XCircle" :size="14" />
      <span>
        {{ test.ok ? $t('siem.testOk', { message: test.message }) : $t('siem.testFailed', { code: test.status_code ?? '—' }) }}
        <code v-if="!test.ok">{{ test.message }}</code>
      </span>
    </p>
    <p v-if="!modificabile" class="note">{{ $t('siem.readOnly') }}</p>
  </form>
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
button.mini { padding: 2px 10px; min-height: 26px; font-size: 12px; }

.numbers { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; margin: 0; }
.numbers div { padding: 8px 10px; border-radius: 8px; border: 1px solid var(--border-soft); }
.numbers dt { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; }
.numbers dd { margin: 2px 0 0; font-size: 15px; font-variant-numeric: tabular-nums; }
.last-error { margin: 0; font-size: 12px; color: var(--muted); }
.last-error code, .outcome code { display: block; margin-top: 4px; font-size: 11.5px; white-space: pre-wrap; overflow-wrap: anywhere; color: var(--text); }

.fields { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px 14px; }
.field { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); min-width: 0; }
.field.largo { grid-column: 1 / -1; }
.field small { font-size: 11.5px; line-height: 1.45; }
.link { border: 0; background: none; padding: 0; min-height: 0; font-size: 11.5px; color: var(--danger); cursor: pointer; text-decoration: underline; }
.tick { display: inline-flex; align-items: center; gap: 8px; font-size: 13px; }
.notice { margin: 0; display: flex; gap: 6px; align-items: flex-start; font-size: 12.5px; color: var(--danger); }
.actions { display: flex; gap: 8px; flex-wrap: wrap; }
.outcome { margin: 0; display: flex; gap: 8px; align-items: flex-start; font-size: 13px; }
.outcome.ok svg { color: var(--accent-2); }
.outcome.ko svg { color: var(--danger); }

@media (max-width: 720px) {
  .fields, .numbers { grid-template-columns: 1fr; }
}
</style>
