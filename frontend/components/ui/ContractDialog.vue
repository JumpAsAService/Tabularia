<script setup lang="ts">
// Il data contract di una datasource: scriverlo, vederne lo stato, capire quale
// regola non regge e perché.
//
// Le regole sono raggruppate per colonna (più un gruppo per quelle che
// riguardano la tabella intera), e ogni regola porta accanto il suo ULTIMO
// esito: il referto non è una tabella a parte da incrociare a mano.
import { computed, ref, watch } from 'vue'
import { Bell, CircleCheck, CircleX, CircleAlert, CircleDashed, Download, History, LoaderCircle, RefreshCw, Sparkles, Trash2 } from 'lucide-vue-next'
import { useI18n } from 'vue-i18n'
import Modal from '~/components/ui/Modal.vue'
import ContractBadge from '~/components/ui/ContractBadge.vue'
import { errMessage } from '~/composables/useApi'
import { useConnections, type ConnectionInfo } from '~/composables/useConnections'
import type { DatasourceInfo } from '~/composables/useDatasources'
import {
  COLUMN_KINDS, DTYPES, contractProblems, ruleColumn, useContracts,
  type ContractDocument, type ContractHistoryEntry, type ContractInfo, type ContractRule,
  type Dtype, type RuleKind, type RuleResult, type Severity,
} from '~/composables/useContracts'

const props = defineProps<{ open: boolean; datasource: DatasourceInfo | null }>()
const emit = defineEmits<{ (e: 'close'): void; (e: 'changed'): void }>()

const { t, locale } = useI18n()
const api = useContracts()
const toast = useToast()

// ── la regola com'è nel modulo: tutti i campi presenti, i numeri ancora testo ──
interface Riga {
  uid: number
  id?: string
  kind: RuleKind
  severity: Severity
  column: string
  columns: string[]
  dtype: Dtype | ''
  values: string
  min: string
  max: string
  regex: string
  hours: string
  sql: string
  name: string
}
let prossimoUid = 1
const TABELLA = '' // la chiave del gruppo delle regole sulla tabella intera

const info = ref<ContractInfo | null>(null)
const righe = ref<Riga[]>([])
const enabled = ref(true)
const salvato = ref('') // il documento com'è sul server: per sapere se ci sono modifiche
const loading = ref(false)
const occupato = ref<'' | 'save' | 'check' | 'propose' | 'remove'>('')
const problemi = ref<Record<number, string>>({}) // uid → codice dell'errore di forma
const storico = ref<ContractHistoryEntry[] | null>(null)
// chi avvisare quando lo stato cambia: indirizzi e connessione SMTP con cui spedire
const avvisiA = ref('')
const avvisiCon = ref<number | null>(null)
const postali = ref<ConnectionInfo[]>([])

const colonne = computed(() => props.datasource?.columns ?? [])
// chi può solo leggere vede il contratto e il referto, non i comandi
const soloLettura = computed(() => info.value !== null && !info.value.editable)

/** La famiglia di un tipo fisico: ciò che il contratto promette (vedi l'engine). */
function famiglia(dtype: string | undefined): Dtype | 'other' {
  const d = dtype ?? ''
  if (/^U?Int/.test(d)) return 'integer'
  if (/^(Float|Decimal)/.test(d)) return 'number'
  if (d === 'Boolean') return 'boolean'
  if (d === 'Date') return 'date'
  if (d.startsWith('Datetime')) return 'datetime'
  if (/^(String|Utf8|Categorical|Enum)/.test(d)) return 'string'
  return 'other'
}
const famigliaDi = (colonna: string) => famiglia(colonne.value.find((c) => c.name === colonna)?.dtype)
const numerica = (colonna: string) => ['integer', 'number'].includes(famigliaDi(colonna))

function nuovaRiga(kind: RuleKind, colonna: string): Riga {
  return {
    uid: prossimoUid++, kind, severity: 'error', column: colonna,
    columns: kind === 'unique' && colonna ? [colonna] : [],
    dtype: kind === 'column' && famigliaDi(colonna) !== 'other' ? (famigliaDi(colonna) as Dtype) : '',
    values: '', min: '', max: '', regex: '', hours: '24', sql: '', name: '',
  }
}

