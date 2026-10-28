<script setup lang="ts">
// A pixel-perfect report on screen (docs/specs/reports.md): its parameters, its pages as
// the PDF lays them out, and what can be done with it — download it as PDF or Excel, take
// the address a script downloads it from, look at its versions, and (with EDIT) change it
// in the designer. It reads the data with the rights of whoever opens it.
//
// - The parameters' values live in the address (`?id=4&region=North`, a list repeats its
//   name): a link opens the same report, and Back goes back to the values before. Only
//   the values that differ from the defaults are written there, so a default such as
//   «the last 30 days» keeps moving with the calendar.
// - A click on the pages (a column's header, a group's row) changes the view on screen:
//   the report is rendered again with that view and the scroll stays where it was. The
//   view is not in the address; «Reset view» drops it, and the downloads take it (they
//   are the report as shown).
//
// /report?id=N
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  ChevronDown, ChevronUp, Download, FileChartColumn, FileSpreadsheet, History, Link, ListRestart, LoaderCircle,
  MoveHorizontal, PencilRuler, RefreshCw, RotateCcw, TriangleAlert, X, ZoomIn, ZoomOut,
} from 'lucide-vue-next'
import { errMessage } from '~/composables/useApi'
import { useFormat } from '~/composables/useFormat'
import { useFolderPaths } from '~/composables/useFolderPaths'
import { useDialogA11y } from '~/composables/useDialogA11y'
import { versionNote } from '~/composables/useSavedScripts'
import {
  isPlainState, stateAfter, useReports,
  type ReportDetail, type ReportFormValue, type ReportHotspot, type ReportParameter, type ReportParams,
  type ReportScreen, type ReportState, type ReportVersion,
} from '~/composables/useReports'
import ReportPages from '~/components/report/ReportPages.vue'
import ReportParamsBar from '~/components/report/ReportParamsBar.vue'
import ReportApiDialog from '~/components/report/ReportApiDialog.vue'
import ApiTokensDialog from '~/components/ui/ApiTokensDialog.vue'

const { t, te } = useI18n()
const fmtx = useFormat()
const toast = useToast()
const route = useRoute()
const router = useRouter()
const api = useReports()
const folderPaths = useFolderPaths()

const reportId = ref<number | null>(null)
const detail = ref<ReportDetail | null>(null)
const loading = ref(true)
const notFound = ref(false)
const loadError = ref('')
const folderPath = computed(() => (detail.value ? folderPaths.pathOf(detail.value.project_id) ?? '' : ''))

// ── the parameters: the form, and the address ────────────────────────────────
const parameters = ref<ReportParameter[]>([])
const form = ref<Record<string, ReportFormValue>>({})
// the values the pages are rendered with: the address's (the form may hold changes not
// viewed yet — a click on the pages must not render those)
const chosen = ref<Record<string, ReportFormValue>>({})
const fieldErrors = ref<Record<string, string>>({})
// a parameter called `id` would meet the report's own: in the address it is `_id`
// (a parameter's name starts with a letter, so `_id` is nobody's)
const urlKey = (name: string) => (name === 'id' ? '_id' : name)
const freeMulti = (p: ReportParameter) => p.multi && !(p.choices && !p.choices_truncated)

function defaultValue(p: ReportParameter): ReportFormValue {
  const d = p.default
  const texts = (d === null || d === undefined ? [] : Array.isArray(d) ? d : [d]).map((v) => String(v))
  if (!p.multi) return texts[0] ?? ''
  return freeMulti(p) ? texts.join(', ') : texts
}
function urlValues(): Record<string, string[]> {
  const out: Record<string, string[]> = {}
  for (const [k, v] of Object.entries(route.query)) {
    if (k === 'id') continue
    const name = k === '_id' ? 'id' : k
    out[name] = (Array.isArray(v) ? v : [v]).filter((x): x is string => typeof x === 'string')
  }
  return out
}
function formFromUrl() {
  const given = urlValues()
  const next: Record<string, ReportFormValue> = {}
  for (const p of parameters.value) {
    const v = given[p.name]
    if (!v || !v.length) next[p.name] = defaultValue(p)
    else if (!p.multi) next[p.name] = v[0]
    else next[p.name] = freeMulti(p) ? v.join(', ') : v
  }
  form.value = next
  chosen.value = { ...next }
}
/** The form as a render takes it: a list where the parameter takes several, nothing for
 *  an empty field (the default then holds). */
