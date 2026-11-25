<script setup lang="ts">
// The Report tab of the flow editor's preview, for a statistics node (docs/specs/
// statistics.md, «In the editor»): the pages of the test's report, drawn by the report
// engine from the layout preset for that test, on the rows that reach the node — the chain
// its preview reads, sent as it is. Read-only: zoom, the panel at its largest, PDF, Excel.
//
// For a model node (`model`; docs/specs/statistical-models.md, «The report») the pages are
// those of its latest trial or run, drawn from the results the run kept: no run yet, the tab
// says so and offers Run; the node or its chain changed since, the pages stay, marked «Not up
// to date», with Run (a new run, not a new drawing: the results are the run's).
//
// - It is drawn when the tab is opened for a node. When the node is set differently, or
//   something before it changes, the pages stay, marked «not up to date», with «Update»:
//   never a render at each keystroke. A language asked of the interface is drawn at once.
// - No control chooses other rows: a development sample is the source's, and the preview's
//   bar above says it, as the page's header does.
// - The engine hands a render's file once: the pages are kept here, and in `memory` (the
//   editor's), so going back to a node shows its report at once. The PDF and the Excel are
//   requests of their own, for the pages on screen.
import { computed, onBeforeUnmount, ref, shallowRef, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  CircleAlert, FileSpreadsheet, FileText, FlaskConical, History, LoaderCircle, Maximize2, Minimize2, MoveHorizontal, Play, RefreshCw,
  ZoomIn, ZoomOut,
} from 'lucide-vue-next'
import ReportPages from '~/components/report/ReportPages.vue'
import { errMessage } from '~/composables/useApi'
import { useFormat } from '~/composables/useFormat'
import type { Recent } from '~/composables/previewGrid'
import {
  STAT_REPORT_SETTLE_MS, STAT_REPORT_ZOOM_KEY, STAT_REPORT_ZOOMS, statReportCancelPath, statModelReportKey, statModelReportRequest, statReportKey, statReportProblem,
  statReportRequest, statReportRetryable, statReportShouldDraw, statReportState, statReportUnset, statReportZoomRead,
  statReportZoomStep, type StatModelReportTarget, type StatModelRuns, type StatModelView, type StatReportChain, type StatReportProblem,
  type StatReportShown,
} from '~/composables/statReport'

const props = withDefaults(defineProps<{
  /** the statistics node the tab is about */
  nodeId: string | null
  /** the chain its preview reads, or null when the node has no data to read */
  chain: StatReportChain | null
  /** the name the person gave the node, printed in the page's header (none: the test and
   *  its columns are there already) */
  step?: string
  /** the preview panel is at its largest height */
  expanded?: boolean
  /** the editor's memory of the pages already drawn */
  memory?: Recent<StatReportShown> | null
  /** a model node's runs: its report is its latest trial's or run's (`chain` is then not read) */
  model?: StatModelRuns | null
}>(), { step: '', expanded: false, memory: null, model: null })

const emit = defineEmits<{ (e: 'expand'): void; (e: 'run'): void; (e: 'view', v: StatModelView): void }>()

const { t, locale } = useI18n()
const { apiFetch } = useApiClient()
const reports = useReports()
const fmt = useFormat()
const toast = useToast()

// a model node's: the run whose results are drawn
const target = computed<StatModelReportTarget | null>(() => (props.model && props.nodeId && props.model.runId !== null
  ? { flowId: props.model.flowId, nodeId: props.nodeId, runId: props.model.runId } : null))
const key = computed(() => (props.model ? (target.value ? statModelReportKey(target.value, locale.value) : null)
  : props.chain ? statReportKey(props.chain, locale.value, props.step) : null))
const unset = computed(() => (props.model ? !target.value : statReportUnset(props.chain?.operations)))

const shown = shallowRef<StatReportShown | null>(null)
const busy = ref(false)
const problem = ref<StatReportProblem | null>(null)
const failedKey = ref<string | null>(null)
const announce = ref('')
let controller: AbortController | null = null