function daRegola(r: ContractRule): Riga {
  const testo = (v: unknown) => (v === null || v === undefined ? '' : String(v))
  return {
    uid: prossimoUid++, id: r.id, kind: r.kind, severity: r.severity,
    column: r.column ?? '', columns: [...(r.columns ?? [])], dtype: r.dtype ?? '',
    values: (r.values ?? []).join(', '), min: testo(r.min), max: testo(r.max), regex: r.regex ?? '',
    hours: testo(r.max_age_hours ?? 24), sql: r.sql ?? '', name: r.name ?? '',
  }
}

function aRegola(g: Riga): ContractRule {
  const r: ContractRule = { kind: g.kind, severity: g.severity }
  if (g.id) r.id = g.id
  const numero = (s: string) => (s.trim() === '' ? undefined : Number(s))
  // un estremo di intervallo: numero sulle colonne numeriche, testo (una data) sulle altre
  const estremo = (s: string) => (s.trim() === '' ? undefined : numerica(g.column) && !Number.isNaN(Number(s)) ? Number(s) : s.trim())
  switch (g.kind) {
    case 'column':
      r.column = g.column
      if (g.dtype) r.dtype = g.dtype
      break
    case 'not_null':
      r.column = g.column
      break
    case 'unique':
      r.columns = g.columns
      break
    case 'accepted_values': {
      r.column = g.column
      const voci = g.values.split(/[,\n]/).map((v) => v.trim()).filter(Boolean)
      r.values = numerica(g.column) && voci.every((v) => !Number.isNaN(Number(v))) ? voci.map(Number) : voci
      break
    }
    case 'range':
      r.column = g.column
      r.min = estremo(g.min)
      r.max = estremo(g.max)
      break
    case 'pattern':
      r.column = g.column
      r.regex = g.regex
      break
    case 'row_count':
      r.min = numero(g.min)
      r.max = numero(g.max)
      break
    case 'freshness':
      r.max_age_hours = numero(g.hours)
      if (g.column) r.column = g.column
      break
    case 'expression':
      r.sql = g.sql
      if (g.name.trim()) r.name = g.name.trim()
      break
  }
  return r
}

const documento = computed<ContractDocument>(() => ({ description: info.value?.document.description ?? '', rules: righe.value.map(aRegola) }))
const impronta = computed(() => JSON.stringify([documento.value.rules.map(({ id: _id, ...r }) => r), enabled.value, avvisiA.value.trim(), avvisiCon.value]))
const modificato = computed(() => impronta.value !== salvato.value)

function mostra(c: ContractInfo | null) {
  info.value = c
  righe.value = (c?.document.rules ?? []).map(daRegola)
  enabled.value = c?.enabled ?? true
  avvisiA.value = c?.notify_emails ?? ''
  avvisiCon.value = c?.notify_connection_id ?? null
  problemi.value = {}
  salvato.value = impronta.value
}

async function carica() {
  if (!props.datasource) return
  loading.value = true
  storico.value = null
  // le connessioni SMTP fra cui scegliere: se non si caricano, il contratto si scrive lo stesso
  useConnections().list().then((c) => { postali.value = c.filter((x) => x.db_type === 'smtp') }).catch(() => { postali.value = [] })
  try {
    mostra(await api.get(props.datasource.id))
  } catch (e) {
    toast.error(errMessage(e))
    mostra(null)
  } finally {
    loading.value = false
  }
}
watch(() => [props.open, props.datasource?.id], ([aperto]) => { if (aperto) carica() }, { immediate: true })