function toParams(values: Record<string, ReportFormValue>): ReportParams {
  const out: ReportParams = {}
  for (const p of parameters.value) {
    const v = values[p.name]
    const list = Array.isArray(v) ? v : p.multi ? (v ?? '').split(',') : [v ?? '']
    const clean = list.map((x) => x.trim()).filter((x) => x !== '')
    if (!clean.length) continue
    out[p.name] = p.multi ? clean : clean[0]
  }
  return out
}
const sameValue = (a: ReportFormValue, b: ReportFormValue) => JSON.stringify(a) === JSON.stringify(b)
/** The address of these values: the report, and the values that differ from their defaults. */
function queryFor(values: Record<string, ReportFormValue>): Record<string, string | string[]> {
  const q: Record<string, string | string[]> = { id: String(reportId.value) }
  const params = toParams(values)
  for (const p of parameters.value) {
    const v = params[p.name]
    if (v === undefined || v === null || sameValue(values[p.name], defaultValue(p))) continue
    q[urlKey(p.name)] = Array.isArray(v) ? v.map(String) : String(v)
  }
  return q
}
const queryKey = (q: Record<string, unknown>) =>
  JSON.stringify(Object.keys(q).sort().map((k) => [k, Array.isArray(q[k]) ? q[k] : [q[k]]]))

function missingRequired(values = form.value): string[] {
  const params = toParams(values)
  return parameters.value.filter((p) => p.required && params[p.name] === undefined).map((p) => p.name)
}

// choices that follow another parameter's value: asked again when a value changes
const followsValues = computed(() => parameters.value.length > 1
  && (detail.value?.content.parameters ?? []).some((p) => !!p.choices?.dataset))
let choicesTimer: ReturnType<typeof setTimeout> | null = null
let choicesSeq = 0
async function refreshChoices() {
  if (reportId.value == null || !followsValues.value) return
  const mine = ++choicesSeq
  try {
    const fresh = await api.parameters(reportId.value, toParams(form.value))
    if (mine !== choicesSeq) return
    const byName = new Map(fresh.map((p) => [p.name, p]))
    parameters.value = parameters.value.map((p) => {
      const f = byName.get(p.name)
      return f ? { ...p, choices: f.choices, choices_truncated: f.choices_truncated, choices_error: f.choices_error } : p
    })
  } catch { /* the lists keep what they had */ }
}
function onForm(values: Record<string, ReportFormValue>) {
  const changed = parameters.value.filter((p) => !sameValue(values[p.name], form.value[p.name])).map((p) => p.name)
  form.value = values
  if (changed.length) {
    const errors = { ...fieldErrors.value }
    for (const n of changed) delete errors[n]
    fieldErrors.value = errors
  }
  if (choicesTimer) clearTimeout(choicesTimer)
  choicesTimer = setTimeout(refreshChoices, 400)
}

// ── loading ──────────────────────────────────────────────────────────────────
async function load() {
  const id = Number(route.query.id) || null
  reportId.value = id
  controller?.abort()
  detail.value = null
  parameters.value = []
  screen.value = { pages: [], hotspots: [] }
  warnings.value = []
  problem.value = null
  viewState.value = { items: {} }
  versions.value = null
  notFound.value = false
  loadError.value = ''
  if (id == null) {
    notFound.value = true
    loading.value = false
    return
  }
  loading.value = true
  try {
    const [d, params] = await Promise.all([api.get(id), api.parameters(id)])
    if (reportId.value !== id) return
    detail.value = d
    parameters.value = params
    // the choices that follow a value given in the link
    const given = urlValues()
    const known = Object.fromEntries(Object.entries(given).filter(([k]) => params.some((p) => p.name === k)))
    if (Object.keys(known).length && params.length > 1) {
      try { parameters.value = await api.parameters(id, known) } catch { /* the first answer holds */ }
    }
    formFromUrl()
  } catch (e: any) {
    const code = e?.response?.status ?? e?.statusCode
    if (code === 404) notFound.value = true
    else loadError.value = errMessage(e)
    return
  } finally {
    if (reportId.value === id) loading.value = false
  }
  showPages()
}

// the pages need every required value: without one, the form asks for it first
const waiting = ref(false)
function showPages() {
  const missing = missingRequired(chosen.value)
  waiting.value = missing.length > 0
  if (!waiting.value) renderScreen()
}

