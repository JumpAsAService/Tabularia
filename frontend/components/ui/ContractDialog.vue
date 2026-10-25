<script setup lang="ts">
// A datasource's data contract: writing it, seeing its state, understanding which rule
// does not hold and why.
//
// The rules are grouped by column (plus a group for those about the whole table), and
// every rule carries its LAST outcome next to it: the report is not a separate table to
// cross-check by hand.
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

// ── the rule as it is in the form: every field there, the numbers still text ──
interface Row {
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
let nextUid = 1
const TABLE = '' // the key of the group of the rules on the whole table

const info = ref<ContractInfo | null>(null)
const rows = ref<Row[]>([])
const enabled = ref(true)
const savedValue = ref('') // the document as it is on the server: to know whether there are changes
const loading = ref(false)
const busy = ref<'' | 'save' | 'check' | 'propose' | 'remove'>('')
const problems = ref<Record<number, string>>({}) // uid → code of the form error
const history = ref<ContractHistoryEntry[] | null>(null)
// whom to notify when the state changes: addresses and the SMTP connection to send with
const notifyTo = ref('')
const notifyWith = ref<number | null>(null)
const smtpConnections = ref<ConnectionInfo[]>([])

const dsColumns = computed(() => props.datasource?.columns ?? [])
// whoever can only read sees the contract and the report, not the commands
const readOnly = computed(() => info.value !== null && !info.value.editable)

/** A physical type's family: what the contract promises (see the engine). */
function family(dtype: string | undefined): Dtype | 'other' {
  const d = dtype ?? ''
  if (/^U?Int/.test(d)) return 'integer'
  if (/^(Float|Decimal)/.test(d)) return 'number'
  if (d === 'Boolean') return 'boolean'
  if (d === 'Date') return 'date'
  if (d.startsWith('Datetime')) return 'datetime'
  if (/^(String|Utf8|Categorical|Enum)/.test(d)) return 'string'
  return 'other'
}
const familyOf = (col: string) => family(dsColumns.value.find((c) => c.name === col)?.dtype)
const numericOne = (col: string) => ['integer', 'number'].includes(familyOf(col))

function newRow(kind: RuleKind, col: string): Row {
  return {
    uid: nextUid++, kind, severity: 'error', column: col,
    columns: kind === 'unique' && col ? [col] : [],
    dtype: kind === 'column' && familyOf(col) !== 'other' ? (familyOf(col) as Dtype) : '',
    values: '', min: '', max: '', regex: '', hours: '24', sql: '', name: '',
  }
}

function fromRule(r: ContractRule): Row {
  const text = (v: unknown) => (v === null || v === undefined ? '' : String(v))
  return {
    uid: nextUid++, id: r.id, kind: r.kind, severity: r.severity,
    column: r.column ?? '', columns: [...(r.columns ?? [])], dtype: r.dtype ?? '',
    values: (r.values ?? []).join(', '), min: text(r.min), max: text(r.max), regex: r.regex ?? '',
    hours: text(r.max_age_hours ?? 24), sql: r.sql ?? '', name: r.name ?? '',
  }
}

function toRule(g: Row): ContractRule {
  const r: ContractRule = { kind: g.kind, severity: g.severity }
  if (g.id) r.id = g.id
  const number_ = (s: string) => (s.trim() === '' ? undefined : Number(s))
  // a range bound: a number on the numeric columns, text (a date) on the others
  const bound = (s: string) => (s.trim() === '' ? undefined : numericOne(g.column) && !Number.isNaN(Number(s)) ? Number(s) : s.trim())
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
      const items = g.values.split(/[,\n]/).map((v) => v.trim()).filter(Boolean)
      r.values = numericOne(g.column) && items.every((v) => !Number.isNaN(Number(v))) ? items.map(Number) : items
      break
    }
    case 'range':
      r.column = g.column
      r.min = bound(g.min)
      r.max = bound(g.max)
      break
    case 'pattern':
      r.column = g.column
      r.regex = g.regex
      break
    case 'row_count':
      r.min = number_(g.min)
      r.max = number_(g.max)
      break
    case 'freshness':
      r.max_age_hours = number_(g.hours)
      if (g.column) r.column = g.column
      break
    case 'expression':
      r.sql = g.sql
      if (g.name.trim()) r.name = g.name.trim()
      break
  }
  return r
}

