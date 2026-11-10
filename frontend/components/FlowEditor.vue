<script setup lang="ts">
import { ref, reactive, computed, watch, nextTick, onMounted, onUnmounted, provide } from 'vue'
import { VueFlow, useVueFlow } from '@vue-flow/core'
import type { Node, Connection } from '@vue-flow/core'
import { Background } from '@vue-flow/background'
import { Controls, ControlButton } from '@vue-flow/controls'
import { MiniMap } from '@vue-flow/minimap'
import { opMeta } from '~/composables/useOpIcons'

import { Table2, BarChart3, Wand2, Users, Undo2, Redo2, Sparkles, SlidersHorizontal, ShieldAlert, FlaskConical, X, RotateCw, RefreshCw, MoreHorizontal, Plus, Minus, Maximize, Lock, LockOpen } from 'lucide-vue-next'
import FlowAssistant from '~/components/FlowAssistant.vue'
import { useAi, type AiStatus } from '~/composables/useAi'
import { useApi, errMessage } from '~/composables/useApi'
import { FIRST_PAUSE_MS, nextPause } from '~/composables/waits'
import { knownError } from '~/composables/knownErrors'
import { useAuth } from '~/composables/useAuth'
import { usePreviewSlots, isSuperseded } from '~/composables/usePreviewSlots'
import { useFlowPresence } from '~/composables/useFlowPresence'
import { useAppInfo } from '~/composables/useAppInfo'
import type { NodeStats } from '~/composables/useNodeStats'
// explicit import: in the container new components are not scanned
import FlowEdge from '~/components/edges/FlowEdge.vue'
import ScriptNode from '~/components/nodes/ScriptNode.vue'
import ContainerNode from '~/components/nodes/ContainerNode.vue'
import { useScriptingEditor, type ScriptOutputs } from '~/composables/useScripting'
import { useRegistriesEditor } from '~/composables/useRegistries'
import { useFormat } from '~/composables/useFormat'
import type { PreviewResult, ColumnInfo, Operation, IngestResult, ProfileResult, TopValuesResult, HistogramResult } from '~/composables/useApi'
import type { DetailAsk } from '~/components/ui/DataGrid.vue'
import GridKeys from '~/components/ui/GridKeys.vue'
import { useKeyGroups, useKeyHelp } from '~/composables/useKeyHelp'
import { SOURCE_ID, buildIncoming, resolveChain, leafNodeId, defaultParams, sampleLabel } from '~/composables/useFlowModel'
import { Recent, previewKey, columnChanges, consumedColumns, nodeRowsPhrase, rowsPhrase, suggestSample, type RowCount } from '~/composables/previewGrid'
import { opSubject } from '~/composables/opSummary'
import { profileOpen, profileStart } from '~/composables/profileAsk'
import { usePopover } from '~/composables/usePopover'
import { computeAutoLayout } from '~/composables/useFlowLayout'
import { planStep, chainNeighbour, upstreamEdgeIds, flowEndIds, openingViewport, easeOutExpo, FLOW_STEP_X, type ChainNode, type ChainEdge, type StepOptions, type Walk } from '~/composables/flowChain'
import { createFlowHistory } from '~/composables/useFlowHistory'
import { onBeforeRouteLeave, onBeforeRouteUpdate } from 'vue-router'
import { opensAnotherFlow } from '~/composables/editorAddress'
import Modal from '~/components/ui/Modal.vue'
import FlowQuickMenu from '~/components/ui/FlowQuickMenu.vue'
import FlowShortcutsDialog from '~/components/ui/FlowShortcutsDialog.vue'
import { flowShortcutsShown } from '~/composables/flowShortcuts'
import SheetPickerDialog from '~/components/ui/SheetPickerDialog.vue'
import { buildClip, instantiateClip, readClip, writeClip, type ClipNode, type FlowClip } from '~/composables/useFlowClipboard'
import { useFlows } from '~/composables/useFlows'
import { useProjects } from '~/composables/useProjects'
import { useRuns, type PublishSpec, type DestinationSpec, type MirrorSpec } from '~/composables/useRuns'
import { useDatasources, type DatasourceInfo } from '~/composables/useDatasources'
import { useConnections, type ConnectionInfo } from '~/composables/useConnections'
import { skeletonPad } from '~/composables/useSkeleton'
import { useI18n } from 'vue-i18n'

const api = useApi()
const { t } = useI18n()
const flowsApi = useFlows()
const projectsApi = useProjects()
const runsApi = useRuns()
const dsApi = useDatasources()
const connApi = useConnections()
const route = useRoute()
const router = useRouter()
const bucket = useRuntimeConfig().public.bucket as string

const {
  getNodes,
  getEdges,
  addNodes: addNodesNow,
  addEdges,
  setNodes,
  setEdges,
  removeNodes,
  removeEdges,
  updateNodeData,
  findNode,
  onConnect,
  onNodeClick,
  onPaneClick,
  onPaneContextMenu,
  onNodeContextMenu,
  onMoveStart,
  onEdgeMouseEnter,
  onEdgeMouseLeave,
  getSelectedEdges,
  getSelectedNodes,
  addSelectedNodes,
  removeSelectedNodes,
  screenToFlowCoordinate,
  fitView,
  setCenter,
  setViewport,
  getViewport,
  viewport,
  dimensions: paneSize,
  minZoom,
  maxZoom,
  zoomIn,
  zoomOut,
  setInteractive,
  nodesDraggable,
  nodesConnectable,
  elementsSelectable,
  onNodeDragStart,
  onNodesInitialized,
} = useVueFlow()

// A node someone adds arrives (`.node-new` in main.css: a short fade and growth, once); a
// flow that loads is set whole with setNodes and stands there at once. The class is not
// saved with the flow.
const addNodes: typeof addNodesNow = (nodes) =>
  addNodesNow(typeof nodes === 'function' ? nodes : (Array.isArray(nodes) ? nodes : [nodes]).map((n) => ({ ...n, class: 'node-new' })))

const initialNodes: Node[] = [
  {
    id: SOURCE_ID,
    type: 'source',
    position: { x: 60, y: 140 },
    data: { bucket, datasetId: null, parquetKey: null, filename: null, rows: null, columns: [] },
  },
]

// ── UI state ────────────────────────────────────────────────────────────
const selectedId = ref<string | null>(SOURCE_ID)
const operations = ref<string[]>([])
const inputColumns = ref<ColumnInfo[]>([])
const rightColumns = ref<ColumnInfo[]>([])
const columnsLoading = ref(false) // resolving the selected node's columns in progress
// {{column}} placeholders available to a node INSIDE a foreach container
const placeholders = ref<string[]>([])
const nodeColumns = reactive<Record<string, ColumnInfo[]>>({}) // output cache per node

let opCounter = 0
let sourceCounter = 0
let outputCounter = 0
let commentCounter = 0

const viewTab = ref<'table' | 'chart'>('table') // view under the canvas

// ── Observer in the editor ────────────────────────────────────────────────
// An observer comes in, adds TRANSFORMATION nodes and looks at their preview. They do
// not save, run, upload files, add sources, controls or outputs: not because the server
// would let them — /flows, /files and /tasks/transform-data refuse it — but because
// offering it only to answer 403 is rude. `is_observer` from /auth/me is EFFECTIVE: true
// for admins too, so `is_superuser` is looked at as well.
const { user: signedIn, fetchMe, logout } = useAuth()
// Until who signed in is known, the gate stays CLOSED: a moment without «Save» for an
// administrator is nothing, a moment with «Run» for an observer is a 403 in the face.
// The editor does not go through AppShell, the only place that loads the user: it is
// loaded here at mount.
const observerOnly = computed(() =>
  signedIn.value === null || (!!signedIn.value.is_observer && !signedIn.value.is_superuser))
const TRANSFORMATION = (kind: string) => kind.startsWith('op:') && kind !== 'op:foreach'
function deniedToObserver(): boolean {
  if (!observerOnly.value) return false
  setStatus(t('flowEditor.readOnlyNodes'), 'error')
  return true
}

// ── Resizable right panel and data view (sizes remembered) ──────────────
const PANEL_MIN = 280
const PANEL_MAX = 640
const panelWidth = ref(
  Math.min(PANEL_MAX, Math.max(PANEL_MIN, Number(localStorage.getItem('tabularia.panelWidth')) || 340)),
)

// The table under the canvas: whoever looks at the data decides how tall it is. The cap
// is not a fixed number but «what is left»: the canvas must always keep enough pixels to
// see the nodes being connected.
const GRID_MIN = 120
const CANVAS_MIN = 220
// the toolbar sits in the same column of rows: unless it is taken out of the count, the
// canvas ends up under the minimum exactly when the table is pulled up
const gridMax = () => {
  const toolbar = document.querySelector('.app > .toolbar')?.getBoundingClientRect().height ?? 0
  return Math.max(GRID_MIN, window.innerHeight - toolbar - CANVAS_MIN)
}
const gridHeight = ref(
  Number(localStorage.getItem('tabularia.gridHeight')) || Math.round(window.innerHeight * 0.34),
)
function clampGrid(px: number) {
  return Math.min(gridMax(), Math.max(GRID_MIN, Math.round(px)))
}
gridHeight.value = clampGrid(gridHeight.value)
// a window made smaller must not leave the table taller than the screen
const fallBack = () => { gridHeight.value = clampGrid(gridHeight.value) }
onMounted(() => window.addEventListener('resize', fallBack))
onUnmounted(() => window.removeEventListener('resize', fallBack))

// minmax(0,1fr) as in main.css: wide previews (pivot) must not push the panel off screen
const appStyle = computed(() => ({
  gridTemplateColumns: `200px minmax(0, 1fr) ${panelWidth.value}px`,
  gridTemplateRows: `auto minmax(0, 1fr) ${gridHeight.value}px`,
}))

/**
 * A handle: drag with the mouse, and with the arrow keys (it is a `separator`, not a
 * decoration: whoever does not use the mouse must be able to move it).
 */
function drag(
  ev: MouseEvent,
  cursor: 'col-resize' | 'row-resize',
  onMove: (e: MouseEvent) => void,
  remember: () => void,
) {
  ev.preventDefault()
  const onUp = () => {
    window.removeEventListener('mousemove', onMove)
    window.removeEventListener('mouseup', onUp)
    document.body.style.cursor = ''
    document.body.style.userSelect = ''
    remember()
  }
  window.addEventListener('mousemove', onMove)
  window.addEventListener('mouseup', onUp)
  document.body.style.cursor = cursor
  document.body.style.userSelect = 'none'
}

function startPanelResize(ev: MouseEvent) {
  drag(
    ev, 'col-resize',
    (e) => { panelWidth.value = Math.min(PANEL_MAX, Math.max(PANEL_MIN, window.innerWidth - e.clientX)) },
    () => localStorage.setItem('tabularia.panelWidth', String(panelWidth.value)),
  )
}

function startGridResize(ev: MouseEvent) {
  drag(
    ev, 'row-resize',
    (e) => { gridHeight.value = clampGrid(window.innerHeight - e.clientY) },
    () => localStorage.setItem('tabularia.gridHeight', String(gridHeight.value)),
  )
}

const STEP = 24  // pixels per press; with Shift ten times faster
function panelKeys(e: KeyboardEvent) {
  const d = e.key === 'ArrowLeft' ? STEP : e.key === 'ArrowRight' ? -STEP : 0
  if (!d) return
  e.preventDefault()
  panelWidth.value = Math.min(PANEL_MAX, Math.max(PANEL_MIN, panelWidth.value + d * (e.shiftKey ? 10 : 1)))
  localStorage.setItem('tabularia.panelWidth', String(panelWidth.value))
}
function viewKeys(e: KeyboardEvent) {
  const d = e.key === 'ArrowUp' ? STEP : e.key === 'ArrowDown' ? -STEP : 0
  if (!d) return
  e.preventDefault()
  gridHeight.value = clampGrid(gridHeight.value + d * (e.shiftKey ? 10 : 1))
  localStorage.setItem('tabularia.gridHeight', String(gridHeight.value))
}
// null = nothing has happened yet: the bar describes the canvas instead. A fixed
// «Load a file to get started.» stayed there after a datasource had been picked
// (1.2.0 getting-started run).
const status = ref<string | null>(null)
const idleStatus = computed(() =>
  getNodes.value.some((n) => n.type === 'source' && (n.data?.datasourceId != null || !!n.data?.parquetKey))
    ? t('flowEditor.statusHasData')
    : t('flowEditor.statusInitial'),
)
// kind of state → icon in the toolbar (spinner / check / error)
const statusKind = ref<'info' | 'ok' | 'error' | 'busy'>('info')
const toast = useToast()
// An outcome that went well is said for a few seconds, then the bar describes the canvas
// again: «Flow "…" loaded» stayed there in green for the whole session. An error stays
// until something else is said, and so does what is still going on.
const STATUS_OK_MS = 4000
let statusTimer: ReturnType<typeof setTimeout> | undefined
/** `quiet`: in the bar only, for a fact the canvas shows by itself (a flow that loaded). */
function setStatus(msg: string, kind: 'info' | 'ok' | 'error' | 'busy' = 'info', quiet = false) {
  status.value = msg
  statusKind.value = kind
  clearTimeout(statusTimer)
  if (kind === 'ok') {
    statusTimer = setTimeout(() => {
      status.value = null
      statusKind.value = 'info'
    }, STATUS_OK_MS)
  }
  if (quiet) return
  // successes and errors as toasts too: they show with the bar full/ignored too
  if (kind === 'ok') toast.success(msg)
  else if (kind === 'error') toast.error(msg)
}
onUnmounted(() => clearTimeout(statusTimer))
const busy = ref(false)
// The known rows and columns of each node, from the LAST preview that touched it: no
// extra query, and never saved in the flow (provide → node components).
// `rows` is missing when only the columns are known (`cols` request, limit 1).
const nodeStats = reactive<Record<string, NodeStats>>({})
provide('nodeStats', nodeStats)

// ── Which nodes have their result in the step cache (a halo on the node) ────────
// Asked of the server, which computes the fingerprints previews use, for the
// editor's engine. Guessing from previews would show haloes that are not true: a
// copy can give way to previews, expire after a day, or come from another user.
const cachedNodes = reactive(new Set<string>())
provide('nodeCached', cachedNodes)
let cacheAsked = ''
let cacheTimer: number | undefined
let cacheLateTimer: number | undefined

function cacheChains() {
  const nodes = getNodes.value
  const edges = getEdges.value
  const chains: { node_id: string; bucket: string; input_key: string; operations: Operation[] }[] = []
  for (const n of nodes) {
    if (n.type !== 'operation' || n.parentNode) continue // a foreach body runs per iteration
    const { sourceNode, operations } = resolveChain(nodes, edges, n.id, scriptSources.value)
    if (!sourceNode?.data?.parquetKey || !operations.length) continue
    chains.push({ node_id: n.id, bucket: sourceNode.data.bucket ?? bucket, input_key: sourceNode.data.parquetKey, operations })
  }
  return chains
}

async function refreshCacheStatus(force = false) {
  const chains = cacheChains()
  const asked = JSON.stringify([flowEngine.value, chains])
  if (!force && asked === cacheAsked) return
  cacheAsked = asked
  if (!chains.length) {
    cachedNodes.clear()
    return
  }
  try {
    const res = await api.cacheStatus({ engine: flowEngine.value, chains })
    if (asked !== cacheAsked) return // the flow changed meanwhile: a newer answer is coming
    cachedNodes.clear()
    for (const id of res.cached) cachedNodes.add(id)
  } catch {
    /* the halo is a hint: without an answer the nodes simply have none */
  }
}

function scheduleCacheStatus(delay = 600, force = false) {
  window.clearTimeout(cacheTimer)
  cacheTimer = window.setTimeout(() => refreshCacheStatus(force), delay)
}

// a preview's copy runs after its answer, at the lowest priority: ask again a little
// later, and once more after a while for a busy worker
function cacheCheckAfterPreview() {
  scheduleCacheStatus(1500, true)
  window.clearTimeout(cacheLateTimer)
  cacheLateTimer = window.setTimeout(() => refreshCacheStatus(true), 8000)
}
onUnmounted(() => {
  window.clearTimeout(cacheTimer)
  window.clearTimeout(cacheLateTimer)
})
// edge under the pointer: its component shows the removal «×»
const hoveredEdgeId = ref<string | null>(null)
provide('flowEdgeHovered', hoveredEdgeId)
// from the stroke to the «×» the pointer leaves the edge a moment before entering the
// button: without a tolerance the «×» would vanish under the cursor
let edgeLeaveTimer: number | undefined
onEdgeMouseEnter(({ edge }) => {
  window.clearTimeout(edgeLeaveTimer)
  hoveredEdgeId.value = edge.id
})
onEdgeMouseLeave(({ edge }) => {
  window.clearTimeout(edgeLeaveTimer)
  edgeLeaveTimer = window.setTimeout(() => {
    if (hoveredEdgeId.value === edge.id) hoveredEdgeId.value = null
  }, 250)
})

const preview = ref<PreviewResult | null>(null)
const previewLoading = ref(false)
const previewError = ref('')
// why there is no preview when it is not an error (the nodes after a Script node)
const previewNote = ref('')
// Whose rows `preview` holds, and the node being computed now (null: nothing in flight). The
// grid never passes a node's rows off as another's: from the click on, the rows of the node
// before are dimmed and the summary names the node being computed (critique of 2026-11-08:
// Group by selected, the Pivot's table on screen for a second and a half).
const previewNodeId = ref<string | null>(null)
const computingNodeId = ref<string | null>(null)
const computeStartedAt = ref(0)
const clock = ref(0) // ticks while something computes: the elapsed seconds
let clockTimer: ReturnType<typeof setInterval> | undefined
const previewMs = ref(0) // how long the preview on screen took (0: from this page's memory)
const rowCount = ref<RowCount>({ state: 'none' })
// «Computing…» said only past a short wait: a quick answer does not flash it (the rows
// before dim at once all the same)
const COMPUTING_WORD_MS = 300
const computingWord = ref(false)
let computingWordTimer: ReturnType<typeof setTimeout> | undefined
// the node whose preview was cancelled: «Compute again» in the grid's place
const cancelledNodeId = ref<string | null>(null)
// The step's column profile (nulls, distinct values, min and max): asked after the rows and
// their count, dropped as soon as the person looks elsewhere, like the count
const profile = ref<ProfileResult | null>(null)
const profileState = ref<'none' | 'loading' | 'done' | 'failed'>('none')
const profileError = ref('') // why the whole profile was not made, said in its place
// The previews and counts this page has seen: a step opened again shows at once, without
// asking the server (whose step cache would still recompute the last step). Same engine,
// source, chain and limit, same answer; kept a few minutes.
const PREVIEW_MEMO_MS = 10 * 60 * 1000
const previewMemo = new Recent<{ at: number; res: PreviewResult }>(40)
const countMemo = new Recent<{ at: number; rows: number; over: boolean }>(120)
const profileMemo = new Recent<{ at: number; res: ProfileResult }>(60)
const fresh = <V extends { at: number }>(e: V | undefined): V | undefined =>
  e && Date.now() - e.at < PREVIEW_MEMO_MS ? e : undefined

// the nodes that run outside the engine and hand their output to the nodes after them
const STEP_TYPES = new Set(['script', 'container'])
// the nodes only a run of the flow carries out (an orchestration on the server)
const CONTROL_TYPES = new Set(['refresh', 'runflow', 'script', 'container'])
const selectedNode = computed(() => (selectedId.value ? findNode(selectedId.value) ?? null : null))
// a data edge leaves the selected node (not a sequence edge): a Script node must write its output
const selectedHasDataOut = computed(() => !!selectedId.value
  && getEdges.value.some((e) => e.source === selectedId.value && !String(e.sourceHandle ?? '').startsWith('seq')))
const firstSource = computed(() => getNodes.value.find((n) => n.type === 'source'))
const canRun = computed(
  () =>
    !!firstSource.value?.data?.parquetKey ||
    getNodes.value.some((n) => CONTROL_TYPES.has(n.type ?? '')),
)

// ── Saved flow (persisted in the gateway) ───────────────────────────────
const flowId = ref<number | null>(route.query.flow ? Number(route.query.flow) : null)
// who ELSE has this flow open: whoever saves last overwrites, so a warning
const { others: alsoOpenBy } = useFlowPresence(flowId)
// Script nodes: the environments this person may write them for (the palette offers
// the node only when there is one)
const scriptingEditor = useScriptingEditor()
const scriptingAvailable = scriptingEditor.available
onMounted(() => { scriptingEditor.load(true) })
// Container nodes: the registries this person may take images from (the palette offers
// the node only when there is one)
const registriesEditor = useRegistriesEditor()
const containersAvailable = registriesEditor.available
onMounted(() => { registriesEditor.load(true) })
// the texts of the preview after a step node, by its kind
function stepTexts(nodeId: string): string {
  return findNode(nodeId)?.type === 'container' ? 'containers.node' : 'scripting.node'
}

// What each Script or Container node wrote last (a run of the flow, or «Run» in its panel): the
// nodes after it preview from that. Read when the flow opens, after a save, after a
// trial.
const scriptOutputs = ref<ScriptOutputs>({})
const fmt = useFormat()
const fmtWhen = fmt.dateTime
const scriptSources = computed(() => Object.fromEntries(Object.entries(scriptOutputs.value)
  .filter(([, v]) => v.output).map(([k, v]) => [k, { bucket: v.output!.bucket, key: v.output!.key }])))
