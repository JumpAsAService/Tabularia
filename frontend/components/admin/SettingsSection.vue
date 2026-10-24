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
  Activity, ArrowDownToLine, Boxes, Braces, ChevronRight, Cloud, Container, Cpu, Database, DatabaseBackup,
  FileCode, FileOutput, FolderInput, GitBranch, Globe, Hammer, HardDrive, History, KeyRound, Layers,
  LoaderCircle, Package, RefreshCw, RotateCcw, Search, Server, ShieldCheck, SlidersHorizontal, Sparkles, Timer,
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
const errore = ref('')
const caricando = ref(true)
const salvando = ref(false)
const rollout = ref<RolloutState | null>(null)
const avviando = ref(false)

// what the fields hold: a string, because a half-typed field is not a value
const testo = ref<Record<string, string>>({})
// the ones that go back to the environment's value when saved
const azzera = ref<Set<string>>(new Set())
// the server's refusals at the last save, per setting
const rifiuti = ref<Record<string, SettingError>>({})
const aperti = ref<Set<string>>(new Set())
const cerca = ref('')

const ICONE: Record<string, any> = {
  signin: KeyRound, sso: ShieldCheck, orchestration: Workflow, history: History, webhooks: Webhook, ai: Sparkles,
  dbt: Package, previews: Upload, backups: DatabaseBackup, monitoring: Activity, openlineage: GitBranch,
  processes: Cpu, kubernetes: Boxes, scripts: FileCode, builds: Hammer, containers: Container,
  execution: Timer, cache: Layers, embedded: HardDrive, engine_api: Server, import: ArrowDownToLine,
  export: FileOutput, sftp: FolderInput, s3: Cloud, api: Globe, placeholders: Braces, clickhouse: Database,
  bigquery: Cloud,
}
const SECTIONS: SettingSection[] = ['gateway', 'kubernetes', 'engine']

const key = (v: PanelSetting) => `${v.source}:${v.key}`
const i18nBase = (v: PanelSetting) =>
  v.source === 'engine' ? `engineSettings.items.${settingI18nKey(v.key)}` : `panelSettings.items.${settingI18nKey(v.key)}`
const nome = (v: PanelSetting) => t(`${i18nBase(v)}.label`)
const aiuto = (v: PanelSetting) => t(`${i18nBase(v)}.help`)
const idCampo = (v: PanelSetting) => `impostazione-${v.source}-${settingI18nKey(v.key)}`
const gruppoTitolo = (s: SettingSection, g: string) =>
  s === 'engine' ? t(`engineSettings.categories.${g}.title`) : t(`panelSettings.groups.${g}.title`)
const gruppoLead = (s: SettingSection, g: string) =>
  s === 'engine' ? t(`engineSettings.categories.${g}.lead`) : t(`panelSettings.groups.${g}.lead`)
const componente = (c: string) => t(`panelSettings.components.${c.replace(/-/g, '_')}`)

async function carica() {
  caricando.value = true
  errore.value = ''
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
    errore.value = errMessage(e)
  } finally {
    caricando.value = false
  }
}
onMounted(carica)

// ── the boxes ───────────────────────────────────────────────────────────────

const trovato = (v: PanelSetting) => {
  const q = cerca.value.trim().toLowerCase()
  if (!q) return true
  return [nome(v), v.envVar, v.key].some((s) => s.toLowerCase().includes(q))
}
const scatole = computed(() =>
  SECTIONS.map((s) => ({
    section: s,
    groups: GROUPS[s]
      .map((g) => ({ id: `${s}:${g}`, group: g, items: items.value.filter((v) => sectionOf(v) === s && v.group === g && trovato(v)) }))
      .filter((g) => g.items.length),
  })).filter((s) => s.groups.length || (s.section === 'engine' && engineError.value && !cerca.value)),
)
const aperto = (id: string) => !!cerca.value.trim() || aperti.value.has(id)
function apriChiudi(id: string, ev: Event) {
  const open = (ev.target as HTMLDetailsElement).open
  if (cerca.value.trim()) return
  const s = new Set(aperti.value)
  if (open) s.add(id)
  else s.delete(id)
  aperti.value = s
}
const nelGruppoDalPannello = (list: PanelSetting[]) => list.filter((v) => v.value !== null).length
const nelGruppoDaSalvare = (list: PanelSetting[]) => list.filter((v) => daSalvare(v)).length
const nelGruppoRollout = (list: PanelSetting[]) => list.some((v) => v.rollout.length)
const nelGruppoErrori = (list: PanelSetting[]) => list.some((v) => !!problema(v))

// ── units and formats ───────────────────────────────────────────────────────