const doc = computed<ContractDocument>(() => ({ description: info.value?.document.description ?? '', rules: rows.value.map(toRule) }))
const fingerprint = computed(() => JSON.stringify([doc.value.rules.map(({ id: _id, ...r }) => r), enabled.value, notifyTo.value.trim(), notifyWith.value]))
const changed = computed(() => fingerprint.value !== savedValue.value)

function show(c: ContractInfo | null) {
  info.value = c
  rows.value = (c?.document.rules ?? []).map(fromRule)
  enabled.value = c?.enabled ?? true
  notifyTo.value = c?.notify_emails ?? ''
  notifyWith.value = c?.notify_connection_id ?? null
  problems.value = {}
  savedValue.value = fingerprint.value
}

async function load() {
  if (!props.datasource) return
  loading.value = true
  history.value = null
  // the SMTP connections to choose from: if they do not load, the contract is written all the same
  useConnections().list().then((c) => { smtpConnections.value = c.filter((x) => x.db_type === 'smtp') }).catch(() => { smtpConnections.value = [] })
  try {
    show(await api.get(props.datasource.id))
  } catch (e) {
    toast.error(errMessage(e))
    show(null)
  } finally {
    loading.value = false
  }
}
watch(() => [props.open, props.datasource?.id], ([isOpen]) => { if (isOpen) load() }, { immediate: true })

// ── the groups: one column at a time, then the table ─────────────────────────
const groupOf = (g: Row) => ruleColumn(toRule(g)) ?? TABLE
const groups = computed(() => {
  const known = dsColumns.value.map((c) => c.name)
  // a rule may name a column the datasource no longer has: it stays visible
  const orphans = [...new Set(rows.value.map(groupOf))].filter((n) => n !== TABLE && !known.includes(n))
  return [
    ...known.map((label) => ({ key: label, label, colType: dsColumns.value.find((c) => c.name === label)?.dtype ?? '', missing: false })),
    ...orphans.map((label) => ({ key: label, label, colType: '', missing: true })),
    { key: TABLE, label: t('contracts.tableGroup'), colType: '', missing: false },
  ].map((g) => ({ ...g, rows: rows.value.filter((r) => groupOf(r) === g.key) }))
    // to whoever cannot add rules, an empty group says nothing
    .filter((g) => !readOnly.value || g.rows.length)
})
const TABLE_KINDS: RuleKind[] = ['unique', 'row_count', 'freshness', 'expression']
const kindsFor = (key: string): RuleKind[] => (key === TABLE ? TABLE_KINDS : [...COLUMN_KINDS, 'unique'])

function add(key: string, ev: Event) {
  const choice = ev.target as HTMLSelectElement
  if (choice.value) rows.value.push(newRow(choice.value as RuleKind, key))
  choice.value = ''
}
function removeIt(uid: number) {
  rows.value = rows.value.filter((r) => r.uid !== uid)
}
function toggleOne(g: Row, col: string) {
  g.columns = g.columns.includes(col) ? g.columns.filter((c) => c !== col) : [...g.columns, col]
}

