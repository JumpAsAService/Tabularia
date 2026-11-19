<script setup lang="ts">
// The Report tab of the flow editor's preview, for a statistics node (docs/specs/
// statistics.md, «In the editor»): the pages of the test's report, drawn by the report
// engine from the layout preset for that test, on the rows that reach the node — the chain
// its preview reads, sent as it is. Read-only: zoom, the panel at its largest, PDF, Excel.
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
import { CircleAlert, FileSpreadsheet, FileText, History, LoaderCircle, Maximize2, Minimize2, MoveHorizontal, RefreshCw, ZoomIn, ZoomOut } from 'lucide-vue-next'
import ReportPages from '~/components/report/ReportPages.vue'
import { errMessage } from '~/composables/useApi'
import type { Recent } from '~/composables/previewGrid'
import {
  STAT_REPORT_ZOOM_KEY, STAT_REPORT_ZOOMS, statReportKey, statReportProblem, statReportRequest, statReportRetryable,
  statReportShouldDraw, statReportState, statReportUnset, statReportZoomRead, statReportZoomStep,
  type StatReportChain, type StatReportProblem, type StatReportShown,
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
}>(), { step: '', expanded: false, memory: null })

const emit = defineEmits<{ (e: 'expand'): void }>()

const { t, locale } = useI18n()
const { apiFetch } = useApiClient()
const reports = useReports()
const toast = useToast()

const key = computed(() => (props.chain ? statReportKey(props.chain, locale.value, props.step) : null))
const unset = computed(() => statReportUnset(props.chain?.operations))

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

async function draw() {
  const k = key.value
  if (!props.chain || !k || unset.value) return
  controller?.abort()
  const mine = new AbortController()
  controller = mine
  // the request as it is now: the node may be set differently before the pages arrive
  const chain = JSON.parse(JSON.stringify(props.chain)) as StatReportChain
  const lang = locale.value
  const step = props.step
  busy.value = true
  problem.value = null
  failedKey.value = null
  startClock()
  try {
    const { task_id } = await apiFetch<{ task_id: string }>('/tasks/stat-report', {
      method: 'POST', body: statReportRequest(chain, 'svg', lang, step), signal: mine.signal,
    })
    const outcome = await reports.followRender(task_id, { signal: mine.signal })
    const screen = await reports.screenOf(outcome)
    const entry: StatReportShown = { key: k, pages: screen.pages, chain, locale: lang, step }
    props.memory?.set(k, entry)
    if (controller !== mine) return
    shown.value = entry
    announce.value = t('statReport.drawn', { n: entry.pages.length }, entry.pages.length)
  } catch (e) {
    if (isAbort(e) || controller !== mine) return
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

function stop() {
  controller?.abort()
  controller = null
  busy.value = false
  stopClock()
}

// the tab opened, another node, another language: its pages from memory, or drawn
watch([() => props.nodeId, locale], ([node, lang], before) => {
  const was = before ? { node: before[0] as string | null, locale: String(before[1]) } : null
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
  draw()
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
  } else if (!shown.value && !problem.value && !unset.value) {
    draw()
  }
})

onBeforeUnmount(() => {
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
    const { task_id } = await apiFetch<{ task_id: string }>('/tasks/stat-report', {
      method: 'POST', body: statReportRequest(of.chain, format, of.locale, of.step),
    })
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
  <section class="sr" :aria-label="$t('statReport.region')">
    <div class="sr-tools">
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
        <span v-if="busy" class="sr-status"><LoaderCircle :size="12" class="spin" aria-hidden="true" /> {{ $t('statReport.drawing') }}<template v-if="seconds >= 2"> · {{ $t('dataGrid.seconds', { s: seconds }) }}</template></span>
        <template v-else-if="state === 'stale'">
          <span class="sr-stale" :title="$t('statReport.staleTitle')"><History :size="12" aria-hidden="true" /> {{ $t('statReport.stale') }}</span>
          <button type="button" class="mini primary sr-update" :disabled="unset || !chain" :title="unset ? $t('statReport.unset') : $t('statReport.updateTitle')" @click="draw">
            <RefreshCw :size="12" aria-hidden="true" /> {{ $t('statReport.update') }}
          </button>
        </template>
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
      <p v-else-if="!chain" class="sr-note muted">{{ $t('statReport.noData') }}</p>
      <p v-else-if="state === 'unset'" class="sr-note muted">{{ $t('statReport.unset') }}</p>
      <div v-else-if="state === 'empty' || state === 'failed'" class="sr-state">
        <p class="sr-note muted">{{ $t('statReport.empty') }}</p>
        <button type="button" class="sr-again" @click="draw"><RefreshCw :size="13" aria-hidden="true" /> {{ $t('statReport.draw') }}</button>
      </div>
      <ReportPages
        v-else
        :pages="shown?.pages ?? []"
        :zoom="zoom"
        :busy="busy"
        :label="$t('statReport.pagesOf', { name: step || $t('statReport.region') })"
        @page="(n: number) => (currentPage = n)"
        @scale="(s: number) => (scale = s)"
      />
    </div>
    <span class="sr-only" role="status" aria-live="polite">{{ busy ? $t('statReport.drawing') : state === 'stale' ? $t('statReport.stale') : announce }}</span>
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
  .sr-long, .sr-pageof { display: none; }
  .sr-tools { gap: 6px; padding: 4px 6px; }
}
</style>