// «View report»: the values go in the address (Back returns to these), then the pages
function view() {
  const missing = missingRequired()
  if (missing.length) {
    fieldErrors.value = Object.fromEntries(missing.map((n) => [n, t('report.needsValue')]))
    document.getElementById(`rp-field-${missing[0]}`)?.focus()
    return
  }
  const q = queryFor(form.value)
  if (queryKey(q) === queryKey(route.query as Record<string, unknown>)) {
    chosen.value = { ...form.value }
    renderScreen()
  } else router.push({ path: '/report', query: q })
}

watch(() => route.fullPath, () => {
  if (route.path !== '/report') return
  const id = Number(route.query.id) || null
  if (id !== reportId.value) {
    load()
    return
  }
  if (!detail.value) return
  formFromUrl()
  fieldErrors.value = {}
  refreshChoices()
  showPages()
})

// ── the pages ────────────────────────────────────────────────────────────────
const screen = ref<ReportScreen>({ pages: [], hotspots: [] })
const warnings = ref<string[]>([])
const busy = ref(false)
const applied = ref<ReportParams>({}) // the values of the pages on screen
const viewState = ref<ReportState>({ items: {} })
const announce = ref('')
let controller: AbortController | null = null

interface Problem { kind: string; message: string; parameter?: string }
const problem = ref<Problem | null>(null)
const isAbort = (e: any) => e?.name === 'AbortError' || e?.cause?.name === 'AbortError'

/** What stopped a render, as the person can act on it. */
function problemOf(e: any): Problem {
  const code = e?.response?.status ?? e?.statusCode
  const detail = e?.data?.detail
  const message = errMessage(e)
  if (detail && typeof detail === 'object' && typeof detail.parameter === 'string') {
    return { kind: 'param', message, parameter: detail.parameter }
  }
  if (/more than [\d.,\s ]+ rows/i.test(message)) return { kind: 'rows', message }
  if (/pages, more than|more than \d+ MB|REPORTS__MAX_PAGES|REPORTS__RENDER_MEMORY_MB/i.test(message)) return { kind: 'size', message }
  if (code === 504 || /took longer|REPORTS__RENDER_SECONDS|time limit/i.test(message)) return { kind: 'timeout', message }
  if (code === 503 || /chart service|REPORTS__CHART_URL|could not be drawn/i.test(message)) return { kind: 'charts', message }
  if (/dataset/i.test(message) && (code === 404 || /you can read|cannot read|cannot use|does not exist|no data yet|not found/i.test(message))) {
    return { kind: 'dataset', message }
  }
  if (code === 502) return { kind: 'engine', message }
  return { kind: 'other', message }
}

async function renderScreen(after?: () => string) {
  if (reportId.value == null) return
  controller?.abort()
  const mine = new AbortController()
  controller = mine
  busy.value = true
  problem.value = null
  waiting.value = false
  const params = toParams(chosen.value)
  try {
    const { task_id } = await api.render(reportId.value, {
      format: 'svg', params, state: isPlainState(viewState.value) ? null : viewState.value,
    }, mine.signal)
    const outcome = await api.followRender(task_id, { signal: mine.signal })
    const next = await api.screenOf(outcome)
    if (controller !== mine) return
    screen.value = next
    warnings.value = outcome.warnings
    applied.value = params
    fieldErrors.value = {}
    if (after) announce.value = after()
  } catch (e) {
    if (isAbort(e) || controller !== mine) return
    const p = problemOf(e)
    problem.value = p
    if (p.parameter) fieldErrors.value = { ...fieldErrors.value, [p.parameter]: p.message }
  } finally {
    if (controller === mine) {
      busy.value = false
      controller = null
    }
  }
}

// a click on the pages: the view changes, the report is rendered again with it
function columnName(h: ReportHotspot): string {
  const item = detail.value?.content.sections.body.items.find((i) => i.id === h.item)
  const header = Array.isArray(item?.columns) && h.column != null ? item!.columns[h.column]?.header : ''
  return header || t('report.columnN', { n: (h.column ?? 0) + 1 })
}
function onHotspot(h: ReportHotspot) {
  viewState.value = stateAfter(viewState.value, h)
  const sort = viewState.value.items[h.item]?.sort
  const group = String(h.key?.[h.key.length - 1] ?? t('report.emptyGroup'))
  renderScreen(() => h.kind === 'sort'
    ? (sort ? t(sort.dir === 'asc' ? 'report.sortedAsc' : 'report.sortedDesc', { name: columnName(h) }) : t('report.sortCleared'))
    : t(h.closed ? 'report.groupOpened' : 'report.groupClosed', { name: group }))
}
const viewChanged = computed(() => !isPlainState(viewState.value))
function resetView() {
  viewState.value = { items: {} }
  renderScreen(() => t('report.viewReset'))
}