const state = computed(() => statReportState({
  unset: unset.value, busy: busy.value, key: key.value, shownKey: shown.value?.key ?? null, failedKey: failedKey.value,
}))

// ── the seconds of a render that takes a while ───────────────────────────────
const seconds = ref(0)
let clock: ReturnType<typeof setInterval> | undefined
function startClock() {
  const since = Date.now()
  seconds.value = 0
  clearInterval(clock)
  clock = setInterval(() => { seconds.value = Math.floor((Date.now() - since) / 1000) }, 500)
}
function stopClock() {
  clearInterval(clock)
  clock = undefined
}

const isAbort = (e: any) => e?.name === 'AbortError' || e?.cause?.name === 'AbortError'

/** The request of a render: a model node's run, or a statistics node's chain. */
function ask(of: { chain: StatReportChain | null; model?: StatModelReportTarget; locale: string; step: string },
  format: 'svg' | 'pdf' | 'xlsx', signal?: AbortSignal) {
  return of.model
    ? apiFetch<{ task_id: string }>('/tasks/model-report', { method: 'POST', body: statModelReportRequest(of.model, format, of.locale), signal })
    : apiFetch<{ task_id: string }>('/tasks/stat-report', { method: 'POST', body: statReportRequest(of.chain!, format, of.locale, of.step), signal })
}

async function draw() {
  const k = key.value
  if (!k || unset.value || (!props.chain && !target.value)) return
  controller?.abort()
  const mine = new AbortController()
  controller = mine
  // the request as it is now: the node may be set differently before the pages arrive
  const chain = props.model || !props.chain ? null : JSON.parse(JSON.stringify(props.chain)) as StatReportChain
  const model = target.value ? { ...target.value } : undefined
  const lang = locale.value
  const step = props.step
  busy.value = true
  problem.value = null
  failedKey.value = null
  startClock()
  let started: string | null = null
  try {
    const { task_id } = await ask({ chain, model, locale: lang, step }, 'svg', mine.signal)
    started = task_id
    // left while the request was on its way: the render is stopped on the server too
    if (controller !== mine) { cancelRender(task_id); return }
    const outcome = await reports.followRender(task_id, { signal: mine.signal })
    const screen = await reports.screenOf(outcome)
    const entry: StatReportShown = { key: k, pages: screen.pages, chain, ...(model ? { model } : {}), locale: lang, step }
    props.memory?.set(k, entry)
    if (controller !== mine) return
    shown.value = entry
    announce.value = t('statReport.drawn', { n: entry.pages.length }, entry.pages.length)
  } catch (e) {
    if (isAbort(e) || controller !== mine) {
      // the tab left it: the server stops it too, and gives the person's place back
      if (started) cancelRender(started)
      return
    }
    // the pages of the node as it was go: the answer to «Update» is this sentence
    shown.value = null
    problem.value = statReportProblem(e, errMessage(e))
    failedKey.value = k
    announce.value = ''
  } finally {
    if (controller === mine) {
      busy.value = false
      controller = null
      stopClock()
    }
  }
}

/** «Stop» on a render nobody waits for any more: it would run on the server up to an hour and
 *  hold one of the person's places. The answer is of no use; a render that ended is left as it is. */
function cancelRender(taskId: string) {
  apiFetch(statReportCancelPath(taskId), { method: 'POST' }).catch(() => { /* it ends by itself */ })
}

// a node clicked after another within a moment is not drawn: a click in a row on several nodes
// asks for the report of the last
let settle: ReturnType<typeof setTimeout> | undefined
function drawSoon() {
  clearTimeout(settle)
  busy.value = !unset.value
  settle = setTimeout(() => { settle = undefined; busy.value = false; draw() }, STAT_REPORT_SETTLE_MS)
}

function stop() {
  clearTimeout(settle)
  settle = undefined
  controller?.abort()
  controller = null
  busy.value = false
  stopClock()
}