// ── i gruppi: una colonna alla volta, poi la tabella ─────────────────────────
const gruppoDi = (g: Riga) => ruleColumn(aRegola(g)) ?? TABELLA
const gruppi = computed(() => {
  const noti = colonne.value.map((c) => c.name)
  // una regola può nominare una colonna che la datasource non ha più: resta visibile
  const orfani = [...new Set(righe.value.map(gruppoDi))].filter((n) => n !== TABELLA && !noti.includes(n))
  return [
    ...noti.map((nome) => ({ chiave: nome, nome, tipo: colonne.value.find((c) => c.name === nome)?.dtype ?? '', manca: false })),
    ...orfani.map((nome) => ({ chiave: nome, nome, tipo: '', manca: true })),
    { chiave: TABELLA, nome: t('contracts.tableGroup'), tipo: '', manca: false },
  ].map((g) => ({ ...g, righe: righe.value.filter((r) => gruppoDi(r) === g.chiave) }))
    // a chi non può aggiungere regole, un gruppo vuoto non dice niente
    .filter((g) => !soloLettura.value || g.righe.length)
})
const TIPI_TABELLA: RuleKind[] = ['unique', 'row_count', 'freshness', 'expression']
const tipiPer = (chiave: string): RuleKind[] => (chiave === TABELLA ? TIPI_TABELLA : [...COLUMN_KINDS, 'unique'])

function aggiungi(chiave: string, ev: Event) {
  const scelta = ev.target as HTMLSelectElement
  if (scelta.value) righe.value.push(nuovaRiga(scelta.value as RuleKind, chiave))
  scelta.value = ''
}
function togli(uid: number) {
  righe.value = righe.value.filter((r) => r.uid !== uid)
}
function alterna(g: Riga, colonna: string) {
  g.columns = g.columns.includes(colonna) ? g.columns.filter((c) => c !== colonna) : [...g.columns, colonna]
}

// ── gli esiti: quelli dell'ultimo referto, regola per regola ─────────────────
const esiti = computed(() => new Map((info.value?.report?.rules ?? []).map((r) => [r.id, r] as const)))
const esitoDi = (g: Riga): RuleResult | undefined => (g.id ? esiti.value.get(g.id) : undefined)
function comeEAndata(g: Riga): 'ok' | 'warning' | 'error' | 'none' {
  const e = esitoDi(g)
  if (!e || modificato.value) return 'none'
  return e.passed ? 'ok' : e.severity === 'warning' ? 'warning' : 'error'
}
function dettaglio(e: RuleResult): string {
  if (e.error) return t('contracts.result.error', { error: e.error })
  const parti: string[] = []
  if (e.violations) parti.push(t('contracts.result.violations', { n: e.violations.toLocaleString(locale.value) }))
  if (e.kind === 'row_count' && e.observed !== undefined) parti.push(t('contracts.result.rows', { n: Number(e.observed).toLocaleString(locale.value) }))
  if (e.kind === 'freshness' && e.observed != null) parti.push(t('contracts.result.age', { hours: e.observed }))
  if (e.kind === 'column') parti.push(e.observed ? t('contracts.result.isType', { type: t(`contracts.dtype.${e.observed}`, String(e.observed)) }) : t('contracts.result.missing'))
  if (e.sample?.length) parti.push(t('contracts.result.sample', { values: e.sample.map(String).join(', ') }))
  return parti.join(' · ')
}
const quando = (iso: string | null) => (iso ? new Date(iso).toLocaleString(locale.value, { dateStyle: 'medium', timeStyle: 'short' }) : '')

// ── azioni ───────────────────────────────────────────────────────────────────
async function salva() {
  if (!props.datasource) return
  occupato.value = 'save'
  problemi.value = {}
  const inviate = righe.value.map((r) => r.uid)
  try {
    mostra(await api.save(props.datasource.id, documento.value, enabled.value, { emails: avvisiA.value.trim(), connectionId: avvisiCon.value }))
    emit('changed')
    // il messaggio dice com'è andata la verifica, non solo che il salvataggio è riuscito
    const esito = info.value?.report?.outcome
    if (info.value?.check_error) toast.error(t('contracts.savedUnchecked', { error: info.value.check_error }))
    else if (esito && esito !== 'passed') toast.warning(t('contracts.savedNotRespected'))
    else toast.success(t(esito ? 'contracts.savedPassed' : 'contracts.saved'))
  } catch (e) {
    const difetti = contractProblems(e)
    if (difetti) {
      problemi.value = Object.fromEntries(difetti.filter((d) => d.index !== null).map((d) => [inviate[d.index as number], d.code]))
      toast.error(t(difetti[0]?.index === null ? `contracts.problem.${difetti[0].code}` : 'contracts.invalid'))
    } else {
      toast.error(errMessage(e))
    }
  } finally {
    occupato.value = ''
  }
}