const MIB = 1024 ** 2
// the unit a number is typed in: bytes in GiB or MiB after their range, kilobytes in GiB
function scala(v: PanelSetting): { factor: number, unit: string } {
  if (v.unit === 'bytes') return (v.max ?? 0) >= 8 * GIB ? { factor: GIB, unit: 'gib' } : { factor: MIB, unit: 'mib' }
  if (v.unit === 'kilobytes') return { factor: MIB, unit: 'gib' }
  return { factor: 1, unit: v.unit }
}
const unita = (v: PanelSetting) => {
  const u = scala(v).unit
  return u && te(`panelSettings.units.${u}`) ? t(`panelSettings.units.${u}`) : ''
}
const numerico = (v: PanelSetting) => v.kind === 'int' || v.kind === 'float'

function durata(secondi: number): string {
  const parti: [number, Intl.NumberFormatOptions['unit']][] = []
  let resto = Math.round(secondi)
  for (const [s, u] of [[86400, 'day'], [3600, 'hour'], [60, 'minute'], [1, 'second']] as const) {
    const n = Math.floor(resto / s)
    if (n) parti.push([n, u])
    resto -= n * s
  }
  return parti
    .slice(0, 2)
    .map(([n, u]) => new Intl.NumberFormat(locale.value, { style: 'unit', unit: u, unitDisplay: 'short' }).format(n))
    .join(' ')
}

const numeroLocale = (x: number, cifre = 3) => new Intl.NumberFormat(locale.value, { maximumFractionDigits: cifre }).format(x)

function mostra(v: PanelSetting, x: unknown): string {
  if (x === null || x === undefined) return t('panelSettings.automatic')
  if (v.kind === 'bool') return x ? t('panelSettings.on') : t('panelSettings.off')
  if (typeof x === 'string') return x === '' ? t('panelSettings.empty') : x
  if (typeof x !== 'number') return String(x)
  const { factor } = scala(v)
  const u = unita(v)
  const n = `${numeroLocale(x / factor, factor === 1 ? 3 : 4)}${u ? ` ${u}` : ''}`
  if (v.unit === 'seconds' && x >= 60) return `${n} (${durata(x)})`
  if (v.unit === 'minutes' && x >= 60) return `${n} (${durata(x * 60)})`
  return n
}

// ── the fields ──────────────────────────────────────────────────────────────

const salvato = (v: PanelSetting) => (v.value !== null ? v.value : v.envValue)
const mostrato = (v: PanelSetting) => (azzera.value.has(key(v)) ? v.envValue : salvato(v))