async function loadScriptOutputs() {
  if (flowId.value === null || !getNodes.value.some((n) => STEP_TYPES.has(n.type ?? ''))) return
  try {
    scriptOutputs.value = await scriptingEditor.outputs(flowId.value)
  } catch { /* the nodes after a script just have no preview */ }
}

// «Run» of a Script or Container node: the SAVED flow runs it on the sample (saved first if there
// are changes), and the run is followed until it ends
interface ScriptTrial { runId: number | null; status: string; startedAt: number; error: string }
const scriptTrials = reactive<Record<string, ScriptTrial>>({})
let editorGone = false // the page was left: the trials stop following their runs
onUnmounted(() => { editorGone = true })
async function runScriptNode(nodeId: string) {
  if (deniedToObserver()) return
  const live = scriptTrials[nodeId]
  if (live && (live.status === 'PENDING' || live.status === 'STARTED')) return // a second click while it starts
  scriptTrials[nodeId] = { runId: null, status: 'PENDING', startedAt: Date.now(), error: '' }
  // a change made a moment ago (a field, a script picked) counts: the history settles
  // a change after a short quiet, and «Run» must not run the flow saved before it
  history.flush()
  if (dirty.value || flowId.value === null) await saveFlow()
  if (dirty.value || flowId.value === null) { // not saved: saveFlow said why
    delete scriptTrials[nodeId]
    return
  }
  try {
    const { run_id } = await scriptingEditor.runNode(flowId.value, nodeId)
    scriptTrials[nodeId]!.runId = run_id
    // a network hiccup or a gateway restarting does not end the trial: a few failed
    // reads in a row do, and then the run page still has the truth
    let misses = 0
    let pause = FIRST_PAUSE_MS
    const since = Date.now()
    while (!editorGone) {
      await new Promise((r) => setTimeout(r, pause))
      pause = nextPause(pause, since, 2000)
      let run
      try {
        run = await runsApi.get(run_id)
        misses = 0
      } catch (e) {
        if (++misses < 5) continue
        throw e
      }
      scriptTrials[nodeId]!.status = run.status
      if (run.status === 'SUCCESS' || run.status === 'FAILURE') {
        scriptTrials[nodeId]!.error = run.status === 'FAILURE' ? (run.error ?? '') : ''
        break
      }
    }
  } catch (e) {
    scriptTrials[nodeId] = { ...scriptTrials[nodeId]!, status: 'FAILURE', error: errMessage(e) }
  }
  if (editorGone) return
  await loadScriptOutputs()
  invalidateColumns()
  invalidateStats(nodeId)
  if (selectedId.value) refreshForNode(selectedId.value)
}
const alsoOpenLabel = computed(() => {
  const mails = [...new Set(alsoOpenBy.value.map((o) => o.email))]
  return mails.length <= 2 ? mails.join(', ') : `${mails.slice(0, 2).join(', ')} +${mails.length - 2}`
})
const projectId = ref<number | null>(route.query.project ? Number(route.query.project) : null)
const flowName = ref(t('flowEditor.unnamedFlow'))
// the flow's engine: chosen at creation (?engine=… from the Flows page) for a new flow,
// or loaded from flow.engine. Opening the editor without an explicit choice uses the
// user's FAVOURITE engine. Passed to preview/run and saved.
const { preferredEngine } = usePreferredEngine()
const flowEngine = ref<string>(route.query.engine ? String(route.query.engine) : preferredEngine.value)
// PRODUCTION engine (scheduled runs): informative only in the editor (badge); set from
// the Flows page or from the schedule dialog
const flowProductionEngine = ref<string | null>(null)

// preview/transform ALWAYS inject the current flow's engine. (`dataApi` alias: avoids the
// wrappers referencing themselves in the calls' replacements.)
const dataApi = api
// Four slots per editor — preview, columns, distinct values, chart: in each a new
// request knocks down the previous one (see usePreviewSlots).
const previewSlots = usePreviewSlots('ed')
onUnmounted(() => {
  // a refresh held in the 180 ms wait must not start any more either
  previewSeq++
  columnsSeq++
  previewSlots.cancelAll()
})
const apiPreview = (body: Parameters<typeof api.preview>[0], slot?: 'preview' | 'cols' | 'chart' | `values:${string}`) =>
  previewSlots.preview({ ...body, engine: flowEngine.value }, slot)
const apiTransform = (body: Parameters<typeof api.transform>[0]) =>
  dataApi.transform({ ...body, engine: flowEngine.value })
const projectsList = ref<{ id: number; name: string }[]>([])

// ── Nodes that make room (a step inserted in a chain) ─────────────────────
// What follows an inserted step moves one step on, over SLIDE_MS with --ease-out. The
// position IS the transform of Vue Flow's wrapper: a CSS transition there would make every
// drag rubbery and leave the edges, drawn from the positions, a step ahead of their nodes.
// So the positions themselves move, frame by frame, for the nodes of this one slide only;
// the saved form (and so the history: ONE step) holds where they are going from the start.
const SLIDE_MS = 180
const slideTo = new Map<string, { x: number; y: number }>()
let slideFrame = 0
const reducedMotion = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches
function settledPosition(n: Node): { x: number; y: number } {
  // read in any case: whoever watches the saved form keeps watching this node's position
  const { x, y } = n.position
  const to = slideTo.get(n.id)
  return to ? { x: to.x, y: to.y } : { x, y }
}
function slideNodes(moves: { id: string; x: number; y: number }[]) {
  finishSlide()
  const from = new Map<string, { x: number; y: number }>()
  for (const m of moves) {
    const n = findNode(m.id)
    if (!n) continue
    if (reducedMotion()) n.position = { x: m.x, y: m.y }
    else {
      from.set(m.id, { ...n.position })
      slideTo.set(m.id, { x: m.x, y: m.y })
    }
  }
  if (!slideTo.size) return
  // the clock starts at the second frame, not at the call: the frame that draws the new node
  // is a long one (measured: 170 ms), and --ease-out spends most of its way in its first
  // third — started at once, the slide was all but over when the first frame showed
  let t0 = 0
  let waited = false
  const frame = (now: number) => {
    if (!waited) {
      waited = true
      slideFrame = requestAnimationFrame(frame)
      return
    }
    t0 ||= now
    const k = easeOutExpo((now - t0) / SLIDE_MS)
    if (k >= 1) return finishSlide()
    for (const [id, to] of slideTo) {
      const n = findNode(id)
      const a = from.get(id)
      if (n && a) n.position = { x: a.x + (to.x - a.x) * k, y: a.y + (to.y - a.y) * k }
    }
    slideFrame = requestAnimationFrame(frame)
  }
  slideFrame = requestAnimationFrame(frame)
}
/** The slide ends now, every node where it was going (a drag starts, another change comes). */
function finishSlide() {
  cancelAnimationFrame(slideFrame)
  for (const [id, to] of slideTo) {
    const n = findNode(id)
    if (n) n.position = { ...to }
  }
  slideTo.clear()
}
/** The canvas is replaced whole (a load, an undo): nothing of the slide is written on it. */
function dropSlide() {
  cancelAnimationFrame(slideFrame)
  slideTo.clear()
}
onNodeDragStart(finishSlide)
onUnmounted(dropSlide)
/** The canvas as the chain's rules read it (composables/flowChain.ts). */
function chainNodes(): ChainNode[] {
  return getNodes.value.map((n) => ({
    id: n.id, type: n.type, position: slideTo.get(n.id) ?? n.position, parentNode: n.parentNode,
    width: n.dimensions?.width || undefined, height: n.dimensions?.height || undefined,
  }))
}
const chainEdges = () => getEdges.value as unknown as ChainEdge[]

// serializes ONLY what is needed to rebuild the canvas (no internal Vue Flow state)
function serializeCanvas(): string {
  return JSON.stringify({
    nodes: getNodes.value.map((n) => ({
      id: n.id,
      type: n.type,
      position: settledPosition(n),
      data: n.data,
      // foreach container: children (parentNode) and the box's size
      ...(n.parentNode ? { parentNode: n.parentNode } : {}),
      ...(n.type === 'foreach'
        ? { style: { width: `${n.dimensions?.width || 460}px`, height: `${n.dimensions?.height || 280}px` } }
        : {}),
      // note: persist the box's size (resizable)
      ...(n.type === 'comment'
        ? { style: { width: `${n.dimensions?.width || 220}px`, height: `${n.dimensions?.height || 120}px` } }
        : {}),
    })),
    edges: getEdges.value.map((e) => ({
      id: e.id,
      source: e.source,
      target: e.target,
      sourceHandle: e.sourceHandle ?? null,
      targetHandle: e.targetHandle ?? null,
    })),
  })
}

async function loadFlow(id: number) {
  const f = await flowsApi.get(id)
  loadBlocked.value = ''
  flowName.value = f.name
  projectId.value = f.project_id
  flowEngine.value = f.engine || 'polars'
  flowProductionEngine.value = f.production_engine ?? null
  savedName.value = f.name
  adoptReference = true
  fitView_ = true
  applyDefinition(JSON.parse(f.definition || '{}'))
  selectedId.value = null
  history.reset() // a flow just loaded has nothing to undo
  setStatus(t('flowEditor.flowLoaded', { name: f.name }), 'ok', true)
  loadScriptOutputs()
}

// Puts a definition on the canvas (the saved one, or a snapshot of the history).
// `keepCounters`: the ids' counters NEVER go back — after an undo a new node must not
// take again the id of one just gone, or it would inherit its cached columns and counts.
function applyDefinition(def: any, keepCounters = false) {
  dropSlide()
  // normalize: sources saved without a bucket (old/external flows) get the default one,
  // otherwise preview/run would start without a bucket (422)
  let nodes = (def.nodes ?? []).map((n: any) => {
    if (n.type === 'source') {
      const data = { ...n.data, bucket: n.data?.bucket ?? bucket }
      // source = catalog datasource: the saved key may be STALE (every refresh replaces the
      // snapshot and deletes the old blob) → it is always reattached to the catalog's current
      // snapshot
      const ds =
        data.datasourceId != null ? datasources.value.find((d) => d.id === data.datasourceId) : null
      if (ds && ds.key) {
        data.bucket = ds.bucket
        data.parquetKey = ds.key
        data.rows = ds.rows
        data.columns = ds.columns
        data.filename = ds.name
      }
      return { ...n, data }
    }
    if (n.parentNode) return { ...n, extent: 'parent' } // the children stay in the container
    return n
  })
  // Vue Flow wants the parents BEFORE the children in the array
  nodes = [...nodes.filter((n: any) => !n.parentNode), ...nodes.filter((n: any) => n.parentNode)]
  setNodes(nodes)
  // the SEQUENCE edges (targetHandle seq-in) get a class of their own to tell them apart
  // visually from the DATA flow (now they are all horizontal)
  setEdges((def.edges ?? []).map((e: any) => ({
    ...e,
    class: (e.targetHandle as string) === 'seq-in' ? 'edge-seq' : undefined,
  })))
  // realigns the counters to the loaded ids to avoid collisions on new nodes
  const maxN = (prefix: string) =>
    Math.max(0, ...(def.nodes ?? [])
      .filter((n: any) => String(n.id).startsWith(prefix))
      .map((n: any) => Number(String(n.id).slice(prefix.length)) || 0))
  const base = (current: number) => (keepCounters ? current : 0)
  opCounter = Math.max(base(opCounter), maxN('op-'))
  sourceCounter = Math.max(base(sourceCounter), maxN('src-'))
  outputCounter = Math.max(base(outputCounter), maxN('out-'), maxN('ctl-')) // out- and ctl- share the counter
  commentCounter = Math.max(base(commentCounter), maxN('cmt-'))
}

// ── Undo / redo ─────────────────────────────────────────────────────────
// The history sees the canvas in the same shape it is saved in: every change that
// changes `serializeCanvas()` becomes a step of its own (see useFlowHistory). It is not
// saved: it lives while the editor stays open.
const canUndo = ref(false)
const canRedo = ref(false)

// ── Unsaved changes ──────────────────────────────────────────────────────
// Before, one left the editor (Projects, Sign out, closing the tab) and the work
// vanished without a warning (audit 2026-10-11, F3). The comparison is between the
// history's still state and that of the last load or save; plus the name. An observer
// does not save: no warnings for them.
const savedSnap = ref<string | null>(null)
const currentSnap = ref<string | null>(null)
const savedName = ref('')
let adoptReference = true
// A NEW flow (never saved) also counts as unsaved as soon as it holds work: a
// source with data, a step, a link. Its reference state is adopted when the
// canvas first settles, and a choice made before that moment (a datasource picked
// right after the page appears, on a slow connection or by a script) became part
// of the reference: leaving then lost the work without a word (1.2.0
// getting-started run).
const newFlowHasWork = computed(() =>
  flowId.value === null &&
  (getEdges.value.length > 0 ||
    getNodes.value.some((n) => n.type !== 'source' || n.data?.datasourceId != null || !!n.data?.parquetKey)))
const dirty = computed(() =>
  !observerOnly.value &&
  (newFlowHasWork.value ||
    (savedSnap.value !== null &&
      (currentSnap.value !== savedSnap.value || flowName.value.trim() !== savedName.value))))

const history = createFlowHistory({
  serialize: serializeCanvas,
  restore: restoreSnapshot,
  onChange: () => {
    canUndo.value = history.canUndo()
    canRedo.value = history.canRedo()
    currentSnap.value = history.current()
    // the first still state after a load (or a new flow) is the «saved» one: the sizes the
    // canvas takes after drawing the nodes are not changes
    if (adoptReference && currentSnap.value !== null) {
      savedSnap.value = currentSnap.value
      adoptReference = false
    }
  },
})
watch(serializeCanvas, () => history.touch())
// a settled change of the flow (or of its engine) may change which nodes are cached
watch([currentSnap, flowEngine], () => scheduleCacheStatus())
onUnmounted(() => history.dispose())

function restoreSnapshot(snapshot: string) {
  // preview and columns in flight concern a canvas that is no longer there
  previewSeq++
  columnsSeq++
  previewSlots.cancel('preview')
  previewSlots.cancel('cols')
  applyDefinition(JSON.parse(snapshot), true)
  for (const k of Object.keys(nodeColumns)) delete nodeColumns[k]
  invalidateStats()
  if (selectedId.value && !findNode(selectedId.value)) selectedId.value = null
  if (selectedId.value) {
    refreshForNode(selectedId.value)
  } else {
    clearPreview()
    inputColumns.value = []
    rightColumns.value = []
  }
}

function undo() {
  if (history.undo()) setStatus(t('flowEditor.undone'), 'info')
}
function redo() {
  if (history.redo()) setStatus(t('flowEditor.redone'), 'info')
}

// the flow could not be opened (not found, or it runs images of a container registry
// this person has no access to): an empty canvas is never saved over it
const loadBlocked = ref('')

async function saveFlow() {
  if (deniedToObserver()) return
  if (loadBlocked.value) {
    setStatus(t('flowEditor.cannotSaveUnopened', { error: loadBlocked.value }), 'error')
    return
  }
  if (projectId.value === null) {
    setStatus(t('flowEditor.chooseProjectToSave'), 'error')
    return
  }
  if (!flowName.value.trim()) {
    setStatus(t('flowEditor.nameFlowPrompt'), 'error')
    return
  }
  history.flush() // a change still «hot» is part of what is saved
  const definition = serializeCanvas()
  try {
    if (flowId.value !== null) {
      await flowsApi.update(flowId.value, { name: flowName.value.trim(), definition: definition })
    } else {
      const f = await flowsApi.create(projectId.value, {
        name: flowName.value.trim(),
        definition: definition,
        engine: flowEngine.value,
      })
      flowId.value = f.id
      router.replace({ query: { flow: String(f.id) } }) // the URL now points at the saved flow
    }
    savedSnap.value = currentSnap.value = definition
    savedName.value = flowName.value.trim()
    setStatus(t('flowEditor.flowSaved', { name: flowName.value.trim() }), 'ok')
    loadScriptOutputs() // a saved change of code makes the scripts' outputs stale
  } catch (e) {
    setStatus(t('flowEditor.saveFailed', { error: errMessage(e) }), 'error')
  }
}

// Leaving with unsaved changes asks what to do: stay, leave without saving, save and
// leave. It holds for every navigation of the app; closing the tab or reloading goes
// through the browser's warning.
const leaveOpen = ref(false)
let decideLeaving: ((leave: boolean) => void) | null = null
let leaveConfirmed = false
function askToLeave(): Promise<boolean> {
  history.flush()
  if (!dirty.value) return Promise.resolve(true)
  leaveOpen.value = true
  return new Promise((resolve) => { decideLeaving = resolve })
}
function answerLeaving(leave: boolean) {
  leaveOpen.value = false
  if (leave) leaveConfirmed = true
  decideLeaving?.(leave)
  decideLeaving = null
}
async function saveAndLeave() {
  await saveFlow()
  answerLeaving(!dirty.value) // when the save did not succeed one stays, with the message in sight
}
// a sign-out the account wanted (for inactivity) does not ask about the changes: the
// session is closed already, and the dialog would hang on a save answering 401. A
// session lost for another reason (expired, revoked) still asks.
const wantedLeave = useLeaving()
onBeforeRouteLeave(() => (leaveConfirmed || wantedLeave.value ? true : askToLeave()))
// another flow opened from here replaces this editor (pages/editor.vue): the same question
onBeforeRouteUpdate((to, from) =>
  !opensAnotherFlow(to.query, from.query, flowId.value) || leaveConfirmed || wantedLeave.value ? true : askToLeave())
// which flow is open, for pages/editor.vue to tell a first save from another flow
const shownFlow = useState<number | null>('editor.shownFlow', () => null)
watch(flowId, (id) => { shownFlow.value = id }, { immediate: true })
// signing out of the account goes through here BEFORE removing the session: a «stay»
// after the logout would leave the editor open without a session
async function leave() {
  if (!(await askToLeave())) return
  leaveConfirmed = true
  logout()
}
function browserWarning(e: BeforeUnloadEvent) {
  history.flush()
  if (!dirty.value) return
  e.preventDefault()
  e.returnValue = ''
}
onMounted(() => window.addEventListener('beforeunload', browserWarning))
onUnmounted(() => window.removeEventListener('beforeunload', browserWarning))

// The view fits when the loaded nodes have their sizes: fitting it at start (before the
// flow arrives from the server) zoomed to 2× on emptiness. `maxZoom: 1`: a small flow
// stays at natural size.
let fitView_ = false
onNodesInitialized(() => {
  if (!fitView_) return
  fitView_ = false
  fitReadable()
})
// A flow too long to be read whole does not open as a row of unreadable rectangles (twelve
// nodes fitted at 0.35): it opens at the zoom nodes are read at, on its END — the outputs,
// where the result is and the next step goes — and the rest is in the minimap. A flow that
// fits at a legible zoom is fitted whole, as before. The Controls' «fit view» button still
// shows everything.
function fitReadable(duration = 0) {
  const rects = getNodes.value
    .filter((n) => !n.parentNode && !n.hidden && (n.dimensions?.width ?? 0) > 0)
    .map((n) => ({ id: n.id, x: n.position.x, y: n.position.y, width: n.dimensions.width, height: n.dimensions.height }))
  const ends = new Set(flowEndIds(chainNodes(), chainEdges()))
  const pane = paneSize.value
  // the minimap sits bottom right: on a pane wide enough the flow's end stops at its left
  const at = openingViewport(rects, rects.filter((r) => ends.has(r.id)), pane, { marginRight: pane.width > 760 ? 220 : 48 })
  if (reducedMotion()) duration = 0
  if (at) setViewport(at, { duration })
  else fitView({ padding: 0.15, maxZoom: 1, duration })
}

// ── The assistant beside the node panel ───────────────────────────────────
// A second tab of the right panel, when the installation has an AI model and the
// user may change flows. What it builds comes back as a whole canvas, applied here
// as one Undo step; the nodes it touched glow for a moment.
const aiStatus = ref<AiStatus | null>(null)
const aiReady = computed(() => !!aiStatus.value?.enabled && !!aiStatus.value.models.length && !observerOnly.value)
const rightTab = ref<'node' | 'assistant'>('node')
// the node the user is pointing the assistant at: not the empty source a new flow starts with
const assistantSelection = computed(() => {
  const n = selectedNode.value
  if (!n) return null
  if (n.type === 'source' && n.data?.datasourceId == null && !n.data?.parquetKey) return null
  return n.id
})
onMounted(async () => {
  try {
    aiStatus.value = await useAi().status()
  } catch {
    aiStatus.value = null // no assistant: the panel stays as it was
  }
})

// the ids this editor has handed out: the assistant numbers its nodes after them
const assistantCounters = () => ({ 'op-': opCounter, 'src-': sourceCounter, 'out-': outputCounter })