// ── zoom and pages ───────────────────────────────────────────────────────────
const ZOOM_KEY = 'tabularia-report-zoom'
const STEPS = [0.25, 0.33, 0.5, 0.67, 0.75, 0.9, 1, 1.1, 1.25, 1.5, 1.75, 2, 2.5, 3, 4]
const zoom = ref<number | 'fit'>('fit')
const scale = ref(1)
const pagesRef = ref<InstanceType<typeof ReportPages> | null>(null)
const currentPage = ref(1)
const pageCount = computed(() => screen.value.pages.length)
const pageInput = ref('1')
watch(currentPage, (n) => { pageInput.value = String(n) })
watch(pageCount, () => { currentPage.value = Math.min(currentPage.value, Math.max(1, pageCount.value)) })

function setZoom(z: number | 'fit') {
  zoom.value = z
  try { localStorage.setItem(ZOOM_KEY, String(z)) } catch { /* the choice lasts this visit */ }
}
function zoomBy(dir: 1 | -1) {
  const s = scale.value
  const next = dir > 0 ? STEPS.find((x) => x > s + 0.001) : [...STEPS].reverse().find((x) => x < s - 0.001)
  if (next) setZoom(next)
}
function goTo(n: number) {
  if (!pageCount.value) return
  const target = Math.min(Math.max(1, Math.round(n) || 1), pageCount.value)
  pageInput.value = String(target)
  pagesRef.value?.scrollToPage(target)
}

// keys: Page Up/Down a page, Home/End the first and last (on the pages), + − 0 the zoom
function typing(el: EventTarget | null): boolean {
  const e = el as HTMLElement | null
  return !!e?.closest?.('input, textarea, select, [contenteditable="true"], [role="dialog"], [role="combobox"], .sel-panel, .ms-panel')
}
function onKey(e: KeyboardEvent) {
  if (e.ctrlKey || e.metaKey || e.altKey || typing(e.target) || !pageCount.value) return
  const onPages = !!(e.target as HTMLElement | null)?.closest?.('.rp')
  if (e.key === 'PageDown' || e.key === 'PageUp') {
    e.preventDefault()
    goTo(currentPage.value + (e.key === 'PageDown' ? 1 : -1))
  } else if (onPages && (e.key === 'Home' || e.key === 'End')) {
    e.preventDefault()
    goTo(e.key === 'Home' ? 1 : pageCount.value)
  } else if (e.key === '+' || e.key === '=') {
    e.preventDefault()
    zoomBy(1)
  } else if (e.key === '-' || e.key === '_') {
    e.preventDefault()
    zoomBy(-1)
  } else if (e.key === '0') {
    e.preventDefault()
    setZoom('fit')
  }
}

// ── downloads ────────────────────────────────────────────────────────────────
const downloading = ref<'' | 'pdf' | 'xlsx'>('')
async function download(format: 'pdf' | 'xlsx') {
  if (reportId.value == null || downloading.value) return
  downloading.value = format
  try {
    const { task_id } = await api.render(reportId.value, {
      format, params: applied.value, state: isPlainState(viewState.value) ? null : viewState.value,
    })
    const outcome = await api.followRender(task_id)
    outcome.download()
  } catch (e) {
    toast.error(t('report.downloadFailed', { format: format === 'pdf' ? 'PDF' : 'Excel', error: errMessage(e) }))
  } finally {
    downloading.value = ''
  }
}

// ── the address for scripts ──────────────────────────────────────────────────
const apiOpen = ref(false)
const apiCopied = ref(false)
const tokensOpen = ref(false)
async function copyApiLink() {
  if (reportId.value == null) return
  apiCopied.value = false
  try {
    await navigator.clipboard.writeText(api.apiUrl(reportId.value, 'pdf', applied.value))
    apiCopied.value = true
    toast.success(t('report.apiLinkCopied'))
  } catch { /* the dialog shows it, selected */ }
  apiOpen.value = true
}
function openTokens() {
  apiOpen.value = false
  tokensOpen.value = true
}

// ── versions ─────────────────────────────────────────────────────────────────
const versionsOpen = ref(false)
const versions = ref<ReportVersion[] | null>(null)
const versionsError = ref('')
const restoring = ref<number | null>(null)
const drawerEl = ref<HTMLElement | null>(null)
useDialogA11y(drawerEl, () => versionsOpen.value, () => { versionsOpen.value = false })