// the tab opened, another node, another language: its pages from memory, or drawn
let opening = true
watch([() => props.nodeId, locale], ([node, lang], before) => {
  const was = before ? { node: before[0] as string | null, locale: String(before[1]) } : null
  const first = opening
  opening = false
  if (!statReportShouldDraw({ node, locale: String(lang) }, was)) return
  stop()
  problem.value = null
  failedKey.value = null
  const hit = key.value ? props.memory?.get(key.value) : undefined
  if (hit) {
    shown.value = hit
    return
  }
  // another node's pages are not this one's; the same node's in another language stay,
  // dimmed, until the new ones come
  if (!was || was.node !== node) shown.value = null
  // the tab opening, a model node (a run asked for on purpose) or the same node in another language: at once
  if (first || props.model || was?.node === node) draw()
  else drawSoon()
}, { immediate: true })

// the same node, set differently: pages this page has drawn already are shown at once (an
// undo); a node set for the first time is drawn; anything else waits for «Update»
watch(key, (k) => {
  if (!k || busy.value) return
  const hit = props.memory?.get(k)
  if (hit) {
    problem.value = null
    failedKey.value = null
    shown.value = hit
  } else if (!unset.value && (props.model || (!shown.value && !problem.value))) {
    // a model node's new run (its Run pressed on purpose) is drawn at once
    draw()
  }
})

// a model node's pages are of a run older than the node as it is: they stay, with Run
const modelStale = computed(() => !!props.model?.stale && state.value === 'ready')

onBeforeUnmount(() => {
  clearTimeout(settle)
  controller?.abort()
  stopClock()
})

// ── zoom and pages ───────────────────────────────────────────────────────────
function storedZoom(): number | 'fit' {
  try { return statReportZoomRead(localStorage.getItem(STAT_REPORT_ZOOM_KEY)) } catch { return statReportZoomRead(null) }
}
const zoom = ref<number | 'fit'>(storedZoom())
const scale = ref(typeof zoom.value === 'number' ? zoom.value : 1)
const currentPage = ref(1)
const pageCount = computed(() => shown.value?.pages.length ?? 0)
const pct = computed(() => `${Math.round(scale.value * 100)}%`)
function setZoom(z: number | 'fit') {
  zoom.value = z
  try { localStorage.setItem(STAT_REPORT_ZOOM_KEY, String(z)) } catch { /* the choice lasts this visit */ }
}
function zoomBy(dir: 1 | -1) {
  const next = statReportZoomStep(scale.value, dir)
  if (next) setZoom(next)
}