// ── the outcomes: those of the last report, rule by rule ─────────────────
const outcomes = computed(() => new Map((info.value?.report?.rules ?? []).map((r) => [r.id, r] as const)))
const outcomeOf = (g: Row): RuleResult | undefined => (g.id ? outcomes.value.get(g.id) : undefined)
function howItWent(g: Row): 'ok' | 'warning' | 'error' | 'none' {
  const e = outcomeOf(g)
  if (!e || changed.value) return 'none'
  return e.passed ? 'ok' : e.severity === 'warning' ? 'warning' : 'error'
}
function detail(e: RuleResult): string {
  if (e.error) return t('contracts.result.error', { error: e.error })
  const parts: string[] = []
  if (e.violations) parts.push(t('contracts.result.violations', { n: e.violations.toLocaleString(locale.value) }))
  if (e.kind === 'row_count' && e.observed !== undefined) parts.push(t('contracts.result.rows', { n: Number(e.observed).toLocaleString(locale.value) }))
  if (e.kind === 'freshness' && e.observed != null) parts.push(t('contracts.result.age', { hours: e.observed }))
  if (e.kind === 'column') parts.push(e.observed ? t('contracts.result.isType', { type: t(`contracts.dtype.${e.observed}`, String(e.observed)) }) : t('contracts.result.missing'))
  if (e.sample?.length) parts.push(t('contracts.result.sample', { values: e.sample.map(String).join(', ') }))
  return parts.join(' · ')
}
const when = (iso: string | null) => (iso ? new Date(iso).toLocaleString(locale.value, { dateStyle: 'medium', timeStyle: 'short' }) : '')

// ── actions ───────────────────────────────────────────────────────────────────
async function save() {
  if (!props.datasource) return
  busy.value = 'save'
  problems.value = {}
  const sent = rows.value.map((r) => r.uid)
  try {
    show(await api.save(props.datasource.id, doc.value, enabled.value, { emails: notifyTo.value.trim(), connectionId: notifyWith.value }))
    emit('changed')
    // the message says how the check went, not only that the save succeeded
    const outcome = info.value?.report?.outcome
    if (info.value?.check_error) toast.error(t('contracts.savedUnchecked', { error: info.value.check_error }))
    else if (outcome && outcome !== 'passed') toast.warning(t('contracts.savedNotRespected'))
    else toast.success(t(outcome ? 'contracts.savedPassed' : 'contracts.saved'))
  } catch (e) {
    const defects = contractProblems(e)
    if (defects) {
      problems.value = Object.fromEntries(defects.filter((d) => d.index !== null).map((d) => [sent[d.index as number], d.code]))
      toast.error(t(defects[0]?.index === null ? `contracts.problem.${defects[0].code}` : 'contracts.invalid'))
    } else {
      toast.error(errMessage(e))
    }
  } finally {
    busy.value = ''
  }
}

async function verify() {
  if (!props.datasource) return
  busy.value = 'check'
  try {
    show(await api.check(props.datasource.id))
    emit('changed')
    if (info.value?.check_error) toast.error(info.value.check_error)
  } catch (e) {
    toast.error(errMessage(e))
  } finally {
    busy.value = ''
  }
}

async function propose() {
  if (!props.datasource) return
  if (rows.value.length && !confirm(t('contracts.proposeReplace'))) return
  busy.value = 'propose'
  try {
    const { document } = await api.propose(props.datasource.id)
    rows.value = document.rules.map((r) => fromRule({ ...r, id: undefined }))
    problems.value = {}
  } catch (e) {
    toast.error(errMessage(e))
  } finally {
    busy.value = ''
  }
}

async function remove() {
  if (!props.datasource || !confirm(t('contracts.removeConfirm'))) return
  busy.value = 'remove'
  try {
    await api.remove(props.datasource.id)
    show(null)
    emit('changed')
    toast.success(t('contracts.removed'))
  } catch (e) {
    toast.error(errMessage(e))
  } finally {
    busy.value = ''
  }
}

async function openHistory() {
  if (history.value || !props.datasource) {
    history.value = null
    return
  }
  try {
    history.value = await api.history(props.datasource.id)
  } catch (e) {
    toast.error(errMessage(e))
  }
}