async function verifica() {
  if (!props.datasource) return
  occupato.value = 'check'
  try {
    mostra(await api.check(props.datasource.id))
    emit('changed')
    if (info.value?.check_error) toast.error(info.value.check_error)
  } catch (e) {
    toast.error(errMessage(e))
  } finally {
    occupato.value = ''
  }
}

async function proponi() {
  if (!props.datasource) return
  if (righe.value.length && !confirm(t('contracts.proposeReplace'))) return
  occupato.value = 'propose'
  try {
    const { document } = await api.propose(props.datasource.id)
    righe.value = document.rules.map((r) => daRegola({ ...r, id: undefined }))
    problemi.value = {}
  } catch (e) {
    toast.error(errMessage(e))
  } finally {
    occupato.value = ''
  }
}

async function elimina() {
  if (!props.datasource || !confirm(t('contracts.removeConfirm'))) return
  occupato.value = 'remove'
  try {
    await api.remove(props.datasource.id)
    mostra(null)
    emit('changed')
    toast.success(t('contracts.removed'))
  } catch (e) {
    toast.error(errMessage(e))
  } finally {
    occupato.value = ''
  }
}

async function apriStorico() {
  if (storico.value || !props.datasource) {
    storico.value = null
    return
  }
  try {
    storico.value = await api.history(props.datasource.id)
  } catch (e) {
    toast.error(errMessage(e))
  }
}

// Il contratto SALVATO nel formato aperto ODCS: un file da consegnare a un
// catalogo o a un altro strumento (le modifiche non salvate non ci sono).
async function esporta() {
  if (!props.datasource) return
  try {
    const url = URL.createObjectURL(await api.odcs(props.datasource.id))
    const a = document.createElement('a')
    a.href = url
    a.download = `${props.datasource.name.replace(/[^\w.-]+/g, '_').replace(/^[._]+|[._]+$/g, '') || 'datasource'}.odcs.yaml`
    a.click()
    URL.revokeObjectURL(url)
  } catch (e) {
    toast.error(errMessage(e))
  }
}

function chiudi() {
  if (modificato.value && !confirm(t('contracts.discardConfirm'))) return
  emit('close')
}
</script>