function applyAssistant(change: { definition: string; touched: string[] }) {
  let def: any
  try {
    def = JSON.parse(change.definition)
  } catch {
    return
  }
  history.flush() // what the user was doing stays a step of its own
  const existing = new Set(getNodes.value.map((n) => n.id))
  applyDefinition(def, true)
  invalidateColumns()
  invalidateStats()
  const touched = change.touched.filter((id) => findNode(id))
  for (const id of touched) {
    const n = findNode(id)
    if (n) n.class = 'ai-touched'
  }
  setTimeout(() => {
    for (const id of touched) {
      const n = findNode(id)
      if (n && n.class === 'ai-touched') n.class = undefined
    }
  }, 2600)
  const last = touched[touched.length - 1]
  if (last) {
    // as a click on it would: its settings in the panel, its rows in the grid
    selectedId.value = last
    refreshForNode(last)
  }
  // new nodes are placed beside the ones they read, possibly out of view: show the flow
  // whole once they are measured (fitView leaves out a node it cannot measure yet)
  const added = touched.filter((id) => !existing.has(id))
  if (added.length) fitWhenMeasured(added)
}

function fitWhenMeasured(ids: string[], frames = 30) {
  const measured = ids.every((id) => (findNode(id)?.dimensions?.width ?? 0) > 0)
  if (measured || frames <= 0) fitReadable(300)
  else requestAnimationFrame(() => fitWhenMeasured(ids, frames - 1))
}

onMounted(async () => {
  if (!signedIn.value) await fetchMe()
  // side by side, one round trip instead of four in a row: the app's settings (the
  // automatic development sample must be known BEFORE the first preview), the operations,
  // the projects' list (the toolbar's selector AND the run dialog) and the catalog, which
  // comes BEFORE the flow: loadFlow reattaches the datasource sources to the current
  // snapshot (the saved keys become stale at every refresh)
  const [, ops] = await Promise.all([
    useAppInfo().load(),
    api.operations().then((o) => ({ ok: true as const, o }), (e) => ({ ok: false as const, e })),
    projectsApi.list().then((p) => { projectsList.value = p }, () => { projectsList.value = [] }),
    refreshDatasources(),
  ])
  if (!ops.ok) {
    setStatus(t('flowEditor.backendUnreachable', { error: errMessage(ops.e) }), 'error')
    return
  }
  operations.value = ops.o
  refreshConnections() // for the Output node: may come later, does not block the load
  refreshFlows() // for the "Run flow" node
  try {
    if (flowId.value !== null) await loadFlow(flowId.value)
  } catch (e) {
    loadBlocked.value = errMessage(e)
    setStatus(t('flowEditor.flowLoadFailed', { error: loadBlocked.value }), 'error')
    // nothing on the canvas: it is not a new flow, and nothing of this one is shown
    applyDefinition({ nodes: [], edges: [] })
    selectedId.value = null
  }
  if (flowId.value === null && route.query.ds) {
    // «New flow from here» (Explore's datasource panel, ?ds=<id>): the source starts on that
    // datasource, as if picked in its panel — part of the starting canvas, not a change
    const fromDs = Number(route.query.ds)
    const ds = Number.isInteger(fromDs) && fromDs > 0 ? datasources.value.find((d) => d.id === fromDs) : undefined
    if (ds) {
      updateNodeData(SOURCE_ID, {
        datasourceId: ds.id, datasetId: null, bucket: ds.bucket, parquetKey: ds.key,
        filename: ds.name, sheet: null, rows: ds.rows, columns: ds.columns,
      })
    }
  }
  if (flowId.value === null) {
    // new flow: it starts from the initial canvas, which is also the «saved» reference,
    // and so is its starting name (before: '' against «Untitled flow», and leaving an
    // untouched new flow asked about «unsaved changes»)
    savedName.value = flowName.value.trim()
    adoptReference = true
    fitView_ = true
    history.reset()
  }
})

// ── Datasource catalog (for the source node's picker) ────────────────
const datasources = ref<DatasourceInfo[]>([])
// the data contract state of each datasource, for the icon on the source nodes
provide('datasourceContracts', computed(() => new Map(datasources.value.filter((d) => d.contract).map((d) => [d.id, d.contract!] as const))))

// Reattaches the datasource sources already on the canvas to the catalog's CURRENT
// snapshot: the key in the node may be STALE (a refresh/overwrite replaces the snapshot
// and deletes the old blob) and the preview would use it → 404. It does it by id, as the
// server-side scheduler. It covers the nested sources too (join/foreach): they are all
// 'source' nodes. At the FIRST load the canvas is empty (no-op) and loadFlow sees to
// it; then every refresh of the catalog synchronizes again.
function syncSourceKeys() {
  let changed = false
  for (const n of getNodes.value) {
    if (n.type !== 'source' || n.data?.datasourceId == null) continue
    const ds = datasources.value.find((d) => d.id === n.data.datasourceId)
    if (ds && ds.key && (n.data.parquetKey !== ds.key || n.data.bucket !== ds.bucket)) {
      updateNodeData(n.id, { bucket: ds.bucket, parquetKey: ds.key, rows: ds.rows, columns: ds.columns })
      changed = true
    }
  }
  if (changed) { invalidateColumns(); invalidateStats() } // the chains downstream must be recomputed on the new snapshot
}

async function refreshDatasources() {
  try {
    datasources.value = await dsApi.list()
    // reattaching a source to its new snapshot is not a user's change: undoing it would
    // bring back a key that no longer exists
    history.quietly(syncSourceKeys)
  } catch {
    datasources.value = []
  }
}

// ── Usable connections (for the Output node towards a database) ──────────
const connectionsList = ref<ConnectionInfo[]>([])
async function refreshConnections() {
  try {
    connectionsList.value = await connApi.list()
  } catch {
    connectionsList.value = []
  }
}

// ── Flows (for the "Run flow" node's picker) ─────────────────────────────
const flowsList = ref<{ id: number; name: string; project_id: number }[]>([])
async function refreshFlows() {
  try {
    flowsList.value = (await flowsApi.list()).map((f) => ({ id: f.id, name: f.name, project_id: f.project_id }))
  } catch {
    flowsList.value = []
  }
}

// ── Canvas events ─────────────────────────────────────────────────────────
// Nodes downstream of `fromId` along the DATA edges (fromId included): their counts no
// longer hold when the chain upstream changes.
function invalidateStats(fromId?: string, columnsToo = false) {
  if (!fromId) {
    for (const k of Object.keys(nodeStats)) if (findNode(k)?.type !== 'source') delete nodeStats[k]
    return
  }
  const seen = new Set<string>()
  const queue = [fromId]
  while (queue.length) {
    const id = queue.shift()!
    if (seen.has(id)) continue
    seen.add(id)
    delete nodeStats[id]
    if (columnsToo && findNode(id)?.type !== 'source') delete nodeColumns[id]
    for (const e of getEdges.value) {
      if (e.source === id && (e.targetHandle as string) !== 'seq-in') queue.push(e.target)
    }
  }
}

function removeEdgeById(id: string) {
  const edge = getEdges.value.find((e) => e.id === id)
  if (!edge) return
  const isSeq = (edge.targetHandle as string) === 'seq-in'
  removeEdges([id])
  if (hoveredEdgeId.value === id) hoveredEdgeId.value = null
  if (isSeq) return
  // without that edge the node downstream (and all that follows) has another input
  invalidateColumns()
  invalidateStats(edge.target)
  if (selectedId.value) refreshForNode(selectedId.value)
}
provide('flowEdgeRemove', removeEdgeById)

onConnect((conn: Connection) => {
  const seqSource = (conn.sourceHandle as string) === 'seq-out'
  const seqTarget = (conn.targetHandle as string) === 'seq-in'
  // the SEQUENCE edges connect only seq-out (right) → seq-in (left): a source/data entry has
  // no sequence socket, so no mixing with the data
  if (seqSource !== seqTarget) {
    setStatus(t('flowEditor.seqConnectError'), 'error')
    return
  }
  const handle = (conn.targetHandle as string) || 'left'
  // data: one edge per (target, handle). Sequence: several predecessors are valid
  // (fan-in), only an identical duplicate edge is removed.
  const dup = getEdges.value.filter((e) =>
    seqTarget
      ? e.source === conn.source && e.target === conn.target && (e.targetHandle as string) === 'seq-in'
      : e.target === conn.target && ((e.targetHandle as string) || 'left') === handle,
  )
  if (dup.length) removeEdges(dup.map((e) => e.id))
  addEdges({
    id: `e-${conn.source}-${handle}-${conn.target}`,
    source: conn.source!,
    target: conn.target!,
    sourceHandle: conn.sourceHandle ?? undefined,
    targetHandle: conn.targetHandle ?? undefined,
    class: seqTarget ? 'edge-seq' : undefined,
  })
  if (!seqTarget) {
    // only the DATA edges change the columns downstream; the sequence does not
    invalidateColumns()
    invalidateStats(conn.target!)
    refreshForNode(conn.target!)
  } else {
    autolinkRefreshToSource(conn.source!, conn.target!)
  }
})

// Connecting «Refresh datasource → Source» sets the source's datasource on the Refresh
// (only when it is a DATABASE datasource, the only refreshable ones), so the edge really
// means "update THIS source's datasource".
function autolinkRefreshToSource(sourceId: string, targetId: string) {
  const refresh = findNode(sourceId)
  const src = findNode(targetId)
  if (refresh?.type !== 'refresh' || src?.type !== 'source') return
  const dsId = src.data?.datasourceId
  if (dsId == null) return
  const ds = datasources.value.find((d) => d.id === dsId)
  if (!ds || ds.kind !== 'database') return
  updateNodeData(refresh.id, { datasourceId: ds.id, dsName: ds.name })
  setStatus(t('flowEditor.refreshLinked', { name: ds.name }), 'ok')
}

onNodeClick(({ node }) => {
  selectedId.value = node.id
  refreshForNode(node.id)
})
// The panel and the preview follow the canvas' own selection when it is ONE node, however it
// was made: Tab and Enter on a node selected it on the canvas and left the panel on the node
// before (critique of 2026-11-10). Through the click's own path — the pause that lets a
// person run over the nodes, the page's memory — so it asks the engine nothing a click
// would not. A click has set `selectedId` already when this runs: nothing is asked twice.
watch(() => (getSelectedNodes.value.length === 1 ? getSelectedNodes.value[0]!.id : null), (id) => {
  if (!id || id === selectedId.value || !findNode(id)) return
  selectedId.value = id
  refreshForNode(id)
})

// ── Walking the graph from the keyboard ───────────────────────────────────
// Alt+← and Alt+→: the step before and the step after; Alt+↑ and Alt+↓: the sibling branch
// above and below (composables/flowChain.ts). The node reached is selected as a click
// selects it, takes the focus, and is brought into view.
const WALKS: Record<string, Walk> = { ArrowLeft: 'upstream', ArrowRight: 'downstream', ArrowUp: 'above', ArrowDown: 'below' }
function walkGraph(way: Walk) {
  const focused = (document.activeElement as HTMLElement | null)?.closest?.('.vue-flow__node') as HTMLElement | null
  const from = focused?.dataset.id ?? selectedId.value
  // nowhere yet: the walk starts at the flow's first source
  const to = from && findNode(from) ? chainNeighbour(chainNodes(), chainEdges(), from, way) : firstSource.value?.id ?? null
  if (!to) return
  if (selectedId.value !== to) {
    selectedId.value = to
    refreshForNode(to)
  }
  selectOnCanvas(to)
  nextTick(() => {
    const el = canvasEl.value?.querySelector(`.vue-flow__node[data-id="${CSS.escape(to)}"]`) as HTMLElement | null
    el?.focus({ preventScroll: true })
    revealNode(to)
  })
}
// On the canvas, before Vue Flow sees the key: an arrow on a selected node moved it by a few
// pixels (Vue Flow's own keyboard), so that walking the nodes with the arrows left the flow
// «unsaved» and its nodes out of line. A bare arrow moves nothing now; Shift+arrow still
// moves the node, for whoever lays a flow out without a mouse; Alt+arrow walks the graph.
function onCanvasKeys(e: KeyboardEvent) {
  const way = WALKS[e.key]
  const el = e.target as HTMLElement | null
  if (!way || !el || el.closest('input, textarea, select, [contenteditable="true"], .nokey')) return
  if (e.altKey && !e.ctrlKey && !e.metaKey) {
    e.preventDefault() // Alt+← is the browser's «back»
    e.stopPropagation()
    walkGraph(way)
  } else if (!e.shiftKey && el.closest('.vue-flow__node, .vue-flow__nodesselection-rect')) {
    e.preventDefault()
    e.stopPropagation()
  }
}

onPaneClick(() => {
  selectedId.value = null
})

// ── Actions ─────────────────────────────────────────────────────────────
function targetSourceId(): string {
  const sel = selectedNode.value
  return sel?.type === 'source' ? sel.id : (firstSource.value?.id ?? SOURCE_ID)
}

async function onUpload(file: File) {
  if (deniedToObserver()) return
  const sid = targetSourceId()
  await loadFile(sid, file, undefined, (res) => {
    // a workbook with several sheets: which ones, while the first one converts
    if ((res.sheets?.length ?? 0) > 1) {
      sheetChoice.value = { file, sid, sheets: res.sheets!, hidden: res.hidden_sheets ?? [], read: res.sheet ?? res.sheets![0] }
    }
  })
}

// ── Sheets of a workbook ─────────────────────────────────────────────────
const sheetChoice = ref<{ file: File; sid: string; sheets: string[]; hidden: string[]; read: string } | null>(null)

// every sheet ticked is a source, read from the same file again; the sheet already
// read stays on its source when it is ticked, or that source takes the first one
async function importSheets(names: string[]) {
  const c = sheetChoice.value
  sheetChoice.value = null
  if (!c || !names.length) return
  const rest = names.filter((n) => n !== c.read)
  if (!names.includes(c.read) && findNode(c.sid)) await loadFile(c.sid, c.file, rest.shift())
  for (const name of rest) {
    const id = addSource()
    // a sheet that cannot be read (empty, say) leaves no empty source behind: the status says why
    if (!(await loadFile(id, c.file, name))) removeNodes(id, true)
  }
  // the new sources stand below the others: the whole canvas in view
  if (rest.length) nextTick(() => fitReadable(300))
}

// uploads a file into a source (a workbook: `sheet`, or the first visible one);
// `uploaded` sees the answer before a big file's conversion is waited for
async function loadFile(sid: string, file: File, sheet?: string, uploaded?: (res: IngestResult) => void): Promise<boolean> {
  busy.value = true
  setStatus(t('flowEditor.uploading', { name: sheet ? `${file.name} · ${sheet}` : file.name }), 'busy')
  try {
    const res = await api.uploadFile(file, undefined, sheet)
    updateNodeData(sid, {
      datasetId: res.dataset_id,
      datasourceId: null, // the upload detaches the datasource chosen earlier, if any
      parquetKey: res.parquet_key,
      filename: file.name,
      sheet: res.sheet ?? null,
      rows: res.dataset?.rows ?? null,
      columns: res.dataset?.columns ?? [],
    })
    selectedId.value = sid
    uploaded?.(res)

    if (res.status === 'ready') {
      // small file: conversion done synchronously already
      nodeColumns[sid] = res.dataset?.columns ?? []
      setStatus(t('flowEditor.ready', { rows: res.dataset?.rows }, res.dataset?.rows ?? 2), 'ok')
      await refreshForNode(sid)
    } else {
      // big file: async conversion on Celery → wait for it to complete
      setStatus(t('flowEditor.convertingTask', { id: res.task_id }), 'busy')
      await pollConversion(sid, res.task_id!, res.parquet_key)
    }
    return true
  } catch (e) {
    setStatus(t('flowEditor.uploadFailed', { error: errMessage(e) }), 'error')
    return false
  } finally {
    busy.value = false
  }
}

// Waits for a big file's async conversion, then fills the source.
async function pollConversion(sid: string, taskId: string, parquetKey: string) {
  for (let i = 0; i < 1800; i++) {
    // ~2s * 1800 = up to ~1h (equal to the backend's task_time_limit)
    await new Promise((r) => setTimeout(r, 2000))
    let st
    try {
      st = await api.taskStatus(taskId)
    } catch {
      continue
    }
    // the source has taken another file or sheet meanwhile: this conversion is not its
    if (findNode(sid)?.data?.parquetKey !== parquetKey) return
    if (st.status === 'SUCCESS') {
      const info: any = st.result ?? {}
      // the end of the conversion completes the upload: it is not a step of its own
      history.quietly(() => updateNodeData(sid, { rows: info.rows ?? null, columns: info.columns ?? [] }))
      nodeColumns[sid] = info.columns ?? []
      setStatus(t('flowEditor.ready', { rows: info.rows }, info.rows ?? 2), 'ok')
      await refreshForNode(sid)
      return
    }
    if (st.status === 'FAILURE') {
      setStatus(t('flowEditor.conversionFailed', { error: st.error }), 'error')
      return
    }
    setStatus(t('flowEditor.convertingStatus', { status: st.status }), 'busy')
  }
  setStatus(t('flowEditor.conversionTimeout'), 'error')
}

function addSource(): string {
  if (deniedToObserver()) return ''
  const id = `src-${++sourceCounter}`
  addNodes({
    id,
    type: 'source',
    position: { x: 60, y: 140 + sourceCounter * 150 },
    data: { bucket, datasetId: null, parquetKey: null, filename: null, rows: null, columns: [] },
  })
  selectedId.value = id
  return id
}

// ── Adding a step ───────────────────────────────────────────────────────
// One rule for every way of adding a transformation to a node (the right-click menu by
// mouse and by keyboard, «+ Operation», a drop from the palette on a node or on an edge,
// a value of a column's profile): composables/flowChain.ts. A node that feeds one node
// takes the step BETWEEN the two, and what follows moves one step on; at the end of a
// chain it is appended; a node that forks already, or Shift, takes one more branch.
// Before, the three ways gave three outcomes, and always a branch (critique of 2026-11-10).
// Everything happens in one go: ONE step of the history undoes the whole insertion.
function addStep(parentId: string, opType: string, params: Record<string, any>, opts: StepOptions & { reveal?: boolean } = {}): string | null {
  finishSlide() // a slide still going: the plan is made on where the nodes end up
  const id = `op-${opCounter + 1}`
  const plan = planStep(chainNodes(), chainEdges(), parentId, id, opts)
  if (!plan) return null
  opCounter++
  fitView_ = false // the node goes where it was put: a new flow's pending fit must not move the view
  history.flush() // what the person was doing stays a step of its own
  addNodes({
    id, type: 'operation', position: plan.position,
    ...(plan.parentNode ? { parentNode: plan.parentNode, extent: 'parent' as const } : {}),
    data: { opType, params },
  })
  if (plan.removeEdgeIds.length) removeEdges(plan.removeEdgeIds)
  addEdges(plan.addEdges.map((e) => ({ ...e, sourceHandle: e.sourceHandle ?? undefined, targetHandle: e.targetHandle ?? undefined })))
  if (plan.resize) {
    const box = findNode(plan.resize.id)
    if (box) box.style = { ...(box.style as object), width: `${plan.resize.width}px` }
  }
  slideNodes(plan.moves)
  if (plan.inserted) {
    // what follows reads the new step from now on: its columns and counts no longer hold.
    // Only what follows: the columns of the nodes BEFORE are what the new step's panel
    // needs, and forgetting them too cost a request to read them again (counted on kind)
    invalidateStats(plan.addEdges[1]!.target, true)
  }
  selectedId.value = id
  refreshForNode(id)
  nextTick(() => {
    selectOnCanvas(id)
    if (opts.reveal) revealNode(id)
  })
  return id
}

/** Brings a node into view when it is not whole on the canvas, at the zoom there is. */
function revealNode(id: string) {
  const n = findNode(id)
  if (!n) return
  const { x, y, zoom } = getViewport()
  const w = n.dimensions?.width || 170
  const h = n.dimensions?.height || 70
  const left = n.computedPosition.x * zoom + x
  const top = n.computedPosition.y * zoom + y
  const pane = paneSize.value
  if (left >= 8 && top >= 8 && left + w * zoom <= pane.width - 8 && top + h * zoom <= pane.height - 8) return
  setCenter(n.computedPosition.x + w / 2, n.computedPosition.y + h / 2, { zoom, duration: reducedMotion() ? 0 : 300 })
}

// «+ Operation»: the same menu the right click opens, beside the node the step is for
// (the selected one, or the flow's last). Before, it always made the first transformation
// of the list, on top of the next node.
function addOperation() {
  if (loadBlocked.value || !operations.value.length) return
  const parentId = selectedId.value ?? leafNodeId(getNodes.value, getEdges.value)
  if (!findNode(parentId)) return
  if (shortcutItems.value.length) openQuickMenuBeside(parentId)
  // a person who emptied their menu still gets a step: the first of the list, by the same rule
  else addStep(parentId, operations.value[0]!, defaultParams(operations.value[0]!), { reveal: true })
}

// ── Right-click menu: the person's transformations around the click ──────
// Right-click on the canvas opens the ring (components/ui/FlowQuickMenu.vue) where the
// click was; on a node, it selects that node first. A choice creates the operation AT the
// click, linked from the selected node as «+» links it. The choices are the person's
// (GET/PUT /users/me/flow-shortcuts), the defaults until they choose.
const canvasEl = ref<HTMLElement | null>(null)
const quickMenu = ref<InstanceType<typeof FlowQuickMenu> | null>(null)
const savedShortcuts = ref<string[] | null>(null)
const shortcutsOpen = ref(false)
const shortcutsSaving = ref(false)
const shortcutItems = computed(() => flowShortcutsShown(savedShortcuts.value, operations.value))
let quickAt: { x: number; y: number } | null = null // the click, in flow coordinates
// the node the step is for, and whether the step stands beside it (a right click ON a
// node, the keyboard, «+ Operation») or at the click (a right click on the empty canvas)
let quickParent: string | null = null
let quickBeside = false
// node types whose data output an operation can read (the «out» handle)
const DATA_PARENTS = new Set(['source', 'operation', 'foreach', 'script', 'container'])