// The SAVED contract in the open ODCS format: a file to hand to a catalog or another tool
// (the unsaved changes are not there).
async function exportIt() {
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

function close() {
  if (changed.value && !confirm(t('contracts.discardConfirm'))) return
  emit('close')
}
</script>

<template>
  <Modal :open="open" :title="$t('contracts.title', { name: datasource?.name ?? '' })" :width="860" @close="close">
    <template #head>
      <ContractBadge v-if="info && info.enabled" :contract="info" :size="16" label />
    </template>

    <p v-if="loading" class="muted"><LoaderCircle :size="13" class="spin" /> {{ $t('contracts.loading') }}</p>

    <template v-else>
      <!-- ── state ───────────────────────────────────────────────────────── -->
      <div v-if="info" class="cd-state" :class="info.status">
        <p v-if="info.blocked" class="cd-refusal">
          <CircleX :size="14" /> {{ $t('contracts.blockedExplain', { when: when(info.blocked_at) }) }}
        </p>
        <p class="muted">
          <template v-if="info.checked_at">{{ $t('contracts.checkedAt', { when: when(info.checked_at), version: info.version }) }}</template>
          <template v-else>{{ $t('contracts.neverChecked') }}</template>
          <template v-if="info.report && info.report.rows != null"> · {{ $t('contracts.result.rows', { n: info.report.rows.toLocaleString(locale) }) }}</template>
        </p>
      </div>
      <p v-else class="cd-intro">{{ $t('contracts.intro') }}</p>

      <div class="cd-legenda">
        <span><i class="point warning" /> <strong>{{ $t('contracts.severity.warning') }}</strong> — {{ $t('contracts.severityHint.warning') }}</span>
        <span><i class="point error" /> <strong>{{ $t('contracts.severity.error') }}</strong> — {{ $t('contracts.severityHint.error') }}</span>
      </div>

      <div v-if="!readOnly" class="cd-bar">
        <button :disabled="!!busy" @click="propose">
          <LoaderCircle v-if="busy === 'propose'" :size="13" class="spin" /><Sparkles v-else :size="13" />
          {{ $t('contracts.propose') }}
        </button>
        <label class="cd-on"><input v-model="enabled" type="checkbox" :disabled="!!busy" /> {{ $t('contracts.enabled') }}</label>
        <span v-if="changed && info" class="cd-note">{{ $t('contracts.unsaved') }}</span>
      </div>

      <!-- ── rules, one column at a time ─────────────────────────────── -->
      <!-- still while saving or checking too: the answer rewrites the form with what the server
           has, and would delete what was written meanwhile -->
      <fieldset class="cd-rules" :class="{ 'read-only': readOnly }" :disabled="readOnly || !!busy">
      <section v-for="g in groups" :key="g.key" class="cd-group" :class="{ empty: !g.rows.length }">
        <header>
          <span class="cd-name" :class="{ missing: g.missing }">{{ g.label }}</span>
          <span v-if="g.colType" class="cd-type">{{ g.colType }}</span>
          <span v-if="g.missing" class="cd-type missing">{{ $t('contracts.columnGone') }}</span>
          <select v-if="!readOnly" class="cd-add" :aria-label="$t('contracts.addRuleTo', { name: g.label })" @change="add(g.key, $event)">
            <option value="">+ {{ $t('contracts.addRule') }}</option>
            <option v-for="k in kindsFor(g.key)" :key="k" :value="k">{{ $t(`contracts.kind.${k}`) }}</option>
          </select>
        </header>

        <div v-for="r in g.rows" :key="r.uid" class="cd-rule" :class="[howItWent(r), { defect: problems[r.uid] }]">
          <span class="cd-outcome" :title="$t(`contracts.outcome.${howItWent(r)}`)">
            <CircleCheck v-if="howItWent(r) === 'ok'" :size="15" />
            <CircleAlert v-else-if="howItWent(r) === 'warning'" :size="15" />
            <CircleX v-else-if="howItWent(r) === 'error'" :size="15" />
            <CircleDashed v-else :size="15" />
          </span>
          <span class="cd-kind" :title="$t(`contracts.kindHint.${r.kind}`)">{{ $t(`contracts.kind.${r.kind}`) }}</span>

          <span class="cd-fields">
            <select v-if="r.kind === 'column'" v-model="r.dtype" :aria-label="$t('contracts.field.dtype')">
              <option value="">{{ $t('contracts.field.anyType') }}</option>
              <option v-for="d in DTYPES" :key="d" :value="d">{{ $t(`contracts.dtype.${d}`) }}</option>
            </select>
            <span v-else-if="r.kind === 'unique' && g.key === TABLE" class="cd-chips">
              <button
                v-for="c in dsColumns" :key="c.name" type="button" class="chip" :class="{ on: r.columns.includes(c.name) }"
                :aria-pressed="r.columns.includes(c.name)" @click="toggleOne(r, c.name)"
              >{{ c.name }}</button>
            </span>
            <input v-else-if="r.kind === 'accepted_values'" v-model="r.values" type="text" spellcheck="false" :placeholder="$t('contracts.field.values')" :aria-label="$t('contracts.field.values')" />
            <template v-else-if="r.kind === 'range' || r.kind === 'row_count'">
              <input v-model="r.min" type="text" inputmode="decimal" :placeholder="$t('contracts.field.min')" :aria-label="$t('contracts.field.min')" class="short" />
              <span class="muted">…</span>
              <input v-model="r.max" type="text" inputmode="decimal" :placeholder="$t('contracts.field.max')" :aria-label="$t('contracts.field.max')" class="short" />
            </template>
            <input v-else-if="r.kind === 'pattern'" v-model="r.regex" type="text" spellcheck="false" :placeholder="$t('contracts.field.regex')" :aria-label="$t('contracts.field.regex')" class="mono" />
            <template v-else-if="r.kind === 'freshness'">
              <input v-model="r.hours" type="text" inputmode="decimal" :aria-label="$t('contracts.field.maxAge')" class="short" />
              <span class="muted">{{ $t('contracts.field.hoursOf') }}</span>
              <select v-model="r.column" :aria-label="$t('contracts.field.freshnessOf')">
                <option value="">{{ $t('contracts.field.snapshot') }}</option>
                <option v-for="c in dsColumns.filter((c) => ['date', 'datetime'].includes(family(c.dtype)))" :key="c.name" :value="c.name">{{ c.name }}</option>
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
          <button v-if="!readOnly" class="cd-via" :title="$t('contracts.removeRule')" :aria-label="$t('contracts.removeRule')" @click="removeIt(r.uid)"><Trash2 :size="13" /></button>

          <p v-if="problems[r.uid]" class="cd-below defect">{{ $t(`contracts.problem.${problems[r.uid]}`) }}</p>
          <p v-else-if="howItWent(r) !== 'none' && howItWent(r) !== 'ok' && outcomeOf(r)" class="cd-below">{{ detail(outcomeOf(r)!) }}</p>
        </div>
      </section>
      </fieldset>

      <!-- ── notices ──────────────────────────────────────────────────────── -->
      <section v-if="!readOnly" class="cd-notices">
        <header><Bell :size="13" /> {{ $t('contracts.notify.title') }}</header>
        <p class="muted">{{ $t('contracts.notify.hint') }}</p>
        <div class="cd-notice-fields">
          <input v-model="notifyTo" type="text" spellcheck="false" :disabled="!!busy" :placeholder="$t('contracts.notify.emailsPlaceholder')" :aria-label="$t('contracts.notify.emails')" />
          <select v-model="notifyWith" :disabled="!!busy" :aria-label="$t('contracts.notify.connection')">
            <option :value="null">{{ $t('contracts.notify.noConnection') }}</option>
            <option v-for="c in smtpConnections" :key="c.id" :value="c.id">{{ c.name }}</option>
            <!-- the connection chosen already may sit in a folder whoever opens does not see -->
            <option v-if="notifyWith !== null && !smtpConnections.some((c) => c.id === notifyWith)" :value="notifyWith">{{ $t('contracts.notify.otherConnection', { id: notifyWith }) }}</option>
          </select>
        </div>
        <p v-if="!smtpConnections.length && notifyWith === null" class="muted">{{ $t('contracts.notify.noSmtp') }}</p>
        <p v-else-if="!!notifyTo.trim() !== (notifyWith !== null)" class="cd-note">{{ $t('contracts.notify.incomplete') }}</p>
      </section>

      <!-- ── history ─────────────────────────────────────────────────────── -->
      <div v-if="info" class="cd-history">
        <div class="cd-links">
          <button class="cd-link" @click="openHistory"><History :size="13" /> {{ $t('contracts.history') }}</button>
          <button class="cd-link" :title="$t('contracts.exportOdcsTitle')" @click="exportIt"><Download :size="13" /> {{ $t('contracts.exportOdcs') }}</button>
        </div>
        <table v-if="history && history.length">
          <tr v-for="s in history" :key="s.id">
            <td><ContractBadge :contract="{ status: s.outcome, blocked: s.blocked, errors: s.errors, warnings: s.warnings, checked_at: s.evaluated_at, version: s.contract_version }" /></td>
            <td>{{ when(s.evaluated_at) }}</td>
            <td>{{ $t(`contracts.trigger.${s.trigger}`) }}</td>
            <td>{{ s.blocked ? $t('contracts.refused') : $t(`contracts.short.${s.outcome}`) }}</td>
            <td class="muted">v{{ s.contract_version }}<template v-if="s.rows != null"> · {{ $t('contracts.result.rows', { n: s.rows.toLocaleString(locale) }) }}</template></td>
          </tr>
        </table>
        <p v-else-if="history" class="muted">{{ $t('contracts.historyEmpty') }}</p>
      </div>
    </template>

    <template v-if="!readOnly" #footer>
      <button v-if="info" class="cd-remove" :disabled="!!busy" @click="remove"><Trash2 :size="13" /> {{ $t('contracts.remove') }}</button>
      <span class="cd-space" />
      <button v-if="info" :disabled="!!busy || changed" :title="changed ? $t('contracts.saveFirst') : ''" @click="verify">
        <LoaderCircle v-if="busy === 'check'" :size="13" class="spin" /><RefreshCw v-else :size="13" />
        {{ $t('contracts.checkNow') }}
      </button>
      <button class="primary" :disabled="!!busy || (info ? !changed : !rows.length)" @click="save">
        <LoaderCircle v-if="busy === 'save'" :size="13" class="spin" />
        {{ $t(info ? 'contracts.save' : 'contracts.create') }}
      </button>
    </template>
  </Modal>
</template>

<style scoped>
.cd-intro { margin: 0 0 10px; color: var(--muted); }
.cd-state { margin-bottom: 10px; }
.cd-state p { margin: 0 0 4px; font-size: 12.5px; }
.cd-refusal { display: flex; gap: 6px; align-items: flex-start; color: var(--danger); font-weight: 600; }
.cd-legenda { display: grid; gap: 4px; margin-bottom: 12px; padding: 8px 10px; border: 1px solid var(--border-soft); border-radius: 8px; font-size: 12px; color: var(--muted); }
.cd-legenda strong { color: var(--text); }
.point { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 4px; }
.point.warning { background: var(--warning); }
.point.error { background: var(--danger); }
.cd-bar { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; flex-wrap: wrap; }
.cd-bar button { display: inline-flex; align-items: center; gap: 6px; }
.cd-on { display: inline-flex; align-items: center; gap: 6px; margin: 0; font-size: 12.5px; color: var(--text); }
.cd-note { font-size: 12px; color: var(--warning); }

.cd-rules { margin: 0; padding: 0; border: 0; min-width: 0; }
.cd-rules.read-only .cd-sev button:not(.on) { display: none; }
.cd-group { border-top: 1px solid var(--border-soft); padding: 8px 0; }
.cd-group header { display: flex; align-items: center; gap: 8px; min-height: 26px; }
.cd-name { font-weight: 600; font-size: 13px; }
.cd-group.empty .cd-name { color: var(--muted); font-weight: 500; }
.cd-name.missing { text-decoration: line-through; }
.cd-type { font-size: 11px; color: var(--muted); font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.cd-type.missing { color: var(--danger); font-family: inherit; }
.cd-add { margin-left: auto; width: auto; padding: 2px 6px; font-size: 12px; color: var(--muted); }

.cd-rule {
  display: grid; grid-template-columns: 18px 128px minmax(0, 1fr) auto 26px; gap: 8px; align-items: center;
  padding: 5px 0 5px 4px; border-left: 2px solid transparent;
}
.cd-rule.warning { border-left-color: var(--warning); }
.cd-rule.error, .cd-rule.defect { border-left-color: var(--danger); }
.cd-outcome { display: grid; place-items: center; color: var(--muted); }
.cd-rule.ok .cd-outcome { color: var(--accent-2); }
.cd-rule.warning .cd-outcome { color: var(--warning); }
.cd-rule.error .cd-outcome { color: var(--danger); }
.cd-kind { font-size: 12.5px; }
.cd-fields { display: flex; align-items: center; gap: 6px; min-width: 0; }
.cd-fields input, .cd-fields select { margin: 0; padding: 4px 8px; font-size: 12.5px; min-width: 0; }
.cd-fields input.short { width: 92px; flex: none; }
.cd-fields > .muted { white-space: nowrap; }
.cd-fields select { flex: 1; }
.cd-fields input.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.cd-chips { display: flex; flex-wrap: wrap; gap: 4px; }
.chip { padding: 2px 8px; font-size: 11.5px; border-radius: 999px; color: var(--muted); }
.chip.on { color: var(--text); border-color: var(--accent); background: var(--panel-2); }

.cd-sev { display: inline-flex; border: 1px solid var(--control-border); border-radius: 8px; overflow: hidden; }
.cd-sev button { border: 0; border-radius: 0; padding: 3px 9px; font-size: 11.5px; background: transparent; color: var(--muted); box-shadow: none; }
.cd-sev button.on.warning { background: var(--warning); color: var(--on-warning); font-weight: 600; }
.cd-sev button.on.error { background: var(--danger-fill); color: var(--on-danger); font-weight: 600; }
.cd-via { display: grid; place-items: center; padding: 4px; border: 0; background: transparent; color: var(--muted); box-shadow: none; }
.cd-via:hover { color: var(--danger); }
.cd-below { grid-column: 2 / -1; margin: 0; font-size: 12px; color: var(--muted); overflow-wrap: anywhere; }
.cd-below.defect { color: var(--danger); }

.cd-notices { border-top: 1px solid var(--border-soft); padding: 10px 0 12px; }
.cd-notices header { display: flex; align-items: center; gap: 6px; font-weight: 600; font-size: 13px; }
.cd-notices p { margin: 4px 0 8px; font-size: 12px; }
.cd-notice-fields { display: grid; grid-template-columns: minmax(0, 1fr) 220px; gap: 8px; }
.cd-notice-fields input, .cd-notice-fields select { margin: 0; padding: 4px 8px; font-size: 12.5px; min-width: 0; }
.cd-history { border-top: 1px solid var(--border-soft); padding-top: 10px; margin-top: 4px; }
.cd-links { display: flex; flex-wrap: wrap; gap: 6px 18px; }
.cd-link { display: inline-flex; align-items: center; gap: 6px; border: 0; background: transparent; padding: 0; color: var(--accent); box-shadow: none; }
.cd-history table { width: 100%; margin-top: 8px; border-collapse: collapse; font-size: 12px; }
.cd-history td { padding: 4px 6px 4px 0; border-top: 1px solid var(--border-soft); }
.cd-space { flex: 1; }
.cd-remove { display: inline-flex; align-items: center; gap: 6px; color: var(--danger); }
.spin { animation: cd-gira 1s linear infinite; }
@keyframes cd-gira { to { transform: rotate(360deg); } }
</style>
