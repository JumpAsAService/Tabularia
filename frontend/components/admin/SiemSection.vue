<script setup lang="ts">
// SIEM (pannello Admin): il registro di audit inoltrato a Splunk (HTTP Event
// Collector). In cima lo stato (acceso, in pari o indietro, l'ultimo errore), sotto
// la destinazione. Il token si scrive e non si rilegge. L'osservatore vede lo
// stato senza indirizzo completo né errori di Splunk, e non tocca niente.
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { CheckCircle2, LoaderCircle, Power, Radar, Send, TriangleAlert, XCircle } from 'lucide-vue-next'
import { errMessage } from '~/composables/useApi'
import { useFormat } from '~/composables/useFormat'
import { useSiem, type SiemState, type SiemTest } from '~/composables/useSiem'

const emit = defineEmits<{ (e: 'state', acceso: boolean): void }>()

const { t } = useI18n()
const fmt = useFormat()
const api = useSiem()
const toast = useToast()

const stato = ref<SiemState | null>(null)
const errore = ref('')
const salvando = ref(false)
const provando = ref(false)
const prova = ref<SiemTest | null>(null)
// il modulo: quello che c'è scritto, separato da quello salvato
const modulo = ref({ url: '', token: '', index: '', sourcetype: '', source: '', host: '', verify_tls: true })

function riempi(s: SiemState) {
  modulo.value = {
    url: s.url, token: '', index: s.index ?? '', sourcetype: s.sourcetype ?? '', source: s.source ?? '',
    host: s.host ?? '', verify_tls: s.verify_tls,
  }
}

async function carica(silenzioso = false) {
  try {
    const s = await api.get()
    // il modulo si riempie solo la prima volta: l'aggiornamento periodico dello
    // stato non deve cancellare quello che si sta scrivendo
    if (!stato.value) riempi(s)
    stato.value = s
    errore.value = ''
    emit('state', s.enabled)
  } catch (e) {
    if (!silenzioso) errore.value = errMessage(e)
  }
}

// lo stato cambia da solo (le voci partono ogni pochi secondi): si rilegge
let timer: ReturnType<typeof setInterval> | undefined
onMounted(() => {
  carica()
  timer = setInterval(() => carica(true), 10_000)
})
onBeforeUnmount(() => clearInterval(timer))

const modificabile = computed(() => !!stato.value?.editable)