onMounted(() => {
  api.myFlowShortcuts().then((s) => { savedShortcuts.value = s }, () => { /* the defaults */ })
})

function canvasBounds() {
  const r = canvasEl.value!.getBoundingClientRect()
  return { left: r.left, top: r.top, right: r.right, bottom: r.bottom }
}
function openQuickMenu(ev: MouseEvent, onNode?: string) {
  ev.preventDefault()
  if (loadBlocked.value || !operations.value.length || !canvasEl.value) return
  quickAt = screenToFlowCoordinate({ x: ev.clientX, y: ev.clientY })
  // on a node: the step is for THAT node and stands beside it, never on top of it (a foreach
  // container is a place to put a node in: there the click is a place, as on the canvas)
  const beside = onNode && findNode(onNode)?.type !== 'foreach' ? onNode : null
  quickParent = beside ?? selectedId.value
  quickBeside = !!beside
  quickMenu.value?.show({ x: ev.clientX, y: ev.clientY }, canvasBounds())
}
// Without a pointer on the canvas (the Menu key, Shift+F10, «+ Operation») the menu opens
// beside the node the step is for, where the step will stand; with no such node, in the
// canvas' middle, and the node is born there.
function openQuickMenuBeside(parentId: string | null, keyboard = false) {
  if (loadBlocked.value || !operations.value.length || !canvasEl.value) return
  const pane = (canvasEl.value.querySelector('.vue-flow') as HTMLElement | null) ?? canvasEl.value
  const p = pane.getBoundingClientRect()
  const { x, y, zoom } = getViewport()
  const parent = parentId ? findNode(parentId) : undefined
  quickAt = parent
    ? { x: parent.computedPosition.x + FLOW_STEP_X, y: parent.computedPosition.y }
    : screenToFlowCoordinate({ x: p.left + p.width / 2, y: p.top + p.height / 2 })
  quickParent = parent?.id ?? null
  quickBeside = !!parent
  quickMenu.value?.show({ x: p.left + x + quickAt.x * zoom, y: p.top + y + quickAt.y * zoom }, canvasBounds(), keyboard)
}
function openQuickMenuFromKeyboard() {
  openQuickMenuBeside(selectedId.value, true)
}
// a right button (button 2) opens it at the pointer; the keyboard's contextmenu reaches
// onCanvasContextMenu
onPaneContextMenu((ev) => { if ((ev as MouseEvent).button === 2) openQuickMenu(ev as MouseEvent) })
// the canvas' own selection follows, as after a click
function selectOnCanvas(id: string) {
  const n = findNode(id)
  if (!n) return
  removeSelectedNodes(getSelectedNodes.value)
  addSelectedNodes([n])
}
onNodeContextMenu(({ event, node }) => {
  if ((event as MouseEvent).button !== 2) return
  if (selectedId.value !== node.id) {
    selectedId.value = node.id
    refreshForNode(node.id)
  }
  selectOnCanvas(node.id)
  openQuickMenu(event as MouseEvent, node.id)
})
onMoveStart(() => quickMenu.value?.hide())

// the browser's own menu has nothing to do on the canvas, except in a field of a node
function onCanvasContextMenu(ev: MouseEvent) {
  const el = ev.target as HTMLElement | null
  if (el?.closest('input, textarea, [contenteditable="true"]')) return
  ev.preventDefault()
  if (ev.button === 2) return // the pointer's: the pane and the nodes open it
  // the keyboard's, on the focused node (it becomes the selected one) or on the canvas
  const id = (el?.closest('.vue-flow__node') as HTMLElement | null)?.dataset.id
  if (id && findNode(id)) {
    if (selectedId.value !== id) {
      selectedId.value = id
      refreshForNode(id)
    }
    selectOnCanvas(id)
  }
  openQuickMenuFromKeyboard()
}

function onQuickMenuClose(reason: string) {
  if (reason === 'escape') canvasEl.value?.focus()
}

/** The foreach container a point of the canvas falls in. */
function containerAt(position: { x: number; y: number }) {
  return getNodes.value.find(
    (n) =>
      n.type === 'foreach' &&
      position.x > n.position.x &&
      position.x < n.position.x + (n.dimensions?.width ?? 0) &&
      position.y > n.position.y &&
      position.y < n.position.y + (n.dimensions?.height ?? 0),
  )
}

// A choice of the menu: a step for the node the menu was opened for, by the chain's rule
// (`branch`: Shift was held); a free node at the click when there is no such node.
function addOperationAt(opType: string, branch = false) {
  if (!quickAt) return
  const position = quickAt
  const parent = quickParent ? findNode(quickParent) : undefined
  const container = containerAt(position)
  // at a click INSIDE a foreach container the node is its child, as a drop there: a step of
  // a node of that body, otherwise free in it
  const inBody = !quickBeside && !!container && container.id !== parent?.parentNode
  if (parent && DATA_PARENTS.has(parent.type ?? '') && !inBody) {
    // the place the person pointed at, in the coordinates of the parent's container
    const box = parent.parentNode ? findNode(parent.parentNode) : undefined
    const at = quickBeside ? undefined : { x: position.x - (box?.position.x ?? 0), y: position.y - (box?.position.y ?? 0) }
    if (addStep(parent.id, opType, defaultParams(opType), { branch, at, reveal: quickBeside })) return
  }
  fitView_ = false // the node goes where the person put it: a new flow's pending fit must not move the view
  const id = `op-${++opCounter}`
  if (container) {
    addNodes({
      id, type: 'operation', parentNode: container.id, extent: 'parent',
      position: { x: position.x - container.position.x, y: position.y - container.position.y },
      data: { opType, params: defaultParams(opType) },
    })
  } else {
    addNodes({ id, type: 'operation', position, data: { opType, params: defaultParams(opType) } })
  }
  selectedId.value = id
  refreshForNode(id)
  nextTick(() => selectOnCanvas(id))
}

async function saveShortcuts(list: string[] | null) {
  shortcutsSaving.value = true
  try {
    savedShortcuts.value = await api.setMyFlowShortcuts(list)
    shortcutsOpen.value = false
    setStatus(t('quickMenu.saved'), 'ok')
  } catch (e) {
    setStatus(t('quickMenu.saveFailed', { err: errMessage(e) }), 'error')
  } finally {
    shortcutsSaving.value = false
  }
}

// ── The editor's keys, listed ─────────────────────────────────────────────
// A keyboard button in the canvas' top right corner (always there, whatever is selected)
// opens the list of EVERY key that works here, in groups; «?» opens it too. The same quiet
// button and panel as the data grid's (components/ui/GridKeys.vue), and the same list as
// the top bar's on the other pages (composables/useKeyHelp.ts): the editor DECLARES its
// groups, the keys of everywhere and of the table on screen come with them. Each line is a
// key some handler of the editor really takes (`onKeydown`, `onCanvasKeys`, the quick menu,
// Vue Flow's own, the dividers): a key added there is added here. Every action has a way
// with no function key and no Home, End, Del nor Menu key (a Mac has none of them). The
// quick menu's CHOICES are another thing (FlowShortcutsDialog, «Customize» in the ring).
const canvasKeys = ref<InstanceType<typeof GridKeys> | null>(null)
const canvasLocked = computed(() => !(nodesDraggable.value || nodesConnectable.value || elementsSelectable.value))
const keyHelp = useKeyHelp()
keyHelp.host(() => canvasKeys.value?.toggle())
useKeyGroups(() => {
  const k = keyHelp.names
  const nodes: [string, string][] = [
    [`Tab · ${k.enter}`, t('canvasKeys.tabEnter')],
    [`${k.chord(k.alt, '←')} · ${k.chord(k.alt, '→')}`, t('canvasKeys.walk')],
    [`${k.chord(k.alt, '↑')} · ${k.chord(k.alt, '↓')}`, t('canvasKeys.branches')],
    [k.chord(k.shift, '← ↑ → ↓'), t('canvasKeys.move')],
    [k.ways({ pc: ['Del'], any: [k.backspace] }), t('canvasKeys.remove')],
    [['C', 'X', 'V'].map((c) => k.chord(k.mod, c)).join(' · '), t('canvasKeys.clipboard')],
    [k.chord(k.mod, 'D'), t('canvasKeys.duplicate')],
    [k.chord(k.mod, 'Z'), t('canvasKeys.undo')],
    // ⌘Y is the browser's history on a Mac
    [k.ways({ pc: ['Ctrl+Y'], any: [k.chord(k.mod, k.shift, 'Z')] }), t('canvasKeys.redo')],
  ]
  const steps: [string, string][] = [
    [k.ways({ pc: ['Shift+F10'], any: ['m'] }), t('canvasKeys.menu')],
    [`← → · ${k.enter}`, t('canvasKeys.menuPick')],
    [k.chord(k.shift, k.enter), t('canvasKeys.branch')],
    ['Tab · Esc', t('canvasKeys.menuOther')],
  ]
  const running: [string, string][] = [
    ...(observerOnly.value ? [] : [[k.chord(k.mod, k.enter), t('canvasKeys.run')] as [string, string]]),
    ['Esc', t('canvasKeys.stop')],
    [k.chord(k.mod, k.enter), t('canvasKeys.sqlPreview')],
  ]
  const rest: [string, string][] = [
    ...(observerOnly.value ? [] : [[k.chord(k.mod, 'S'), t('canvasKeys.save')] as [string, string]]),
    ['← → · ↑ ↓', t('canvasKeys.dividers')],
  ]
  return [
    { title: t('canvasKeys.groupNodes'), keys: nodes },
    { title: t('canvasKeys.groupSteps'), keys: steps },
    // the two Ctrl+Enter are told apart by their words: the flow's, and the SQL field's
    { title: t('canvasKeys.groupRun'), keys: running },
    { title: t('canvasKeys.groupRest'), keys: rest },
  ]
})

// ── Minimap: each node in the colour of its edge on the canvas ────────────
const NODE_TYPE_COLOR: Record<string, string> = {
  source: 'var(--accent-2)',
  output: '#fbbf24',
  refresh: '#38bdf8',
  runflow: '#c084fc',
  script: '#34d399',
  container: '#818cf8',
  foreach: '#f472b6',
  comment: 'var(--border)',
}
function miniMapColor(node: { type?: string; data?: any }): string {
  if (node.type === 'operation') return opMeta(node.data?.opType ?? '').color
  return NODE_TYPE_COLOR[node.type ?? ''] ?? 'var(--muted)'
}

// ── Auto-layout ("Tidy the flow", a button in the canvas Controls) ────
function autoLayout() {
  finishSlide()
  const { positions, childPositions, containerSizes } = computeAutoLayout(getNodes.value, getEdges.value)
  for (const [id, size] of containerSizes) {
    const n = findNode(id)
    if (n) n.style = { ...(n.style as object), width: `${size.width}px`, height: `${size.height}px` }
  }
  for (const [id, pos] of positions) {
    const n = findNode(id)
    if (n) n.position = pos
  }
  for (const [id, pos] of childPositions) {
    const n = findNode(id)
    if (n) n.position = pos
  }
  nextTick(() => fitView({ padding: 0.15, duration: 300 }))
}

// ── Drag & drop from the components' sidebar ─────────────────────────────
// A transformation dragged from the palette: over an edge it will go IN there, over a node
// it will be that node's next step (the chain's rule), over the empty canvas it stays a
// free node. What it is over is lit while it is dragged, so the drop is no surprise. The
// payload cannot be read before the drop: every drag of ours lights its target, and the
// drop decides (a Source or an Output dropped on a node still lands free, as before).
let dropLit: Element | null = null
function dropTarget(ev: DragEvent): { edge?: string; node?: string; el: Element } | null {
  const el = ev.target as Element | null
  const edgeEl = el?.closest?.('.vue-flow__edge')
  const edgeId = edgeEl?.getAttribute('data-id')
  if (edgeEl && edgeId && !edgeEl.classList.contains('edge-seq')) return { edge: edgeId, el: edgeEl }
  const nodeEl = el?.closest?.('.vue-flow__node')
  const node = nodeEl ? findNode(nodeEl.getAttribute('data-id') ?? '') : undefined
  // a foreach container takes the node INSIDE; a comment is paper on the canvas
  if (nodeEl && node && node.type !== 'foreach' && DATA_PARENTS.has(node.type ?? '')) return { node: node.id, el: nodeEl }
  return null
}
function lightDrop(el: Element | null) {
  if (dropLit === el) return
  dropLit?.classList.remove('drop-into')
  dropLit = el
  dropLit?.classList.add('drop-into')
}
function onCanvasDragOver(ev: DragEvent) {
  ev.preventDefault()
  if (ev.dataTransfer) ev.dataTransfer.dropEffect = 'move'
  canvasEl.value?.classList.add('dropping')
  lightDrop(ev.dataTransfer?.types.includes('application/tabularia') ? dropTarget(ev)?.el ?? null : null)
}
function endDropHint() {
  canvasEl.value?.classList.remove('dropping')
  lightDrop(null)
}
function onCanvasDragLeave(ev: DragEvent) {
  // leaving the canvas, not passing from one of its elements to another
  if (!canvasEl.value?.contains(ev.relatedTarget as globalThis.Node | null)) endDropHint()
}

function onCanvasDrop(ev: DragEvent) {
  const over = dropTarget(ev)
  endDropHint()
  const kind = ev.dataTransfer?.getData('application/tabularia')
  if (!kind || loadBlocked.value) return // a flow that cannot be opened takes nothing under its notice
  if (!TRANSFORMATION(kind) && deniedToObserver()) return
  const position = screenToFlowCoordinate({ x: ev.clientX, y: ev.clientY })
  fitView_ = false // the node goes where it was dropped: a new flow's pending fit must not move the view

  if (kind === 'source') {
    const id = `src-${++sourceCounter}`
    addNodes({
      id,
      type: 'source',
      position,
      data: { bucket, datasetId: null, parquetKey: null, filename: null, rows: null, columns: [] },
    })
    selectedId.value = id
    return
  }

  if (kind === 'output') {
    // terminal node: where the result ends up (datasource or database table)
    const id = `out-${++outputCounter}`
    addNodes({
      id,
      type: 'output',
      position,
      data: { destType: 'datasource', mode: 'append' },
    })
    selectedId.value = id
    refreshForNode(id)
    return
  }

  if (kind === 'script') {
    // code run by the gateway as a Kubernetes Job: its environment, then its code,
    // are chosen in the node panel; input and output come from its edges
    const id = `ctl-${++outputCounter}`
    addNodes({
      id, type: 'script', position,
      data: { label: t('scripting.node.title'), files: [], entrypoint: '', timeoutSeconds: 900, retries: 0, retryDelaySeconds: 60 },
    })
    selectedId.value = id
    return
  }

  if (kind === 'container') {
    // an image of a registry, run by the gateway as a Kubernetes Job: the registry and
    // the image are chosen in the node panel; input and output come from its edges
    const id = `ctl-${++outputCounter}`
    addNodes({
      id, type: 'container', position,
      data: { label: t('containers.node.title'), registryId: null, repository: '', tag: '', command: [], args: [],
              variables: [], cpu: '1', memory: '1Gi', timeoutSeconds: 900, retries: 0, retryDelaySeconds: 60 },
    })
    selectedId.value = id
    return
  }

  if (kind === 'comment') {
    // free note: an annotation on the canvas, ignored by the run
    const id = `cmt-${++commentCounter}`
    addNodes({
      id,
      type: 'comment',
      position,
      style: { width: '220px', height: '120px' },
      data: { text: '' },
    })
    selectedId.value = id
    return
  }

  if (kind === 'op:foreach') {
    // the loop's container: a big node, the children are dragged into it
    const id = `op-${++opCounter}`
    addNodes({
      id,
      type: 'foreach',
      position,
      style: { width: '460px', height: '280px' },
      data: { opType: 'foreach', params: defaultParams('foreach') },
    })
    selectedId.value = id
    return
  }

  if (kind.startsWith('op:')) {
    const opType = kind.slice(3)
    // on an edge: in there; on a node: its next step, by the chain's rule (Shift: a branch)
    if (over?.edge) {
      const edge = getEdges.value.find((e) => e.id === over.edge)
      const box = edge && findNode(edge.source)?.parentNode ? findNode(findNode(edge.source)!.parentNode!) : undefined
      const at = { x: position.x - (box?.position.x ?? 0), y: position.y - (box?.position.y ?? 0) }
      if (edge && addStep(edge.source, opType, defaultParams(opType), { edgeId: edge.id, at, snap: true })) return
    } else if (over?.node) {
      if (addStep(over.node, opType, defaultParams(opType), { branch: ev.shiftKey })) return
    }
    const id = `op-${++opCounter}`

    // drop INSIDE a foreach container → it becomes a child (relative position)
    const container = containerAt(position)
    if (container) {
      addNodes({
        id,
        type: 'operation',
        position: { x: position.x - container.position.x, y: position.y - container.position.y },
        parentNode: container.id,
        extent: 'parent',
        data: { opType, params: defaultParams(opType) },
      })
    } else {
      addNodes({ id, type: 'operation', position, data: { opType, params: defaultParams(opType) } })
    }
    selectedId.value = id
    refreshForNode(id)
  }
}

function patchSelected(patch: Record<string, any>) {
  if (!selectedId.value) return
  updateNodeData(selectedId.value, patch)
  invalidateColumns()
  invalidateStats(selectedId.value)
  // the SQL node does NOT run the preview at every change: a query may be heavy and
  // trying it again at every key/blur is wasteful. The preview is on command («Preview»
  // button or Cmd/Ctrl+Enter → previewSelected).
  if (findNode(selectedId.value)?.data?.opType === 'sql') return
  // a Script or Container node previews its last output: its code or its image change
  // nothing there until it runs
  if (STEP_TYPES.has(findNode(selectedId.value)?.type ?? '')) return
  refreshForNode(selectedId.value)
}

// preview on command of the selected node (used by the SQL node)
function previewSelected() {
  if (selectedId.value) refreshForNode(selectedId.value)
}

function deleteSelected() {
  if (selectedId.value) deleteNodes([selectedId.value])
}

function deleteNodes(ids: string[]) {
  if (!ids.length) return
  finishSlide()
  // the preview and the columns of the nodes going away must no longer write anything
  previewSeq++
  columnsSeq++
  previewSlots.cancel('preview')
  previewSlots.cancel('cols')
  for (const id of ids) {
    // a foreach container takes the children away too (the loop's body)
    const node = findNode(id)
    if (!node) continue // gone already with its container
    if (node.type === 'foreach') {
      const children = getNodes.value.filter((n) => n.parentNode === id).map((n) => n.id)
      if (children.length) removeNodes(children, true)
      children.forEach((c) => delete nodeColumns[c])
    }
    invalidateStats(id) // the node and all that followed it
    removeNodes(id, true) // removes the connected edges too
    delete nodeColumns[id]
  }
  selectedId.value = null
  clearPreview()
  inputColumns.value = []
  rightColumns.value = []
  invalidateColumns()
}

// ── Copy / cut / paste / duplicate ───────────────────────────────────
// The nodes they act on: the canvas's multiple selection (a box with Shift, or
// Ctrl/Cmd+click) if there is one, otherwise the node open in the panel.
function selectedIds(): string[] {
  const multi = getSelectedNodes.value.map((n) => n.id)
  if (multi.length > 1) return multi
  return selectedId.value ? [selectedId.value] : multi
}

function clipOfSelection(): FlowClip | null {
  const canvas = JSON.parse(serializeCanvas())
  return buildClip(canvas.nodes, canvas.edges, selectedIds())
}

let pasteCount = 0 // every paste of the same piece lands a bit further than the previous one

function copySelection(): boolean {
  const clip = clipOfSelection()
  if (!clip) return false
  writeClip(clip)
  pasteCount = 0
  setStatus(t('flowEditor.nodesCopied', { n: clip.nodes.length }), 'ok')
  return true
}

function newNodeId(n: ClipNode): string {
  if (n.type === 'source') return `src-${++sourceCounter}`
  if (n.type === 'output') return `out-${++outputCounter}`
  if (CONTROL_TYPES.has(n.type ?? '')) return `ctl-${++outputCounter}`
  if (n.type === 'comment') return `cmt-${++commentCounter}`
  return `op-${++opCounter}` // operation e foreach
}

function pasteClip(clip: FlowClip) {
  if (loadBlocked.value) return
  const step = 40 * ++pasteCount
  // an observer pastes what they could add by hand too: the transformations
  const allowed = (n: ClipNode) => !observerOnly.value || n.type === 'operation'
  const { nodes, edges } = instantiateClip(clip, newNodeId, { x: step, y: step }, allowed)
  if (!nodes.length) {
    deniedToObserver()
    return
  }
  addNodes(nodes.map((n) => (n.parentNode ? { ...n, extent: 'parent' as const } : n)))
  addEdges(edges.map((e) => ({
    ...e,
    sourceHandle: e.sourceHandle ?? undefined,
    targetHandle: e.targetHandle ?? undefined,
    class: e.targetHandle === 'seq-in' ? 'edge-seq' : undefined,
  })))
  invalidateColumns()
  // the pasted nodes stay selected: they move together, are copied again, are removed
  const newOnes = new Set(nodes.map((n) => n.id))
  selectedId.value = nodes[0].id
  nextTick(() => {
    removeSelectedNodes(getSelectedNodes.value)
    addSelectedNodes(getNodes.value.filter((n) => newOnes.has(n.id)))
  })
  refreshForNode(nodes[0].id)
  setStatus(t('flowEditor.nodesPasted', { n: nodes.length }), 'ok')
}