<template>
  <Modal :open="open" :title="$t('contracts.title', { name: datasource?.name ?? '' })" :width="860" @close="chiudi">
    <template #head>
      <ContractBadge v-if="info && info.enabled" :contract="info" :size="16" label />
    </template>

    <p v-if="loading" class="muted"><LoaderCircle :size="13" class="spin" /> {{ $t('contracts.loading') }}</p>

    <template v-else>
      <!-- ── stato ─────────────────────────────────────────────────────── -->
      <div v-if="info" class="cd-stato" :class="info.status">
        <p v-if="info.blocked" class="cd-rifiuto">
          <CircleX :size="14" /> {{ $t('contracts.blockedExplain', { when: quando(info.blocked_at) }) }}
        </p>
        <p class="muted">
          <template v-if="info.checked_at">{{ $t('contracts.checkedAt', { when: quando(info.checked_at), version: info.version }) }}</template>
          <template v-else>{{ $t('contracts.neverChecked') }}</template>
          <template v-if="info.report && info.report.rows != null"> · {{ $t('contracts.result.rows', { n: info.report.rows.toLocaleString(locale) }) }}</template>
        </p>
      </div>
      <p v-else class="cd-intro">{{ $t('contracts.intro') }}</p>

      <div class="cd-legenda">
        <span><i class="punto warning" /> <strong>{{ $t('contracts.severity.warning') }}</strong> — {{ $t('contracts.severityHint.warning') }}</span>
        <span><i class="punto error" /> <strong>{{ $t('contracts.severity.error') }}</strong> — {{ $t('contracts.severityHint.error') }}</span>
      </div>

      <div v-if="!soloLettura" class="cd-barra">
        <button :disabled="!!occupato" @click="proponi">
          <LoaderCircle v-if="occupato === 'propose'" :size="13" class="spin" /><Sparkles v-else :size="13" />
          {{ $t('contracts.propose') }}
        </button>
        <label class="cd-acceso"><input v-model="enabled" type="checkbox" :disabled="!!occupato" /> {{ $t('contracts.enabled') }}</label>
        <span v-if="modificato && info" class="cd-nota">{{ $t('contracts.unsaved') }}</span>
      </div>

      <!-- ── regole, una colonna alla volta ─────────────────────────────── -->
      <!-- fermo anche mentre si salva o si verifica: la risposta riscrive il modulo
           con ciò che il server ha, e cancellerebbe quello scritto nel frattempo -->
      <fieldset class="cd-regole" :class="{ 'sola-lettura': soloLettura }" :disabled="soloLettura || !!occupato">
      <section v-for="g in gruppi" :key="g.chiave" class="cd-gruppo" :class="{ vuoto: !g.righe.length }">
        <header>
          <span class="cd-nome" :class="{ manca: g.manca }">{{ g.nome }}</span>
          <span v-if="g.tipo" class="cd-tipo">{{ g.tipo }}</span>
          <span v-if="g.manca" class="cd-tipo manca">{{ $t('contracts.columnGone') }}</span>
          <select v-if="!soloLettura" class="cd-aggiungi" :aria-label="$t('contracts.addRuleTo', { name: g.nome })" @change="aggiungi(g.chiave, $event)">
            <option value="">+ {{ $t('contracts.addRule') }}</option>
            <option v-for="k in tipiPer(g.chiave)" :key="k" :value="k">{{ $t(`contracts.kind.${k}`) }}</option>
          </select>
        </header>

        <div v-for="r in g.righe" :key="r.uid" class="cd-regola" :class="[comeEAndata(r), { difetto: problemi[r.uid] }]">
          <span class="cd-esito" :title="$t(`contracts.outcome.${comeEAndata(r)}`)">
            <CircleCheck v-if="comeEAndata(r) === 'ok'" :size="15" />
            <CircleAlert v-else-if="comeEAndata(r) === 'warning'" :size="15" />
            <CircleX v-else-if="comeEAndata(r) === 'error'" :size="15" />
            <CircleDashed v-else :size="15" />
          </span>
          <span class="cd-kind" :title="$t(`contracts.kindHint.${r.kind}`)">{{ $t(`contracts.kind.${r.kind}`) }}</span>

          <span class="cd-campi">
            <select v-if="r.kind === 'column'" v-model="r.dtype" :aria-label="$t('contracts.field.dtype')">
              <option value="">{{ $t('contracts.field.anyType') }}</option>
              <option v-for="d in DTYPES" :key="d" :value="d">{{ $t(`contracts.dtype.${d}`) }}</option>
            </select>
            <span v-else-if="r.kind === 'unique' && g.chiave === TABELLA" class="cd-chips">
              <button
                v-for="c in colonne" :key="c.name" type="button" class="chip" :class="{ on: r.columns.includes(c.name) }"
                :aria-pressed="r.columns.includes(c.name)" @click="alterna(r, c.name)"
              >{{ c.name }}</button>
            </span>
            <input v-else-if="r.kind === 'accepted_values'" v-model="r.values" type="text" spellcheck="false" :placeholder="$t('contracts.field.values')" :aria-label="$t('contracts.field.values')" />
            <template v-else-if="r.kind === 'range' || r.kind === 'row_count'">
              <input v-model="r.min" type="text" inputmode="decimal" :placeholder="$t('contracts.field.min')" :aria-label="$t('contracts.field.min')" class="corto" />
              <span class="muted">…</span>
              <input v-model="r.max" type="text" inputmode="decimal" :placeholder="$t('contracts.field.max')" :aria-label="$t('contracts.field.max')" class="corto" />
            </template>
            <input v-else-if="r.kind === 'pattern'" v-model="r.regex" type="text" spellcheck="false" :placeholder="$t('contracts.field.regex')" :aria-label="$t('contracts.field.regex')" class="mono" />
            <template v-else-if="r.kind === 'freshness'">
              <input v-model="r.hours" type="text" inputmode="decimal" :aria-label="$t('contracts.field.maxAge')" class="corto" />
              <span class="muted">{{ $t('contracts.field.hoursOf') }}</span>
              <select v-model="r.column" :aria-label="$t('contracts.field.freshnessOf')">
                <option value="">{{ $t('contracts.field.snapshot') }}</option>
                <option v-for="c in colonne.filter((c) => ['date', 'datetime'].includes(famiglia(c.dtype)))" :key="c.name" :value="c.name">{{ c.name }}</option>
              </select>
            </template>
            <template v-else-if="r.kind === 'expression'">
              <input v-model="r.sql" type="text" spellcheck="false" :placeholder="$t('contracts.field.sql')" :aria-label="$t('contracts.field.sql')" class="mono" />
            </template>
          </span>

          <span class="cd-sev" role="group" :aria-label="$t('contracts.severityOf')">
            <button type="button" :class="{ on: r.severity === 'warning' }" :aria-pressed="r.severity === 'warning'" class="warning" @click="r.severity = 'warning'">{{ $t('contracts.severity.warning') }}</button>
            <button type="button" :class="{ on: r.severity === 'error' }" :aria-pressed="r.severity === 'error'" class="error" @click="r.severity = 'error'">{{ $t('contracts.severity.error') }}</button>
          </span>
          <button v-if="!soloLettura" class="cd-via" :title="$t('contracts.removeRule')" :aria-label="$t('contracts.removeRule')" @click="togli(r.uid)"><Trash2 :size="13" /></button>

          <p v-if="problemi[r.uid]" class="cd-sotto difetto">{{ $t(`contracts.problem.${problemi[r.uid]}`) }}</p>
          <p v-else-if="comeEAndata(r) !== 'none' && comeEAndata(r) !== 'ok' && esitoDi(r)" class="cd-sotto">{{ dettaglio(esitoDi(r)!) }}</p>
        </div>
      </section>
      </fieldset>

      <!-- ── avvisi ────────────────────────────────────────────────────── -->
      <section v-if="!soloLettura" class="cd-avvisi">
        <header><Bell :size="13" /> {{ $t('contracts.notify.title') }}</header>
        <p class="muted">{{ $t('contracts.notify.hint') }}</p>
        <div class="cd-avvisi-campi">
          <input v-model="avvisiA" type="text" spellcheck="false" :disabled="!!occupato" :placeholder="$t('contracts.notify.emailsPlaceholder')" :aria-label="$t('contracts.notify.emails')" />
          <select v-model="avvisiCon" :disabled="!!occupato" :aria-label="$t('contracts.notify.connection')">
            <option :value="null">{{ $t('contracts.notify.noConnection') }}</option>
            <option v-for="c in postali" :key="c.id" :value="c.id">{{ c.name }}</option>
            <!-- la connessione già scelta può stare in una cartella che chi apre non vede -->
            <option v-if="avvisiCon !== null && !postali.some((c) => c.id === avvisiCon)" :value="avvisiCon">{{ $t('contracts.notify.otherConnection', { id: avvisiCon }) }}</option>
          </select>
        </div>
        <p v-if="!postali.length && avvisiCon === null" class="muted">{{ $t('contracts.notify.noSmtp') }}</p>
        <p v-else-if="!!avvisiA.trim() !== (avvisiCon !== null)" class="cd-nota">{{ $t('contracts.notify.incomplete') }}</p>
      </section>

      <!-- ── storico ───────────────────────────────────────────────────── -->
      <div v-if="info" class="cd-storico">
        <div class="cd-collegamenti">
          <button class="cd-link" @click="apriStorico"><History :size="13" /> {{ $t('contracts.history') }}</button>
          <button class="cd-link" :title="$t('contracts.exportOdcsTitle')" @click="esporta"><Download :size="13" /> {{ $t('contracts.exportOdcs') }}</button>
        </div>
        <table v-if="storico && storico.length">
          <tr v-for="s in storico" :key="s.id">
            <td><ContractBadge :contract="{ status: s.outcome, blocked: s.blocked, errors: s.errors, warnings: s.warnings, checked_at: s.evaluated_at, version: s.contract_version }" /></td>
            <td>{{ quando(s.evaluated_at) }}</td>
            <td>{{ $t(`contracts.trigger.${s.trigger}`) }}</td>
            <td>{{ s.blocked ? $t('contracts.refused') : $t(`contracts.short.${s.outcome}`) }}</td>
            <td class="muted">v{{ s.contract_version }}<template v-if="s.rows != null"> · {{ $t('contracts.result.rows', { n: s.rows.toLocaleString(locale) }) }}</template></td>
          </tr>
        </table>
        <p v-else-if="storico" class="muted">{{ $t('contracts.historyEmpty') }}</p>
      </div>
    </template>

    <template v-if="!soloLettura" #footer>
      <button v-if="info" class="cd-elimina" :disabled="!!occupato" @click="elimina"><Trash2 :size="13" /> {{ $t('contracts.remove') }}</button>
      <span class="cd-spazio" />
      <button v-if="info" :disabled="!!occupato || modificato" :title="modificato ? $t('contracts.saveFirst') : ''" @click="verifica">
        <LoaderCircle v-if="occupato === 'check'" :size="13" class="spin" /><RefreshCw v-else :size="13" />
        {{ $t('contracts.checkNow') }}
      </button>
      <button class="primary" :disabled="!!occupato || (info ? !modificato : !righe.length)" @click="salva">
        <LoaderCircle v-if="occupato === 'save'" :size="13" class="spin" />
        {{ $t(info ? 'contracts.save' : 'contracts.create') }}
      </button>
    </template>
  </Modal>
