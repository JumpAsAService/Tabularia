<script setup lang="ts">
import { ref, reactive, computed, watch, nextTick, onMounted, onUnmounted, provide } from 'vue'
import { VueFlow, useVueFlow } from '@vue-flow/core'
import type { Node, Connection } from '@vue-flow/core'
import { Background } from '@vue-flow/background'
import { Controls, ControlButton } from '@vue-flow/controls'
import { MiniMap } from '@vue-flow/minimap'
import { opMeta } from '~/composables/useOpIcons'

import { Table2, BarChart3, Wand2, Users, Undo2, Redo2, Sparkles, SlidersHorizontal, ShieldAlert } from 'lucide-vue-next'
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
import type { PreviewResult, ColumnInfo, Operation, IngestResult } from '~/composables/useApi'
import { SOURCE_ID, buildIncoming, resolveChain, leafNodeId, defaultParams } from '~/composables/useFlowModel'
import { computeAutoLayout } from '~/composables/useFlowLayout'
import { createFlowHistory } from '~/composables/useFlowHistory'
import { onBeforeRouteLeave } from 'vue-router'
import Modal from '~/components/ui/Modal.vue'
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
  addNodes,
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
  onEdgeMouseEnter,
  onEdgeMouseLeave,
  getSelectedEdges,
  getSelectedNodes,
  addSelectedNodes,
  removeSelectedNodes,
  screenToFlowCoordinate,
  fitView,
  setCenter,
  getViewport,
  onNodesInitialized,
} = useVueFlow()

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
function setStatus(msg: string, kind: 'info' | 'ok' | 'error' | 'busy' = 'info') {
  status.value = msg
  statusKind.value = kind
  // successes and errors as toasts too: they show with the bar full/ignored too
  if (kind === 'ok') toast.success(msg)
  else if (kind === 'error') toast.error(msg)
}
const busy = ref(false)
// The known rows and columns of each node, from the LAST preview that touched it: no
// extra query, and never saved in the flow (provide → node components).
// `rows` is missing when only the columns are known (`cols` request, limit 1).
const nodeStats = reactive<Record<string, NodeStats>>({})
// nodes for which the «upstream step over the cache cap» warning has been shown already
// in this session: the toast once, then the node speaks
const cacheWarned = new Set<string>()
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
const fmtWhen = useFormat().dateTime
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

