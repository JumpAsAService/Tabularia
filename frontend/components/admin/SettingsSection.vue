<script setup lang="ts">
// Administration → Settings: every setting an administrator may change without touching
// the deployment (docs/specs/panel-settings.md), in one box per component. The live ones
// apply within seconds on every process; the ones read when a process starts carry the
// rollout icon, and the banner on top restarts their components (Kubernetes) or names
// the services to restart (Docker Compose). The starting value stays the environment's,
// and one click goes back to it. Observers read; with ENGINE__SETTINGS_PANEL=false nobody
// changes them from here. The server checks ranges and the constraints between settings;
// a refusal comes back as a code written here in the reader's language.
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { onBeforeRouteLeave } from 'vue-router'
import {
  Activity, ArrowDownToLine, Bot, Boxes, Braces, ChevronRight, Cloud, Container, Cpu, Database, DatabaseBackup,
  FileCode, FileOutput, FolderInput, GitBranch, Globe, Hammer, HardDrive, History, KeyRound, Layers,
  LoaderCircle, NotebookPen, Package, RefreshCw, RotateCcw, Search, Server, ShieldCheck, SlidersHorizontal, Sparkles, Timer,
  Upload, Webhook, Workflow, X,
} from 'lucide-vue-next'
import { errMessage } from '~/composables/useApi'
import { useFormat } from '~/composables/useFormat'
import { GIB, settingI18nKey } from '~/composables/useEngineSettings'
import {
  GROUPS, fromEngine, fromGateway, sectionOf, usePanelSettings,
  type PanelSetting, type RolloutComponent, type RolloutState, type SettingError, type SettingSection,
} from '~/composables/usePanelSettings'

const emit = defineEmits<{ (e: 'count', n: number): void; (e: 'dirty', d: boolean): void }>()

const { t, te, locale } = useI18n()
const fmt = useFormat()
const api = usePanelSettings()
const toast = useToast()

const items = ref<PanelSetting[]>([])
const editable = ref(false)
const panelEnabled = ref(true)
const refreshSeconds = ref(10)
const engineError = ref('')
const error = ref('')
const loading_ = ref(true)
const saving = ref(false)
const rollout = ref<RolloutState | null>(null)
const starting = ref(false)

// what the fields hold: a string, because a half-typed field is not a value
const text = ref<Record<string, string>>({})
// the ones that go back to the environment's value when saved
const azzera = ref<Set<string>>(new Set())
// the server's refusals at the last save, per setting
const refusals = ref<Record<string, SettingError>>({})
const openOnes = ref<Set<string>>(new Set())
const search = ref('')

const ICONE: Record<string, any> = {
  signin: KeyRound, sso: ShieldCheck, orchestration: Workflow, history: History, webhooks: Webhook, ai: Sparkles, mcp: Bot,
  dbt: Package, previews: Upload, backups: DatabaseBackup, monitoring: Activity, openlineage: GitBranch,
  processes: Cpu, kubernetes: Boxes, scripts: FileCode, builds: Hammer, notebooks: NotebookPen, containers: Container,
  execution: Timer, cache: Layers, embedded: HardDrive, engine_api: Server, import: ArrowDownToLine,
  export: FileOutput, sftp: FolderInput, s3: Cloud, api: Globe, placeholders: Braces, clickhouse: Database,
  bigquery: Cloud,
}
const SECTIONS: SettingSection[] = ['gateway', 'kubernetes', 'engine']

const key = (v: PanelSetting) => `${v.source}:${v.key}`
const i18nBase = (v: PanelSetting) =>
  v.source === 'engine' ? `engineSettings.items.${settingI18nKey(v.key)}` : `panelSettings.items.${settingI18nKey(v.key)}`
const name = (v: PanelSetting) => t(`${i18nBase(v)}.label`)
const help = (v: PanelSetting) => t(`${i18nBase(v)}.help`)
const fieldId = (v: PanelSetting) => `impostazione-${v.source}-${settingI18nKey(v.key)}`
const groupTitle = (s: SettingSection, g: string) =>
  s === 'engine' ? t(`engineSettings.categories.${g}.title`) : t(`panelSettings.groups.${g}.title`)
const groupLead = (s: SettingSection, g: string) =>
  s === 'engine' ? t(`engineSettings.categories.${g}.lead`) : t(`panelSettings.groups.${g}.lead`)
const component = (c: string) => t(`panelSettings.components.${c.replace(/-/g, '_')}`)

async function load() {
  loading_.value = true
  error.value = ''
  try {
    const [gw, eng, ro] = await Promise.allSettled([api.gateway(), api.engine(), api.rollout()])
    if (gw.status === 'rejected') throw gw.reason
    const out = gw.value.items.map(fromGateway)
    editable.value = gw.value.editable
    panelEnabled.value = gw.value.panel_enabled
    refreshSeconds.value = gw.value.refresh_seconds
    engineError.value = ''
    if (eng.status === 'fulfilled') {
      // the processes per worker have a section of their own (Administration → Workers)
      out.push(...eng.value.items.filter((v) => v.category !== 'workers')
        .map((v) => fromEngine(v, eng.value.unavailable)))
    } else {
      engineError.value = errMessage(eng.reason)
    }
    items.value = out
    rollout.value = ro.status === 'fulfilled' ? ro.value : null
    emit('count', out.filter((v) => v.value !== null).length)
  } catch (e) {
    error.value = errMessage(e)
  } finally {
    loading_.value = false
  }
}
onMounted(load)

