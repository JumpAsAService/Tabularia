// The report of a statistics step in the flow editor (docs/specs/statistics.md, «The
// report»): what is asked of POST /tasks/stat-report, when the pages on screen are no longer
// those of the node as it is set, the zoom's steps, what a failure is. The view is
// components/ui/StatReportView.vue; the pages are drawn by the report engine, from a layout
// preset for the test, on the rows that reach the node — the same chain as its preview.
//
// Pure functions, nothing of Nuxt or the DOM: tests/stat-report.test.mjs imports them as
// they are. Every export starts with `stat`, `STAT_` or `Stat`: a composable's exports are
// auto-imported everywhere.

import { previewKey } from '~/composables/previewGrid'
import { statIsUnset, statTestOf } from '~/composables/statTests'

export type StatReportFormat = 'svg' | 'pdf' | 'xlsx'

/** The chain a node's preview reads: the report is asked on the same one, as it is. */
export interface StatReportChain {
  engine?: string | null
  bucket: string
  input_key: string
  operations: unknown[]
}

/** The body of POST /tasks/stat-report. No file name: the engine names the file after the
 *  test and its columns, in the language asked («Welch's t test — amount by channel»). */
export interface StatReportRequest extends StatReportChain {
  format: StatReportFormat
  locale: string
  step?: string
}

/** A render this page keeps: the pages, and the request they were drawn for (the PDF and
 *  the Excel of the pages on screen are asked with it, whatever the node says now): a
 *  statistics node's chain, or a model node's run. */
export interface StatReportShown {
  key: string
  pages: string[]
  chain: StatReportChain | null
  model?: StatModelReportTarget
  locale: string
  step: string
}

// ── a model node's report (docs/specs/statistical-models.md, «The report») ───
/** The run whose results a model node's report draws: POST /tasks/model-report. */
export interface StatModelReportTarget {
  flowId: number
  nodeId: string
  runId: number
}

/** What the Report tab knows of a model node's runs: the run to draw (none yet: Run), whether
 *  the node or its chain changed since (`stale`), whether it was a trial, whether the latest
 *  failed, whether a trial is running now. */
export interface StatModelRuns {
  flowId: number
  runId: number | null
  stale: boolean
  trial: boolean
  failed: boolean
  running: boolean
}

type NodeRun = { run_id: number; status?: string; trial?: boolean; stale?: boolean } | null | undefined

/** The run a model node's report draws, from the node's entry of GET /scripting/flows/{id}/outputs
 *  (C2 gives model nodes theirs, as Script nodes have): its latest run when it succeeded, else
 *  the latest that did (`output`), with whether the latest failed. */
export function statModelReportRun(entry: { last?: NodeRun; output?: NodeRun } | null | undefined):
  { runId: number | null; stale: boolean; trial: boolean; failed: boolean } {
  const last = entry?.last
  const failed = !!last && last.status === 'FAILURE'
  const good = last && last.status === 'SUCCESS' ? last : entry?.output && entry.output.status !== 'FAILURE' ? entry.output : null
  if (!good) return { runId: null, stale: false, trial: false, failed }
  return { runId: good.run_id, stale: !!good.stale, trial: !!good.trial, failed }
}

/** The body of POST /tasks/model-report: the run, the format, the language. Nothing else: the
 *  gateway reads the model, its parameters and where the results are from the run itself. */
export function statModelReportRequest(target: StatModelReportTarget, format: StatReportFormat, locale: string) {
  return { flow_id: target.flowId, node_id: target.nodeId, run_id: target.runId, format, locale: statReportLocale(locale) }
}

/** The identity of a model report's pages: the run and the language (the step's name is the
 *  node's, read by the gateway). */
export function statModelReportKey(target: StatModelReportTarget, locale: string): string {
  return JSON.stringify(['model', target.flowId, target.nodeId, target.runId, statReportLocale(locale)])
}

export const STAT_REPORT_LOCALES: readonly string[] = ['en', 'it', 'de', 'es', 'fr']
/** The longest step name the header band takes (the route's own limit). */
export const STAT_REPORT_STEP_MAX = 200
/** How many nodes' pages a page of the editor remembers. */
export const STAT_REPORT_REMEMBERED = 12

// ── zoom ──────────────────────────────────────────────────────────────────────
export const STAT_REPORT_ZOOM_KEY = 'tabularia.statReport.zoom'
export const STAT_REPORT_ZOOM_DEFAULT = 0.75
export const STAT_REPORT_ZOOMS: readonly number[] = [0.25, 0.33, 0.5, 0.67, 0.75, 0.9, 1, 1.1, 1.25, 1.5, 1.75, 2, 2.5, 3, 4]

/** The zoom kept in localStorage, or the default: 'fit', or a scale within the steps' range. */
export function statReportZoomRead(raw: string | null | undefined): number | 'fit' {
  if (raw === 'fit') return 'fit'
  const n = Number(raw)
  if (!raw || !Number.isFinite(n)) return STAT_REPORT_ZOOM_DEFAULT
  return Math.min(STAT_REPORT_ZOOMS[STAT_REPORT_ZOOMS.length - 1], Math.max(STAT_REPORT_ZOOMS[0], n))
}

/** The next step up or down from the scale on screen (which «fit» makes any number), or
 *  null at the end of the steps. */
export function statReportZoomStep(scale: number, dir: 1 | -1): number | null {
  const next = dir > 0
    ? STAT_REPORT_ZOOMS.find((x) => x > scale + 0.001)
    : [...STAT_REPORT_ZOOMS].reverse().find((x) => x < scale - 0.001)
  return next ?? null
}