function duplicateSelection() {
  const clip = clipOfSelection()
  if (!clip) return
  pasteCount = 0 // next to the original; it does not touch what had been copied
  pasteClip(clip)
}

function cutSelection() {
  const ids = selectedIds()
  if (copySelection()) deleteNodes(ids)
}

// In the fields the browser's shortcuts hold: there Ctrl+Z undoes the text, Ctrl+C
// copies the text, Del deletes a character. Also inside a dialog and inside
// `.nokey` (the code editor): on Chrome and Edge Monaco types through EditContext, a
// div that is none of the above, and Ctrl+X there cut the Script node off the flow.
function typingInAField(): boolean {
  const el = document.activeElement as HTMLElement | null
  return ['INPUT', 'TEXTAREA', 'SELECT'].includes((el?.tagName ?? '').toUpperCase()) || !!el?.isContentEditable
    || !!el?.closest('.nokey, [role="dialog"]')
}

// The canvas's shortcuts: undo/redo, copy/cut/paste/duplicate, and Del / Backspace
// deleting what is selected.
function onKeydown(e: KeyboardEvent) {
  // Ctrl/Cmd+S saves the flow, even while typing in a field (before, it opened the
  // browser's «Save page»)
  if ((e.ctrlKey || e.metaKey) && !e.altKey && e.key.toLowerCase() === 's') {
    e.preventDefault()
    if (runDialogOpen.value || observerOnly.value) return
    // a field of the node's panel records its value on «change», which fires when it loses
    // the focus: saved at once, the flow kept the value from before the one just typed (and
    // nothing said it was not saved). The field gives its value, then takes the focus back.
    const field = document.activeElement as HTMLElement | null
    if (field && ['INPUT', 'TEXTAREA', 'SELECT'].includes(field.tagName.toUpperCase())) {
      field.blur()
      field.focus()
    }
    nextTick(() => saveFlow())
    return
  }
  if (typingInAField() || runDialogOpen.value) return
  // Esc stops a preview being computed, from the preview or the canvas (the radial menu, a
  // dialog, a popover and an open Select take their own Esc first)
  if (e.key === 'Escape') {
    if (e.defaultPrevented || !computingNodeId.value) return
    const el = document.activeElement as HTMLElement | null
    if (el && el !== document.body && !el.closest('.grid, .canvas')) return
    e.preventDefault()
    cancelPreview()
    return
  }
  // in the preview's grid Delete and Ctrl+C belong to its cells, never to the nodes selected
  const inGrid = !!(document.activeElement as HTMLElement | null)?.closest?.('.datagrid')
  // Alt+arrows walk the graph from wherever the focus is, but a cell of the grid (on the
  // canvas itself `onCanvasKeys` took the key already)
  if (e.altKey && !e.ctrlKey && !e.metaKey && WALKS[e.key] && !inGrid && !e.defaultPrevented) {
    e.preventDefault()
    walkGraph(WALKS[e.key]!)
    return
  }
  // «m» opens the menu of steps beside the selected node, as Shift+F10 and the Menu key do:
  // the way of a keyboard without them, or whose function keys are its media keys. («?»,
  // the list of keys, is the whole app's: components/ui/AppKeys.vue.)
  if (e.key === 'm' && !e.ctrlKey && !e.metaKey && !e.altKey && !e.shiftKey && !inGrid && !e.defaultPrevented) {
    e.preventDefault()
    openQuickMenuFromKeyboard()
    return
  }
  if ((e.ctrlKey || e.metaKey) && !e.altKey) {
    const key_ = e.key.toLowerCase()
    if (inGrid && key_ !== 'z' && key_ !== 'y') return
    // Ctrl/Cmd+Enter runs the flow: the Run button's own way (its dialog, its checks)
    if (e.key === 'Enter') {
      e.preventDefault()
      if (!observerOnly.value && canRun.value && !busy.value) run()
      return
    }
    if (key_ === 'z' || key_ === 'y') {
      e.preventDefault()
      if (key_ === 'y' || e.shiftKey) redo()
      else undo()
    } else if (key_ === 'c' || key_ === 'x') {
      // with highlighted text (a cell of the preview) the text is copied
      if (window.getSelection()?.toString()) return
      if (!selectedIds().length) return
      e.preventDefault()
      if (key_ === 'x') cutSelection()
      else copySelection()
    } else if (key_ === 'v') {
      const clip = readClip()
      if (!clip) return
      e.preventDefault()
      pasteClip(clip)
    } else if (key_ === 'd') {
      if (!selectedIds().length) return
      e.preventDefault() // otherwise the browser opens «add to bookmarks»
      duplicateSelection()
    }
    return
  }
  if (e.key !== 'Delete' && e.key !== 'Backspace') return
  if (inGrid) return
  // a selected edge (click on the stroke) is removed with the same key as the node
  const edges = getSelectedEdges.value
  if (edges.length) {
    e.preventDefault()
    edges.forEach((edge) => removeEdgeById(edge.id))
    return
  }
  const ids = selectedIds()
  if (!ids.length) return
  e.preventDefault()
  deleteNodes(ids)
}
onMounted(() => window.addEventListener('keydown', onKeydown))
onUnmounted(() => window.removeEventListener('keydown', onKeydown))

// the pickers (connections for the Output, datasources for source/refresh, flows for
// runflow) are loaded once at mount: when you create a connection/datasource/flow in
// another tab and come back here, we refresh them on becoming visible again
function onVisible() {
  if (document.visibilityState !== 'visible') return
  refreshConnections()
  refreshDatasources()
  refreshFlows()
}
onMounted(() => document.addEventListener('visibilitychange', onVisible))
onUnmounted(() => document.removeEventListener('visibilitychange', onVisible))

// ── Columns / preview ───────────────────────────────────────────────────
function invalidateColumns() {
  for (const k of Object.keys(nodeColumns)) if (findNode(k)?.type !== 'source') delete nodeColumns[k]
}

async function ensureColumns(nodeId: string): Promise<ColumnInfo[]> {
  const node = findNode(nodeId)
  if (!node) return []
  if (node.type === 'source') return (node.data.columns as ColumnInfo[]) ?? []
  if (nodeColumns[nodeId]) return nodeColumns[nodeId]
  const { sourceNode, operations: ops } = resolveChain(getNodes.value, getEdges.value, nodeId, scriptSources.value)
  if (!sourceNode?.data?.parquetKey) return []
  const res = await apiPreview({
    bucket: sourceNode.data.bucket ?? bucket,
    input_key: sourceNode.data.parquetKey,
    operations: ops,
    limit: 1,
  }, 'cols')
  nodeColumns[nodeId] = res.columns
  if (!nodeStats[nodeId]) nodeStats[nodeId] = { cols: res.columns.length }
  return res.columns
}

const CLICK_SETTLE_MS = 180
let columnsNodeId: string | null = null // whose fields the panel shows
function leftOf(nodeId: string): string | undefined {
  const inc = buildIncoming(getEdges.value)
  const node = findNode(nodeId)
  return inc.get(nodeId)?.left ?? (node?.parentNode ? inc.get(node.parentNode)?.left : undefined)
}
/** A node's output columns when known without asking (a source's, or a preview's). */
function knownColumns(id: string | undefined): ColumnInfo[] | null {
  const n = id ? findNode(id) : null
  if (!n) return id ? null : []
  if (n.type === 'source') return (n.data.columns as ColumnInfo[]) ?? []
  return nodeColumns[n.id] ?? null
}
let columnsSeq = 0 // as previewSeq: only the last resolution turns the skeleton off
async function refreshForNode(nodeId: string) {
  const inc = buildIncoming(getEdges.value)
  const node = findNode(nodeId)
  columnsLoading.value = true
  const colSeq = ++columnsSeq
  const t0 = performance.now()
  // the panel's fields are the new node's from the click on: its input columns at once when
  // they are known, otherwise none (the fields' skeleton), never the node before's
  if (nodeId !== columnsNodeId) {
    columnsNodeId = nodeId
    const known = knownColumns(leftOf(nodeId))
    inputColumns.value = known ?? []
    rightColumns.value = []
  }
  // A step seen already in this page shows at once: no wait, no request. Otherwise the
  // rows on screen are dimmed NOW and the summary names this node, then whoever goes
  // through the nodes one after another gets a moment before anything starts: if
  // another click arrives meanwhile this one does not even start.
  const shown = showFromMemory(nodeId)
  if (!shown) {
    beginCompute(nodeId)
    await new Promise((r) => setTimeout(r, CLICK_SETTLE_MS))
    if (colSeq !== columnsSeq) return
    // The rows first, then the input columns when they are not known already: side by side
    // they computed the same upstream chain twice at once (measured on kind: a worker at
    // 2.7 GB instead of 1.7 on a Group by after a 42M x 12M join), and the rows matter more.
    // Cancelled in the pause itself (Esc right after the click): no preview, and «Compute
    // again» stays; the panel's columns come all the same.
    if (cancelledNodeId.value !== nodeId) {
      if (knownColumns(leftOf(nodeId)) !== null) void runPreview(nodeId)
      else await runPreview(nodeId)
      if (colSeq !== columnsSeq) return
    }
  }

  // input columns (the left parent's output); the FIRST node of a foreach body has no
  // internal edges → its input is the container's
  const leftId =
    inc.get(nodeId)?.left ?? (node?.parentNode ? inc.get(node.parentNode)?.left : undefined)
  // as previewSeq in runPreview: an OLDER resolution must not overwrite the columns of the
  // node selected afterwards (the panel would show another node's columns). The guard
  // holds for EVERY state write.
  try {
    const cols = leftId ? await ensureColumns(leftId) : []
    if (colSeq === columnsSeq) inputColumns.value = cols
  } catch (e) {
    // "overtaken" = a newer refresh cancelled this request: it is not "no column", it is
    // "no longer my turn"
    if (colSeq === columnsSeq && !isSuperseded(e)) inputColumns.value = []
  }
  // Overtaken while waiting: STOP. Every extra request of this old refresh would cancel
  // (same slot) the new refresh's, which would stay without columns.
  if (colSeq !== columnsSeq) return

  // placeholders available to the nodes inside a container: the driver's columns (the
  // container's top input) or the keys of the first static iteration
  if (node?.parentNode) {
    const container = findNode(node.parentNode)
    const drvId = inc.get(node.parentNode)?.right
    try {
      if (drvId) {
        const ph = (await ensureColumns(drvId)).map((c) => c.name)
        if (colSeq !== columnsSeq) return
        placeholders.value = ph
      } else {
        const items = container?.data?.params?.items ?? []
        if (colSeq === columnsSeq) placeholders.value = Object.keys(items[0] ?? {})
      }
    } catch (e) {
      if (colSeq === columnsSeq && !isSuperseded(e)) placeholders.value = []
    }
    if (colSeq !== columnsSeq) return
  } else if (colSeq === columnsSeq) {
    placeholders.value = []
  }

  // right side's columns = the right branch of join/union, or the foreach's driver (for
  // the foreach they are the {{column}} placeholders available in the body)
  if (node?.data?.opType === 'join' || node?.data?.opType === 'union' || node?.type === 'foreach') {
    const rightId = inc.get(nodeId)?.right
    try {
      const right = rightId ? await ensureColumns(rightId) : []
      if (colSeq === columnsSeq) rightColumns.value = right
    } catch (e) {
      if (colSeq === columnsSeq && !isSuperseded(e)) rightColumns.value = []
    }
    if (colSeq !== columnsSeq) return
  } else if (colSeq === columnsSeq) {
    rightColumns.value = []
  }

  // turns the columns' skeleton off after the minimum visibility
  skeletonPad(t0).then(() => {
    if (colSeq === columnsSeq) columnsLoading.value = false
  })
}

// ── The preview on screen: whose it is, how long it takes, how many rows ─────────────
/** The name a person knows a node by: the label given to it, else its operation and what it
 *  works on («Group by regione, segmento», «Join on ordine_id»: two Group by nodes are told
 *  apart), a source's file. Cut at NODE_NAME_MAX characters, whole in the summary's tooltip. */
const NODE_NAME_MAX = 44
function clipName(s: string): string {
  return s.length > NODE_NAME_MAX ? `${s.slice(0, NODE_NAME_MAX - 1).trimEnd()}…` : s
}
function nodeFullName(id: string | null): string {
  const n = id ? findNode(id) : null
  if (!n) return ''
  const given = String(n.data?.label ?? '').trim()
  if (n.type === 'operation') {
    if (given) return given
    const op = String(n.data?.opType ?? '')
    const m = opMeta(op)
    const name = m.label.startsWith('ops.') ? t(m.label) : op
    const what = opSubject(op, n.data?.params)
    if (!what) return name
    return op === 'join' ? t('dataGrid.joinOn', { name, keys: what }) : `${name} ${what}`
  }
  if (n.type === 'source') return String(n.data?.filename || n.data?.dsName || t('dataGrid.sourceNode'))
  return String(given || n.data?.name || n.type || '')
}
function nodeName(id: string | null): string {
  return clipName(nodeFullName(id))
}

/** The request a node's preview is, or null when the node has no data to read. */
function previewBody(nodeId: string) {
  const chain = resolveChain(getNodes.value, getEdges.value, nodeId, scriptSources.value)
  if (!chain.sourceNode?.data?.parquetKey) return { chain, body: null }
  const body = {
    bucket: chain.sourceNode.data.bucket ?? bucket,
    input_key: chain.sourceNode.data.parquetKey as string,
    operations: chain.operations,
    limit: 100,
  }
  return { chain, body, key: previewKey({ ...body, engine: flowEngine.value }) }
}

function tickClock() {
  clock.value = performance.now()
}
/** From the click on: the rows on screen are not this node's. */
function beginCompute(nodeId: string) {
  stopCount()
  stopProfile()
  // the input columns still being asked for the node before are no one's now: stopped on
  // the server, or they take a worker from this preview
  if (previewSlots.busy('cols')) previewSlots.stop('cols')
  // a preview still in flight for the node before no longer writes (its answer is kept in
  // memory all the same; the next preview on the slot knocks it down on the server)
  previewSeq++
  cancelledNodeId.value = null
  computingNodeId.value = nodeId
  computeStartedAt.value = performance.now()
  tickClock()
  clearInterval(clockTimer)
  clockTimer = setInterval(tickClock, 500)
  computingWord.value = false
  clearTimeout(computingWordTimer)
  computingWordTimer = setTimeout(() => { computingWord.value = true }, COMPUTING_WORD_MS)
  // nothing on screen yet: the skeleton (never «select a node» with a node selected)
  if (!preview.value) previewLoading.value = true
  previewError.value = ''
}
/** Nothing on screen: the node shown went away. */
function clearPreview() {
  endCompute()
  stopCount()
  stopProfile()
  preview.value = null
  previewNodeId.value = null
  previewLoading.value = false
  inputShownFor.value = null
}
function endCompute() {
  computingNodeId.value = null
  clearInterval(clockTimer)
  clockTimer = undefined
  clearTimeout(computingWordTimer)
  computingWord.value = false
}
onUnmounted(() => { clearInterval(clockTimer); clearTimeout(computingWordTimer); stopCount(); stopProfile() })

/** A node's preview this page has seen already: on screen at once, with its count. */
function showFromMemory(nodeId: string): boolean {
  const { chain, body, key } = previewBody(nodeId)
  const hit = body && key ? fresh(previewMemo.get(key)) : undefined
  if (!body || !key || !hit) return false
  previewSeq++ // a preview still in flight for another node no longer writes
  previewSlots.cancel('preview')
  endCompute()
  // the line over the rows is this node's: not the hint left by a step not set yet
  previewNote.value = scriptNote(chain.afterScript)
  showPreview(nodeId, hit.res, key, body, 0)
  previewLoading.value = false
  return true
}

/** The data come from a script's last run: which, and whether its code changed since. */
function scriptNote(afterScript: string | undefined): string {
  const fromScript = afterScript ? scriptOutputs.value[afterScript]?.output : null
  if (!fromScript || !afterScript) return ''
  return t(`${stepTexts(afterScript)}.${fromScript.trial ? 'previewFromTrial' : 'previewFromRun'}`, { when: fmtWhen(fromScript.finished_at) })
    + (fromScript.stale ? ` ${t(`${stepTexts(afterScript)}.previewStale`)}` : '')
}

// A step just added and not set yet has no rows of its own, and used to blank the table: the
// person was asked to pick a column and a value with nothing to look at (critique of
// 2026-11-10). Its INPUT's rows are shown instead, with a line saying whose they are — only
// when this page holds them already (the step before was looked at): nothing is asked of the
// engine for it, neither the rows, nor their count, nor their profile. Without them in
// memory the hint stays alone, as before.
const inputShownFor = ref<string | null>(null) // the node whose input is on screen
function showInputInstead(nodeId: string) {
  previewNote.value = t('flowEditor.completeNodeHint')
  const leftId = leftOf(nodeId)
  const { body, key } = leftId ? previewBody(leftId) : { body: null, key: undefined }
  const hit = body && key ? fresh(previewMemo.get(key)) : undefined
  if (!leftId || !body || !key || !hit) return
  preview.value = hit.res
  previewNodeId.value = leftId
  previewMs.value = 0
  inputShownFor.value = nodeId
  const counted = hit.res.truncated ? fresh(countMemo.get(key)) : undefined
  rowCount.value = !hit.res.truncated
    ? { state: 'done', rows: hit.res.rows.length, over: false }
    : counted ? { state: 'done', rows: counted.rows, over: counted.over } : { state: 'none' }
  // the profile this page remembers; a column opened by hand asks its own, as on that step
  const known = fresh(profileMemo.get(key)) ?? fresh(profileMemo.get(`${key}\u0001footer`))
  profileAsk = { key, body, columns: hit.res.columns }
  if (known) {
    profile.value = known.res
    profileState.value = 'done'
  }
  previewNote.value = t('flowEditor.inputOfStep')
}

function showPreview(nodeId: string, res: PreviewResult, key: string, body: NonNullable<ReturnType<typeof previewBody>['body']>, ms: number) {
  stopCount()
  stopProfile()
  cancelledNodeId.value = null
  inputShownFor.value = null
  preview.value = res
  previewNodeId.value = nodeId
  previewError.value = ''
  previewMs.value = ms
  nodeColumns[nodeId] = res.columns
  const counted = res.truncated ? fresh(countMemo.get(key)) : undefined
  rowCount.value = !res.truncated
    ? { state: 'done', rows: res.rows.length, over: false }
    : counted ? { state: 'done', rows: counted.rows, over: counted.over } : { state: 'none' }
  nodeStats[nodeId] = {
    rows: res.row_count, truncated: res.truncated, cols: res.columns.length,
    cacheState: res.cache_state ?? null, cacheCap: res.cache_cap_rows ?? null,
    count: rowCount.value,
  }
  if (res.truncated && !counted) void startCount(nodeId, key, body)
  else queueProfile(key, body, res.columns)
}

// The real count comes after the rows, apart, and is dropped as soon as the person looks
// elsewhere: a click on another node stops it on the server too (it would take a worker
// the next preview needs).
let countSeq = 0
const countError = ref('') // why the rows were not counted: the bar's tooltip
let countAsk: { nodeId: string; key: string; body: NonNullable<ReturnType<typeof previewBody>['body']> } | null = null
async function startCount(nodeId: string, key: string, body: NonNullable<ReturnType<typeof previewBody>['body']>) {
  const seq = ++countSeq
  countAsk = { nodeId, key, body }
  countError.value = ''
  rowCount.value = { state: 'counting' }
  try {
    const c = await previewSlots.count({ bucket: body.bucket, input_key: body.input_key, operations: body.operations, engine: flowEngine.value }, 'count')
    countMemo.set(key, { at: Date.now(), rows: c.rows, over: c.over })
    if (seq !== countSeq) return
    rowCount.value = { state: 'done', rows: c.rows, over: c.over }
    if (nodeStats[nodeId]) nodeStats[nodeId] = { ...nodeStats[nodeId], count: rowCount.value }
  } catch (e) {
    if (seq !== countSeq || isSuperseded(e)) return
    const status = (e as any)?.response?.status ?? (e as any)?.statusCode
    countError.value = whyNot(e, 'count')
    rowCount.value = { state: 'failed', slow: status === 504 }
  }
  // the profile after the count, whatever it said: never beside it on the worker
  if (seq === countSeq && preview.value) queueProfile(key, body, preview.value.columns)
}
/** «Try again» on a count that failed: the same step, counted again. */
function countRetry() {
  const a = countAsk
  if (!a || a.nodeId !== previewNodeId.value || computingNodeId.value) return
  void startCount(a.nodeId, a.key, a.body)
}
function stopCount() {
  countSeq++
  if (previewSlots.busy('count')) previewSlots.stop('count')
  if (rowCount.value.state === 'counting') rowCount.value = { state: 'none' }
}