// ── the boxes ───────────────────────────────────────────────────────────────

const found = (v: PanelSetting) => {
  const q = search.value.trim().toLowerCase()
  if (!q) return true
  return [name(v), v.envVar, v.key].some((s) => s.toLowerCase().includes(q))
}
const boxes = computed(() =>
  SECTIONS.map((s) => ({
    section: s,
    groups: GROUPS[s]
      .map((g) => ({ id: `${s}:${g}`, group: g, items: items.value.filter((v) => sectionOf(v) === s && v.group === g && found(v)) }))
      .filter((g) => g.items.length),
  })).filter((s) => s.groups.length || (s.section === 'engine' && engineError.value && !search.value)),
)
const isOpen = (id: string) => !!search.value.trim() || openOnes.value.has(id)
function toggleOpen(id: string, ev: Event) {
  const open = (ev.target as HTMLDetailsElement).open
  if (search.value.trim()) return
  const s = new Set(openOnes.value)
  if (open) s.add(id)
  else s.delete(id)
  openOnes.value = s
}
const inGroupFromPanel = (list: PanelSetting[]) => list.filter((v) => v.value !== null).length
const inGroupToSave = (list: PanelSetting[]) => list.filter((v) => toSave(v)).length
const inGroupRollout = (list: PanelSetting[]) => list.some((v) => v.rollout.length)
const inGroupErrors = (list: PanelSetting[]) => list.some((v) => !!problem(v))

// ── units and formats ───────────────────────────────────────────────────────

const MIB = 1024 ** 2
// the unit a number is typed in: bytes in GiB or MiB after their range, kilobytes in GiB
function scala(v: PanelSetting): { factor: number, unit: string } {
  if (v.unit === 'bytes') return (v.max ?? 0) >= 8 * GIB ? { factor: GIB, unit: 'gib' } : { factor: MIB, unit: 'mib' }
  if (v.unit === 'kilobytes') return { factor: MIB, unit: 'gib' }
  return { factor: 1, unit: v.unit }
}
const unit = (v: PanelSetting) => {
  const u = scala(v).unit
  return u && te(`panelSettings.units.${u}`) ? t(`panelSettings.units.${u}`) : ''
}
const numeric = (v: PanelSetting) => v.kind === 'int' || v.kind === 'float'

function duration(seconds: number): string {
  const parts: [number, Intl.NumberFormatOptions['unit']][] = []
  let rest = Math.round(seconds)
  for (const [s, u] of [[86400, 'day'], [3600, 'hour'], [60, 'minute'], [1, 'second']] as const) {
    const n = Math.floor(rest / s)
    if (n) parts.push([n, u])
    rest -= n * s
  }
  return parts
    .slice(0, 2)
    .map(([n, u]) => new Intl.NumberFormat(locale.value, { style: 'unit', unit: u, unitDisplay: 'short' }).format(n))
    .join(' ')
}

const localNumber = (x: number, digits = 3) => new Intl.NumberFormat(locale.value, { maximumFractionDigits: digits }).format(x)

function show(v: PanelSetting, x: unknown): string {
  if (x === null || x === undefined) return t('panelSettings.automatic')
  if (v.kind === 'bool') return x ? t('panelSettings.on') : t('panelSettings.off')
  if (typeof x === 'string') return x === '' ? t('panelSettings.empty') : x
  if (typeof x !== 'number') return String(x)
  const { factor } = scala(v)
  const u = unit(v)
  const n = `${localNumber(x / factor, factor === 1 ? 3 : 4)}${u ? ` ${u}` : ''}`
  if (v.unit === 'seconds' && x >= 60) return `${n} (${duration(x)})`
  if (v.unit === 'minutes' && x >= 60) return `${n} (${duration(x * 60)})`
  return n
}

// ── the fields ──────────────────────────────────────────────────────────────

const savedValue = (v: PanelSetting) => (v.value !== null ? v.value : v.envValue)
const shownOne = (v: PanelSetting) => (azzera.value.has(key(v)) ? v.envValue : savedValue(v))

function separators() {
  const parts = new Intl.NumberFormat(locale.value).formatToParts(12345.6)
  return {
    group: parts.find((x) => x.type === 'group')?.value ?? ',',
    decimal: parts.find((x) => x.type === 'decimal')?.value ?? '.',
  }
}

// a value as its field shows it (four decimals in GiB or MiB: 4096 bytes is not «0»)
function formattato(v: PanelSetting, x: unknown): string {
  if (x === null || x === undefined) return ''
  if (typeof x === 'number') {
    const { factor } = scala(v)
    return new Intl.NumberFormat(locale.value, { maximumFractionDigits: factor === 1 ? 3 : 4 }).format(x / factor)
  }
  return String(x)
}