// serializes ONLY what is needed to rebuild the canvas (no internal Vue Flow state)
function serializeCanvas(): string {
  return JSON.stringify({
    nodes: getNodes.value.map((n) => ({
      id: n.id,
      type: n.type,
      position: { x: n.position.x, y: n.position.y },
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
  setStatus(t('flowEditor.flowLoaded', { name: f.name }), 'ok')
  loadScriptOutputs()
}

// Puts a definition on the canvas (the saved one, or a snapshot of the history).
// `keepCounters`: the ids' counters NEVER go back — after an undo a new node must not
// take again the id of one just gone, or it would inherit its cached columns and counts.
function applyDefinition(def: any, keepCounters = false) {
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
    preview.value = null
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
  fitView({ padding: 0.15, maxZoom: 1 })
})

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
  if (measured || frames <= 0) fitView({ padding: 0.15, duration: 300, maxZoom: 1 })
  else requestAnimationFrame(() => fitWhenMeasured(ids, frames - 1))
}

onMounted(async () => {
  if (!signedIn.value) await fetchMe()
  // automatic development sample: must be known BEFORE the first preview
  await useAppInfo().load()
  try {
    operations.value = await api.operations()
  } catch (e) {
    setStatus(t('flowEditor.backendUnreachable', { error: errMessage(e) }), 'error')
    return
  }
  try {
    // the projects' list serves the toolbar's selector AND the run dialog
    projectsList.value = await projectsApi.list()
  } catch {
    projectsList.value = []
  }
  // the catalog BEFORE the flow: loadFlow reattaches the datasource sources to the
  // current snapshot (the saved keys become stale at every refresh)
  await refreshDatasources()
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
  if (flowId.value === null) {
    // new flow: it starts from the initial canvas, which is also the «saved» reference
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
function invalidateStats(fromId?: string) {
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
  if (rest.length) nextTick(() => fitView({ padding: 0.15, duration: 300, maxZoom: 1 }))
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

function addOperation() {
  const parentId = selectedId.value ?? leafNodeId(getNodes.value, getEdges.value)
  const parent = findNode(parentId)
  const id = `op-${++opCounter}`
  addNodes({
    id,
    type: 'operation',
    position: { x: (parent?.position.x ?? 60) + 230, y: parent?.position.y ?? 140 },
    data: { opType: operations.value[0] ?? 'select', params: {} },
  })
  addEdges({ id: `e-${parentId}-left-${id}`, source: parentId, target: id, targetHandle: 'left' })
  selectedId.value = id
  refreshForNode(id)
  // the new node beside the last one could land off the canvas, under the minimap: the
  // view moves to it, at the zoom it has
  const at = { x: (parent?.position.x ?? 60) + 230 + 80, y: (parent?.position.y ?? 140) + 40 }
  nextTick(() => setCenter(at.x, at.y, { zoom: getViewport().zoom, duration: 300 }))
}

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
function onCanvasDragOver(ev: DragEvent) {
  ev.preventDefault()
  if (ev.dataTransfer) ev.dataTransfer.dropEffect = 'move'
}

function onCanvasDrop(ev: DragEvent) {
  const kind = ev.dataTransfer?.getData('application/tabularia')
  if (!kind || loadBlocked.value) return // a flow that cannot be opened takes nothing under its notice
  if (!TRANSFORMATION(kind) && deniedToObserver()) return
  const position = screenToFlowCoordinate({ x: ev.clientX, y: ev.clientY })

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
    const id = `op-${++opCounter}`

    // drop INSIDE a foreach container → it becomes a child (relative position)
    const container = getNodes.value.find(
      (n) =>
        n.type === 'foreach' &&
        position.x > n.position.x &&
        position.x < n.position.x + (n.dimensions?.width ?? 0) &&
        position.y > n.position.y &&
        position.y < n.position.y + (n.dimensions?.height ?? 0),
    )
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
  preview.value = null
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
  if ((e.ctrlKey || e.metaKey) && !e.altKey) {
    const key_ = e.key.toLowerCase()
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
let columnsSeq = 0 // as previewSeq: only the last resolution turns the skeleton off
async function refreshForNode(nodeId: string) {
  const inc = buildIncoming(getEdges.value)
  const node = findNode(nodeId)
  columnsLoading.value = true
  const colSeq = ++columnsSeq
  const t0 = performance.now()
  // Whoever goes through the nodes one after another does not want each one's preview: it
  // waits a moment, and if another click arrived meanwhile this one does not even start.
  // The skeleton is on already: the delay does not show.
  await new Promise((r) => setTimeout(r, CLICK_SETTLE_MS))
  if (colSeq !== columnsSeq) return

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

  // turns the columns' skeleton off after the minimum visibility, WITHOUT delaying the
  // preview's start (which starts right here below)
  skeletonPad(t0).then(() => {
    if (colSeq === columnsSeq) columnsLoading.value = false
  })
  // Overtaken while resolving the columns: do NOT launch the old node's preview — it
  // would take the slot and knock down the new node's.
  if (colSeq !== columnsSeq) return
  await runPreview(nodeId)
}

let previewSeq = 0 // only the LAST preview launched writes the result and turns the skeleton off
async function runPreview(nodeId: string) {
  const { sourceNode, operations: ops, afterScript } = resolveChain(getNodes.value, getEdges.value, nodeId, scriptSources.value)
  // the seq ALWAYS moves on: a preview that does not start must invalidate the old one
  // still in flight too, or that one would write its result on the new node
  const seq = ++previewSeq
  if (!sourceNode?.data?.parquetKey) {
    previewSlots.cancel('preview')
    preview.value = null
    previewLoading.value = false
    // what a script writes exists only during a run: the editor cannot preview it
    previewError.value = ''
    previewNote.value = afterScript ? t(`${stepTexts(afterScript)}.previewAfterRun`) : ''
    return
  }
  const t0 = performance.now()
  previewError.value = ''
  // the data come from a script's last run: say which, and whether its code changed since
  const fromScript = afterScript ? scriptOutputs.value[afterScript]?.output : null
  previewNote.value = fromScript && afterScript
    ? t(`${stepTexts(afterScript)}.${fromScript.trial ? 'previewFromTrial' : 'previewFromRun'}`,
      { when: fmtWhen(fromScript.finished_at) })
      + (fromScript.stale ? ` ${t(`${stepTexts(afterScript)}.previewStale`)}` : '')
    : ''
  previewLoading.value = true
  try {
    const res = await apiPreview({
      bucket: sourceNode.data.bucket ?? bucket,
      input_key: sourceNode.data.parquetKey,
      operations: ops,
      limit: 100,
    }, 'preview')
    nodeColumns[nodeId] = res.columns
    nodeStats[nodeId] = {
      rows: res.row_count, truncated: res.truncated, cols: res.columns.length,
      cacheState: res.cache_state ?? null, cacheCap: res.cache_cap_rows ?? null,
    }
    cacheCheckAfterPreview()
    if (res.cache_state === 'skipped' && !cacheWarned.has(nodeId)) {
      cacheWarned.add(nodeId)
      toast.warning(t('flowEditor.cacheSkippedToast', { n: (res.cache_cap_rows ?? 0).toLocaleString() }))
    }
    if (seq === previewSeq) preview.value = res
  } catch (e) {
    // "overtaken by a newer one" is not an error: it must never be shown
    if (seq === previewSeq && !isSuperseded(e)) {
      preview.value = null
      const message = errMessage(e)
      // a node just added and not set yet: not an error in red, the hint to set it
      if (/required parameter missing: '/.test(message)) previewNote.value = t('flowEditor.completeNodeHint')
      else previewError.value = message
    }
  } finally {
    await skeletonPad(t0) // skeleton visible at least the minimum: no flash
    if (seq === previewSeq) previewLoading.value = false
  }
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
      pollRun(launched.id) // in the background: no lock on the editor
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
      pollTask(res.task_id)
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
    pollOrchestration(run_id)
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
        pollRun(launched.id, label)
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

    <div class="canvas" @drop="onCanvasDrop" @dragover="onCanvasDragOver">
      <div v-if="loadBlocked" class="not-open" role="alert">
        <ShieldAlert :size="22" />
        <h2>{{ $t('flowEditor.cannotOpenTitle') }}</h2>
        <p>{{ loadBlocked }}</p>
      </div>
      <VueFlow
        :nodes="initialNodes"
        :min-zoom="0.2"
        :delete-key-code="null"
        :default-edge-options="{ animated: true }"
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
        <Controls>
          <ControlButton :title="$t('flowEditor.orderFlowTitle')" @click="autoLayout">
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
      <div v-if="aiReady" class="panel-tabs" role="tablist" :aria-label="$t('flowAssistant.panelTabs')">
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
      <div class="viewtabs">
        <button :class="{ active: viewTab === 'table' }" @click="viewTab = 'table'">
          <Table2 :size="13" /> {{ $t('flowEditor.tableTab') }}
        </button>
        <button :class="{ active: viewTab === 'chart' }" @click="viewTab = 'chart'">
          <BarChart3 :size="13" /> {{ $t('flowEditor.chartTab') }}
        </button>
      </div>
      <div class="viewbody" :class="{ scroll: viewTab === 'table' }">
        <DataGrid v-if="viewTab === 'table'" :result="preview" :loading="previewLoading" :error="previewError" :note="previewNote" />
        <ChartPanel v-else :columns="preview?.columns ?? []" :query="chartQuery" />
      </div>
    </div>
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

/* the «unsaved changes» dialog's text */
.leave-body { margin: 0; line-height: 1.5; }
</style>