function separatori() {
  const parti = new Intl.NumberFormat(locale.value).formatToParts(12345.6)
  return {
    gruppo: parti.find((x) => x.type === 'group')?.value ?? ',',
    decimale: parti.find((x) => x.type === 'decimal')?.value ?? '.',
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

function campo(v: PanelSetting): string {
  const k = key(v)
  if (k in testo.value) return testo.value[k]
  return formattato(v, mostrato(v))
}
const acceso = (v: PanelSetting) => {
  const k = key(v)
  return k in testo.value ? testo.value[k] === 'true' : !!mostrato(v)
}

function scrivi(v: PanelSetting, s: string) {
  const k = key(v)
  testo.value = { ...testo.value, [k]: s }
  azzera.value.delete(k)
  azzera.value = new Set(azzera.value)
  const { [k]: _via, ...altri } = rifiuti.value
  rifiuti.value = altri
}

function torna(v: PanelSetting) {
  const k = key(v)
  const { [k]: _via, ...altri } = testo.value
  testo.value = altri
  if (v.value !== null) azzera.value = new Set([...azzera.value, k])
  // the button goes away: the focus back on the field, not lost on the page
  nextTick(() => document.getElementById(idCampo(v))?.focus())
}

// the value typed, in the setting's unit, or undefined if it is not one: who types
// «250.000» in Italian means two hundred and fifty thousand, not 250
function valore(v: PanelSetting): unknown {
  const s0 = testo.value[key(v)] ?? ''
  if (v.kind === 'bool') return s0 === 'true'
  if (!numerico(v)) return s0.trim()
  const { gruppo, decimale } = separatori()
  let s = s0.trim().replace(/[\s  ']/g, '')
  if (!s) return undefined
  // the thousands separators a reader may type: the language's, and the dot where the
  // decimal mark is a comma (French groups with a narrow space, but «10.000» is ten
  // thousand there too, never ten). Only in groups of three digits
  const e = (x: string) => x.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  for (const g of new Set([gruppo, decimale === ',' ? '.' : ','])) {
    if (!g.trim() || g === decimale || !s.includes(g)) continue
    if (!new RegExp(`^-?\\d{1,3}(${e(g)}\\d{3})+(${e(decimale)}\\d+)?$`).test(s)) return undefined
    s = s.split(g).join('')
  }
  s = s.replace(decimale, '.')
  if (!/^-?\d+(\.\d+)?$/.test(s)) return undefined
  const x = Number(s)
  if (!Number.isFinite(x)) return undefined
  const { factor } = scala(v)
  // a whole number typed in a larger unit (1,5 GiB): to the nearest byte
  return v.kind === 'int' && factor !== 1 ? Math.round(x * factor) : x * factor
}

function messaggioDi(v: PanelSetting, e: SettingError): string {
  const altra = items.value.find((x) => x.key === e.other)
  const k = `panelSettings.errors.${e.code}`
  // the pool's limit is connections, whichever setting is blamed
  const limit = e.limit === undefined ? '' : e.code === 'concurrent_pool' ? numeroLocale(e.limit) : mostra(altra ?? v, e.limit)
  if (!te(k)) return e.message || t('panelSettings.errors.invalid', { limit })
  const frase = t(k, { limit, other: altra ? nome(altra) : (e.other ?? ''), max: v.maxLength ?? '' })
  // the setting's own validator says why (a Kubernetes quantity, a time zone): in English, but precise
  return (e.code === 'invalid' || e.code === 'too_small') && e.message ? `${frase}: ${e.message}` : frase
}

// what the page itself can tell is wrong (it blocks «Save»); the server's refusals are
// shown too but do not block a new try (an engine that was not answering may now)
function problemaLocale(v: PanelSetting): string {
  const k = key(v)
  if (k in testo.value) {
    const x = valore(v)
    if (numerico(v)) {
      if (x === undefined) return messaggioDi(v, { key: v.key, code: v.kind === 'int' ? 'integer' : 'number' })
      if (v.kind === 'int' && !Number.isInteger(x)) return messaggioDi(v, { key: v.key, code: 'integer' })
      if (v.min !== null && (x as number) < v.min) return messaggioDi(v, { key: v.key, code: 'min', limit: v.min })
      if (v.max !== null && (x as number) > v.max) return messaggioDi(v, { key: v.key, code: 'max', limit: v.max })
    } else if (v.kind !== 'bool') {
      const s = x as string
      if (v.maxLength !== null && s.length > v.maxLength) return messaggioDi(v, { key: v.key, code: 'too_long' })
      if (v.kind !== 'str' && !s) return messaggioDi(v, { key: v.key, code: 'invalid' })
    }
  }
  return ''
}

function problema(v: PanelSetting): string {
  const r = rifiuti.value[key(v)]
  return problemaLocale(v) || (r ? messaggioDi(v, r) : '')
}

const cambi = computed(() => {
  const out: { gateway: Record<string, unknown>, engine: Record<string, unknown> } = { gateway: {}, engine: {} }
  for (const v of items.value) {
    const k = key(v)
    if (azzera.value.has(k)) {
      if (v.value !== null) out[v.source][v.key] = null
    } else if (k in testo.value) {
      // the shown value typed again (a rounded 1,4305 GiB) is not a change
      if (testo.value[k] === formattato(v, salvato(v))) continue
      const x = valore(v)
      if (x !== undefined && x !== salvato(v)) out[v.source][v.key] = x
    }
  }
  return out
})
const numeroCambi = computed(() => Object.keys(cambi.value.gateway).length + Object.keys(cambi.value.engine).length)

// Changes typed and not saved were lost without a word on going to another section (the
// panel asks, told by «dirty»), to another page, or closing the tab.
watch(numeroCambi, (n) => emit('dirty', n > 0), { immediate: true })
onBeforeRouteLeave(() => !numeroCambi.value || confirm(t('panelSettings.leaveConfirm')))
function avvisaUscita(e: BeforeUnloadEvent) {
  if (numeroCambi.value) e.preventDefault()
}
onMounted(() => window.addEventListener('beforeunload', avvisaUscita))
onBeforeUnmount(() => {
  window.removeEventListener('beforeunload', avvisaUscita)
  emit('dirty', false)
})
const nonSalvabile = computed(() => items.value.some((v) => key(v) in testo.value && !!problemaLocale(v)))
const daSalvare = (v: PanelSetting) => v.key in cambi.value[v.source]
const modificabile = (v: PanelSetting) => editable.value && !v.off

function annulla() {
  testo.value = {}
  azzera.value = new Set()
  rifiuti.value = {}
}

async function salva() {
  if (!numeroCambi.value || nonSalvabile.value) return
  salvando.value = true
  rifiuti.value = {}
  const conRiavvio = items.value.some((v) => daSalvare(v) && v.rollout.length)
  const rifiutate: Record<string, SettingError> = {}
  let altroErrore = ''
  let salvate = 0
  // each side saves all or none: what a refused side had changed stays in the fields
  const fallite = new Set<string>()
  // the lost-run threshold lowered: the engine's hard limit (which must stay under it)
  // goes down first, or the gateway would refuse against the old one
  const stale = items.value.find((v) => v.source === 'gateway' && v.key === 'engine.run_stale_timeout_seconds')
  const nuovaSoglia = stale && 'engine.run_stale_timeout_seconds' in cambi.value.gateway
    ? (cambi.value.gateway['engine.run_stale_timeout_seconds'] ?? stale.envValue) : undefined
  const ordine = stale && nuovaSoglia !== undefined && (nuovaSoglia as number) < (salvato(stale) as number)
    ? (['engine', 'gateway'] as const) : (['gateway', 'engine'] as const)
  for (const source of ordine) {
    const valori = cambi.value[source]
    if (!Object.keys(valori).length) continue
    try {
      if (source === 'gateway') await api.saveGateway(valori)
      else await api.saveEngine(valori)
      salvate += Object.keys(valori).length
    } catch (e: any) {
      fallite.add(source)
      const errori: SettingError[] | undefined = e?.data?.detail?.errors
      if (errori?.length) for (const x of errori) rifiutate[`${source}:${x.key}`] = x
      else altroErrore = errMessage(e)
    }
  }
  const scritti = { ...testo.value }
  const tornati = new Set(azzera.value)
  await carica()
  if (fallite.size) {
    // the refused side's changes stay in their fields, with the reasons; the other side is saved
    const resta = (k: string) => fallite.has(k.split(':', 1)[0])
    testo.value = Object.fromEntries(Object.entries(scritti).filter(([k]) => resta(k)))
    azzera.value = new Set([...tornati].filter(resta))
    rifiuti.value = rifiutate
    aperti.value = new Set([...aperti.value, ...items.value
      .filter((v) => key(v) in rifiutate).map((v) => `${sectionOf(v)}:${v.group}`)])
    toast.error(altroErrore || t('panelSettings.rejected', { n: salvate }))
  } else {
    annulla()
    toast.success(t(conRiavvio ? 'panelSettings.savedRollout' : 'panelSettings.saved', { seconds: refreshSeconds.value }))
  }
  salvando.value = false
}

// ── the rollout ─────────────────────────────────────────────────────────────

const pendenti = computed(() => (rollout.value?.components ?? []).filter((c) => c.pending))
const inCorso = (c: RolloutComponent) => rollout.value?.mode === 'kubernetes' && !!c.requested_at && c.pending
const daAvviare = computed(() => pendenti.value.filter((c) => !inCorso(c)))
// a setting read at start that its components do not run yet: the gateway says what its
// process started with; for the engine's, the ones set from the panel while a restart is due
const inAttesaDi = (v: PanelSetting) => {
  const dovuti = v.rollout.filter((c) => pendenti.value.some((p) => p.component === c))
  if (!dovuti.length) return []
  if (v.source === 'gateway') return v.startedWith !== salvato(v) ? dovuti : []
  return v.value !== null ? dovuti : []
}
const servizi = computed(() => [...new Set(pendenti.value.map((c) => c.compose_service))])
const comando = computed(() => `docker compose restart ${servizi.value.join(' ')}`)
const comandoKube = computed(() => {
  const ns = rollout.value?.namespace || '<namespace>'
  const rel = rollout.value?.release ? `app.kubernetes.io/instance=${rollout.value.release},` : ''
  return `kubectl rollout restart deployment -n ${ns} -l '${rel}app.kubernetes.io/component in (${pendenti.value.map((c) => c.component).join(',')})'`
})
// the components whose pods could not be read: not offered for a restart, the reason said
const conErrore = computed(() => (rollout.value?.components ?? []).filter((c) => c.error))
const conOrchestratore = computed(() => daAvviare.value.some((c) => c.component === 'orchestrator'))
const rolloutModificabile = computed(() => !!rollout.value?.editable)

async function avviaRollout() {
  if (!daAvviare.value.length) return
  avviando.value = true
  try {
    rollout.value = await api.startRollout(daAvviare.value.map((c) => c.component))
    if (rollout.value.mode === 'kubernetes') toast.success(t('panelSettings.rollout.started'))
  } catch (e) {
    toast.error(errMessage(e))
  } finally {
    avviando.value = false
  }
}

async function fermaRollout(c: RolloutComponent) {
  try {
    rollout.value = await api.cancelRollout(c.component)
  } catch (e) {
    toast.error(errMessage(e))
  }
}

const comandoEl = ref<HTMLElement | null>(null)
async function copia() {
  try {
    await navigator.clipboard.writeText(rollout.value?.mode === 'compose' ? comando.value : comandoKube.value)
    toast.success(t('panelSettings.rollout.copied'))
  } catch {
    // no clipboard on a plain-http address: the command selected, for Ctrl+C
    const el = comandoEl.value
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
let finito = false
async function segui() {
  if (finito) return
  const corre = !!rollout.value?.components.some(inCorso)
  if ((corre || pendenti.value.length) && !document.hidden) {
    try {
      rollout.value = await api.rollout()
    } catch { /* the gateway pod serving us may be the one restarting: next time */ }
  }
  if (!finito) timer = setTimeout(segui, rollout.value?.components.some(inCorso) ? 3000 : 15000)
}
onMounted(() => { timer = setTimeout(segui, 3000) })
onBeforeUnmount(() => { finito = true; if (timer) clearTimeout(timer) })

const fusi = computed(() => {
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
    <p v-if="!panelEnabled" class="nota">{{ $t('engineSettings.lockedByEnv') }}</p>
    <p v-else-if="!caricando && !editable" class="nota">{{ $t('engineSettings.readOnly') }}</p>
  </div>

  <p v-if="caricando && !items.length" class="muted small"><LoaderCircle :size="13" class="spin" /> {{ $t('engineSettings.loading') }}</p>
  <p v-else-if="errore" class="card errore-pagina" role="alert">{{ errore }}</p>

  <template v-else>
    <p v-for="c in conErrore" :key="`err-${c.component}`" class="nota" role="status">
      {{ $t('panelSettings.rollout.podsError', { component: componente(c.component), error: c.error }) }}
    </p>
    <section v-if="pendenti.length" class="card rollout" :aria-label="$t('panelSettings.rollout.region')">
      <h4><RefreshCw :size="14" class="icona-rollout" /> {{ $t('panelSettings.rollout.title', { n: pendenti.length }, pendenti.length) }}</h4>
      <p class="hint small">{{ pendenti.map((c) => componente(c.component)).join(', ') }}</p>

      <template v-if="rollout?.mode === 'kubernetes'">
        <ul v-if="pendenti.some(inCorso)" class="in-corso">
          <li v-for="c in pendenti.filter(inCorso)" :key="c.component">
            <LoaderCircle :size="13" class="spin" />
            <span>
              <strong>{{ componente(c.component) }}</strong> ·
              {{ $t('panelSettings.rollout.left', { old: c.old ?? 0, pods: c.pods ?? 0 }) }}
              <template v-if="c.waiting_for.length"> · {{ $t('panelSettings.rollout.waitingFor', { pods: c.waiting_for.join(', ') }) }}</template>
            </span>
            <button v-if="rolloutModificabile" type="button" class="torna" @click="fermaRollout(c)"><X :size="12" /> {{ $t('panelSettings.rollout.stop') }}</button>
          </li>
        </ul>
        <template v-if="daAvviare.length">
          <p class="hint muted small">{{ $t('panelSettings.rollout.howKube') }}</p>
          <p v-if="conOrchestratore" class="nota">{{ $t('panelSettings.rollout.orchestratorWarning') }}</p>
          <div v-if="rolloutModificabile" class="azioni">
            <button type="button" class="primary" :disabled="avviando || numeroCambi > 0" @click="avviaRollout">
              <LoaderCircle v-if="avviando" :size="13" class="spin" /><RefreshCw v-else :size="13" />
              {{ $t('panelSettings.rollout.start', { n: daAvviare.length }, daAvviare.length) }}
            </button>
            <span v-if="numeroCambi" class="muted small">{{ $t('panelSettings.rollout.saveFirst') }}</span>
          </div>
        </template>
      </template>

      <template v-else-if="rollout">
        <p class="hint muted small">{{ $t(rollout.mode === 'compose' ? 'panelSettings.rollout.howCompose' : 'panelSettings.rollout.howManual') }}</p>
        <div class="comando">
          <code ref="comandoEl">{{ rollout.mode === 'compose' ? comando : comandoKube }}</code>
          <button type="button" class="torna" @click="copia">{{ $t('panelSettings.rollout.copy') }}</button>
        </div>
        <div v-if="rolloutModificabile" class="azioni">
          <button type="button" :disabled="avviando" @click="avviaRollout">{{ $t('panelSettings.rollout.done') }}</button>
        </div>
      </template>
    </section>

    <div class="cerca">
      <Search :size="14" />
      <input v-model="cerca" type="search" :placeholder="$t('panelSettings.search')" :aria-label="$t('panelSettings.search')" />
    </div>
    <p v-if="cerca.trim() && !scatole.length" class="muted small">{{ $t('panelSettings.noMatch') }}</p>

    <section v-for="s in scatole" :key="s.section" class="sezione" :aria-labelledby="`sezione-${s.section}`">
      <h3 :id="`sezione-${s.section}`">{{ $t(`panelSettings.sections.${s.section}.title`) }}</h3>
      <p class="hint muted small">{{ $t(`panelSettings.sections.${s.section}.lead`) }}</p>
      <p v-if="s.section === 'engine' && engineError" class="card errore-pagina" role="alert">{{ $t('panelSettings.engineDown', { error: engineError }) }}</p>

      <details
        v-for="g in s.groups"
        :key="g.id"
        class="scatola"
        :class="{ spenta: g.items.every((v) => v.off) }"
        :open="aperto(g.id) || nelGruppoErrori(g.items)"
        @toggle="apriChiudi(g.id, $event)"
      >
        <summary>
          <ChevronRight :size="14" class="freccia" />
          <component :is="ICONE[g.group] ?? SlidersHorizontal" :size="15" class="icona" />
          <span class="titolo">
            <span class="nome-gruppo">{{ gruppoTitolo(s.section, g.group) }}</span>
            <span class="lead">{{ gruppoLead(s.section, g.group) }}</span>
          </span>
          <span class="conti">
            <span v-if="nelGruppoRollout(g.items)" class="icona-rollout" :title="$t('panelSettings.groupHasRollout')"><RefreshCw :size="12" /></span>
            <span v-if="nelGruppoDaSalvare(g.items)" class="chip da-salvare">{{ $t('panelSettings.unsavedN', { n: nelGruppoDaSalvare(g.items) }) }}</span>
            <span v-if="nelGruppoDalPannello(g.items)" class="chip dal-pannello">{{ $t('panelSettings.fromPanelN', { n: nelGruppoDalPannello(g.items) }) }}</span>
            <span class="muted small">{{ g.items.length }}</span>
          </span>
        </summary>

        <p v-if="g.items.every((v) => v.off)" class="nota">{{ $t('engineSettings.unavailable') }}</p>

        <div v-for="v in g.items" :key="key(v)" class="voce">
          <div class="descrizione">
            <label :for="idCampo(v)" class="nome">
              {{ nome(v) }}
              <span
                v-if="v.rollout.length"
                class="icona-rollout"
                role="img"
                :aria-label="$t('panelSettings.appliesAfter', { components: v.rollout.map(componente).join(', ') })"
                :title="$t('panelSettings.appliesAfter', { components: v.rollout.map(componente).join(', ') })"
              ><RefreshCw :size="12" /></span>
            </label>
            <p class="aiuto">{{ aiuto(v) }}</p>
            <p class="meta">
              {{ $t('engineSettings.envValue', { value: mostra(v, v.envValue) }) }}
              <code>{{ v.envVar }}</code>
              <template v-if="v.value !== null && v.updatedBy">
                · {{ $t('engineSettings.setBy', { who: v.updatedBy, when: fmt.ago(v.updatedAt) }) }}
              </template>
            </p>
          </div>

          <div class="valore">
            <label v-if="v.kind === 'bool'" class="interruttore" :class="{ modificato: daSalvare(v) }">
              <input
                :id="idCampo(v)"
                type="checkbox"
                role="switch"
                :checked="acceso(v)"
                :disabled="!modificabile(v) || salvando"
                @change="scrivi(v, String(($event.target as HTMLInputElement).checked))"
              />
              <span>{{ acceso(v) ? $t('panelSettings.on') : $t('panelSettings.off') }}</span>
            </label>
            <div v-else class="campo" :class="{ modificato: daSalvare(v), sbagliato: !!problema(v), testo: !numerico(v) }">
              <input
                :id="idCampo(v)"
                type="text"
                :inputmode="numerico(v) ? 'decimal' : 'text'"
                autocomplete="off"
                :spellcheck="false"
                :list="v.kind === 'timezone' && fusi.length ? 'fusi-orari' : undefined"
                :placeholder="v.kind === 'cpu' ? '2, 500m' : v.kind === 'memory' ? '4Gi, 512Mi' : mostrato(v) === null ? $t('panelSettings.automatic') : ''"
                :value="campo(v)"
                :disabled="!modificabile(v) || salvando"
                :aria-invalid="!!problema(v)"
                :aria-describedby="numerico(v) ? `${idCampo(v)}-intervallo` : undefined"
                @input="scrivi(v, ($event.target as HTMLInputElement).value)"
              />
              <span v-if="unita(v)" class="unita">{{ unita(v) }}</span>
            </div>
            <p v-if="numerico(v) && v.min !== null && v.max !== null" :id="`${idCampo(v)}-intervallo`" class="intervallo">
              {{ $t('engineSettings.range', { min: mostra(v, v.min), max: mostra(v, v.max) }) }}
            </p>
            <p v-if="problema(v)" class="errore" role="alert">{{ problema(v) }}</p>
            <div class="stato">
              <span v-if="daSalvare(v)" class="chip da-salvare">{{ $t('engineSettings.unsaved') }}</span>
              <span v-else-if="v.value !== null" class="chip dal-pannello">{{ $t('engineSettings.fromPanel') }}</span>
              <span v-if="!daSalvare(v) && inAttesaDi(v).length" class="chip attesa" :title="$t('panelSettings.waitingRolloutHint')">
                {{ $t('panelSettings.waitingRollout') }}
              </span>
              <span
                v-else-if="v.value !== null && v.applied !== v.value && !v.rollout.length && !daSalvare(v)"
                class="chip non-applicata"
                :title="v.source === 'gateway' ? $t('panelSettings.notAppliedGatewayHint', { seconds: refreshSeconds }) : $t('engineSettings.notAppliedHint')"
              >{{ $t('engineSettings.notApplied') }}</span>
              <button
                v-if="editable && ((v.value !== null && !azzera.has(key(v))) || (modificabile(v) && key(v) in testo))"
                type="button"
                class="torna"
                :disabled="salvando"
                @click="torna(v)"
              >
                <RotateCcw :size="12" /> {{ v.value !== null ? $t('engineSettings.resetToEnv') : $t('engineSettings.undo') }}
              </button>
            </div>
          </div>
        </div>
      </details>
    </section>

    <datalist v-if="fusi.length" id="fusi-orari">
      <option v-for="z in fusi" :key="z" :value="z" />
    </datalist>

    <div v-if="numeroCambi" class="barra" role="region" :aria-label="$t('engineSettings.pendingRegion')">
      <span>{{ $t('engineSettings.changes', { n: numeroCambi }, numeroCambi) }}</span>
      <button type="button" :disabled="salvando" @click="annulla">{{ $t('engineSettings.cancel') }}</button>
      <button type="button" class="primary" :disabled="salvando || nonSalvabile" @click="salva">
        <LoaderCircle v-if="salvando" :size="13" class="spin" /> {{ $t('engineSettings.save') }}
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
.nota { margin: 0; font-size: 12.5px; padding: 8px 10px; border-radius: 8px; background: var(--bg-soft); border: 1px solid var(--border); }
.errore-pagina { color: var(--danger); font-size: 13px; }
.icona-rollout { display: inline-flex; align-items: center; color: var(--accent-hi); }

.rollout { border-color: color-mix(in srgb, var(--accent) 45%, var(--border)); background: color-mix(in srgb, var(--accent) 6%, var(--panel)); }
.rollout .in-corso { margin: 0; padding: 0; list-style: none; display: flex; flex-direction: column; gap: 6px; font-size: 12.5px; }
.rollout .in-corso li { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.azioni { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.comando { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.comando code { font-size: 12px; padding: 6px 9px; border-radius: 7px; background: var(--bg-soft); border: 1px solid var(--border-soft); overflow-wrap: anywhere; }

.cerca {
  display: flex; align-items: center; gap: 8px; padding: 0 10px; border: 1px solid var(--control-border);
  border-radius: 8px; background: var(--bg-soft); color: var(--muted); max-width: 420px;
}
.cerca:focus-within { border-color: var(--accent); }
.cerca input { flex: 1; min-width: 0; border: 0; background: transparent; box-shadow: none; padding: 7px 0; }
.cerca input:focus { outline: none; box-shadow: none; }

.sezione { display: flex; flex-direction: column; gap: 8px; }
.sezione h3 { margin: 10px 0 0; font-size: 15px; }

.scatola { background: var(--panel); border: 1px solid var(--border); border-radius: var(--radius); }
.scatola > summary {
  display: flex; align-items: center; gap: 10px; padding: 12px 14px; cursor: pointer; list-style: none;
  border-radius: var(--radius);
}
.scatola > summary::-webkit-details-marker { display: none; }
.scatola > summary:hover { background: var(--bg-soft); }
.scatola > summary:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
.freccia { flex: none; color: var(--muted); transition: transform 0.15s ease; }
.scatola[open] > summary .freccia { transform: rotate(90deg); }
.scatola[open] > summary { border-bottom: 1px solid var(--border-soft); border-radius: var(--radius) var(--radius) 0 0; }
.icona { flex: none; color: var(--muted); }
.titolo { display: flex; flex-direction: column; min-width: 0; flex: 1; }
.nome-gruppo { font-size: 13.5px; font-weight: 600; color: var(--text); }
.lead { font-size: 12px; color: var(--muted); line-height: 1.45; }
.conti { display: inline-flex; align-items: center; gap: 6px; flex: none; }
.scatola > :not(summary) { margin-left: 14px; margin-right: 14px; }
.scatola > .voce:last-child { padding-bottom: 14px; }
.scatola > .nota { margin-top: 12px; }
.scatola.spenta .voce { opacity: 0.6; }

.voce {
  display: grid; grid-template-columns: minmax(0, 1fr) minmax(220px, 280px); gap: 8px 24px;
  padding: 14px 0 4px; border-top: 1px solid var(--border-soft);
}
.scatola > summary + .voce, .scatola > .nota + .voce { border-top: 0; }
.descrizione { min-width: 0; }
.nome { display: inline-flex; align-items: center; gap: 6px; font-size: 13.5px; font-weight: 600; color: var(--text); }
.aiuto { margin: 4px 0 0; font-size: 12.5px; line-height: 1.55; color: var(--muted); max-width: 72ch; }
.meta { margin: 6px 0 0; font-size: 11.5px; color: var(--muted); overflow-wrap: anywhere; }
.meta code { font-size: 11px; padding: 1px 5px; border-radius: 5px; background: var(--bg-soft); border: 1px solid var(--border-soft); }

.valore { display: flex; flex-direction: column; gap: 4px; }
.campo {
  display: flex; align-items: stretch; border: 1px solid var(--control-border); border-radius: 8px;
  background: var(--bg-soft); overflow: hidden;
}
.campo:focus-within { border-color: var(--accent); box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 22%, transparent); }
.campo.modificato { border-color: color-mix(in srgb, var(--accent) 60%, var(--control-border)); }
.campo.sbagliato { border-color: var(--danger); }
.campo input {
  flex: 1; min-width: 0; border: 0; background: transparent; box-shadow: none; padding: 7px 9px;
  font-variant-numeric: tabular-nums; text-align: right;
}
.campo.testo input { text-align: left; }
.campo input:focus { outline: none; box-shadow: none; }
.unita { display: inline-flex; align-items: center; padding: 0 10px; font-size: 12px; color: var(--muted); border-left: 1px solid var(--border-soft); white-space: nowrap; }
.interruttore { display: inline-flex; align-items: center; gap: 8px; font-size: 13px; min-height: 34px; cursor: pointer; }
.interruttore.modificato span { color: var(--accent-hi); }
.interruttore input { width: 16px; height: 16px; accent-color: var(--accent); }
.intervallo { margin: 0; font-size: 11.5px; color: var(--muted); }
.errore { margin: 0; font-size: 12px; color: var(--danger); }
.stato { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; min-height: 22px; }
.chip { font-size: 10.5px; letter-spacing: 0.02em; border-radius: 999px; padding: 1px 8px; border: 1px solid var(--border); background: var(--panel-2); color: var(--muted); white-space: nowrap; }
.chip.dal-pannello { color: var(--accent-hi); border-color: color-mix(in srgb, var(--accent) 40%, var(--border)); background: var(--tint-accent); }
.chip.da-salvare { color: var(--text); border-style: dashed; }
.chip.non-applicata { color: var(--danger); border-color: color-mix(in srgb, var(--danger) 45%, var(--border)); }
.chip.attesa { color: var(--accent-hi); border-style: dashed; border-color: color-mix(in srgb, var(--accent) 45%, var(--border)); }
.torna { display: inline-flex; align-items: center; gap: 4px; padding: 0 2px; min-height: 22px; border: 0; background: none; font-size: 12px; color: var(--muted); cursor: pointer; }
.torna:hover:not(:disabled) { color: var(--accent); box-shadow: none; }

.barra {
  position: sticky; bottom: 0; z-index: 2; display: flex; align-items: center; justify-content: flex-end; gap: 10px;
  padding: 10px 14px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--panel);
  box-shadow: 0 -6px 18px color-mix(in srgb, var(--text) 8%, transparent);
}
.barra span { margin-right: auto; font-size: 13px; }

@media (max-width: 720px) {
  .voce { grid-template-columns: 1fr; }
  .scatola > summary { flex-wrap: wrap; }
  .lead { display: none; }
}
</style>