function field(v: PanelSetting): string {
  const k = key(v)
  if (k in text.value) return text.value[k]
  return formattato(v, shownOne(v))
}
const isOn = (v: PanelSetting) => {
  const k = key(v)
  return k in text.value ? text.value[k] === 'true' : !!shownOne(v)
}

function write(v: PanelSetting, s: string) {
  const k = key(v)
  text.value = { ...text.value, [k]: s }
  azzera.value.delete(k)
  azzera.value = new Set(azzera.value)
  const { [k]: _via, ...others } = refusals.value
  refusals.value = others
}

function reset(v: PanelSetting) {
  const k = key(v)
  const { [k]: _via, ...others } = text.value
  text.value = others
  if (v.value !== null) azzera.value = new Set([...azzera.value, k])
  // the button goes away: the focus back on the field, not lost on the page
  nextTick(() => document.getElementById(fieldId(v))?.focus())
}

// the value typed, in the setting's unit, or undefined if it is not one: who types
// «250.000» in Italian means two hundred and fifty thousand, not 250
function value(v: PanelSetting): unknown {
  const s0 = text.value[key(v)] ?? ''
  if (v.kind === 'bool') return s0 === 'true'
  if (!numeric(v)) return s0.trim()
  const { group, decimal } = separators()
  let s = s0.trim().replace(/[\s  ']/g, '')
  if (!s) return undefined
  // the thousands separators a reader may type: the language's, and the dot where the
  // decimal mark is a comma (French groups with a narrow space, but «10.000» is ten
  // thousand there too, never ten). Only in groups of three digits
  const e = (x: string) => x.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  for (const g of new Set([group, decimal === ',' ? '.' : ','])) {
    if (!g.trim() || g === decimal || !s.includes(g)) continue
    if (!new RegExp(`^-?\\d{1,3}(${e(g)}\\d{3})+(${e(decimal)}\\d+)?$`).test(s)) return undefined
    s = s.split(g).join('')
  }
  s = s.replace(decimal, '.')
  if (!/^-?\d+(\.\d+)?$/.test(s)) return undefined
  const x = Number(s)
  if (!Number.isFinite(x)) return undefined
  const { factor } = scala(v)
  // a whole number typed in a larger unit (1,5 GiB): to the nearest byte
  return v.kind === 'int' && factor !== 1 ? Math.round(x * factor) : x * factor
}

function messageOf(v: PanelSetting, e: SettingError): string {
  const otherOne = items.value.find((x) => x.key === e.other)
  const k = `panelSettings.errors.${e.code}`
  // the pool's limit is connections, whichever setting is blamed
  const limit = e.limit === undefined ? '' : e.code === 'concurrent_pool' ? localNumber(e.limit) : show(otherOne ?? v, e.limit)
  if (!te(k)) return e.message || t('panelSettings.errors.invalid', { limit })
  const sentence = t(k, { limit, other: otherOne ? name(otherOne) : (e.other ?? ''), max: v.maxLength ?? '' })
  // the setting's own validator says why (a Kubernetes quantity, a time zone): in English, but precise
  return (e.code === 'invalid' || e.code === 'too_small') && e.message ? `${sentence}: ${e.message}` : sentence
}

// what the page itself can tell is wrong (it blocks «Save»); the server's refusals are
// shown too but do not block a new try (an engine that was not answering may now)
function localProblem(v: PanelSetting): string {
  const k = key(v)
  if (k in text.value) {
    const x = value(v)
    if (numeric(v)) {
      if (x === undefined) return messageOf(v, { key: v.key, code: v.kind === 'int' ? 'integer' : 'number' })
      if (v.kind === 'int' && !Number.isInteger(x)) return messageOf(v, { key: v.key, code: 'integer' })
      if (v.min !== null && (x as number) < v.min) return messageOf(v, { key: v.key, code: 'min', limit: v.min })
      if (v.max !== null && (x as number) > v.max) return messageOf(v, { key: v.key, code: 'max', limit: v.max })
    } else if (v.kind !== 'bool') {
      const s = x as string
      if (v.maxLength !== null && s.length > v.maxLength) return messageOf(v, { key: v.key, code: 'too_long' })
      if (v.kind !== 'str' && !s) return messageOf(v, { key: v.key, code: 'invalid' })
    }
  }
  return ''
}

function problem(v: PanelSetting): string {
  const r = refusals.value[key(v)]
  return localProblem(v) || (r ? messageOf(v, r) : '')
}

const changes = computed(() => {
  const out: { gateway: Record<string, unknown>, engine: Record<string, unknown> } = { gateway: {}, engine: {} }
  for (const v of items.value) {
    const k = key(v)
    if (azzera.value.has(k)) {
      if (v.value !== null) out[v.source][v.key] = null
    } else if (k in text.value) {
      // the shown value typed again (a rounded 1,4305 GiB) is not a change
      if (text.value[k] === formattato(v, savedValue(v))) continue
      const x = value(v)
      if (x !== undefined && x !== savedValue(v)) out[v.source][v.key] = x
    }
  }
  return out
})
const changeCount = computed(() => Object.keys(changes.value.gateway).length + Object.keys(changes.value.engine).length)

// Changes typed and not saved were lost without a word on going to another section (the
// panel asks, told by «dirty»), to another page, or closing the tab.
watch(changeCount, (n) => emit('dirty', n > 0), { immediate: true })
onBeforeRouteLeave(() => !changeCount.value || confirm(t('panelSettings.leaveConfirm')))
function warnLeaving(e: BeforeUnloadEvent) {
  if (changeCount.value) e.preventDefault()
}
onMounted(() => window.addEventListener('beforeunload', warnLeaving))
onBeforeUnmount(() => {
  window.removeEventListener('beforeunload', warnLeaving)
  emit('dirty', false)
})
const notSavable = computed(() => items.value.some((v) => key(v) in text.value && !!localProblem(v)))
const toSave = (v: PanelSetting) => v.key in changes.value[v.source]
const canEdit = (v: PanelSetting) => editable.value && !v.off

function cancel() {
  text.value = {}
  azzera.value = new Set()
  refusals.value = {}
}

async function save() {
  if (!changeCount.value || notSavable.value) return
  saving.value = true
  refusals.value = {}
  const withRestart = items.value.some((v) => toSave(v) && v.rollout.length)
  const refused: Record<string, SettingError> = {}
  let otherError = ''
  let saved = 0
  // each side saves all or none: what a refused side had changed stays in the fields
  const fallite = new Set<string>()
  // the lost-run threshold lowered: the engine's hard limit (which must stay under it)
  // goes down first, or the gateway would refuse against the old one
  const stale = items.value.find((v) => v.source === 'gateway' && v.key === 'engine.run_stale_timeout_seconds')
  const newThreshold = stale && 'engine.run_stale_timeout_seconds' in changes.value.gateway
    ? (changes.value.gateway['engine.run_stale_timeout_seconds'] ?? stale.envValue) : undefined
  const order = stale && newThreshold !== undefined && (newThreshold as number) < (savedValue(stale) as number)
    ? (['engine', 'gateway'] as const) : (['gateway', 'engine'] as const)
  for (const source of order) {
    const values = changes.value[source]
    if (!Object.keys(values).length) continue
    try {
      if (source === 'gateway') await api.saveGateway(values)
      else await api.saveEngine(values)
      saved += Object.keys(values).length
    } catch (e: any) {
      fallite.add(source)
      const errors: SettingError[] | undefined = e?.data?.detail?.errors
      if (errors?.length) for (const x of errors) refused[`${source}:${x.key}`] = x
      else otherError = errMessage(e)
    }
  }
  const written = { ...text.value }
  const returned = new Set(azzera.value)
  await load()
  if (fallite.size) {
    // the refused side's changes stay in their fields, with the reasons; the other side is saved
    const stays = (k: string) => fallite.has(k.split(':', 1)[0])
    text.value = Object.fromEntries(Object.entries(written).filter(([k]) => stays(k)))
    azzera.value = new Set([...returned].filter(stays))
    refusals.value = refused
    openOnes.value = new Set([...openOnes.value, ...items.value
      .filter((v) => key(v) in refused).map((v) => `${sectionOf(v)}:${v.group}`)])
    toast.error(otherError || t('panelSettings.rejected', { n: saved }))
  } else {
    cancel()
    toast.success(t(withRestart ? 'panelSettings.savedRollout' : 'panelSettings.saved', { seconds: refreshSeconds.value }))
  }
  saving.value = false
}

// ── the rollout ─────────────────────────────────────────────────────────────

const pendingOnes = computed(() => (rollout.value?.components ?? []).filter((c) => c.pending))
const running = (c: RolloutComponent) => rollout.value?.mode === 'kubernetes' && !!c.requested_at && c.pending
const toStart = computed(() => pendingOnes.value.filter((c) => !running(c)))
// a setting read at start that its components do not run yet: the gateway says what its
// process started with; for the engine's, the ones set from the panel while a restart is due
const waitingFor = (v: PanelSetting) => {
  const due = v.rollout.filter((c) => pendingOnes.value.some((p) => p.component === c))
  if (!due.length) return []
  if (v.source === 'gateway') return v.startedWith !== savedValue(v) ? due : []
  return v.value !== null ? due : []
}
const services = computed(() => [...new Set(pendingOnes.value.map((c) => c.compose_service))])
const command = computed(() => `docker compose restart ${services.value.join(' ')}`)
const kubeCommand = computed(() => {
  const ns = rollout.value?.namespace || '<namespace>'
  const rel = rollout.value?.release ? `app.kubernetes.io/instance=${rollout.value.release},` : ''
  return `kubectl rollout restart deployment -n ${ns} -l '${rel}app.kubernetes.io/component in (${pendingOnes.value.map((c) => c.component).join(',')})'`
})
// the components whose pods could not be read: not offered for a restart, the reason said
const withError = computed(() => (rollout.value?.components ?? []).filter((c) => c.error))
const withOrchestrator = computed(() => toStart.value.some((c) => c.component === 'orchestrator'))
const rolloutModificabile = computed(() => !!rollout.value?.editable)

async function startRollout() {
  if (!toStart.value.length) return
  starting.value = true
  try {
    rollout.value = await api.startRollout(toStart.value.map((c) => c.component))
    if (rollout.value.mode === 'kubernetes') toast.success(t('panelSettings.rollout.started'))
  } catch (e) {
    toast.error(errMessage(e))
  } finally {
    starting.value = false
  }
}

async function stopRollout(c: RolloutComponent) {
  try {
    rollout.value = await api.cancelRollout(c.component)
  } catch (e) {
    toast.error(errMessage(e))
  }
}

const commandEl = ref<HTMLElement | null>(null)
async function copy() {
  try {
    await navigator.clipboard.writeText(rollout.value?.mode === 'compose' ? command.value : kubeCommand.value)
    toast.success(t('panelSettings.rollout.copied'))
  } catch {
    // no clipboard on a plain-http address: the command selected, for Ctrl+C
    const el = commandEl.value
    if (el) {
      const r = document.createRange()
      r.selectNodeContents(el)
      const sel = window.getSelection()
      sel?.removeAllRanges()
      sel?.addRange(r)
    }
    toast.info(t('panelSettings.rollout.copyFailed'))
  }
}

// every few seconds while a restart runs (the pods come and go), slower while something
// waits for one (another administrator may start it); one request at a time
let timer: ReturnType<typeof setTimeout> | null = null
let finished = false
async function follow() {
  if (finished) return
  const running_ = !!rollout.value?.components.some(running)
  if ((running_ || pendingOnes.value.length) && !document.hidden) {
    try {
      rollout.value = await api.rollout()
    } catch { /* the gateway pod serving us may be the one restarting: next time */ }
  }
  if (!finished) timer = setTimeout(follow, rollout.value?.components.some(running) ? 3000 : 15000)
}
onMounted(() => { timer = setTimeout(follow, 3000) })
onBeforeUnmount(() => { finished = true; if (timer) clearTimeout(timer) })

const zones = computed(() => {
  try {
    const zone = ((Intl as any).supportedValuesOf?.('timeZone') as string[] | undefined) ?? []
    // the default, UTC, is not among the browser's zones
    return zone.length ? ['UTC', ...zone.filter((z) => z !== 'UTC')] : []
  } catch {
    return []
  }
})
</script>

<template>
  <div class="card">
    <h4><SlidersHorizontal :size="14" /> {{ $t('panelSettings.title') }}</h4>
    <p class="hint muted small">{{ $t('panelSettings.lead', { seconds: refreshSeconds }) }}</p>
    <p class="hint muted small legenda"><RefreshCw :size="12" class="icona-rollout" /> {{ $t('panelSettings.rolloutLegend') }}</p>
    <p v-if="!panelEnabled" class="note">{{ $t('engineSettings.lockedByEnv') }}</p>
    <p v-else-if="!loading_ && !editable" class="note">{{ $t('engineSettings.readOnly') }}</p>
  </div>

  <p v-if="loading_ && !items.length" class="muted small"><LoaderCircle :size="13" class="spin" /> {{ $t('engineSettings.loading') }}</p>
  <p v-else-if="error" class="card page-error" role="alert">{{ error }}</p>

  <template v-else>
    <p v-for="c in withError" :key="`err-${c.component}`" class="note" role="status">
      {{ $t('panelSettings.rollout.podsError', { component: component(c.component), error: c.error }) }}
    </p>
    <section v-if="pendingOnes.length" class="card rollout" :aria-label="$t('panelSettings.rollout.region')">
      <h4><RefreshCw :size="14" class="icona-rollout" /> {{ $t('panelSettings.rollout.title', { n: pendingOnes.length }, pendingOnes.length) }}</h4>
      <p class="hint small">{{ pendingOnes.map((c) => component(c.component)).join(', ') }}</p>

      <template v-if="rollout?.mode === 'kubernetes'">
        <ul v-if="pendingOnes.some(running)" class="running">
          <li v-for="c in pendingOnes.filter(running)" :key="c.component">
            <LoaderCircle :size="13" class="spin" />
            <span>
              <strong>{{ component(c.component) }}</strong> ·
              {{ $t('panelSettings.rollout.left', { old: c.old ?? 0, pods: c.pods ?? 0 }) }}
              <template v-if="c.waiting_for.length"> · {{ $t('panelSettings.rollout.waitingFor', { pods: c.waiting_for.join(', ') }) }}</template>
            </span>
            <button v-if="rolloutModificabile" type="button" class="back" @click="stopRollout(c)"><X :size="12" /> {{ $t('panelSettings.rollout.stop') }}</button>
          </li>
        </ul>
        <template v-if="toStart.length">
          <p class="hint muted small">{{ $t('panelSettings.rollout.howKube') }}</p>
          <p v-if="withOrchestrator" class="note">{{ $t('panelSettings.rollout.orchestratorWarning') }}</p>
          <div v-if="rolloutModificabile" class="actions">
            <button type="button" class="primary" :disabled="starting || changeCount > 0" @click="startRollout">
              <LoaderCircle v-if="starting" :size="13" class="spin" /><RefreshCw v-else :size="13" />
              {{ $t('panelSettings.rollout.start', { n: toStart.length }, toStart.length) }}
            </button>
            <span v-if="changeCount" class="muted small">{{ $t('panelSettings.rollout.saveFirst') }}</span>
          </div>
        </template>
      </template>

      <template v-else-if="rollout">
        <p class="hint muted small">{{ $t(rollout.mode === 'compose' ? 'panelSettings.rollout.howCompose' : 'panelSettings.rollout.howManual') }}</p>
        <div class="command">
          <code ref="commandEl">{{ rollout.mode === 'compose' ? command : kubeCommand }}</code>
          <button type="button" class="back" @click="copy">{{ $t('panelSettings.rollout.copy') }}</button>
        </div>
        <div v-if="rolloutModificabile" class="actions">
          <button type="button" :disabled="starting" @click="startRollout">{{ $t('panelSettings.rollout.done') }}</button>
        </div>
      </template>
    </section>

    <div class="search">
      <Search :size="14" />
      <input v-model="search" type="search" :placeholder="$t('panelSettings.search')" :aria-label="$t('panelSettings.search')" />
    </div>
    <p v-if="search.trim() && !boxes.length" class="muted small">{{ $t('panelSettings.noMatch') }}</p>

    <section v-for="s in boxes" :key="s.section" class="section" :aria-labelledby="`sezione-${s.section}`">
      <h3 :id="`sezione-${s.section}`">{{ $t(`panelSettings.sections.${s.section}.title`) }}</h3>
      <p class="hint muted small">{{ $t(`panelSettings.sections.${s.section}.lead`) }}</p>
      <p v-if="s.section === 'engine' && engineError" class="card page-error" role="alert">{{ $t('panelSettings.engineDown', { error: engineError }) }}</p>

      <details
        v-for="g in s.groups"
        :key="g.id"
        class="box"
        :class="{ 'is-off': g.items.every((v) => v.off) }"
        :open="isOpen(g.id) || inGroupErrors(g.items)"
        @toggle="toggleOpen(g.id, $event)"
      >
        <summary>
          <ChevronRight :size="14" class="arrow" />
          <component :is="ICONE[g.group] ?? SlidersHorizontal" :size="15" class="icona" />
          <span class="title">
            <span class="group-name">{{ groupTitle(s.section, g.group) }}</span>
            <span class="lead">{{ groupLead(s.section, g.group) }}</span>
          </span>
          <span class="counts">
            <span v-if="inGroupRollout(g.items)" class="icona-rollout" :title="$t('panelSettings.groupHasRollout')"><RefreshCw :size="12" /></span>
            <span v-if="inGroupToSave(g.items)" class="chip to-save">{{ $t('panelSettings.unsavedN', { n: inGroupToSave(g.items) }) }}</span>
            <span v-if="inGroupFromPanel(g.items)" class="chip from-panel">{{ $t('panelSettings.fromPanelN', { n: inGroupFromPanel(g.items) }) }}</span>
            <span class="muted small">{{ g.items.length }}</span>
          </span>
        </summary>

        <p v-if="g.items.every((v) => v.off)" class="note">{{ $t('engineSettings.unavailable') }}</p>

        <div v-for="v in g.items" :key="key(v)" class="item">
          <div class="description">
            <label :for="fieldId(v)" class="name">
              {{ name(v) }}
              <span
                v-if="v.rollout.length"
                class="icona-rollout"
                role="img"
                :aria-label="$t('panelSettings.appliesAfter', { components: v.rollout.map(component).join(', ') })"
                :title="$t('panelSettings.appliesAfter', { components: v.rollout.map(component).join(', ') })"
              ><RefreshCw :size="12" /></span>
            </label>
            <p class="help">{{ help(v) }}</p>
            <p class="meta">
              {{ $t('engineSettings.envValue', { value: show(v, v.envValue) }) }}
              <code>{{ v.envVar }}</code>
              <template v-if="v.value !== null && v.updatedBy">
                · {{ $t('engineSettings.setBy', { who: v.updatedBy, when: fmt.ago(v.updatedAt) }) }}
              </template>
            </p>
          </div>

          <div class="value">
            <label v-if="v.kind === 'bool'" class="switch" :class="{ changed: toSave(v) }">
              <input
                :id="fieldId(v)"
                type="checkbox"
                role="switch"
                :checked="isOn(v)"
                :disabled="!canEdit(v) || saving"
                @change="write(v, String(($event.target as HTMLInputElement).checked))"
              />
              <span>{{ isOn(v) ? $t('panelSettings.on') : $t('panelSettings.off') }}</span>
            </label>
            <div v-else class="field" :class="{ changed: toSave(v), wrong: !!problem(v), text: !numeric(v) }">
              <input
                :id="fieldId(v)"
                type="text"
                :inputmode="numeric(v) ? 'decimal' : 'text'"
                autocomplete="off"
                :spellcheck="false"
                :list="v.kind === 'timezone' && zones.length ? 'fusi-orari' : undefined"
                :placeholder="v.kind === 'cpu' ? '2, 500m' : v.kind === 'memory' ? '4Gi, 512Mi' : shownOne(v) === null ? $t('panelSettings.automatic') : ''"
                :value="field(v)"
                :disabled="!canEdit(v) || saving"
                :aria-invalid="!!problem(v)"
                :aria-describedby="numeric(v) ? `${fieldId(v)}-intervallo` : undefined"
                @input="write(v, ($event.target as HTMLInputElement).value)"
              />
              <span v-if="unit(v)" class="unit">{{ unit(v) }}</span>
            </div>
            <p v-if="numeric(v) && v.min !== null && v.max !== null" :id="`${fieldId(v)}-intervallo`" class="range">
              {{ $t('engineSettings.range', { min: show(v, v.min), max: show(v, v.max) }) }}
            </p>
            <p v-if="problem(v)" class="error" role="alert">{{ problem(v) }}</p>
            <div class="state">
              <span v-if="toSave(v)" class="chip to-save">{{ $t('engineSettings.unsaved') }}</span>
              <span v-else-if="v.value !== null" class="chip from-panel">{{ $t('engineSettings.fromPanel') }}</span>
              <span v-if="!toSave(v) && waitingFor(v).length" class="chip waiting" :title="$t('panelSettings.waitingRolloutHint')">
                {{ $t('panelSettings.waitingRollout') }}
              </span>
              <span
                v-else-if="v.value !== null && v.applied !== v.value && !v.rollout.length && !toSave(v)"
                class="chip not-applied"
                :title="v.source === 'gateway' ? $t('panelSettings.notAppliedGatewayHint', { seconds: refreshSeconds }) : $t('engineSettings.notAppliedHint')"
              >{{ $t('engineSettings.notApplied') }}</span>
              <button
                v-if="editable && ((v.value !== null && !azzera.has(key(v))) || (canEdit(v) && key(v) in text))"
                type="button"
                class="back"
                :disabled="saving"
                @click="reset(v)"
              >
                <RotateCcw :size="12" /> {{ v.value !== null ? $t('engineSettings.resetToEnv') : $t('engineSettings.undo') }}
              </button>
            </div>
          </div>
        </div>
      </details>
    </section>

    <datalist v-if="zones.length" id="fusi-orari">
      <option v-for="z in zones" :key="z" :value="z" />
    </datalist>

    <div v-if="changeCount" class="bar" role="region" :aria-label="$t('engineSettings.pendingRegion')">
      <span>{{ $t('engineSettings.changes', { n: changeCount }, changeCount) }}</span>
      <button type="button" :disabled="saving" @click="cancel">{{ $t('engineSettings.cancel') }}</button>
      <button type="button" class="primary" :disabled="saving || notSavable" @click="save">
        <LoaderCircle v-if="saving" :size="13" class="spin" /> {{ $t('engineSettings.save') }}
      </button>
    </div>
  </template>
</template>

<style scoped>
.card { display: flex; flex-direction: column; gap: 10px; padding: 16px; background: var(--panel); border: 1px solid var(--border); border-radius: var(--radius); }
.card h4 { margin: 0; display: inline-flex; align-items: center; gap: 7px; }
.hint { margin: 0; line-height: 1.5; }
.small { font-size: 12px; }
.legenda { display: inline-flex; align-items: center; gap: 6px; }
.note { margin: 0; font-size: 12.5px; padding: 8px 10px; border-radius: 8px; background: var(--bg-soft); border: 1px solid var(--border); }
.page-error { color: var(--danger); font-size: 13px; }
.icona-rollout { display: inline-flex; align-items: center; color: var(--accent-hi); }

.rollout { border-color: color-mix(in srgb, var(--accent) 45%, var(--border)); background: color-mix(in srgb, var(--accent) 6%, var(--panel)); }
.rollout .running { margin: 0; padding: 0; list-style: none; display: flex; flex-direction: column; gap: 6px; font-size: 12.5px; }
.rollout .running li { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.actions { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.command { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.command code { font-size: 12px; padding: 6px 9px; border-radius: 7px; background: var(--bg-soft); border: 1px solid var(--border-soft); overflow-wrap: anywhere; }

.search {
  display: flex; align-items: center; gap: 8px; padding: 0 10px; border: 1px solid var(--control-border);
  border-radius: 8px; background: var(--bg-soft); color: var(--muted); max-width: 420px;
}
.search:focus-within { border-color: var(--accent); }
.search input { flex: 1; min-width: 0; border: 0; background: transparent; box-shadow: none; padding: 7px 0; }
.search input:focus { outline: none; box-shadow: none; }

.section { display: flex; flex-direction: column; gap: 8px; }
.section h3 { margin: 10px 0 0; font-size: 15px; }

.box { background: var(--panel); border: 1px solid var(--border); border-radius: var(--radius); }
.box > summary {
  display: flex; align-items: center; gap: 10px; padding: 12px 14px; cursor: pointer; list-style: none;
  border-radius: var(--radius);
}
.box > summary::-webkit-details-marker { display: none; }
.box > summary:hover { background: var(--bg-soft); }
.box > summary:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
.arrow { flex: none; color: var(--muted); transition: transform 0.15s ease; }
.box[open] > summary .arrow { transform: rotate(90deg); }
.box[open] > summary { border-bottom: 1px solid var(--border-soft); border-radius: var(--radius) var(--radius) 0 0; }
.icona { flex: none; color: var(--muted); }
.title { display: flex; flex-direction: column; min-width: 0; flex: 1; }
.group-name { font-size: 13.5px; font-weight: 600; color: var(--text); }
.lead { font-size: 12px; color: var(--muted); line-height: 1.45; }
.counts { display: inline-flex; align-items: center; gap: 6px; flex: none; }
.box > :not(summary) { margin-left: 14px; margin-right: 14px; }
.box > .item:last-child { padding-bottom: 14px; }
.box > .note { margin-top: 12px; }
.box.is-off .item { opacity: 0.6; }

.item {
  display: grid; grid-template-columns: minmax(0, 1fr) minmax(220px, 280px); gap: 8px 24px;
  padding: 14px 0 4px; border-top: 1px solid var(--border-soft);
}
.box > summary + .item, .box > .note + .item { border-top: 0; }
.description { min-width: 0; }
.name { display: inline-flex; align-items: center; gap: 6px; font-size: 13.5px; font-weight: 600; color: var(--text); }
.help { margin: 4px 0 0; font-size: 12.5px; line-height: 1.55; color: var(--muted); max-width: 72ch; }
.meta { margin: 6px 0 0; font-size: 11.5px; color: var(--muted); overflow-wrap: anywhere; }
.meta code { font-size: 11px; padding: 1px 5px; border-radius: 5px; background: var(--bg-soft); border: 1px solid var(--border-soft); }

.value { display: flex; flex-direction: column; gap: 4px; }
.field {
  display: flex; align-items: stretch; border: 1px solid var(--control-border); border-radius: 8px;
  background: var(--bg-soft); overflow: hidden;
}
.field:focus-within { border-color: var(--accent); box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 22%, transparent); }
.field.changed { border-color: color-mix(in srgb, var(--accent) 60%, var(--control-border)); }
.field.wrong { border-color: var(--danger); }
.field input {
  flex: 1; min-width: 0; border: 0; background: transparent; box-shadow: none; padding: 7px 9px;
  font-variant-numeric: tabular-nums; text-align: right;
}
.field.text input { text-align: left; }
.field input:focus { outline: none; box-shadow: none; }
.unit { display: inline-flex; align-items: center; padding: 0 10px; font-size: 12px; color: var(--muted); border-left: 1px solid var(--border-soft); white-space: nowrap; }
.switch { display: inline-flex; align-items: center; gap: 8px; font-size: 13px; min-height: 34px; cursor: pointer; }
.switch.changed span { color: var(--accent-hi); }
.switch input { width: 16px; height: 16px; accent-color: var(--accent); }
.range { margin: 0; font-size: 11.5px; color: var(--muted); }
.error { margin: 0; font-size: 12px; color: var(--danger); }
.state { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; min-height: 22px; }
.chip { font-size: 10.5px; letter-spacing: 0.02em; border-radius: 999px; padding: 1px 8px; border: 1px solid var(--border); background: var(--panel-2); color: var(--muted); white-space: nowrap; }
.chip.from-panel { color: var(--accent-hi); border-color: color-mix(in srgb, var(--accent) 40%, var(--border)); background: var(--tint-accent); }
.chip.to-save { color: var(--text); border-style: dashed; }
.chip.not-applied { color: var(--danger); border-color: color-mix(in srgb, var(--danger) 45%, var(--border)); }
.chip.waiting { color: var(--accent-hi); border-style: dashed; border-color: color-mix(in srgb, var(--accent) 45%, var(--border)); }
.back { display: inline-flex; align-items: center; gap: 4px; padding: 0 2px; min-height: 22px; border: 0; background: none; font-size: 12px; color: var(--muted); cursor: pointer; }
.back:hover:not(:disabled) { color: var(--accent); box-shadow: none; }

.bar {
  position: sticky; bottom: 0; z-index: 2; display: flex; align-items: center; justify-content: flex-end; gap: 10px;
  padding: 10px 14px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--panel);
  box-shadow: 0 -6px 18px color-mix(in srgb, var(--text) 8%, transparent);
}
.bar span { margin-right: auto; font-size: 13px; }

@media (max-width: 720px) {
  .item { grid-template-columns: 1fr; }
  .box > summary { flex-wrap: wrap; }
  .lead { display: none; }
}
</style>