// The column profile comes last, after a short pause (whoever goes through the nodes one
// after another never starts one), on its own slot, and is stopped on the server as soon
// as another node is clicked: it never shares the worker with the next preview.
// In the editor a step's profile costs a query on rows that may be billions: by itself it
// asks only what the parquet footer says (the nulls, a number's range: no query), and the
// whole profile is made when a column is opened (`profileWhole`). The Viewer profiles whole.
const PROFILE_SETTLE_MS = 250
let profileSeq = 0
let profileTimer: ReturnType<typeof setTimeout> | undefined
let wholeAsked = -1 // the profileSeq whose whole profile was asked
let wholeWanted = false // a column opened before the step's profile was asked (composables/profileAsk.ts)
let profileAsk: { key: string; body: NonNullable<ReturnType<typeof previewBody>['body']>; columns: ColumnInfo[] } | null = null
function queueProfile(key: string, body: NonNullable<ReturnType<typeof previewBody>['body']>, columns: ColumnInfo[]) {
  const wanted = wholeWanted
  stopProfile()
  if (!columns.length) return
  profileAsk = { key, body, columns }
  const seq = profileSeq
  const known = fresh(profileMemo.get(key)) ?? fresh(profileMemo.get(`${key}\u0001footer`))
  const start = profileStart(known?.res, wanted)
  if (known) profile.value = known.res
  if (start === 'known') {
    profileState.value = 'done'
    return
  }
  profileState.value = 'loading'
  if (start === 'whole') {
    wholeAsked = seq
    void askProfile(seq, false)
    return
  }
  profileTimer = setTimeout(() => { void askProfile(seq, true) }, PROFILE_SETTLE_MS)
}
async function askProfile(seq: number, footerOnly: boolean) {
  const ask = profileAsk
  if (seq !== profileSeq || !ask) return
  try {
    const res = await previewSlots.profile({
      bucket: ask.body.bucket, input_key: ask.body.input_key, operations: ask.body.operations, engine: flowEngine.value,
      columns: ask.columns.map((c) => ({ name: c.name, dtype: c.dtype })),
      ...(footerOnly ? { footer_only: true } : {}),
    }, 'profile')
    profileMemo.set(res.partial ? `${ask.key}\u0001footer` : ask.key, { at: Date.now(), res })
    // a footer-only answer that comes after the whole was asked does not stand for it
    if (seq !== profileSeq || (footerOnly && wholeAsked === seq)) return
    profile.value = res
    profileState.value = 'done'
  } catch (e) {
    if (seq !== profileSeq || isSuperseded(e) || (footerOnly && wholeAsked === seq)) return
    // a whole profile that failed leaves what the footer said on screen, with the reason
    profileError.value = whyNot(e, 'profile')
    profileState.value = footerOnly && profile.value ? 'done' : 'failed'
  }
}
/** Why a count or a profile was not made, in words: too slow (the cap), the worker's memory,
 *  or the server's own reason. */
function whyNot(e: unknown, what: 'count' | 'profile'): string {
  const status = (e as any)?.response?.status ?? (e as any)?.statusCode
  if (status === 504) return t(what === 'count' ? 'dataGrid.countSlowWhy' : 'dataGrid.profileSlowWhy')
  if (status === 503) return t('dataGrid.workerBusyWhy')
  return errMessage(e)
}
/** «Try again» on a whole profile that failed. */
function profileRetry() {
  if (!profileAsk) return
  wholeAsked = -1
  profileError.value = ''
  profileWhole()
}
/** A column opened in the grid: the step's whole profile (one query for every column). */
function profileWhole() {
  const next = profileOpen(!!profileAsk, profile.value, wholeAsked === profileSeq)
  if (next === 'remember') wholeWanted = true // asked as soon as the count is done
  if (next !== 'ask') return
  wholeAsked = profileSeq
  clearTimeout(profileTimer)
  profileError.value = ''
  profileState.value = 'loading'
  // on the same slot: a footer-only request still out is knocked down by this one
  void askProfile(profileSeq, false)
}
function stopProfile() {
  profileSeq++
  wholeWanted = false
  clearTimeout(profileTimer)
  if (previewSlots.busy('profile')) previewSlots.stop('profile')
  if (previewSlots.busy('top')) previewSlots.stop('top')
  profile.value = null
  profileState.value = 'none'
  profileError.value = ''
  profileAsk = null
}
// a column's most frequent values or a number's histogram, when the column is opened in the
// grid (after the whole profile): one query a column
const topMemo = new Recent<{ at: number; res: TopValuesResult | HistogramResult }>(120)
async function columnDetail(column: string, ask: DetailAsk): Promise<TopValuesResult | HistogramResult> {
  const id = previewNodeId.value
  const { body, key } = id ? previewBody(id) : { body: null, key: null }
  if (!body || !key) throw new Error('no step')
  const histogram = ask.kind === 'histogram' ? { lo: ask.lo, hi: ask.hi, bins: ask.bins } : undefined
  const mk = `${key}\u0001${column}\u0001${JSON.stringify(histogram ?? null)}`
  const known = fresh(topMemo.get(mk))
  if (known) return known.res
  const res = await previewSlots.profile<TopValuesResult | HistogramResult>({
    bucket: body.bucket, input_key: body.input_key, operations: body.operations, engine: flowEngine.value, column,
    ...(histogram ? { histogram } : {}),
  }, 'top')
  topMemo.set(mk, { at: Date.now(), res })
  return res
}
// What a value of the profile can do in the editor: a Filter step after the node on screen
// (keep it, or leave it out), a Fill nulls step for its nulls, added through `addStep` by
// the rule of every added step: into the chain when the node already feeds another, at
// its end otherwise (it used to branch aside, alone among the ways to add a step).
function addStepAfter(parentId: string, opType: string, params: Record<string, any>) {
  // a transformation: an observer may add one too, as from the palette
  addStep(parentId, opType, params, { reveal: true })
}
const valueActions = computed(() => {
  const id = previewNodeId.value
  // the rows of a step's input, shown while the step is not set: they are not the place to
  // start another step from
  if (!id || loadBlocked.value || inputShownFor.value) return null
  return {
    filter: (column: string, value: unknown, exclude: boolean) => {
      const params = value === null || value === undefined
        ? { column, operator: exclude ? 'is_not_null' : 'is_null' }
        : { column, operator: exclude ? 'ne' : 'eq', value }
      addStepAfter(id, 'filter', params)
    },
    fillNulls: (column: string) => addStepAfter(id, 'fill_null', { columns: { [column]: null } }),
  }
})

/** «Cancel» on a slow preview (or Esc): stopped on the server too, nothing of another node
 *  shown, and the way back in the grid's place, with the focus on it. */
function cancelPreview() {
  const id = computingNodeId.value
  if (!id) return
  const name = nodeName(id)
  previewSeq++
  previewSlots.stop('preview')
  endCompute()
  stopProfile()
  preview.value = null
  previewNodeId.value = null
  inputShownFor.value = null
  previewLoading.value = false
  previewError.value = ''
  previewNote.value = t('dataGrid.cancelled', { name })
  cancelledNodeId.value = id
  nextTick(() => (document.querySelector('.pv-again') as HTMLElement | null)?.focus())
}
/** «Compute again» after a Cancel: the same node, from the start. */
function computeAgain() {
  const id = cancelledNodeId.value
  cancelledNodeId.value = null
  if (!id || !findNode(id)) return
  previewNote.value = ''
  refreshForNode(id)
  // the button goes with the note: the focus to the preview's region, not to the page
  nextTick(() => (document.querySelector('.viewbody .datagrid') as HTMLElement | null)?.focus())
}