// ── downloads: the pages on screen, in another format ───────────────────────
const downloading = ref<'' | 'pdf' | 'xlsx'>('')
async function download(format: 'pdf' | 'xlsx') {
  const of = shown.value
  if (!of || downloading.value) return
  downloading.value = format
  try {
    const { task_id } = await ask(of, format)
    const outcome = await reports.followRender(task_id)
    // the file as a Blob, saved from here: a link to the gateway is a navigation when the
    // gateway is another origin, and the editor would ask to confirm leaving an unsaved flow
    const blob = await apiFetch<Blob>(`/tasks/exports/${encodeURIComponent(outcome.task_id)}/file`, { responseType: 'blob' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = outcome.filename || `report.${format}`
    a.click()
    setTimeout(() => URL.revokeObjectURL(url), 60_000)
    announce.value = t('statReport.downloaded', { filename: outcome.filename })
  } catch (e) {
    toast.error(t('report.downloadFailed', { format: format === 'pdf' ? 'PDF' : 'Excel', error: errMessage(e) }))
  } finally {
    downloading.value = ''
  }
}

const problemTitle = computed(() => {
  const kind = problem.value?.kind
  return kind && kind !== 'refusal' && kind !== 'unset' && kind !== 'other' && kind !== 'invalid' ? t(`statReport.problems.${kind}`) : ''
})
</script>

<template>
  <section class="sr" :aria-label="model ? $t('modelReport.region') : $t('statReport.region')">
    <div data-fin="bar" class="sr-tools">
      <div class="sr-group" role="group" :aria-label="$t('statReport.zoomGroup')">
        <button type="button" class="mini sr-icon" :title="$t('statReport.zoomOut')" :aria-label="$t('statReport.zoomOut')" :disabled="scale <= STAT_REPORT_ZOOMS[0] + 0.001" @click="zoomBy(-1)">
          <ZoomOut :size="14" aria-hidden="true" />
        </button>
        <button type="button" class="mini sr-pct" :title="$t('statReport.zoomActual')" :aria-label="$t('statReport.zoomLevel', { pct })" @click="setZoom(1)">{{ pct }}</button>
        <button type="button" class="mini sr-icon" :title="$t('statReport.zoomIn')" :aria-label="$t('statReport.zoomIn')" :disabled="scale >= STAT_REPORT_ZOOMS[STAT_REPORT_ZOOMS.length - 1] - 0.001" @click="zoomBy(1)">
          <ZoomIn :size="14" aria-hidden="true" />
        </button>
        <button type="button" class="mini sr-fit" :class="{ on: zoom === 'fit' }" :aria-pressed="zoom === 'fit'" :title="$t('statReport.fitWidthTitle')" :aria-label="$t('statReport.fitWidth')" @click="setZoom('fit')">
          <MoveHorizontal :size="13" aria-hidden="true" /><span class="sr-long">{{ $t('statReport.fitWidth') }}</span>
        </button>
        <span v-if="pageCount" class="sr-pageof">{{ $t('statReport.pageOf', { n: Math.min(currentPage, pageCount), total: pageCount }) }}</span>
      </div>

      <div class="sr-mid">
        <!-- a Bayesian model with a prior predictive check: the report of the sampling or the check's -->
        <div v-if="model?.hasPrior" class="sr-group sr-switch" role="group" :aria-label="$t('modelReport.switch')">
          <button type="button" class="mini" :class="{ on: model.view !== 'prior' }" :aria-pressed="model.view !== 'prior'" @click="emit('view', 'posterior')">
            {{ $t('modelReport.posterior') }}
          </button>
          <button type="button" class="mini" :class="{ on: model.view === 'prior' }" :aria-pressed="model.view === 'prior'" :title="$t('modelReport.priorTitle')" @click="emit('view', 'prior')">
            <span class="sr-sw-long">{{ $t('modelReport.prior') }}</span><span class="sr-sw-short">{{ $t('modelReport.priorShort') }}</span>
          </button>
        </div>
        <span v-if="model?.running" class="sr-status"><LoaderCircle :size="12" class="spin" aria-hidden="true" /> {{ $t('modelReport.running') }}</span>
        <span v-else-if="busy" class="sr-status"><LoaderCircle :size="12" class="spin" aria-hidden="true" /> {{ $t('statReport.drawing') }}<template v-if="seconds >= 2"> · {{ $t('dataGrid.seconds', { s: seconds }) }}</template></span>
        <template v-else-if="model && modelStale">
          <span class="sr-stale" :title="$t('modelReport.staleTitle')"><History :size="12" aria-hidden="true" /> {{ $t('statReport.stale') }}</span>
          <button type="button" class="mini primary sr-update" :title="$t('modelReport.runTitle')" @click="emit('run')">
            <Play :size="12" aria-hidden="true" /> {{ $t('modelReport.run') }}
          </button>
        </template>
        <template v-else-if="state === 'stale' && !model">
          <span class="sr-stale" :title="$t('statReport.staleTitle')"><History :size="12" aria-hidden="true" /> {{ $t('statReport.stale') }}</span>
          <button type="button" class="mini primary sr-update" :disabled="unset || !chain" :title="unset ? $t('statReport.unset') : $t('statReport.updateTitle')" @click="draw">
            <RefreshCw :size="12" aria-hidden="true" /> {{ $t('statReport.update') }}
          </button>
        </template>
        <span v-if="model?.trial && shown && !busy" class="sr-trial" :title="$t('modelReport.trialTitle')">
          <FlaskConical :size="12" aria-hidden="true" /> {{ $t('modelReport.trial') }}
        </span>
      </div>

      <div class="sr-group">
        <button type="button" class="mini" :disabled="!shown || !!downloading" :title="$t('statReport.pdfTitle')" :aria-label="$t('statReport.pdfTitle')" @click="download('pdf')">
          <LoaderCircle v-if="downloading === 'pdf'" :size="13" class="spin" aria-hidden="true" /><FileText v-else :size="13" aria-hidden="true" /> PDF
        </button>
        <button type="button" class="mini" :disabled="!shown || !!downloading" :title="$t('statReport.excelTitle')" :aria-label="$t('statReport.excelTitle')" @click="download('xlsx')">
          <LoaderCircle v-if="downloading === 'xlsx'" :size="13" class="spin" aria-hidden="true" /><FileSpreadsheet v-else :size="13" aria-hidden="true" /> Excel
        </button>
        <button
          type="button"
          class="mini sr-icon"
          :class="{ on: expanded }"
          :aria-pressed="expanded"
          :title="expanded ? $t('statReport.restore') : $t('statReport.expand')"
          :aria-label="$t('statReport.expand')"
          @click="emit('expand')"
        >
          <Minimize2 v-if="expanded" :size="14" aria-hidden="true" /><Maximize2 v-else :size="14" aria-hidden="true" />
        </button>
      </div>
    </div>

    <!-- which of the two reports this is, and when its run ended -->
    <p v-if="model?.hasPrior && model.at && target && !model.running" class="sr-sub">
      <span class="sr-sub-what">{{ model.view === 'prior' ? $t('modelReport.prior') : $t('modelReport.posterior') }}</span>
      <span class="sr-when">{{ $t('modelReport.when', { when: fmt.ago(model.at) }) }}</span>
    </p>
    <div class="sr-stage">
      <div v-if="problem && !busy" class="sr-state">
        <div class="sr-error" role="alert">
          <CircleAlert :size="16" class="sr-state-icon" aria-hidden="true" />
          <span><strong v-if="problemTitle">{{ problemTitle }} </strong>{{ problem.kind === 'unset' ? $t('statReport.unset') : problem.message }}</span>
        </div>
        <button v-if="statReportRetryable(problem.kind) || failedKey !== key" type="button" class="sr-again" :disabled="unset" @click="draw">
          <RefreshCw :size="13" aria-hidden="true" /> {{ failedKey !== key ? $t('statReport.update') : $t('statReport.tryAgain') }}
        </button>
      </div>
      <div v-else-if="model && !target && !busy" class="sr-state">
        <p class="sr-note muted">{{ model.view === 'prior' ? $t('modelReport.priorFailed') : model.failed ? $t('modelReport.failed') : $t('modelReport.noRun') }}</p>
        <button type="button" class="sr-again" :disabled="model.running" :title="$t('modelReport.runTitle')" @click="emit('run')">
          <LoaderCircle v-if="model.running" :size="13" class="spin" aria-hidden="true" /><Play v-else :size="13" aria-hidden="true" />
          {{ model.running ? $t('modelReport.running') : $t('modelReport.run') }}
        </button>
      </div>
      <p v-else-if="!chain && !model" class="sr-note muted">{{ $t('statReport.noData') }}</p>
      <p v-else-if="state === 'unset'" class="sr-note muted">{{ $t('statReport.unset') }}</p>
      <div v-else-if="state === 'empty' || state === 'failed'" class="sr-state">
        <p class="sr-note muted">{{ $t('statReport.empty') }}</p>
        <button type="button" class="sr-again" @click="draw"><RefreshCw :size="13" aria-hidden="true" /> {{ $t('statReport.draw') }}</button>
      </div>
      <ReportPages
        v-else
        :pages="shown?.pages ?? []"
        :doc-key="shown?.key ?? null"
        :zoom="zoom"
        :busy="busy"
        :label="$t('statReport.pagesOf', { name: step || (model ? $t('modelReport.region') : $t('statReport.region')) })"
        @page="(n: number) => (currentPage = n)"
        @scale="(s: number) => (scale = s)"
      />
    </div>
    <span class="sr-only" role="status" aria-live="polite">{{ busy ? $t('statReport.drawing') : state === 'stale' || modelStale ? $t('statReport.stale') : announce }}</span>
  </section>
</template>

<style scoped>
.sr { display: flex; flex-direction: column; height: 100%; min-height: 0; min-width: 0; container: statreport / inline-size; }
.sr-tools {
  display: flex; align-items: center; gap: 10px; padding: 4px 10px; min-height: 34px; box-sizing: border-box; flex-shrink: 0;
  background: var(--panel); border-bottom: 1px solid var(--border-soft);
}
.sr-group { display: flex; align-items: center; gap: 6px; flex-shrink: 0; }
.sr-group button { display: inline-flex; align-items: center; gap: 4px; padding: 3px 8px; font-size: 12px; white-space: nowrap; }
.sr-icon { padding: 3px 6px; }
.sr-pct { min-width: 50px; justify-content: center; font-variant-numeric: tabular-nums; font-size: 12px; }
.sr-group .on { border-color: var(--accent); background: var(--tint-accent); color: var(--accent-hi); }
.sr-pageof { margin-left: 4px; font-size: 12px; color: var(--muted); white-space: nowrap; font-variant-numeric: tabular-nums; }
.sr-mid { flex: 1; min-width: 0; display: flex; align-items: center; justify-content: center; gap: 10px; }
.sr-status { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: var(--muted); white-space: nowrap; }
.sr-stale {
  display: inline-flex; align-items: center; gap: 5px; padding: 2px 8px; font-size: 12px; white-space: nowrap;
  border-radius: 999px; color: var(--warning); border: 1px solid color-mix(in srgb, var(--warning) 45%, var(--border));
}
/* the height of the buttons beside it: a primary button's own padding made it stand a step taller */
.sr-update { display: inline-flex; align-items: center; gap: 4px; padding: 3px 9px; font-size: 12px; white-space: nowrap; }
/* a trial's pages: its rows before the node were the development sample's (the header of the page says it too) */
.sr-trial { display: inline-flex; align-items: center; gap: 4px; font-size: 12px; color: var(--muted); white-space: nowrap; }
.sr-switch { gap: 0; }
.sr-switch button { border-radius: 0; }
.sr-switch button:first-child { border-radius: 6px 0 0 6px; }
.sr-switch button:last-child { border-radius: 0 6px 6px 0; margin-left: -1px; }
.sr-switch button:focus-visible { position: relative; z-index: 1; }
.sr-short, .sr-sw-short { display: none; }
.sr-sub { margin: 0; padding: 3px 10px; display: flex; gap: 8px; align-items: baseline; flex-shrink: 0; font-size: 12px; border-bottom: 1px solid var(--border-soft); }
.sr-sub-what { color: var(--text); font-weight: 600; }
.sr-when { color: var(--muted); }
/* the panel opened beside the canvas leaves the switch the room of its short words */
@container statreport (max-width: 960px) {
  .sr-sw-long { display: none; }
  .sr-sw-short { display: inline; }
}
.sr-stage { position: relative; flex: 1; min-height: 0; overflow: auto; }
.sr-state { display: flex; flex-direction: column; align-items: flex-start; gap: 10px; margin: 12px; }
.sr-state .sr-note { margin: 0; }
.sr-note { margin: 12px; font-size: 12.5px; line-height: 1.5; }
.sr-state-icon { flex: none; margin-top: 2px; }
.sr-error {
  display: flex; align-items: flex-start; gap: 8px; padding: 10px 12px; font-size: 12.5px; line-height: 1.5;
  border: 1px solid color-mix(in srgb, var(--danger) 45%, transparent); border-radius: 8px;
  background: var(--tint-danger); color: var(--danger-text); white-space: pre-wrap; overflow-wrap: anywhere;
}
.sr-again { display: inline-flex; align-items: center; gap: 5px; padding: 4px 10px; font-size: 12px; }

/* a narrow panel (the node's settings open beside it): the words give way, the names stay
   for the screen reader and in the tooltips */
@container statreport (max-width: 640px) {
  .sr-long, .sr-pageof, .sr-trial { display: none; }
  .sr-short { display: inline; }
  .sr-tools { gap: 6px; padding: 4px 6px; }
}
</style>