// cosa è cambiato rispetto al salvato
const cambi = computed(() => {
  const s = stato.value
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
const daSalvare = computed(() => Object.keys(cambi.value).length > 0)

async function salva(extra: Record<string, unknown> = {}) {
  salvando.value = true
  try {
    const s = await api.save({ ...cambi.value, ...extra })
    stato.value = s
    riempi(s)
    emit('state', s.enabled)
    toast.success(t('siem.saved'))
  } catch (e) {
    toast.error(errMessage(e))
  } finally {
    salvando.value = false
  }
}

function accendi(acceso: boolean) {
  if (!acceso && !confirm(t('siem.confirmOff'))) return
  salva({ enabled: acceso })
}

function togliToken() {
  if (!confirm(t('siem.confirmRemoveToken'))) return
  salva({ token: '', enabled: false })
}

async function invia() {
  provando.value = true
  prova.value = null
  try {
    if (daSalvare.value) await salva()
    prova.value = await api.test()
  } catch (e) {
    prova.value = { ok: false, status_code: null, message: errMessage(e) }
  } finally {
    provando.value = false
  }
}

// lo stato in una frase, con il suo colore
const situazione = computed<{ tono: 'off' | 'ok' | 'errore' | 'attesa'; testo: string }>(() => {
  const s = stato.value
  if (!s || !s.enabled) return { tono: 'off', testo: t('siem.stateOff') }
  if (s.failures > 0) {
    return {
      tono: 'errore',
      testo: t('siem.stateFailing', { n: s.failures, when: fmt.ago(s.last_error_at), next: fmt.ago(s.next_attempt_at) }),
    }
  }
  if (s.backlog > 0 && s.oldest_pending_at && Date.now() - new Date(s.oldest_pending_at).getTime() > 60_000) {
    return { tono: 'attesa', testo: t('siem.stateBehind', { n: fmt.count(s.backlog), when: fmt.ago(s.oldest_pending_at) }) }
  }
  return { tono: 'ok', testo: s.last_ok_at ? t('siem.stateOk', { when: fmt.ago(s.last_ok_at) }) : t('siem.stateWaiting') }
})
</script>

<template>
  <div class="card">
    <h4><Radar :size="14" /> {{ $t('siem.title') }}</h4>
    <p class="hint muted small">{{ $t('siem.lead') }}</p>
    <p v-if="errore" class="errore-pagina" role="alert">{{ errore }}</p>

    <template v-if="stato">
      <div class="stato" :class="situazione.tono" role="status">
        <component :is="situazione.tono === 'ok' ? CheckCircle2 : situazione.tono === 'off' ? Power : situazione.tono === 'errore' ? XCircle : TriangleAlert" :size="15" />
        <span>{{ situazione.testo }}</span>
        <button v-if="modificabile && stato.enabled" type="button" class="mini" :disabled="salvando" @click="accendi(false)">
          {{ $t('siem.turnOff') }}
        </button>
        <button
          v-else-if="modificabile"
          type="button"
          class="primary mini"
          :disabled="salvando || !stato.url || !stato.token_set || daSalvare"
          :title="!stato.url || !stato.token_set ? $t('siem.needsUrlToken') : daSalvare ? $t('siem.saveFirst') : ''"
          @click="accendi(true)"
        >
          {{ $t('siem.turnOn') }}
        </button>
      </div>
      <dl class="numeri">
        <div><dt>{{ $t('siem.sent') }}</dt><dd>{{ fmt.count(stato.sent_total) }}</dd></div>
        <div><dt>{{ $t('siem.pending') }}</dt><dd>{{ fmt.count(stato.backlog) }}</dd></div>
        <div><dt>{{ $t('siem.lastDelivery') }}</dt><dd>{{ stato.last_ok_at ? fmt.ago(stato.last_ok_at) : $t('siem.never') }}</dd></div>
      </dl>
      <!-- l'errore in evidenza solo mentre i tentativi falliscono; risolto, resta una riga discreta -->
      <p v-if="stato.last_error && stato.failures > 0" class="ultimo-errore">
        {{ $t('siem.lastError', { when: fmt.ago(stato.last_error_at) }) }} <code>{{ stato.last_error }}</code>
      </p>
      <p v-else-if="stato.last_error_at" class="ultimo-errore" :title="stato.last_error ?? undefined">
        {{ $t('siem.lastErrorResolved', { when: fmt.ago(stato.last_error_at) }) }}
      </p>
    </template>
  </div>

  <form v-if="stato" class="card" @submit.prevent="salva()">
    <h4>{{ $t('siem.destinationTitle') }}</h4>
    <p class="hint muted small">{{ $t('siem.destinationLead') }}</p>

    <div class="campi">
      <label class="campo largo">
        <span>{{ $t('siem.url') }}</span>
        <input v-model="modulo.url" type="url" autocomplete="off" placeholder="https://splunk.example.com:8088" :disabled="!modificabile || salvando" />
        <small>{{ $t('siem.urlHint') }}</small>
      </label>
      <label class="campo largo">
        <span>{{ $t('siem.token') }}</span>
        <input
          v-model="modulo.token"
          type="password"
          autocomplete="new-password"
          :placeholder="stato.token_set ? $t('siem.tokenSet') : 'xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx'"
          :disabled="!modificabile || salvando"
        />
        <small>
          {{ stato.token_set ? $t('siem.tokenSetHint') : $t('siem.tokenHint') }}
          <button v-if="modificabile && stato.token_set" type="button" class="link" @click="togliToken">{{ $t('siem.removeToken') }}</button>
        </small>
      </label>
      <label class="campo">
        <span>{{ $t('siem.index') }}</span>
        <input v-model="modulo.index" type="text" :placeholder="$t('siem.indexPlaceholder')" :disabled="!modificabile || salvando" />
      </label>
      <label class="campo">
        <span>{{ $t('siem.sourcetype') }}</span>
        <input v-model="modulo.sourcetype" type="text" placeholder="tabularia:audit" :disabled="!modificabile || salvando" />
      </label>
      <label class="campo">
        <span>{{ $t('siem.source') }}</span>
        <input v-model="modulo.source" type="text" placeholder="tabularia" :disabled="!modificabile || salvando" />
      </label>
      <label class="campo">
        <span>{{ $t('siem.host') }}</span>
        <input v-model="modulo.host" type="text" placeholder="tabularia" :disabled="!modificabile || salvando" />
      </label>
    </div>

    <label class="spunta">
      <input v-model="modulo.verify_tls" type="checkbox" :disabled="!modificabile || salvando" />
      <span>{{ $t('siem.verifyTls') }}</span>
    </label>
    <p v-if="!modulo.verify_tls" class="avviso" role="note"><TriangleAlert :size="13" /> {{ $t('siem.verifyTlsOff') }}</p>
    <p v-else class="hint muted small">{{ $t('siem.verifyTlsHint') }}</p>

    <div v-if="modificabile" class="azioni">
      <button type="submit" class="primary" :disabled="salvando || !daSalvare">
        <LoaderCircle v-if="salvando" :size="13" class="spin" /> {{ $t('siem.save') }}
      </button>
      <button type="button" :disabled="provando || salvando || !(modulo.url && (stato.token_set || modulo.token))" @click="invia">
        <LoaderCircle v-if="provando" :size="13" class="spin" /><Send v-else :size="13" /> {{ $t('siem.test') }}
      </button>
    </div>
    <p v-if="prova" class="esito" :class="prova.ok ? 'ok' : 'ko'" role="status">
      <component :is="prova.ok ? CheckCircle2 : XCircle" :size="14" />
      <span>
        {{ prova.ok ? $t('siem.testOk', { message: prova.message }) : $t('siem.testFailed', { code: prova.status_code ?? '—' }) }}
        <code v-if="!prova.ok">{{ prova.message }}</code>
      </span>
    </p>
    <p v-if="!modificabile" class="nota">{{ $t('siem.readOnly') }}</p>
  </form>
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
button.mini { padding: 2px 10px; min-height: 26px; font-size: 12px; }

.numeri { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; margin: 0; }
.numeri div { padding: 8px 10px; border-radius: 8px; border: 1px solid var(--border-soft); }
.numeri dt { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; }
.numeri dd { margin: 2px 0 0; font-size: 15px; font-variant-numeric: tabular-nums; }
.ultimo-errore { margin: 0; font-size: 12px; color: var(--muted); }
.ultimo-errore code, .esito code { display: block; margin-top: 4px; font-size: 11.5px; white-space: pre-wrap; overflow-wrap: anywhere; color: var(--text); }

.campi { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px 14px; }
.campo { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); min-width: 0; }
.campo.largo { grid-column: 1 / -1; }
.campo small { font-size: 11.5px; line-height: 1.45; }
.link { border: 0; background: none; padding: 0; min-height: 0; font-size: 11.5px; color: var(--danger); cursor: pointer; text-decoration: underline; }
.spunta { display: inline-flex; align-items: center; gap: 8px; font-size: 13px; }
.avviso { margin: 0; display: flex; gap: 6px; align-items: flex-start; font-size: 12.5px; color: var(--danger); }
.azioni { display: flex; gap: 8px; flex-wrap: wrap; }
.esito { margin: 0; display: flex; gap: 8px; align-items: flex-start; font-size: 13px; }
.esito.ok svg { color: var(--accent-2); }
.esito.ko svg { color: var(--danger); }

@media (max-width: 720px) {
  .campi, .numeri { grid-template-columns: 1fr; }
}
</style>