let previewSeq = 0 // only the LAST preview launched writes the result and turns the skeleton off
async function runPreview(nodeId: string) {
  const { chain, body, key } = previewBody(nodeId)
  const { sourceNode, afterScript } = chain
  if (body && key && !fresh(previewMemo.get(key)) && computingNodeId.value !== nodeId) beginCompute(nodeId)
  // the seq ALWAYS moves on: a preview that does not start must invalidate the old one
  // still in flight too, or that one would write its result on the new node
  const seq = ++previewSeq
  if (!sourceNode?.data?.parquetKey || !body || !key) {
    previewSlots.cancel('preview')
    endCompute()
    stopCount()
    preview.value = null
    previewNodeId.value = nodeId
    inputShownFor.value = null
    previewLoading.value = false
    // what a script writes exists only during a run: the editor cannot preview it
    previewError.value = ''
    previewNote.value = afterScript ? t(`${stepTexts(afterScript)}.previewAfterRun`) : ''
    return
  }
  previewError.value = ''
  // the data come from a script's last run: say which, and whether its code changed since.
  // While the input of a step not set yet is on screen its line stays until the answer.
  const note = scriptNote(afterScript)
  if (inputShownFor.value !== nodeId) previewNote.value = note
  const hit = fresh(previewMemo.get(key))
  if (hit) {
    endCompute()
    previewNote.value = note
    showPreview(nodeId, hit.res, key, body, 0)
    previewLoading.value = false
    return
  }
  const t0 = performance.now()
  const skeleton = previewLoading.value // the skeleton is up: it stays its minimum, no flash
  try {
    const res = await apiPreview(body, 'preview')
    previewMemo.set(key, { at: Date.now(), res })
    cacheCheckAfterPreview()
    if (seq === previewSeq) {
      if (skeleton) await skeletonPad(t0)
      if (seq !== previewSeq) return
      endCompute()
      previewNote.value = note
      showPreview(nodeId, res, key, body, performance.now() - t0)
    } else {
      nodeColumns[nodeId] = res.columns
    }
  } catch (e) {
    // "overtaken by a newer one" is not an error: it must never be shown
    if (seq === previewSeq && !isSuperseded(e)) {
      endCompute()
      preview.value = null
      previewNodeId.value = nodeId
      inputShownFor.value = null
      const message = errMessage(e)
      // a node just added and not set yet: not an error in red, the hint to set it, over
      // its input's rows when this page has them
      if (/required parameter missing: '/.test(message)) showInputInstead(nodeId)
      else {
        previewNote.value = ''
        previewError.value = message
      }
    }
  } finally {
    if (seq === previewSeq) previewLoading.value = false
  }
}

// ── What the tab bar says about the rows on screen ─────────────────────────────────
const computeSeconds = computed(() => (computingNodeId.value ? Math.floor((clock.value - computeStartedAt.value) / 1000) : 0))
// the columns of the node being computed when this page knows them (its last preview, a
// source's schema): the grid's header names them at once over the rows that are going
const computingColumns = computed<ColumnInfo[] | null>(() => (computingNodeId.value ? knownColumns(computingNodeId.value) : null))
// the step on screen is recomputed from its source at every preview: its upstream step is
// over the cache cap (the panel and the node say it too)
const shownCacheSkipped = computed(() => {
  const s = previewNodeId.value && !computingNodeId.value ? nodeStats[previewNodeId.value] : null
  return s?.cacheState === 'skipped' ? fmt.count(s.cacheCap ?? 0) : null
})
const slowCompute = computed(() => !!computingNodeId.value && computeSeconds.value >= 2)
// the source of the chain on screen (or being computed), and its development sample
const shownSource = computed(() => {
  const id = computingNodeId.value ?? previewNodeId.value
  return id ? resolveChain(getNodes.value, getEdges.value, id, scriptSources.value).sourceNode : null
})
const shownSample = computed(() => (shownSource.value?.type === 'source' ? sampleLabel(shownSource.value.data, t, fmt.count) : null))
// the step's changes against its input: the selected node's own preview only. Compared with
// the columns of the node before as this page knows them already (a source's, or that node's
// own preview), so the marks come with the rows; when they are not known yet the input's
// columns are asked after the rows, and the summary keeps the place of «N removed» meanwhile.
const previewInput = computed<ColumnInfo[] | null>(() => {
  const id = previewNodeId.value
  const node = id ? findNode(id) : null
  if (!preview.value || !node || id !== selectedId.value || node.type !== 'operation') return null
  return knownColumns(leftOf(id!))
})
const changesPending = computed(() => {
  const id = previewNodeId.value
  const node = id ? findNode(id) : null
  // asked after the rows while the panel's columns load; a node before that never says its
  // columns (a Script's) leaves no marks rather than a place kept forever
  return !!preview.value && !computingNodeId.value && node?.type === 'operation' && id === selectedId.value
    && previewInput.value === null && columnsLoading.value
})
const previewChanges = computed(() => {
  const node = previewNodeId.value ? findNode(previewNodeId.value) : null
  const upstream = previewInput.value
  if (!preview.value || !node || !upstream) return null
  const op = String(node.data?.opType ?? '')
  const renames = op === 'rename' ? (node.data?.params?.mapping ?? {}) : {}
  return columnChanges(upstream, preview.value.columns, renames, consumedColumns(op, node.data?.params))
})
// «N columns removed» opens the list of the columns removed, under its own button; the
// cache's mark opens what it means
const removedOpen = ref(false)
const removedPop = ref<HTMLElement | null>(null)
const cacheOpen = ref(false)
const cachePop = ref<HTMLElement | null>(null)
const popLeft = ref(0)
usePopover(removedOpen, removedPop, () => { removedOpen.value = false })
usePopover(cacheOpen, cachePop, () => { cacheOpen.value = false })
watch(previewChanges, () => { removedOpen.value = false })
watch(previewNodeId, () => { cacheOpen.value = false })
/** Where a popover of the bar opens: under the button that opened it, kept inside the bar. */
function anchorTo(e: MouseEvent, width = 260) {
  const btn = e.currentTarget as HTMLElement | null
  const bar = btn?.closest('.pv-bar') as HTMLElement | null
  if (!btn || !bar) return
  const b = bar.getBoundingClientRect()
  popLeft.value = Math.max(0, Math.min(btn.getBoundingClientRect().left - b.left, b.width - width))
}
function toggleRemoved(e: MouseEvent) {
  anchorTo(e)
  cacheOpen.value = false
  removedOpen.value = !removedOpen.value
}
function toggleCache(e: MouseEvent) {
  anchorTo(e, 320)
  removedOpen.value = false
  cacheOpen.value = !cacheOpen.value
}
// the list takes the focus (read out, and leaving it closes it); a click elsewhere closes it
function onRemovedOutside(e: PointerEvent) {
  const el = e.target as HTMLElement | null
  if (removedPop.value?.contains(el) || el?.closest?.('.pv-removed')) return
  removedOpen.value = false
}
// the explanation holds no control: the panel itself takes the focus, so Esc and Tab close it
watch(cacheOpen, (open) => { if (open) nextTick(() => cachePop.value?.focus()) })
watch(removedOpen, (open) => {
  if (open) {
    document.addEventListener('pointerdown', onRemovedOutside, true)
    nextTick(() => removedPop.value?.focus())
  } else {
    document.removeEventListener('pointerdown', onRemovedOutside, true)
  }
})
onUnmounted(() => document.removeEventListener('pointerdown', onRemovedOutside, true))
const summaryRows = computed(() => {
  const r = preview.value
  if (!r) return ''
  return rowsPhrase(r.rows.length, r.truncated, rowCount.value, t, (n) => fmt.count(n))
})
// the same, as short as a node of the canvas says it, for a narrow bar
const summaryRowsShort = computed(() => {
  const r = preview.value
  if (!r) return ''
  return nodeRowsPhrase(r.rows.length, r.truncated, rowCount.value, t, (n) => fmt.count(n))
})
// One announcement a click, once everything about it has arrived (rows, count, changes): the
// summary itself changes three or four times, and the running seconds are never read out.
const liveText = ref('')
const settledSummary = computed(() => {
  const r = preview.value
  if (!r || computingNodeId.value || rowCount.value.state === 'counting' || changesPending.value) return ''
  const parts = [nodeFullName(previewNodeId.value), summaryRows.value]
  if (rowCount.value.state === 'failed') parts.push(rowCount.value.slow ? t('dataGrid.countSlow') : t('dataGrid.countFailed'))
  parts.push(t('dataGrid.columns', { n: r.columns.length }))
  const removed = previewChanges.value?.removed.length ?? 0
  if (removed) parts.push(t('dataGrid.removed', { n: removed }, removed))
  if (shownCacheSkipped.value) parts.push(t('dataGrid.cacheCap'))
  return parts.filter(Boolean).join(' · ')
})
watch(settledSummary, (now) => {
  if (!now) return
  // emptied first: the same words again (a step computed again) are said again
  liveText.value = ''
  nextTick(() => { liveText.value = now })
})
// a sample on the source is suggested, never switched on behind the person's back: when the
// source is big (its rows, not the step's: a Group by's 30 rows over 42 million still cost
// the 42 million), while a node computes slowly or beside its rows
const offerSample = computed(() => {
  if (shownSample.value || shownSource.value?.type !== 'source') return false
  if (!suggestSample(shownSource.value.data?.rows, rowCount.value)) return false
  return slowCompute.value || (!!preview.value && !computingNodeId.value)
})
// the «…» menu the sample goes into when the bar is narrow
const moreOpen = ref(false)
const morePop = ref<HTMLElement | null>(null)
usePopover(moreOpen, morePop, () => { moreOpen.value = false })
function sampleFromMenu() {
  moreOpen.value = false
  openSourceSample()
}
function openSourceSample() {
  const src = shownSource.value
  if (!src) return
  selectedId.value = src.id
  refreshForNode(src.id)
  nextTick(() => (document.querySelector('.samplebox .sel-trigger, .samplebox select') as HTMLElement | null)?.focus())
}

// ── Distinct values of a column at the selected node's INPUT ─────────
// (for the in/not_in filter's picker: unique on the chain upstream, cache included)
async function fetchDistinctValues(column: string): Promise<any[]> {
  const nodeId = selectedId.value
  if (!nodeId) return []
  const node = findNode(nodeId)
  const inc = buildIncoming(getEdges.value)
  let sourceNode: Node | null = null
  let ops: Operation[] = []
  if (node?.type === 'source') {
    // the source node's upstream filters: the values come from the source's RAW data (not
    // from the chain, which would hold the filters themselves)
    sourceNode = node
  } else {
    const leftId =
      inc.get(nodeId)?.left ?? (node?.parentNode ? inc.get(node.parentNode)?.left : undefined)
    if (!leftId) return []
    ;({ sourceNode, operations: ops } = resolveChain(getNodes.value, getEdges.value, leftId, scriptSources.value))
  }
  if (!sourceNode?.data?.parquetKey) return []
  const res = await apiPreview({
    bucket: sourceNode.data.bucket ?? bucket,
    input_key: sourceNode.data.parquetKey,
    operations: [
      ...ops,
      { type: 'select', params: { columns: [column] } },
      { type: 'unique', params: {} },
      { type: 'sort', params: { by: column } },
      { type: 'limit', params: { n: 200 } },
    ],
    limit: 200,
  }, `values:${column}`)
  return res.rows.map((r) => r[column]).filter((v) => v !== null)
}

// ── Chart: runs the node's chain + extra aggregations on the engine ──
async function chartQuery(extraOps: Operation[], limit = 1000): Promise<PreviewResult | null> {
  const nodeId = selectedId.value ?? leafNodeId(getNodes.value, getEdges.value)
  const { sourceNode, operations: ops } = resolveChain(getNodes.value, getEdges.value, nodeId, scriptSources.value)
  if (!sourceNode?.data?.parquetKey) return null
  return await apiPreview({
    bucket: sourceNode.data.bucket ?? bucket,
    input_key: sourceNode.data.parquetKey,
    operations: [...ops, ...extraOps],
    limit,
  }, 'chart')
}

// ── Export (csv/xlsx download of the selected node) ─────────────────────
async function exportSelected(format: 'csv' | 'xlsx') {
  const nodeId = selectedId.value ?? leafNodeId(getNodes.value, getEdges.value)
  const { sourceNode, operations: ops } = resolveChain(getNodes.value, getEdges.value, nodeId, scriptSources.value)
  if (!sourceNode?.data?.parquetKey) {
    setStatus(t('flowEditor.exportNoData'), 'error')
    return
  }
  const node = findNode(nodeId)
  const label = node?.type === 'source' ? 'source' : (node?.data?.opType ?? 'node')
  const filename = `${flowName.value.trim() || 'tabularia'}_${label}.${format}`
    .toLowerCase().replace(/[^a-z0-9._-]+/g, '_')

  busy.value = true
  setStatus(t('flowEditor.exporting', { format: format.toUpperCase() }), 'busy')
  try {
    await api.exportData({
      bucket: sourceNode.data.bucket ?? bucket,
      input_key: sourceNode.data.parquetKey,
      operations: ops,
      format,
      filename,
    })
    setStatus(t('flowEditor.downloaded', { filename }), 'ok')
  } catch (e) {
    setStatus(t('flowEditor.exportFailed', { error: errMessage(e) }), 'error')
  } finally {
    busy.value = false
  }
}

// ── Run (async through Celery) ─────────────────────────────────────────
// The click on Run opens the dialog; the real launch goes through the GATEWAY when the
// flow is saved → run history. When the canvas has Output nodes, every output becomes a
// run (the destination is configured on the node); without Output nodes the classic
// path stays (optional publishing from the dialog).
const runDialogOpen = ref(false)
const runDialogError = ref('')

interface OutputSummary {
  id: string
  label: string
  detail: string
  error: string | null
}

function outputNodes() {
  return getNodes.value.filter((n) => n.type === 'output')
}

function describeOutput(n: Node): OutputSummary {
  const d = n.data ?? {}
  if ((d.destType ?? 'datasource') === 'database') {
    const conn = connectionsList.value.find((c) => c.id === d.connectionId)
    let error: string | null = null
    if (d.connectionId == null) error = t('flowEditor.chooseConnection')
    else if (!conn) error = t('flowEditor.connectionUnavailable')
    else if (conn.db_type === 's3') error = t('flowEditor.connectionIsS3')
    else if (!d.table?.trim()) error = t('flowEditor.enterTargetTable')
    return {
      id: n.id,
      label: t('flowEditor.tableLabel', { table: d.table?.trim() || '…' }),
      detail: conn
        ? `${conn.name} (${conn.db_type}) · ${d.mode === 'replace' ? t('flowEditor.destModeReplace') : t('flowEditor.destModeAppend')}`
        : '',
      error,
    }
  }
  if ((d.destType ?? 'datasource') === 's3') {
    const conn = connectionsList.value.find((c) => c.id === d.connectionId)
    const bucket = d.s3Bucket?.trim() || conn?.database?.trim() || ''
    let error: string | null = null
    if (d.connectionId == null) error = t('flowEditor.chooseS3Connection')
    else if (!conn) error = t('flowEditor.connectionUnavailable')
    else if (conn.db_type !== 's3') error = t('flowEditor.connectionNotS3')
    else if (!d.s3Key?.trim()) error = t('flowEditor.enterS3Key')
    else if (!bucket) error = t('flowEditor.noBucketError')
    const parts = (d.partitionBy ?? []).length
      ? t('flowEditor.partitionsLabel', { list: (d.partitionBy as string[]).join(', ') })
      : ''
    return {
      id: n.id,
      label: `S3 ${bucket ? `${bucket}/` : ''}${d.s3Key?.trim() || '…'}`,
      detail: conn ? `${conn.name} · ${d.s3Format ?? 'parquet'}${parts}` : '',
      error,
    }
  }
  if ((d.destType ?? 'datasource') === 'sftp') {
    const conn = connectionsList.value.find((c) => c.id === d.connectionId)
    const name = (d.sftpFileName ?? '').trim()
    let error: string | null = null
    if (d.connectionId == null) error = t('sftp.chooseConnection')
    else if (!conn) error = t('flowEditor.connectionUnavailable')
    else if (conn.db_type !== 'sftp') error = t('sftp.connectionNotSftp')
    else if (!name) error = t('sftp.enterFileName')
    const folder = (d.sftpFolder ?? '').trim()
    return {
      id: n.id,
      label: `SFTP ${folder ? `${folder.replace(/\/$/, '')}/` : ''}${name || '…'}`,
      detail: conn ? `${conn.name} · ${d.sftpFormat ?? 'csv'}` : '',
      error,
    }
  }
  if ((d.destType ?? 'datasource') === 'email') {
    const conn = connectionsList.value.find((c) => c.id === d.connectionId)
    const to: string[] = d.emailTo ?? []
    let error: string | null = null
    if (d.connectionId == null) error = t('flowEditor.chooseSmtpConnection')
    else if (!conn) error = t('flowEditor.connectionUnavailable')
    else if (conn.db_type !== 'smtp') error = t('flowEditor.connectionNotSmtp')
    else if (!to.length) error = t('flowEditor.enterRecipients')
    else if (!d.emailSubject?.trim()) error = t('flowEditor.enterSubject')
    return {
      id: n.id,
      label: t('flowEditor.emailLabel', { to: to.join(', ') || '…' }),
      detail: conn ? `${conn.name} · ${d.attachmentFormat ?? 'xlsx'}` : '',
      error,
    }
  }
  const proj = projectsList.value.find((p) => p.id === d.projectId)
  let error: string | null = null
  if (!d.name?.trim()) error = t('flowEditor.enterDatasourceName')
  else if (d.projectId == null) error = t('flowEditor.chooseFolder')
  return {
    id: n.id,
    label: t('flowEditor.datasourceLabel', { name: d.name?.trim() || '…' }),
    detail: proj ? t('flowEditor.folderDetail', { name: proj.name }) : '',
    error,
  }
}

const runOutputs = computed(() => outputNodes().map(describeOutput))

// ── Edges move only where the engine works ────────────────────────────────
// Every data edge ran its dashes forever (eleven endless animations on a flow nobody was
// touching): motion that said nothing. At rest the edges are still, drawn whole (the
// sequence edges stay violet and dashed: the two are told apart by more than colour). They
// run only along what leads to the node whose preview is being computed, and everywhere
// while a run of this flow started here is being followed. Nothing under reduced motion
// (main.css).
const followedRuns = ref(0) // runs of this flow the editor is polling
const NO_EDGES = new Set<string>()
const workingEdges = computed<Set<string>>(() => {
  const running = followedRuns.value > 0
    || Object.values(scriptTrials).some((x) => x.status === 'PENDING' || x.status === 'STARTED')
  if (running) return new Set(getEdges.value.filter((e) => (e.targetHandle as string) !== 'seq-in').map((e) => e.id))
  if (!computingNodeId.value) return NO_EDGES
  return upstreamEdgeIds(getNodes.value.map((n) => ({ id: n.id, position: n.position, parentNode: n.parentNode })), chainEdges(), computingNodeId.value)
})
watch(workingEdges, (now) => {
  for (const e of getEdges.value) {
    const on = now.has(e.id)
    if (!!e.animated !== on) e.animated = on
  }
})
/** Follows a run to its end: the edges move meanwhile. */
async function following<T>(poll: () => Promise<T>): Promise<T> {
  followedRuns.value++
  try {
    return await poll()
  } finally {
    followedRuns.value--
  }
}

// control nodes (refresh/runflow/script/container): they require the server-side orchestration
const hasControlNodes = () =>
  getNodes.value.some((n) => CONTROL_TYPES.has(n.type ?? ''))

function run() {
  if (deniedToObserver()) return
  // flows with control nodes → server orchestration (refresh→output→runflow), no dialog:
  // the configuration is on the nodes
  if (hasControlNodes()) {
    if (flowId.value === null) {
      setStatus(t('flowEditor.saveFlowForControlNodes'), 'error')
      return
    }
    executeOrchestration()
    return
  }
  const outs = outputNodes()
  if (outs.length) {
    // the outputs require the gateway (history, RBAC, credentials)
    if (flowId.value === null) {
      setStatus(t('flowEditor.saveFlowForOutputNodes'), 'error')
      return
    }
    const bad = runOutputs.value.find((o) => o.error)
    if (bad) {
      selectedId.value = bad.id
      setStatus(`${bad.label}: ${bad.error}`, 'error')
      return
    }
  } else {
    const leaf = leafNodeId(getNodes.value, getEdges.value)
    const { sourceNode } = resolveChain(getNodes.value, getEdges.value, leaf, scriptSources.value)
    if (!sourceNode?.data?.parquetKey) return
  }
  runDialogError.value = ''
  runDialogOpen.value = true
}

async function executeRun(publish: PublishSpec | null) {
  if (outputNodes().length) {
    await executeOutputRuns()
    return
  }
  const leaf = leafNodeId(getNodes.value, getEdges.value)
  const { sourceNode, operations: ops } = resolveChain(getNodes.value, getEdges.value, leaf, scriptSources.value)
  if (!sourceNode?.data?.parquetKey) {
    runDialogOpen.value = false
    return
  }
  runDialogError.value = ''
  busy.value = true // only for the length of the LAUNCH: the canvas stays usable during the run
  pollToken++ // new cycle: the earlier runs' polls stop
  try {
    if (flowId.value !== null) {
      // path with history: the gateway records the run and publishes the output
      const launched = await runsApi.launch(flowId.value, {
        bucket: sourceNode.data.bucket ?? bucket,
        input_key: sourceNode.data.parquetKey,
        operations: ops,
        publish,
      })
      runDialogOpen.value = false // closed ONLY at a successful launch
      setStatus(t('flowEditor.runStarted', { id: launched.id }), 'busy')
      void following(() => pollRun(launched.id)) // in the background: no lock on the editor
    } else {
      // unsaved flow: direct run, without history or publishing. The SERVER assigns the output
      // key (out/<uuid> write-once): here we no longer choose it, only the task_id is polled.
      const res = await apiTransform({
        bucket: sourceNode.data.bucket ?? bucket,
        input_key: sourceNode.data.parquetKey,
        output_key: '',
        operations: ops,
      })
      runDialogOpen.value = false
      setStatus(t('flowEditor.taskStarted', { id: res.task_id }), 'busy')
      void following(() => pollTask(res.task_id))
    }
  } catch (e) {
    // 409 duplicate name / 403 permissions: the dialog stays open with the input intact
    runDialogError.value = errMessage(e)
  } finally {
    busy.value = false
  }
}

// server-side orchestration (flows with control nodes): the gateway does refresh →
// output → runflow. It returns at once, the flow's history is polled.
async function executeOrchestration() {
  busy.value = true
  pollToken++
  try {
    const { run_id } = await flowsApi.runNow(flowId.value!)
    setStatus(t('flowEditor.orchestrationStarted'), 'busy')
    void following(() => pollOrchestration(run_id))
  } catch (e) {
    setStatus(t('flowEditor.errorGeneric', { error: errMessage(e) }), 'error')
  } finally {
    busy.value = false
  }
}

// polls the ORCHESTRATION RUN by id (run-now creates it): it tracks the whole run even
// for flows without an Output node, which make no runs of their own.
async function pollOrchestration(runId: number) {
  const token = pollToken
  // soon at first, then every two seconds, for five minutes at most
  const since = Date.now()
  for (let pause = FIRST_PAUSE_MS; Date.now() < since + 300_000; pause = nextPause(pause, since, 2000)) {
    await new Promise((r) => setTimeout(r, pause))
    if (token !== pollToken) return
    let run
    try {
      run = await runsApi.get(runId)
    } catch {
      continue
    }
    if (token !== pollToken) return
    if (run.status === 'SUCCESS') {
      setStatus(t('flowEditor.orchestrationCompleted'), 'ok')
      refreshDatasources()
      return
    }
    if (run.status === 'FAILURE') {
      setStatus(t('flowEditor.orchestrationError', { error: run.error ? knownError(run.error, t) : t('flowEditor.unknownError') }), 'error')
      return
    }
  }
  setStatus(t('flowEditor.orchestrationTimeout'), 'info')
}

// The fields' names are those the server-side resolver reads again for the scheduled
// runs (services/flow_resolver): keeping them aligned is what prevents a node from
// working with the Run button and sending to nobody on schedule.
function emailSpecOf(d: any) {
  return {
    connection_id: d.connectionId,
    to: d.emailTo ?? [],
    cc: d.emailCc ?? [],
    subject: d.emailSubject ?? '',
    body: d.emailBody ?? '',
    body_is_html: !!d.emailHtml,
    attachment_name: (d.attachmentName ?? '').trim(),
    attachment_format: d.attachmentFormat ?? 'xlsx',
    stop_on_failure: d.stopOnFailure !== false,
    timezone: String(d.timezone ?? '').trim(), // of the dates in subject and attachment name
  }
}

// Dry run of the email node: the same body as a real run, but the gateway sets the
// recipient (the signed-in user) and nothing is published. It serves to see the
// attachment really generated before sending it to someone else.
async function emailTestSelected() {
  const node = selectedNode.value
  if (!node) return
  if (flowId.value === null) {
    setStatus(t('flowEditor.emailTestNeedsSavedFlow'), 'error')
    return
  }
  const { sourceNode, operations: ops } = resolveChain(getNodes.value, getEdges.value, node.id, scriptSources.value)
  if (!sourceNode?.data?.parquetKey) {
    setStatus(t('flowEditor.outputMissingChain'), 'error')
    return
  }
  busy.value = true
  try {
    const launched = await runsApi.emailTest(flowId.value, {
      bucket: sourceNode.data.bucket ?? bucket,
      input_key: sourceNode.data.parquetKey,
      operations: ops,
      email: emailSpecOf(node.data),
    })
    setStatus(t('flowEditor.emailTestStarted'), 'busy')
    pollRun(launched.id, t('flowEditor.emailTestLabel'))
  } catch (e) {
    setStatus(errMessage(e), 'error')
  } finally {
    busy.value = false
  }
}

// one run per Output node: each one's chain is its left input
async function executeOutputRuns() {
  runDialogError.value = ''
  busy.value = true
  pollToken++ // new cycle: the earlier runs' polls stop
  try {
    for (const node of outputNodes()) {
      const label = describeOutput(node).label
      const { sourceNode, operations: ops } = resolveChain(getNodes.value, getEdges.value, node.id, scriptSources.value)
      if (!sourceNode?.data?.parquetKey) {
        runDialogError.value = `${label}: ${t('flowEditor.outputMissingChain')}`
        selectedId.value = node.id
        return // dialog open: the outputs launched already go on anyway
      }
      const d = node.data
      const destType = d.destType ?? 'datasource'
      let publish = null
      let destination = null
      let email = null
      let mirror: MirrorSpec | null = null
      if (destType === 'email') {
        email = emailSpecOf(d)
      } else if (destType === 'database') {
        destination = {
          type: 'database' as const,
          connection_id: d.connectionId,
          table: d.table.trim(),
          mode: d.mode ?? 'append',
          post_sql: d.postSql ?? '',
        }
      } else if (destType === 'sftp') {
        // the gateway adds the run's number and the flow's name for the template
        destination = {
          type: 'sftp' as const,
          connection_id: d.connectionId,
          folder: (d.sftpFolder ?? '').trim(),
          file_name: (d.sftpFileName ?? '').trim(),
          format: d.sftpFormat ?? 'csv',
          timezone: String(d.timezone ?? '').trim(),
        }
      } else if (destType === 's3') {
        destination = {
          type: 's3' as const,
          connection_id: d.connectionId,
          bucket: d.s3Bucket?.trim() ?? '',
          key: d.s3Key.trim(),
          format: d.s3Format ?? 'parquet',
          partition_by: d.partitionBy ?? [],
          timezone: String(d.timezone ?? '').trim(),
        }
      } else {
        publish = {
          name: d.name.trim(),
          project_id: d.projectId,
          description: d.description ?? '',
          overwrite: !!d.overwrite,
          sort_keys: d.sortKeys ?? [],
        }
        // the S3 copy IN ADDITION to the datasource, as a scheduled run sends it
        // (gateway flow_resolver): best effort, it never fails the publish
        if (d.mirrorEnabled && d.mirrorConnectionId && String(d.mirrorKey ?? '').trim()) {
          mirror = {
            connection_id: d.mirrorConnectionId,
            bucket: String(d.mirrorBucket ?? '').trim(),
            key: String(d.mirrorKey ?? '').trim(),
            timezone: String(d.timezone ?? '').trim(),
          }
        }
      }
      try {
        const launched = await runsApi.launch(flowId.value!, {
          bucket: sourceNode.data.bucket ?? bucket,
          input_key: sourceNode.data.parquetKey,
          operations: ops,
          publish,
          destination,
          email,
          mirror,
        })
        setStatus(`${label}: ${t('flowEditor.outputRunStarted', { id: launched.id })}`, 'busy')
        void following(() => pollRun(launched.id, label))
      } catch (e) {
        // 409 duplicate name / 403 permissions: the dialog stays open on the error
        runDialogError.value = `${label}: ${errMessage(e)}`
        selectedId.value = node.id
        return
      }
    }
    runDialogOpen.value = false // all launched
  } finally {
    busy.value = false
  }
}

// cancellation token: a new CYCLE of runs (or the unmount) invalidates the earlier
// polls; the polls of the same cycle (one run per Output node) live together
let pollToken = 0
onUnmounted(() => {
  pollToken++
})

async function pollRun(runId: number, label = '') {
  const token = pollToken // captures the current cycle (bumped by executeRun*)
  const prefix = label ? `${label}: ` : ''
  // ~1.5s * 2400 ≈ 1h, aligned with the engine's task_time_limit
  for (let i = 0; i < 2400; i++) {
    await new Promise((r) => setTimeout(r, 1500))
    if (token !== pollToken) return // page closed or new cycle of runs: stop
    let run
    try {
      run = await runsApi.get(runId) // the GET reconciles the state on the gateway
    } catch {
      continue
    }
    if (token !== pollToken) return
    if (run.status === 'SUCCESS') {
      let target = run.publish_name ? ` → datasource “${run.publish_name}”` : ''
      if (run.destination) {
        try {
          const d = JSON.parse(run.destination)
          target = d.type === 's3'
            ? ` → s3://${d.bucket}/${d.written_key ?? d.key}` // the key its placeholders gave
            : d.type === 'sftp'
              ? ` → sftp://${d.host}${d.path ?? ''}`
              : ` → ${d.db_type} ${d.database ? d.database + '.' : ''}${d.table}`
        } catch { /* unreadable summary: the base message stays */ }
      }
      setStatus(`${prefix}${t('flowEditor.completedRows', { rows: run.rows_written })}${target}`, 'ok')
      if (run.datasource_id) refreshDatasources() // usable in the picker at once
      return
    }
    if (run.status === 'FAILURE') {
      setStatus(`${prefix}${t('flowEditor.errorInline', { error: knownError(run.error, t) })}`, 'error')
      return
    }
  }
  setStatus(`${prefix}${t('flowEditor.runTimeout')}`, 'error')
}

async function pollTask(id: string) {
  const token = pollToken
  for (let i = 0; i < 120; i++) {
    await new Promise((r) => setTimeout(r, 1000))
    if (token !== pollToken) return
    const st = await api.taskStatus(id)
    if (token !== pollToken) return
    if (st.status === 'SUCCESS') {
      setStatus(t('flowEditor.taskCompleted', { rows: st.result?.rows_written }), 'ok')
      return
    }
    if (st.status === 'FAILURE') {
      setStatus(t('flowEditor.errorGeneric', { error: st.error }), 'error')
      return
    }
  }
  setStatus(t('flowEditor.taskTimeout'), 'error')
}
</script>

<template>
  <div v-label-fields class="app" :style="appStyle">
    <Toolbar
      class="toolbar"
      :status="status ?? idleStatus"
      :status-kind="statusKind"
      :busy="busy"
      :can-run="canRun"
      :flow-name="flowName"
      :projects="projectsList"
      :project-id="projectId"
      :engine="flowEngine"
      :production-engine="flowProductionEngine"
      :read-only="observerOnly"
      @upload="onUpload"
      @add-op="addOperation"
      @add-source="addSource"
      @run="run"
      @save="saveFlow"
      @logout="leave"
      :dirty="dirty"
      @update:flow-name="flowName = $event"
      @update:project-id="projectId = $event"
    />
    <p v-if="alsoOpenBy.length" class="also-open" role="status">
      <Users :size="14" aria-hidden="true" />
      <span>{{ $t('presence.alsoOpenBy', { who: alsoOpenLabel }) }}</span>
      <span class="also-open-why">{{ $t('presence.lastSaveWins') }}</span>
    </p>

    <div class="sidebar">
      <OpSidebar
        :operations="operations" :read-only="observerOnly" :scripting="scriptingAvailable" :containers="containersAvailable"
        :scripting-off="scriptingEditor.state.value?.disabled_reason === 'kubernetes'"
        :containers-off="registriesEditor.state.value?.disabled_reason === 'kubernetes'"
      />
    </div>

    <div
      ref="canvasEl" class="canvas" tabindex="-1"
      @drop="onCanvasDrop" @dragover="onCanvasDragOver" @dragleave="onCanvasDragLeave"
      @contextmenu="onCanvasContextMenu" @keydown.capture="onCanvasKeys"
    >
      <!-- the canvas' keys, listed («?» opens it from the keyboard) -->
      <GridKeys v-if="!loadBlocked" ref="canvasKeys" class="canvas-keys" :groups="keyHelp.groups.value" :title="$t('appKeys.title')" :label="$t('canvasKeys.button')" wide />
      <div v-if="loadBlocked" class="not-open" role="alert">
        <ShieldAlert :size="22" />
        <h2>{{ $t('flowEditor.cannotOpenTitle') }}</h2>
        <p>{{ loadBlocked }}</p>
      </div>
      <VueFlow
        :nodes="initialNodes"
        :min-zoom="0.2"
        :delete-key-code="null"
      >
        <template #edge-default="props">
          <FlowEdge v-bind="props" />
        </template>
        <template #node-source="props">
          <SourceNode :id="props.id" :data="props.data" />
        </template>
        <template #node-operation="props">
          <OperationNode :id="props.id" :data="props.data" />
        </template>
        <template #node-foreach="props">
          <ForeachNode :id="props.id" :data="props.data" />
        </template>
        <template #node-output="props">
          <OutputNode :id="props.id" :data="props.data" />
        </template>
        <template #node-refresh="props">
          <RefreshNode :id="props.id" :data="props.data" />
        </template>
        <template #node-runflow="props">
          <RunFlowNode :id="props.id" :data="props.data" />
        </template>
        <template #node-script="props">
          <ScriptNode :id="props.id" :data="props.data" />
        </template>
        <template #node-container="props">
          <ContainerNode :id="props.id" :data="props.data" />
        </template>
        <template #node-comment="props">
          <CommentNode :id="props.id" :data="props.data" />
        </template>
        <Background pattern-color="#2a2f3a" :gap="16" />
        <!-- Vue Flow's four buttons came without a name (an icon alone, nothing for a screen
             reader, no tooltip): drawn here, in the icons of the three beside them -->
        <Controls>
          <template #control-zoom-in>
            <ControlButton class="vue-flow__controls-zoomin" :title="$t('canvasKeys.zoomIn')" :aria-label="$t('canvasKeys.zoomIn')" :disabled="viewport.zoom >= maxZoom" @click="zoomIn()">
              <Plus :size="13" />
            </ControlButton>
          </template>
          <template #control-zoom-out>
            <ControlButton class="vue-flow__controls-zoomout" :title="$t('canvasKeys.zoomOut')" :aria-label="$t('canvasKeys.zoomOut')" :disabled="viewport.zoom <= minZoom" @click="zoomOut()">
              <Minus :size="13" />
            </ControlButton>
          </template>
          <template #control-fit-view>
            <ControlButton class="vue-flow__controls-fitview" :title="$t('canvasKeys.fitView')" :aria-label="$t('canvasKeys.fitView')" @click="fitView()">
              <Maximize :size="13" />
            </ControlButton>
          </template>
          <template #control-interactive>
            <ControlButton
              class="vue-flow__controls-interactive"
              :title="canvasLocked ? $t('canvasKeys.unlock') : $t('canvasKeys.lock')"
              :aria-label="$t('canvasKeys.lock')"
              :aria-pressed="canvasLocked"
              @click="setInteractive(canvasLocked)"
            >
              <component :is="canvasLocked ? Lock : LockOpen" :size="13" />
            </ControlButton>
          </template>
          <ControlButton :title="$t('flowEditor.orderFlowTitle')" :aria-label="$t('flowEditor.orderFlowTitle')" @click="autoLayout">
            <Wand2 :size="13" />
          </ControlButton>
          <ControlButton :title="$t('flowEditor.undoTitle')" :aria-label="$t('flowEditor.undoTitle')" :disabled="!canUndo" @click="undo">
            <Undo2 :size="13" />
          </ControlButton>
          <ControlButton :title="$t('flowEditor.redoTitle')" :aria-label="$t('flowEditor.redoTitle')" :disabled="!canRedo" @click="redo">
            <Redo2 :size="13" />
          </ControlButton>
        </Controls>
        <MiniMap
          pannable
          zoomable
          position="bottom-right"
          :width="180"
          :height="110"
          :node-color="miniMapColor"
          :node-border-radius="3"
          mask-color="color-mix(in srgb, var(--bg) 72%, transparent)"
          mask-stroke-color="var(--accent)"
          :mask-stroke-width="1"
          :aria-label="$t('flowEditor.miniMapLabel')"
        />
      </VueFlow>
    </div>

    <div class="panel" :class="{ tabbed: aiReady }">
      <div
        class="panel-resizer"
        role="separator"
        aria-orientation="vertical"
        tabindex="0"
        :aria-label="$t('flowEditor.resizePanel')"
        :title="$t('flowEditor.resizeHint')"
        @mousedown="startPanelResize"
        @keydown="panelKeys"
      />
      <div v-if="aiReady" v-roving class="panel-tabs" role="tablist" :aria-label="$t('flowAssistant.panelTabs')">
        <button
          type="button"
          role="tab"
          :aria-selected="rightTab === 'node'"
          :class="{ on: rightTab === 'node' }"
          @click="rightTab = 'node'"
        ><SlidersHorizontal :size="13" /> {{ $t('flowAssistant.tabNode') }}</button>
        <button
          type="button"
          role="tab"
          :aria-selected="rightTab === 'assistant'"
          :class="{ on: rightTab === 'assistant' }"
          @click="rightTab = 'assistant'"
        ><Sparkles :size="13" /> {{ $t('flowAssistant.tabAssistant') }}</button>
      </div>
      <FlowAssistant
        v-if="aiReady && aiStatus"
        v-show="rightTab === 'assistant'"
        :active="rightTab === 'assistant'"
        :status="aiStatus"
        :flow-id="flowId"
        :engine="flowEngine"
        :selected-id="assistantSelection"
        :snapshot="serializeCanvas"
        :counters="assistantCounters"
        @apply="applyAssistant"
      />
      <NodePanel
        v-show="!aiReady || rightTab === 'node'"
        :node="selectedNode"
        :operations="operations"
        :input-columns="inputColumns"
        :right-columns="rightColumns"
        :columns-loading="columnsLoading"
        :placeholders="placeholders"
        :fetch-distinct="fetchDistinctValues"
        :datasources="datasources"
        :projects="projectsList"
        :connections="connectionsList"
        :flows="flowsList"
        :current-flow-id="flowId"
        :flow-name="flowName"
        :script-outputs="scriptOutputs"
        :script-trials="scriptTrials"
        :flow-dirty="dirty"
        :has-data-out="selectedHasDataOut"
        :read-only="observerOnly"
        @script-run="runScriptNode"
        @update="patchSelected"
        @delete="deleteSelected"
        @export="exportSelected"
        @preview="previewSelected"
        @email-test="emailTestSelected"
      />
    </div>

    <SheetPickerDialog
      :open="!!sheetChoice"
      :filename="sheetChoice?.file.name ?? ''"
      :sheets="sheetChoice?.sheets ?? []"
      :hidden="sheetChoice?.hidden ?? []"
      :read="sheetChoice?.read ?? ''"
      @choose="importSheets"
      @close="sheetChoice = null"
    />

    <RunDialog
      :open="runDialogOpen"
      :can-publish="flowId !== null"
      :projects="projectsList"
      :default-project-id="projectId"
      :error="runDialogError"
      :busy="busy"
      :outputs="runOutputs"
      @confirm="executeRun"
      @cancel="runDialogOpen = false"
    />

    <div class="grid">
      <div
        class="grid-resizer"
        role="separator"
        aria-orientation="horizontal"
        tabindex="0"
        :aria-label="$t('flowEditor.resizePreview')"
        :title="$t('flowEditor.resizeHint')"
        @mousedown="startGridResize"
        @keydown="viewKeys"
      />
      <div class="viewhead">
      <div v-roving.main class="viewtabs" role="tablist" :aria-label="$t('appKeys.viewTabs')">
        <button
          role="tab"
          :class="{ active: viewTab === 'table' }"
          :aria-selected="viewTab === 'table'"
          :aria-label="$t('flowEditor.tableTab')"
          @click="viewTab = 'table'"
        ><Table2 :size="13" aria-hidden="true" /><span class="vt-label">{{ $t('flowEditor.tableTab') }}</span></button>
        <button
          role="tab"
          :class="{ active: viewTab === 'chart' }"
          :aria-selected="viewTab === 'chart'"
          :aria-label="$t('flowEditor.chartTab')"
          @click="viewTab = 'chart'"
        ><BarChart3 :size="13" aria-hidden="true" /><span class="vt-label">{{ $t('flowEditor.chartTab') }}</span></button>
      </div>
        <!-- what the rows on screen are: whose (always first, where it stays), how many of how
             many, what the step removed; while a node computes, its name, the seconds, and
             the way out. Not a live region: one announcement a click, below. -->
        <div class="pv-bar">
          <p id="pv-summary" class="pv-summary" :title="settledSummary || undefined">
            <template v-if="computingNodeId">
              <span class="pv-node" :title="nodeFullName(computingNodeId)">{{ nodeName(computingNodeId) }}</span>
              <span v-if="computingWord" class="pv-part">{{ $t('dataGrid.computing') }}</span>
              <span v-if="slowCompute" class="pv-part">{{ $t('dataGrid.seconds', { s: computeSeconds }) }}</span>
            </template>
            <template v-else-if="preview">
              <span class="pv-node" :title="nodeFullName(previewNodeId)">{{ nodeName(previewNodeId) }}</span>
              <span class="pv-part pv-rows"><span class="pv-long">{{ summaryRows }}</span><span class="pv-short">{{ summaryRowsShort }}</span></span>
              <span v-if="rowCount.state === 'counting'" class="pv-part pv-quiet">{{ $t('dataGrid.counting') }}</span>
              <span
                v-else-if="rowCount.state === 'failed'"
                class="pv-part pv-quiet pv-failed"
                :title="rowCount.slow ? $t('dataGrid.countSlowTitle') : countError || undefined"
              >{{ rowCount.slow ? $t('dataGrid.countSlow') : $t('dataGrid.countFailed') }}<button
                type="button"
                class="pv-retry"
                :aria-label="$t('dataGrid.countAgain')"
                :title="$t('dataGrid.countAgain')"
                @click="countRetry"
              ><RotateCw :size="11" aria-hidden="true" /></button></span>
              <span class="pv-part">{{ $t('dataGrid.columns', { n: preview.columns.length }) }}</span>
              <!-- «N columns removed», «−N» when the bar is narrow (its name stays whole) -->
              <span v-if="previewChanges?.removed.length" class="pv-part">
                <button
                  type="button"
                  class="pv-removed"
                  aria-haspopup="true"
                  :aria-expanded="removedOpen"
                  aria-controls="pv-removed-list"
                  :aria-label="$t('dataGrid.removed', { n: previewChanges.removed.length }, previewChanges.removed.length)"
                  @click="toggleRemoved"
                ><span class="pv-long">{{ $t('dataGrid.removed', { n: previewChanges.removed.length }, previewChanges.removed.length) }}</span><span
                  class="pv-short"
                  aria-hidden="true"
                >−{{ previewChanges.removed.length }}</span></button>
              </span>
              <!-- the step is computed again at every preview: said quietly, explained on
                   demand; an icon alone (its name kept) when the bar is narrow -->
              <span v-if="shownCacheSkipped" class="pv-part">
                <button
                  type="button"
                  class="pv-removed pv-cache"
                  aria-haspopup="true"
                  :aria-expanded="cacheOpen"
                  aria-controls="pv-cache-why"
                  :aria-label="$t('dataGrid.cacheCap')"
                  :title="$t('nodeStats.noCache', { n: shownCacheSkipped })"
                  @click="toggleCache"
                ><RefreshCw :size="11" aria-hidden="true" /><span class="pv-long">{{ $t('dataGrid.cacheCap') }}</span></button>
              </span>
            </template>
            <span v-if="shownSample && (computingNodeId || preview)" class="pv-part pv-sample"><FlaskConical :size="11" aria-hidden="true" /> {{ shownSample }}</span>
          </p>
          <div
            v-if="removedOpen && previewChanges?.removed.length"
            id="pv-removed-list"
            ref="removedPop"
            class="pv-pop"
            :style="{ left: `${popLeft}px` }"
            role="group"
            :aria-label="$t('dataGrid.removedTitle')"
            tabindex="-1"
          >
            <p class="pv-pop-title">{{ $t('dataGrid.removedTitle') }}</p>
            <ul>
              <li v-for="c in previewChanges.removed" :key="c">{{ c }}</li>
            </ul>
          </div>
          <div
            v-if="cacheOpen && shownCacheSkipped"
            id="pv-cache-why"
            ref="cachePop"
            class="pv-pop pv-pop-text"
            :style="{ left: `${popLeft}px` }"
            role="group"
            :aria-label="$t('dataGrid.cacheCap')"
            tabindex="-1"
          >
            <p>{{ $t('nodePanel.cacheSkipped', { n: shownCacheSkipped }) }}</p>
          </div>
          <span class="sr-only" role="status" aria-live="polite">{{ liveText }}</span>
          <GridKeys v-if="viewTab === 'table' && (preview || computingNodeId)" profile />
          <button
            v-if="offerSample"
            type="button"
            class="pv-action pv-sample-btn"
            :title="$t('dataGrid.useSampleTitle')"
            @click="openSourceSample"
          >
            <FlaskConical :size="12" aria-hidden="true" /> {{ $t('dataGrid.useSample') }}
          </button>
          <!-- the bar narrow: «Use a sample» goes into a menu, first of what gives way -->
          <span v-if="offerSample" class="pv-more">
            <button
              type="button"
              class="pv-more-btn"
              aria-haspopup="menu"
              :aria-expanded="moreOpen"
              :aria-label="$t('dataGrid.moreActions')"
              :title="$t('dataGrid.moreActions')"
              @click="moreOpen = !moreOpen"
            ><MoreHorizontal :size="14" aria-hidden="true" /></button>
            <div v-if="moreOpen" ref="morePop" class="pv-pop pv-more-pop" role="menu">
              <button type="button" role="menuitem" class="pv-more-item" :title="$t('dataGrid.useSampleTitle')" @click="sampleFromMenu">
                <FlaskConical :size="12" aria-hidden="true" /> {{ $t('dataGrid.useSample') }}
              </button>
            </div>
          </span>
          <button v-if="slowCompute" type="button" class="pv-action" @click="cancelPreview">
            <X :size="12" aria-hidden="true" /> {{ $t('dataGrid.cancel') }}
          </button>
        </div>
      </div>
      <div class="viewbody">
        <DataGrid
          v-if="viewTab === 'table'"
          :result="preview"
          :loading="previewLoading"
          :stale="!!computingNodeId"
          :error="previewError"
          :note="previewNote"
          :summary="summaryRows"
          summary-id="pv-summary"
          :changes="previewChanges"
          :count="rowCount"
          :pending-columns="computingColumns"
          :profile="profile"
          :profile-state="profileState"
          :profile-error="profileError"
          :profile-retry="profileRetry"
          :profile-whole="profileWhole"
          :column-detail="columnDetail"
          :value-actions="valueActions"
          :empty="selectedId ? $t('dataGrid.nothingYet') : undefined"
        >
          <template #action>
            <button v-if="cancelledNodeId" type="button" class="pv-again" @click="computeAgain">
              <RotateCw :size="13" aria-hidden="true" /> {{ $t('dataGrid.computeAgain') }}
            </button>
          </template>
        </DataGrid>
        <ChartPanel v-else :columns="preview?.columns ?? []" :query="chartQuery" />
      </div>
    </div>
    <FlowQuickMenu ref="quickMenu" :items="shortcutItems" @pick="addOperationAt" @customize="shortcutsOpen = true" @close="onQuickMenuClose" />
    <FlowShortcutsDialog
      :open="shortcutsOpen"
      :available="operations"
      :current="shortcutItems"
      :saving="shortcutsSaving"
      @save="saveShortcuts"
      @close="shortcutsOpen = false"
    />
    <Modal :open="leaveOpen" :title="$t('flowEditor.leaveTitle')" :width="440" @close="answerLeaving(false)">
      <p class="leave-body">{{ $t('flowEditor.leaveBody', { name: flowName.trim() || '—' }) }}</p>
      <template #footer>
        <button @click="answerLeaving(false)">{{ $t('flowEditor.leaveStay') }}</button>
        <button class="danger" @click="answerLeaving(true)">{{ $t('flowEditor.leaveDiscard') }}</button>
        <button class="primary" @click="saveAndLeave">{{ $t('flowEditor.leaveSave') }}</button>
      </template>
    </Modal>
  </div>