// ── the request ───────────────────────────────────────────────────────────────

/** The operation a chain ends with, when it is a hypothesis test. */
export function statReportTest(operations: readonly unknown[] | null | undefined) {
  const last = operations?.length ? operations[operations.length - 1] as { type?: unknown } | null : null
  return statTestOf(last?.type)
}

/** Whether the test a chain ends with still lacks a key it needs: nothing is asked for it. */
export function statReportUnset(operations: readonly unknown[] | null | undefined): boolean {
  const last = operations?.length ? operations[operations.length - 1] as { type?: unknown; params?: Record<string, unknown> | null } | null : null
  const test = statTestOf(last?.type)
  if (!test) return false
  const params = last?.params ?? {}
  return Object.keys(test.needs).some((k) => {
    const v = params[k]
    return v === null || v === undefined || v === ''
  })
}

export function statReportLocale(locale: string | null | undefined): string {
  const short = String(locale ?? '').toLowerCase().split(/[-_]/)[0]
  return STAT_REPORT_LOCALES.includes(short) ? short : 'en'
}

/** The request of a node's report: its preview's chain untouched (the marks of a
 *  development sample included, no `detail`, no limit), the format, the language. */
export function statReportRequest(chain: StatReportChain, format: StatReportFormat, locale: string, step = ''): StatReportRequest {
  const name = step.trim().slice(0, STAT_REPORT_STEP_MAX)
  return {
    engine: chain.engine ?? null,
    bucket: chain.bucket,
    input_key: chain.input_key,
    operations: chain.operations,
    format,
    locale: statReportLocale(locale),
    ...(name ? { step: name } : {}),
  }
}

/** The identity of a report's pages: the preview's (engine, source, chain), the language
 *  and the step's name, which is printed on them. */
export function statReportKey(chain: StatReportChain, locale: string, step = ''): string {
  return JSON.stringify([previewKey({ ...chain, limit: 0 }), statReportLocale(locale), step.trim().slice(0, STAT_REPORT_STEP_MAX)])
}

// ── what the tab shows ────────────────────────────────────────────────────────
/** - `unset`: the test lacks a column: the hint to set it, nothing asked;
 *  - `drawing`: a render is running and there is nothing of this node to show meanwhile;
 *  - `ready`: the pages are those of the node as it is;
 *  - `stale`: the pages are of the node as it was: they stay, with «Update»;
 *  - `failed`: the last render stopped; `empty`: nothing yet. */
export type StatReportState = 'unset' | 'drawing' | 'ready' | 'stale' | 'failed' | 'empty'

export function statReportState(s: {
  unset: boolean
  busy: boolean
  /** the key of the node as it is now */
  key: string | null
  /** the key of the pages on screen */
  shownKey: string | null
  /** the key of the render that failed */
  failedKey: string | null
}): StatReportState {
  if (s.shownKey && s.key && s.shownKey === s.key) return 'ready'
  if (s.busy) return s.shownKey ? 'stale' : 'drawing'
  if (s.unset) return s.shownKey ? 'stale' : 'unset'
  if (s.shownKey) return 'stale'
  return s.failedKey ? 'failed' : 'empty'
}

/** Whether a change of what the tab is about draws by itself: another node or another
 *  language does (the tab is «opened» for it, and a language is asked on purpose); the same
 *  node set differently, or something before it changed, does not — the pages stay, marked
 *  «not up to date»: never a render at each keystroke. */
export function statReportShouldDraw(now: { node: string | null; locale: string },
  before: { node: string | null; locale: string } | null): boolean {
  if (!now.node) return false
  return !before || before.node !== now.node || before.locale !== now.locale
}

// ── a failure ─────────────────────────────────────────────────────────────────
/** - `refusal`: the test refuses the setup (three groups for two samples): its sentence, as
 *    a preview's error;  - `unset`: a key it needs is missing;
 *  - `forbidden`: the person may not read what the chain reads;  - `busy`: too many
 *    exports of theirs at once;  - `size`, `timeout`, `charts`, `gone`: as a report's;
 *  - `invalid`: the request itself (422);  - `other`. */
export type StatReportProblemKind = 'refusal' | 'unset' | 'forbidden' | 'busy' | 'size' | 'timeout' | 'charts' | 'gone' | 'invalid' | 'other'

export interface StatReportProblem {
  kind: StatReportProblemKind
  message: string
}

/** A failed request or render as something to say: the status of the POST (403, 413, 422,
 *  429) or the code the engine tagged the render with (400 a refusal, 404, 422, 503, 504).
 *  `message` is the sentence read from it (`errMessage`, which needs the app). */
export function statReportProblem(e: any, message: string): StatReportProblem {
  const code = Number(e?.statusCode ?? e?.response?.status ?? e?.status ?? 0)
  if (statIsUnset(message)) return { kind: 'unset', message }
  const kind: StatReportProblemKind =
    code === 400 ? 'refusal'
      : code === 403 ? 'forbidden'
        : code === 429 ? 'busy'
          : code === 413 ? 'size'
            : code === 504 ? 'timeout'
              : code === 503 ? 'charts'
                : code === 404 ? 'gone'
                  : code === 422 ? 'invalid'
                    : 'other'
  return { kind, message }
}

/** Whether asking again as it is may succeed (a refusal needs the node set differently). */
export function statReportRetryable(kind: StatReportProblemKind): boolean {
  return kind !== 'refusal' && kind !== 'unset' && kind !== 'forbidden' && kind !== 'invalid'
}