async function openVersions() {
  versionsOpen.value = true
  if (reportId.value == null) return
  versionsError.value = ''
  try { versions.value = await api.versions(reportId.value) } catch (e) { versionsError.value = errMessage(e) }
}
async function restore(v: ReportVersion) {
  if (reportId.value == null || restoring.value != null || !detail.value?.editable) return
  if (!confirm(t('report.restoreConfirm', { n: v.version }))) return
  restoring.value = v.version
  try {
    detail.value = await api.restore(reportId.value, v.version)
    versions.value = await api.versions(reportId.value)
    toast.success(t('report.restored', { n: v.version }))
    // another version may ask other parameters
    parameters.value = await api.parameters(reportId.value, {})
    formFromUrl()
    viewState.value = { items: {} }
    showPages()
  } catch (e) {
    toast.error(errMessage(e))
  } finally {
    restoring.value = null
  }
}

// ── the page's life ──────────────────────────────────────────────────────────
onMounted(() => {
  try {
    const z = localStorage.getItem(ZOOM_KEY)
    if (z && z !== 'fit' && Number.isFinite(Number(z))) zoom.value = Math.min(4, Math.max(0.25, Number(z)))
  } catch { /* fit */ }
  document.addEventListener('keydown', onKey)
  folderPaths.load()
  load()
})
onBeforeUnmount(() => {
  controller?.abort()
  if (choicesTimer) clearTimeout(choicesTimer)
  document.removeEventListener('keydown', onKey)
})

const pct = computed(() => `${Math.round(scale.value * 100)}%`)
const problemTitle = computed(() => (problem.value ? t(`report.err_${problem.value.kind}_title`) : ''))
// without parameters, «narrow it down with the parameters» is no advice: what the designer can do
const problemHint = computed(() => {
  if (!problem.value) return ''
  const fixed = `report.err_${problem.value.kind}_hint_fixed`
  return !parameters.value.length && te(fixed) ? t(fixed) : t(`report.err_${problem.value.kind}_hint`)
})
const notesOpen = ref(false)
</script>