</template>

<style scoped>
.cd-intro { margin: 0 0 10px; color: var(--muted); }
.cd-stato { margin-bottom: 10px; }
.cd-stato p { margin: 0 0 4px; font-size: 12.5px; }
.cd-rifiuto { display: flex; gap: 6px; align-items: flex-start; color: var(--danger); font-weight: 600; }
.cd-legenda { display: grid; gap: 4px; margin-bottom: 12px; padding: 8px 10px; border: 1px solid var(--border-soft); border-radius: 8px; font-size: 12px; color: var(--muted); }
.cd-legenda strong { color: var(--text); }
.punto { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 4px; }
.punto.warning { background: var(--warning); }
.punto.error { background: var(--danger); }
.cd-barra { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; flex-wrap: wrap; }
.cd-barra button { display: inline-flex; align-items: center; gap: 6px; }
.cd-acceso { display: inline-flex; align-items: center; gap: 6px; margin: 0; font-size: 12.5px; color: var(--text); }
.cd-nota { font-size: 12px; color: var(--warning); }

.cd-regole { margin: 0; padding: 0; border: 0; min-width: 0; }
.cd-regole.sola-lettura .cd-sev button:not(.on) { display: none; }
.cd-gruppo { border-top: 1px solid var(--border-soft); padding: 8px 0; }
.cd-gruppo header { display: flex; align-items: center; gap: 8px; min-height: 26px; }
.cd-nome { font-weight: 600; font-size: 13px; }
.cd-gruppo.vuoto .cd-nome { color: var(--muted); font-weight: 500; }
.cd-nome.manca { text-decoration: line-through; }
.cd-tipo { font-size: 11px; color: var(--muted); font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.cd-tipo.manca { color: var(--danger); font-family: inherit; }
.cd-aggiungi { margin-left: auto; width: auto; padding: 2px 6px; font-size: 12px; color: var(--muted); }

.cd-regola {
  display: grid; grid-template-columns: 18px 128px minmax(0, 1fr) auto 26px; gap: 8px; align-items: center;
  padding: 5px 0 5px 4px; border-left: 2px solid transparent;
}
.cd-regola.warning { border-left-color: var(--warning); }
.cd-regola.error, .cd-regola.difetto { border-left-color: var(--danger); }
.cd-esito { display: grid; place-items: center; color: var(--muted); }
.cd-regola.ok .cd-esito { color: var(--accent-2); }
.cd-regola.warning .cd-esito { color: var(--warning); }
.cd-regola.error .cd-esito { color: var(--danger); }
.cd-kind { font-size: 12.5px; }
.cd-campi { display: flex; align-items: center; gap: 6px; min-width: 0; }
.cd-campi input, .cd-campi select { margin: 0; padding: 4px 8px; font-size: 12.5px; min-width: 0; }
.cd-campi input.corto { width: 92px; flex: none; }
.cd-campi > .muted { white-space: nowrap; }
.cd-campi select { flex: 1; }
.cd-campi input.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.cd-chips { display: flex; flex-wrap: wrap; gap: 4px; }
.chip { padding: 2px 8px; font-size: 11.5px; border-radius: 999px; color: var(--muted); }
.chip.on { color: var(--text); border-color: var(--accent); background: var(--panel-2); }

.cd-sev { display: inline-flex; border: 1px solid var(--control-border); border-radius: 8px; overflow: hidden; }
.cd-sev button { border: 0; border-radius: 0; padding: 3px 9px; font-size: 11.5px; background: transparent; color: var(--muted); box-shadow: none; }
.cd-sev button.on.warning { background: var(--warning); color: var(--on-warning); font-weight: 600; }
.cd-sev button.on.error { background: var(--danger); color: #fff; font-weight: 600; }
.cd-via { display: grid; place-items: center; padding: 4px; border: 0; background: transparent; color: var(--muted); box-shadow: none; }
.cd-via:hover { color: var(--danger); }
.cd-sotto { grid-column: 2 / -1; margin: 0; font-size: 12px; color: var(--muted); overflow-wrap: anywhere; }
.cd-sotto.difetto { color: var(--danger); }

.cd-avvisi { border-top: 1px solid var(--border-soft); padding: 10px 0 12px; }
.cd-avvisi header { display: flex; align-items: center; gap: 6px; font-weight: 600; font-size: 13px; }
.cd-avvisi p { margin: 4px 0 8px; font-size: 12px; }
.cd-avvisi-campi { display: grid; grid-template-columns: minmax(0, 1fr) 220px; gap: 8px; }
.cd-avvisi-campi input, .cd-avvisi-campi select { margin: 0; padding: 4px 8px; font-size: 12.5px; min-width: 0; }
.cd-storico { border-top: 1px solid var(--border-soft); padding-top: 10px; margin-top: 4px; }
.cd-collegamenti { display: flex; flex-wrap: wrap; gap: 6px 18px; }
.cd-link { display: inline-flex; align-items: center; gap: 6px; border: 0; background: transparent; padding: 0; color: var(--accent); box-shadow: none; }
.cd-storico table { width: 100%; margin-top: 8px; border-collapse: collapse; font-size: 12px; }
.cd-storico td { padding: 4px 6px 4px 0; border-top: 1px solid var(--border-soft); }
.cd-spazio { flex: 1; }
.cd-elimina { display: inline-flex; align-items: center; gap: 6px; color: var(--danger); }
.spin { animation: cd-gira 1s linear infinite; }
@keyframes cd-gira { to { transform: rotate(360deg); } }
</style>