</template>

<style scoped>
.not-open {
  position: absolute; inset: 0; z-index: 20; display: flex; flex-direction: column; align-items: center;
  justify-content: center; gap: 10px; padding: 24px; text-align: center; background: var(--bg);
}
.not-open svg { color: var(--warning); }
.not-open h2 { margin: 0; font-size: 16px; }
.not-open p { margin: 0; max-width: 520px; font-size: 13.5px; line-height: 1.5; color: var(--muted); }
/* the right panel with the assistant beside the node settings */
.panel.tabbed { display: flex; flex-direction: column; min-height: 0; }
.panel.tabbed > .nodepanel, .panel.tabbed > .fa { flex: 1; min-height: 0; height: auto; }
.panel-tabs { display: flex; gap: 2px; padding: 6px 8px 0; border-bottom: 1px solid var(--border-soft); }
.panel-tabs button {
  display: inline-flex; align-items: center; gap: 5px; padding: 6px 10px; font-size: 12.5px;
  border: 0; border-bottom: 2px solid transparent; border-radius: 6px 6px 0 0; background: transparent;
  color: var(--muted); box-shadow: none;
}
.panel-tabs button:hover { color: var(--text); box-shadow: none; }
.panel-tabs button.on { color: var(--text); border-bottom-color: var(--accent); }

/* the preview's summary, in the tab bar: one line after the tabs, the node first and where
   it stays (the parts that arrive later are added after it, nothing before it moves), the
   actions at the right edge */
/* The bar is a size container: as it narrows, the parts give way in order, never cut in
   the middle of a word (critique of 2026-11-08: «Filter s…», «12 columns remove»):
   1. «Use a sample» goes into a «…» menu;  2. the cache's mark keeps its icon alone;
   3. «N columns removed» becomes «−N». The node's name keeps 14 characters at least, all of
   it in the tooltip; a part that still finds no room goes to a second line that is not
   shown (the whole sentence is the summary's tooltip). */
.viewhead { position: relative; z-index: 5; display: flex; align-items: center; gap: 12px; flex-shrink: 0; min-width: 0; container: pvbar / inline-size; }
.pv-bar { position: relative; flex: 1; min-width: 0; display: flex; align-items: center; gap: 6px; }
.pv-summary {
  display: flex; flex-wrap: wrap; align-items: baseline; row-gap: 12px; min-width: 0;
  /* room inside for the buttons' focus rings, which the clip would cut */
  height: 24px; padding: 4px; margin: -4px auto -4px -4px; overflow: hidden; white-space: nowrap;
  font-size: 12px; line-height: 16px; color: var(--muted); font-variant-numeric: tabular-nums;
}
.pv-node { flex: 1 1 14ch; min-width: 14ch; max-width: max-content; overflow: hidden; text-overflow: ellipsis; color: var(--text); font-weight: 500; }
.pv-part { flex: none; }
.pv-part::before { content: '·'; margin: 0 6px; color: var(--muted); }
.pv-quiet { opacity: 0.8; }
.pv-failed { display: inline-flex; align-items: baseline; gap: 4px; }
.pv-retry {
  margin: 0; padding: 0 2px; border: 0; background: none; box-shadow: none; color: inherit; line-height: 1;
  align-self: center; cursor: pointer;
}
.pv-retry:hover { color: var(--text); background: none; box-shadow: none; }
.pv-retry:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; border-radius: 3px; }
.pv-short { display: none; }
.pv-more { display: none; position: relative; flex: none; }
.pv-more-btn {
  display: inline-flex; align-items: center; justify-content: center; width: 24px; height: 24px; padding: 0;
  border: 1px solid transparent; border-radius: 6px; background: none; box-shadow: none; color: var(--muted);
}
.pv-more-btn:hover, .pv-more-btn[aria-expanded="true"] { color: var(--text); background: var(--panel-2); border-color: var(--border); box-shadow: none; }
.pv-more-btn:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
.pv-more-pop { right: 0; left: auto; min-width: 0; width: max-content; padding: 4px; }
.pv-more-item { display: inline-flex; align-items: center; gap: 6px; width: 100%; padding: 5px 8px; font-size: 12px; border: 0; background: none; box-shadow: none; }
.pv-more-item:hover { background: var(--row-hover); box-shadow: none; }
@container pvbar (max-width: 960px) {
  .pv-bar .pv-sample-btn { display: none; }
  .pv-bar .pv-more { display: inline-flex; }
}
@container pvbar (max-width: 860px) {
  .pv-cache .pv-long { display: none; }
}
@container pvbar (max-width: 780px) {
  .pv-removed .pv-long { display: none; }
  .pv-removed .pv-short { display: inline; }
}
/* 4. narrower still (a 1024px window): the tabs keep their icons (their names kept for the
   screen reader), the count says it as a node does («42,007,616 rows») */
.viewtabs button { display: inline-flex; align-items: center; gap: 5px; }
@container pvbar (max-width: 600px) {
  .viewtabs .vt-label { display: none; }
  .viewtabs button { padding: 4px 9px; }
  .pv-rows .pv-long { display: none; }
  .pv-rows .pv-short { display: inline; }
}
.pv-removed {
  margin: 0; padding: 0; border: 0; background: none; box-shadow: none; font: inherit; color: inherit;
  text-decoration: underline dotted; text-underline-offset: 3px; cursor: pointer;
}
.pv-removed:hover, .pv-removed[aria-expanded="true"] { color: var(--text); background: none; box-shadow: none; }
.pv-removed:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; border-radius: 3px; }
.pv-pop {
  position: absolute; top: calc(100% + 6px); z-index: 30;
  min-width: 180px; max-width: min(360px, 100%); max-height: 240px; overflow: auto;
  padding: 8px 10px; background: var(--panel); border: 1px solid var(--border);
  border-radius: var(--radius); box-shadow: var(--shadow-2); font-size: 12px;
}
.pv-pop:focus { outline: none; }
.pv-pop-text { max-width: min(320px, 100%); }
.pv-pop-text p { margin: 0; line-height: 1.45; color: var(--text); }
.pv-cache { display: inline-flex; align-items: center; gap: 3px; text-decoration: none; }
.pv-cache .pv-long { text-decoration: underline dotted; text-underline-offset: 3px; }
.pv-pop-title { margin: 0 0 4px; color: var(--muted); }
.pv-pop ul { margin: 0; padding: 0; list-style: none; }
.pv-pop li { padding: 2px 0; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11.5px; overflow-wrap: anywhere; }
.pv-sample { display: inline-flex; align-items: center; gap: 3px; }
.pv-sample svg { vertical-align: -1px; }
.pv-action {
  flex: none; display: inline-flex; align-items: center; gap: 4px;
  padding: 3px 9px; font-size: 12px;
}
.pv-again { display: inline-flex; align-items: center; gap: 5px; padding: 4px 10px; font-size: 12px; }

/* the «unsaved changes» dialog's text */
.leave-body { margin: 0; line-height: 1.5; }
</style>