<template>
  <AppShell fluid>
    <div class="rv">
      <!-- a report that is not there, or not for this person: the same answer -->
      <div v-if="notFound" class="gone" role="alert">
        <FileChartColumn :size="28" aria-hidden="true" />
        <h1>{{ $t('report.notFoundTitle') }}</h1>
        <p class="muted">{{ $t('report.notFoundBody') }}</p>
        <NuxtLink to="/" class="back">{{ $t('report.backToExplore') }}</NuxtLink>
      </div>
      <div v-else-if="loadError" class="gone" role="alert">
        <TriangleAlert :size="28" aria-hidden="true" />
        <h1>{{ $t('report.loadFailed') }}</h1>
        <p class="muted">{{ loadError }}</p>
        <button type="button" @click="load"><RefreshCw :size="14" /> {{ $t('report.tryAgain') }}</button>
      </div>

      <template v-else>
        <div class="page-head">
          <h1 :title="detail?.name">
            <FileChartColumn :size="18" aria-hidden="true" />
            <span v-if="detail" class="name">{{ detail.name }}</span>
            <span v-else class="sk" style="width: 220px; height: 16px" />
          </h1>
          <span v-if="folderPath" class="where muted" :title="folderPath">{{ $t('report.inFolder', { path: folderPath }) }}</span>
          <div v-if="detail" class="head-actions">
            <button type="button" class="tagbtn" :title="$t('report.versionsTitle')" @click="openVersions">
              <History :size="13" /> v{{ detail.current_version }}
            </button>
            <button type="button" :title="$t('report.copyApiLinkTitle')" @click="copyApiLink">
              <Link :size="14" /> <span class="lbl">{{ $t('report.copyApiLink') }}</span>
            </button>
            <button type="button" :disabled="!!downloading || !pageCount" :title="$t('report.downloadPdfTitle')" :aria-label="$t('report.downloadPdfTitle')" @click="download('pdf')">
              <LoaderCircle v-if="downloading === 'pdf'" :size="14" class="spin" /><Download v-else :size="14" /> PDF
            </button>
            <button type="button" :disabled="!!downloading || !pageCount" :title="$t('report.downloadExcelTitle')" :aria-label="$t('report.downloadExcelTitle')" @click="download('xlsx')">
              <LoaderCircle v-if="downloading === 'xlsx'" :size="14" class="spin" /><FileSpreadsheet v-else :size="14" /> Excel
            </button>
            <button v-if="detail.editable" type="button" class="primary" :title="$t('report.editTitle')" @click="navigateTo(`/report-design?id=${detail.id}`)">
              <PencilRuler :size="14" /> {{ $t('report.edit') }}
            </button>
          </div>
        </div>
        <p v-if="detail?.description" class="desc muted" :title="detail.description">{{ detail.description }}</p>

        <ReportParamsBar
          v-if="parameters.length"
          :parameters="parameters"
          :model-value="form"
          :errors="fieldErrors"
          :busy="busy"
          @update:model-value="onForm"
          @submit="view"
        />

        <section class="frame" :aria-label="$t('report.pagesRegion')" :aria-busy="busy ? 'true' : 'false'">
          <div class="tools">
            <div class="nav" :class="{ off: !pageCount }">
              <button type="button" class="mini icon" :disabled="currentPage <= 1" :title="$t('report.prevPage')" :aria-label="$t('report.prevPage')" @click="goTo(currentPage - 1)">
                <ChevronUp :size="14" />
              </button>
              <label class="pageof">
                <span>{{ $t('report.page') }}</span>
                <input
                  v-model="pageInput"
                  class="pagein"
                  type="text"
                  inputmode="numeric"
                  :disabled="!pageCount"
                  :aria-label="$t('report.pageNumber')"
                  @keydown.enter.prevent="goTo(Number(pageInput))"
                  @change="goTo(Number(pageInput))"
                />
                <span>{{ $t('report.ofPages', { total: pageCount || '–' }) }}</span>
              </label>
              <button type="button" class="mini icon" :disabled="currentPage >= pageCount" :title="$t('report.nextPage')" :aria-label="$t('report.nextPage')" @click="goTo(currentPage + 1)">
                <ChevronDown :size="14" />
              </button>
            </div>

            <div class="mid">
              <button v-if="warnings.length" type="button" class="mini notesbtn" :aria-expanded="notesOpen" @click="notesOpen = !notesOpen">
                <TriangleAlert :size="12" /> {{ $t('report.notes', { n: warnings.length }, warnings.length) }}
              </button>
              <span v-if="busy" class="status muted"><LoaderCircle :size="12" class="spin" /> {{ $t('report.rendering') }}</span>
              <span class="sr-only" role="status" aria-live="polite">{{ busy ? $t('report.rendering') : announce }}</span>
            </div>

            <div class="zoom">
              <button v-if="viewChanged" type="button" class="mini" :title="$t('report.resetViewTitle')" @click="resetView">
                <ListRestart :size="13" /> {{ $t('report.resetView') }}
              </button>
              <span v-if="viewChanged" class="sep" aria-hidden="true" />
              <button type="button" class="mini icon" :title="$t('report.zoomOut')" :aria-label="$t('report.zoomOut')" :disabled="scale <= STEPS[0] + 0.001" @click="zoomBy(-1)">
                <ZoomOut :size="14" />
              </button>
              <button type="button" class="mini pct" :title="$t('report.zoomActual')" :aria-label="$t('report.zoomLevel', { pct })" @click="setZoom(1)">{{ pct }}</button>
              <button type="button" class="mini icon" :title="$t('report.zoomIn')" :aria-label="$t('report.zoomIn')" :disabled="scale >= STEPS[STEPS.length - 1] - 0.001" @click="zoomBy(1)">
                <ZoomIn :size="14" />
              </button>
              <button type="button" class="mini" :class="{ on: zoom === 'fit' }" :aria-pressed="zoom === 'fit'" :title="$t('report.zoomFitTitle')" @click="setZoom('fit')">
                <MoveHorizontal :size="13" /> {{ $t('report.zoomFit') }}
              </button>
            </div>
          </div>
          <ul v-if="notesOpen && warnings.length" class="notes">
            <li v-for="(w, i) in warnings" :key="i">{{ w }}</li>
          </ul>

          <div class="stage">
            <div v-if="problem" class="state" role="alert">
              <TriangleAlert :size="22" class="warnicon" aria-hidden="true" />
              <h2>{{ problemTitle }}</h2>
              <p class="msg">{{ problem.message }}</p>
              <p class="muted hint">{{ problemHint }}</p>
              <button v-if="problem.kind !== 'param'" type="button" @click="renderScreen()"><RefreshCw :size="14" /> {{ $t('report.tryAgain') }}</button>
            </div>
            <div v-else-if="waiting && !busy" class="state">
              <FileChartColumn :size="22" class="muted" aria-hidden="true" />
              <h2>{{ $t('report.needsValuesTitle') }}</h2>
              <p class="muted hint">{{ $t('report.needsValuesBody') }}</p>
            </div>
            <ReportPages
              v-else
              ref="pagesRef"
              :pages="screen.pages"
              :hotspots="screen.hotspots"
              :zoom="zoom"
              :busy="busy || loading"
              :definition="detail?.content ?? null"
              :label="detail ? $t('report.pagesOf', { name: detail.name }) : ''"
              @hotspot="onHotspot"
              @page="(n: number) => (currentPage = n)"
              @scale="(s: number) => (scale = s)"
            />
          </div>
        </section>
      </template>

      <!-- versions: a drawer over the page -->
      <Teleport to="body">
        <template v-if="versionsOpen">
          <div class="drawer-backdrop" @click="versionsOpen = false" />
          <aside ref="drawerEl" class="drawer" role="dialog" aria-modal="true" tabindex="-1" :aria-label="$t('report.versionsTitle')">
            <div class="drawer-head">
              <h2><History :size="15" aria-hidden="true" /> {{ $t('report.versionsTitle') }}</h2>
              <button type="button" class="mini icon" :title="$t('report.close')" :aria-label="$t('report.close')" @click="versionsOpen = false"><X :size="14" /></button>
            </div>
            <p class="muted small">{{ detail?.editable ? $t('report.versionsLead') : $t('report.restoreOnlyEdit') }}</p>
            <p v-if="versionsError" class="err small" role="alert">{{ versionsError }}</p>
            <div v-else-if="versions === null" class="vsk" aria-busy="true">
              <span v-for="i in 3" :key="i" class="sk" style="width: 100%; height: 46px" />
            </div>
            <ol v-else class="versions">
              <li v-for="v in versions" :key="v.version" :class="{ current: v.current }">
                <div class="vtop">
                  <strong>v{{ v.version }}</strong>
                  <span v-if="v.current" class="tag">{{ $t('report.currentVersion') }}</span>
                  <span class="muted small">{{ versionNote(v.note, t) }}</span>
                </div>
                <div class="muted small">{{ fmtx.dateTime(v.created_at) }}<template v-if="v.created_by"> · {{ v.created_by }}</template></div>
                <div v-if="detail?.editable && !v.current" class="vacts">
                  <button type="button" class="mini" :disabled="restoring != null" @click="restore(v)">
                    <LoaderCircle v-if="restoring === v.version" :size="12" class="spin" /><RotateCcw v-else :size="12" />
                    {{ $t('report.restore') }}
                  </button>
                </div>
              </li>
            </ol>
          </aside>
        </template>
      </Teleport>

      <ReportApiDialog
        v-if="detail"
        :open="apiOpen"
        :report-id="detail.id"
        :report-name="detail.name"
        :params="applied"
        :copied="apiCopied"
        @close="apiOpen = false"
        @tokens="openTokens"
      />
      <ApiTokensDialog :open="tokensOpen" @close="tokensOpen = false" />
    </div>
  </AppShell>
</template>

<style scoped>
/* the whole window: the pages scroll inside their frame, the bars above stay */
.rv {
  display: flex; flex-direction: column; gap: 10px; min-height: 0;
  height: calc(100dvh - 52px - 44px); min-height: 460px;
}
.page-head { display: flex; align-items: center; gap: 12px; min-width: 0; }
.page-head h1 {
  display: inline-flex; align-items: center; gap: 8px; margin: 0; min-width: 0;
  font-size: 16px; font-weight: 600; flex: 0 1 auto;
}
.page-head h1 svg { flex-shrink: 0; color: var(--accent); }
.name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.where { font-size: 12.5px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; min-width: 0; flex: 1; }
.head-actions { display: flex; align-items: center; gap: 8px; margin-left: auto; flex-shrink: 0; }
.head-actions button { white-space: nowrap; flex-shrink: 0; }
.tagbtn {
  font-size: 11.5px; padding: 3px 9px; min-height: 26px; color: var(--muted); font-variant-numeric: tabular-nums;
}
.desc { margin: -4px 0 0; font-size: 12.5px; max-width: 110ch; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.frame {
  flex: 1; min-height: 0; display: flex; flex-direction: column;
  border: 1px solid var(--border-soft); border-radius: 10px; overflow: hidden; background: var(--bg);
}
.tools {
  display: flex; align-items: center; gap: 10px; padding: 6px 10px; min-height: 40px; box-sizing: border-box;
  background: var(--panel); border-bottom: 1px solid var(--border-soft);
}
.nav, .zoom { display: flex; align-items: center; gap: 6px; flex-shrink: 0; }
.nav.off { opacity: 0.55; }
.mid { flex: 1; min-width: 0; display: flex; align-items: center; justify-content: center; gap: 10px; }
.icon { padding: 3px 6px; }
.pageof { display: inline-flex; align-items: center; gap: 6px; font-size: 12.5px; color: var(--muted); white-space: nowrap; }
.pagein {
  width: 3.4em; text-align: center; padding: 3px 4px; font-variant-numeric: tabular-nums; font-size: 12.5px;
}
.pct { min-width: 54px; font-variant-numeric: tabular-nums; font-size: 12px; }
.zoom .on { border-color: var(--accent); background: var(--tint-accent); color: var(--accent-hi); }
.zoom button, .nav button { white-space: nowrap; }
.sep { width: 1px; height: 18px; background: var(--border); margin: 0 2px; }
.status { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; white-space: nowrap; }
.notesbtn { color: var(--warning); border-color: color-mix(in srgb, var(--warning) 40%, var(--border)); white-space: nowrap; }
.notes {
  margin: 0; padding: 8px 14px 8px 30px; font-size: 12px; color: var(--muted); line-height: 1.5;
  background: var(--panel); border-bottom: 1px solid var(--border-soft); max-height: 120px; overflow: auto;
}
.stage { flex: 1; min-height: 0; position: relative; display: flex; flex-direction: column; }
.stage > :deep(.rp) { flex: 1; }

.state {
  margin: auto; max-width: 560px; padding: 28px 24px; display: flex; flex-direction: column; align-items: center;
  gap: 8px; text-align: center;
}
.state h2 { margin: 4px 0 0; font-size: 15px; font-weight: 600; }
.state .msg {
  margin: 4px 0 0; padding: 8px 12px; border-radius: 8px; background: var(--panel); border: 1px solid var(--border-soft);
  font-size: 12.5px; line-height: 1.5; overflow-wrap: anywhere; text-align: left;
}
.state .hint { margin: 0; font-size: 12.5px; line-height: 1.5; max-width: 62ch; }
.state button { margin-top: 6px; }
.warnicon { color: var(--warning); }

.gone { margin: 12vh auto 0; max-width: 520px; display: flex; flex-direction: column; align-items: center; gap: 8px; text-align: center; color: var(--muted); }
.gone h1 { margin: 6px 0 0; font-size: 17px; color: var(--text); }
.gone p { margin: 0; line-height: 1.5; }
.back { margin-top: 8px; color: var(--accent-hi); }

.drawer-backdrop { position: fixed; inset: 0; background: var(--scrim); z-index: 120; }
.drawer {
  position: fixed; top: 0; right: 0; bottom: 0; z-index: 121; width: 360px; max-width: 92vw;
  display: flex; flex-direction: column; gap: 10px; padding: 16px; overflow-y: auto; box-sizing: border-box;
  background: var(--panel); border-left: 1px solid var(--border); box-shadow: var(--shadow-2);
}
.drawer-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.drawer-head h2 { margin: 0; font-size: 14px; display: inline-flex; align-items: center; gap: 8px; }
.small { font-size: 12px; margin: 0; line-height: 1.45; }
.err { color: var(--danger); }
.vsk { display: flex; flex-direction: column; gap: 8px; }
.versions { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; }
.versions li { border: 1px solid var(--border-soft); border-radius: 8px; padding: 8px 10px; display: flex; flex-direction: column; gap: 3px; }
.versions li.current { border-color: var(--accent); }
.vtop { display: flex; align-items: center; gap: 7px; flex-wrap: wrap; }
.vacts { display: flex; gap: 6px; margin-top: 4px; }
.vacts .mini { display: inline-flex; align-items: center; gap: 4px; }
.tag {
  font-size: 10px; text-transform: uppercase; letter-spacing: 0.04em; padding: 1px 7px;
  border-radius: 8px; background: var(--panel-2); border: 1px solid var(--border); color: var(--muted);
}

/* narrower: the labels of the secondary actions give way to their icons */
@media (max-width: 1180px) {
  .head-actions .lbl { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); white-space: nowrap; }
  .zoom .on { padding: 3px 6px; }
}
@media (max-width: 900px) {
  .page-head { flex-wrap: wrap; }
  .head-actions { margin-left: 0; }
  .mid .status { display: none; }
}
@media (max-width: 760px) {
  .rv { height: calc(100dvh - 52px - 32px); }
}
</style>
