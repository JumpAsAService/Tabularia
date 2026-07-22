<script setup lang="ts">
import { ref, reactive, computed, nextTick, onMounted, onUnmounted } from 'vue'
import { VueFlow, useVueFlow } from '@vue-flow/core'
import type { Node, Connection } from '@vue-flow/core'
import { Background } from '@vue-flow/background'
import { Controls, ControlButton } from '@vue-flow/controls'

import { Table2, BarChart3, Wand2 } from 'lucide-vue-next'
import { useApi, errMessage } from '~/composables/useApi'
import type { PreviewResult, ColumnInfo, Operation } from '~/composables/useApi'
import { SOURCE_ID, buildIncoming, resolveChain, leafNodeId, defaultParams } from '~/composables/useFlowModel'
import { computeAutoLayout } from '~/composables/useFlowLayout'
import { useFlows } from '~/composables/useFlows'
import { useProjects } from '~/composables/useProjects'
import { useRuns, type PublishSpec, type DestinationSpec } from '~/composables/useRuns'
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
  screenToFlowCoordinate,
  fitView,
} = useVueFlow()

const initialNodes: Node[] = [
  {
    id: SOURCE_ID,
    type: 'source',
    position: { x: 60, y: 140 },
    data: { bucket, datasetId: null, parquetKey: null, filename: null, rows: null, columns: [] },
  },
]

// ── Stato UI ────────────────────────────────────────────────────────────
const selectedId = ref<string | null>(SOURCE_ID)
const operations = ref<string[]>([])
const inputColumns = ref<ColumnInfo[]>([])
const rightColumns = ref<ColumnInfo[]>([])
const columnsLoading = ref(false) // risoluzione colonne del nodo selezionato in corso
// placeholder {{colonna}} disponibili per un nodo DENTRO un container foreach
const placeholders = ref<string[]>([])
const nodeColumns = reactive<Record<string, ColumnInfo[]>>({}) // cache output per nodo

let opCounter = 0
let sourceCounter = 0
let outputCounter = 0
let commentCounter = 0

const viewTab = ref<'table' | 'chart'>('table') // vista sotto il canvas

// ── Pannello destro ridimensionabile (larghezza ricordata) ────────────────
const PANEL_MIN = 280
const PANEL_MAX = 640
const panelWidth = ref(
  Math.min(PANEL_MAX, Math.max(PANEL_MIN, Number(localStorage.getItem('tabularia.panelWidth')) || 340)),
)
// minmax(0,1fr) come in main.css: preview larghe (pivot) non devono spingere il pannello fuori schermo
const appStyle = computed(() => ({ gridTemplateColumns: `200px minmax(0, 1fr) ${panelWidth.value}px` }))

function startPanelResize(ev: MouseEvent) {
  ev.preventDefault()
  const onMove = (e: MouseEvent) => {
    panelWidth.value = Math.min(PANEL_MAX, Math.max(PANEL_MIN, window.innerWidth - e.clientX))
  }
  const onUp = () => {
    window.removeEventListener('mousemove', onMove)
    window.removeEventListener('mouseup', onUp)
    document.body.style.cursor = ''
    document.body.style.userSelect = ''
    localStorage.setItem('tabularia.panelWidth', String(panelWidth.value))
  }
  window.addEventListener('mousemove', onMove)
  window.addEventListener('mouseup', onUp)
  document.body.style.cursor = 'col-resize'
  document.body.style.userSelect = 'none'
}
const status = ref(t('flowEditor.statusInitial'))
// tipo di stato → icona nella toolbar (spinner / check / errore)
const statusKind = ref<'info' | 'ok' | 'error' | 'busy'>('info')
const toast = useToast()
function setStatus(msg: string, kind: 'info' | 'ok' | 'error' | 'busy' = 'info') {
  status.value = msg
  statusKind.value = kind
  // successi ed errori anche come toast: si vedono anche a barra piena/ignorata
  if (kind === 'ok') toast.success(msg)
  else if (kind === 'error') toast.error(msg)
}
const busy = ref(false)
const preview = ref<PreviewResult | null>(null)
const previewLoading = ref(false)
const previewError = ref('')

const selectedNode = computed(() => (selectedId.value ? findNode(selectedId.value) ?? null : null))
const firstSource = computed(() => getNodes.value.find((n) => n.type === 'source'))
const canRun = computed(
  () =>
    !!firstSource.value?.data?.parquetKey ||
    getNodes.value.some((n) => n.type === 'refresh' || n.type === 'runflow'),
)

// ── Flusso salvato (persistenza nel gateway) ─────────────────────────────
const flowId = ref<number | null>(route.query.flow ? Number(route.query.flow) : null)
const projectId = ref<number | null>(route.query.project ? Number(route.query.project) : null)
const flowName = ref(t('flowEditor.unnamedFlow'))
// motore del flusso: scelto in creazione (?engine=… dalla pagina Flows) per un
// flusso nuovo, oppure caricato da flow.engine. Se si apre l'editor senza scelta
// esplicita si usa il motore PREFERITO dell'utente. Passato a preview/run e salvato.
const { preferredEngine } = usePreferredEngine()
const flowEngine = ref<string>(route.query.engine ? String(route.query.engine) : preferredEngine.value)

// preview/transform iniettano SEMPRE l'engine del flusso corrente. (`dataApi`
// alias: evita che i wrapper si auto-referenzino nei rimpiazzi delle chiamate.)
const dataApi = api
const apiPreview = (body: Parameters<typeof api.preview>[0]) =>
  dataApi.preview({ ...body, engine: flowEngine.value })
const apiTransform = (body: Parameters<typeof api.transform>[0]) =>
  dataApi.transform({ ...body, engine: flowEngine.value })
const projectsList = ref<{ id: number; name: string }[]>([])

// serializza SOLO ciò che serve a ricostruire il canvas (niente stato interno Vue Flow)
function serializeCanvas(): string {
  return JSON.stringify({
    nodes: getNodes.value.map((n) => ({
      id: n.id,
      type: n.type,
      position: { x: n.position.x, y: n.position.y },
      data: n.data,
      // container foreach: figli (parentNode) e dimensioni del riquadro
      ...(n.parentNode ? { parentNode: n.parentNode } : {}),
      ...(n.type === 'foreach'
        ? { style: { width: `${n.dimensions?.width || 460}px`, height: `${n.dimensions?.height || 280}px` } }
        : {}),
      // nota: persisti la dimensione del riquadro (ridimensionabile)
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
  flowName.value = f.name
  projectId.value = f.project_id
  flowEngine.value = f.engine || 'polars'
  const def = JSON.parse(f.definition || '{}')
  // normalizza: sorgenti salvate senza bucket (flussi vecchi/esterni) ricevono
  // quello di default, altrimenti preview/run partirebbero senza bucket (422)
  let nodes = (def.nodes ?? []).map((n: any) => {
    if (n.type === 'source') {
      const data = { ...n.data, bucket: n.data?.bucket ?? bucket }
      // sorgente = datasource del catalogo: la chiave salvata può essere
      // STANTIA (ogni refresh sostituisce lo snapshot e cancella il vecchio
      // blob) → si riaggancia sempre allo snapshot corrente del catalogo
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
    if (n.parentNode) return { ...n, extent: 'parent' } // figli restano nel container
    return n
  })
  // Vue Flow richiede i genitori PRIMA dei figli nell'array
  nodes = [...nodes.filter((n: any) => !n.parentNode), ...nodes.filter((n: any) => n.parentNode)]
  setNodes(nodes)
  // gli archi di SEQUENZA (targetHandle seq-in) ricevono una classe dedicata per
  // distinguerli visivamente dal flusso DATI (ora sono tutti orizzontali)
  setEdges((def.edges ?? []).map((e: any) => ({
    ...e,
    class: (e.targetHandle as string) === 'seq-in' ? 'edge-seq' : undefined,
  })))
  // riallinea i contatori agli id caricati per evitare collisioni sui nuovi nodi
  const maxN = (prefix: string) =>
    Math.max(0, ...(def.nodes ?? [])
      .filter((n: any) => String(n.id).startsWith(prefix))
      .map((n: any) => Number(String(n.id).slice(prefix.length)) || 0))
  opCounter = maxN('op-')
  sourceCounter = maxN('src-')
  outputCounter = Math.max(maxN('out-'), maxN('ctl-')) // out- e ctl- condividono il contatore
  commentCounter = maxN('cmt-')
  selectedId.value = null
  setStatus(t('flowEditor.flowLoaded', { name: f.name }), 'ok')
}

async function saveFlow() {
  if (projectId.value === null) {
    setStatus(t('flowEditor.chooseProjectToSave'), 'error')
    return
  }
  if (!flowName.value.trim()) {
    setStatus(t('flowEditor.nameFlowPrompt'), 'error')
    return
  }
  try {
    if (flowId.value !== null) {
      await flowsApi.update(flowId.value, { name: flowName.value.trim(), definition: serializeCanvas() })
    } else {
      const f = await flowsApi.create(projectId.value, {
        name: flowName.value.trim(),
        definition: serializeCanvas(),
        engine: flowEngine.value,
      })
      flowId.value = f.id
      router.replace({ query: { flow: String(f.id) } }) // l'URL ora punta al flusso salvato
    }
    setStatus(t('flowEditor.flowSaved', { name: flowName.value.trim() }), 'ok')
  } catch (e) {
    setStatus(t('flowEditor.saveFailed', { error: errMessage(e) }), 'error')
  }
}

onMounted(async () => {
  try {
    operations.value = await api.operations()
  } catch (e) {
    setStatus(t('flowEditor.backendUnreachable', { error: errMessage(e) }), 'error')
    return
  }
  try {
    // la lista progetti serve al selettore in toolbar E al dialog di run
    projectsList.value = await projectsApi.list()
  } catch {
    projectsList.value = []
  }
  // il catalogo PRIMA del flusso: loadFlow riaggancia le sorgenti-datasource
  // allo snapshot corrente (le chiavi salvate diventano stantie a ogni refresh)
  await refreshDatasources()
  refreshConnections() // per il nodo Output: può arrivare dopo, non blocca il load
  refreshFlows() // per il nodo "Esegui flusso"
  try {
    if (flowId.value !== null) await loadFlow(flowId.value)
  } catch (e) {
    setStatus(t('flowEditor.flowLoadFailed', { error: errMessage(e) }), 'error')
  }
})

// ── Catalogo datasources (per il picker del nodo sorgente) ────────────────
const datasources = ref<DatasourceInfo[]>([])

// Riaggancia le sorgenti-datasource già sul canvas allo snapshot CORRENTE del
// catalogo: la chiave nel nodo può essere STANTIA (un refresh/overwrite sostituisce
// lo snapshot e cancella il vecchio blob) e la preview la userebbe → 404. Lo fa
// per id, come lo scheduler lato server. Copre anche le sorgenti annidate
// (join/foreach): sono tutte nodi 'source'. Al PRIMO caricamento il canvas è
// vuoto (no-op) e ci pensa loadFlow; poi ogni refresh del catalogo ri-sincronizza.
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
  if (changed) invalidateColumns() // le catene a valle vanno ricalcolate sul nuovo snapshot
}

async function refreshDatasources() {
  try {
    datasources.value = await dsApi.list()
    syncSourceKeys()
  } catch {
    datasources.value = []
  }
}

// ── Connessioni utilizzabili (per il nodo Output verso database) ──────────
const connectionsList = ref<ConnectionInfo[]>([])
async function refreshConnections() {
  try {
    connectionsList.value = await connApi.list()
  } catch {
    connectionsList.value = []
  }
}

// ── Flussi (per il picker del nodo "Esegui flusso") ───────────────────────
const flowsList = ref<{ id: number; name: string }[]>([])
async function refreshFlows() {
  try {
    flowsList.value = (await flowsApi.list()).map((f) => ({ id: f.id, name: f.name }))
  } catch {
    flowsList.value = []
  }
}

// ── Eventi canvas ─────────────────────────────────────────────────────────
onConnect((conn: Connection) => {
  const seqSource = (conn.sourceHandle as string) === 'seq-out'
  const seqTarget = (conn.targetHandle as string) === 'seq-in'
  // gli archi di SEQUENZA collegano solo seq-out (destra) → seq-in (sinistra): un
  // source/ingresso-dati non ha presa di sequenza, quindi niente misto coi dati
  if (seqSource !== seqTarget) {
    setStatus(t('flowEditor.seqConnectError'), 'error')
    return
  }
  const handle = (conn.targetHandle as string) || 'left'
  // dati: un solo arco per (target, handle). Sequenza: più predecessori sono
  // validi (fan-in), si rimuove solo l'eventuale arco identico duplicato.
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
    // solo gli archi DATI cambiano le colonne a valle; la sequenza no
    invalidateColumns()
    refreshForNode(conn.target!)
  } else {
    autolinkRefreshToSource(conn.source!, conn.target!)
  }
})

// Collegando «Refresh datasource → Sorgente», imposta sul Refresh la datasource
// della sorgente (solo se è una datasource DATABASE, le uniche refreshabili), così
// l'arco significa davvero "aggiorna la datasource DI questa sorgente".
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

// ── Azioni ────────────────────────────────────────────────────────────────
function targetSourceId(): string {
  const sel = selectedNode.value
  return sel?.type === 'source' ? sel.id : (firstSource.value?.id ?? SOURCE_ID)
}

async function onUpload(file: File) {
  const sid = targetSourceId()
  busy.value = true
  setStatus(t('flowEditor.uploading', { name: file.name }), 'busy')
  try {
    const res = await api.uploadFile(file)
    updateNodeData(sid, {
      datasetId: res.dataset_id,
      datasourceId: null, // l'upload scollega l'eventuale datasource scelta prima
      parquetKey: res.parquet_key,
      filename: file.name,
      rows: res.dataset?.rows ?? null,
      columns: res.dataset?.columns ?? [],
    })
    selectedId.value = sid

    if (res.status === 'ready') {
      // file piccolo: conversione già fatta in modo sincrono
      nodeColumns[sid] = res.dataset?.columns ?? []
      setStatus(t('flowEditor.ready', { rows: res.dataset?.rows }), 'ok')
      await refreshForNode(sid)
    } else {
      // file grande: conversione async su Celery → aspetta il completamento
      setStatus(t('flowEditor.convertingTask', { id: res.task_id }), 'busy')
      await pollConversion(sid, res.task_id!)
    }
  } catch (e) {
    setStatus(t('flowEditor.uploadFailed', { error: errMessage(e) }), 'error')
  } finally {
    busy.value = false
  }
}

// Attende la conversione async di un file grande, poi popola la sorgente.
async function pollConversion(sid: string, taskId: string) {
  for (let i = 0; i < 1800; i++) {
    // ~2s * 1800 = fino a ~1h (pari al task_time_limit del backend)
    await new Promise((r) => setTimeout(r, 2000))
    let st
    try {
      st = await api.taskStatus(taskId)
    } catch {
      continue
    }
    if (st.status === 'SUCCESS') {
      const info: any = st.result ?? {}
      updateNodeData(sid, { rows: info.rows ?? null, columns: info.columns ?? [] })
      nodeColumns[sid] = info.columns ?? []
      setStatus(t('flowEditor.ready', { rows: info.rows }), 'ok')
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

function addSource() {
  const id = `src-${++sourceCounter}`
  addNodes({
    id,
    type: 'source',
    position: { x: 60, y: 140 + sourceCounter * 150 },
    data: { bucket, datasetId: null, parquetKey: null, filename: null, rows: null, columns: [] },
  })
  selectedId.value = id
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
}

// ── Auto-layout ("Ordina il flusso", bottone nei Controls del canvas) ────
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

// ── Drag & drop dalla sidebar dei componenti ─────────────────────────────
function onCanvasDragOver(ev: DragEvent) {
  ev.preventDefault()
  if (ev.dataTransfer) ev.dataTransfer.dropEffect = 'move'
}

function onCanvasDrop(ev: DragEvent) {
  const kind = ev.dataTransfer?.getData('application/tabularia')
  if (!kind) return
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
    // nodo terminale: dove finisce il risultato (datasource o tabella database)
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

  if (kind === 'refresh' || kind === 'runflow') {
    // nodi di CONTROLLO (non nella catena dati): il gateway li interpreta al run
    const id = `ctl-${++outputCounter}`
    addNodes({ id, type: kind, position, data: {} })
    selectedId.value = id
    return
  }

  if (kind === 'comment') {
    // nota libera: annotazione sul canvas, ignorata dall'esecuzione
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
    // container del ciclo: nodo grande, i figli ci si trascinano dentro
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

    // drop DENTRO un container foreach → diventa figlio (posizione relativa)
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
  // il nodo SQL NON esegue l'anteprima a ogni modifica: una query può essere
  // pesante e riprovarla a ogni tasto/blur è sprecato. L'anteprima è a comando
  // (bottone «Anteprima» o Cmd/Ctrl+Enter → previewSelected).
  if (findNode(selectedId.value)?.data?.opType === 'sql') return
  refreshForNode(selectedId.value)
}

// anteprima a comando del nodo selezionato (usata dal nodo SQL)
function previewSelected() {
  if (selectedId.value) refreshForNode(selectedId.value)
}

function deleteSelected() {
  const id = selectedId.value
  if (!id) return
  // un container foreach porta via anche i figli (il corpo del ciclo)
  const node = findNode(id)
  if (node?.type === 'foreach') {
    const children = getNodes.value.filter((n) => n.parentNode === id).map((n) => n.id)
    if (children.length) removeNodes(children, true)
    children.forEach((c) => delete nodeColumns[c])
  }
  removeNodes(id, true) // rimuove anche gli archi collegati
  delete nodeColumns[id]
  selectedId.value = null
  preview.value = null
  inputColumns.value = []
  rightColumns.value = []
  invalidateColumns()
}

// Canc / Backspace elimina il nodo selezionato (ma non mentre si scrive in un campo)
function onKeydown(e: KeyboardEvent) {
  if (e.key !== 'Delete' && e.key !== 'Backspace') return
  const tag = (document.activeElement?.tagName ?? '').toUpperCase()
  if (['INPUT', 'TEXTAREA', 'SELECT'].includes(tag)) return
  if (!selectedId.value) return
  e.preventDefault()
  deleteSelected()
}
onMounted(() => window.addEventListener('keydown', onKeydown))
onUnmounted(() => window.removeEventListener('keydown', onKeydown))

// i picker (connessioni per l'Output, datasource per source/refresh, flussi per
// runflow) sono caricati una volta al mount: se crei una connessione/datasource/
// flusso in un'altra scheda e torni qui, li riaggiorniamo tornando visibili
function onVisible() {
  if (document.visibilityState !== 'visible') return
  refreshConnections()
  refreshDatasources()
  refreshFlows()
}
onMounted(() => document.addEventListener('visibilitychange', onVisible))
onUnmounted(() => document.removeEventListener('visibilitychange', onVisible))

// ── Colonne / preview ───────────────────────────────────────────────────
function invalidateColumns() {
  for (const k of Object.keys(nodeColumns)) if (findNode(k)?.type !== 'source') delete nodeColumns[k]
}

async function ensureColumns(nodeId: string): Promise<ColumnInfo[]> {
  const node = findNode(nodeId)
  if (!node) return []
  if (node.type === 'source') return (node.data.columns as ColumnInfo[]) ?? []
  if (nodeColumns[nodeId]) return nodeColumns[nodeId]
  const { sourceNode, operations: ops } = resolveChain(getNodes.value, getEdges.value, nodeId)
  if (!sourceNode?.data?.parquetKey) return []
  const res = await apiPreview({
    bucket: sourceNode.data.bucket ?? bucket,
    input_key: sourceNode.data.parquetKey,
    operations: ops,
    limit: 1,
  })
  nodeColumns[nodeId] = res.columns
  return res.columns
}

let columnsSeq = 0 // come previewSeq: solo l'ultima risoluzione spegne lo skeleton
async function refreshForNode(nodeId: string) {
  const inc = buildIncoming(getEdges.value)
  const node = findNode(nodeId)
  columnsLoading.value = true
  const colSeq = ++columnsSeq
  const t0 = performance.now()

  // colonne in ingresso (output del genitore sinistro); il PRIMO nodo di un
  // corpo foreach non ha archi interni → il suo input è quello del container
  const leftId =
    inc.get(nodeId)?.left ?? (node?.parentNode ? inc.get(node.parentNode)?.left : undefined)
  try {
    inputColumns.value = leftId ? await ensureColumns(leftId) : []
  } catch {
    inputColumns.value = []
  }

  // placeholder disponibili per i nodi dentro un container: colonne del driver
  // (input in alto del container) o chiavi della prima iterazione statica
  if (node?.parentNode) {
    const container = findNode(node.parentNode)
    const drvId = inc.get(node.parentNode)?.right
    try {
      if (drvId) {
        placeholders.value = (await ensureColumns(drvId)).map((c) => c.name)
      } else {
        const items = container?.data?.params?.items ?? []
        placeholders.value = Object.keys(items[0] ?? {})
      }
    } catch {
      placeholders.value = []
    }
  } else {
    placeholders.value = []
  }

  // colonne del lato destro = ramo destro di join/union, o driver del foreach
  // (per il foreach sono i placeholder {{colonna}} disponibili nel corpo)
  if (node?.data?.opType === 'join' || node?.data?.opType === 'union' || node?.type === 'foreach') {
    const rightId = inc.get(nodeId)?.right
    try {
      rightColumns.value = rightId ? await ensureColumns(rightId) : []
    } catch {
      rightColumns.value = []
    }
  } else {
    rightColumns.value = []
  }

  // spegne lo skeleton delle colonne dopo il minimo di visibilità, SENZA
  // ritardare l'avvio della preview (che parte subito qui sotto)
  skeletonPad(t0).then(() => {
    if (colSeq === columnsSeq) columnsLoading.value = false
  })
  await runPreview(nodeId)
}

let previewSeq = 0 // solo l'ULTIMA preview lanciata scrive risultato e spegne lo skeleton
async function runPreview(nodeId: string) {
  const { sourceNode, operations: ops } = resolveChain(getNodes.value, getEdges.value, nodeId)
  if (!sourceNode?.data?.parquetKey) {
    preview.value = null
    return
  }
  const seq = ++previewSeq
  const t0 = performance.now()
  previewError.value = ''
  previewLoading.value = true
  try {
    const res = await apiPreview({
      bucket: sourceNode.data.bucket ?? bucket,
      input_key: sourceNode.data.parquetKey,
      operations: ops,
      limit: 100,
    })
    nodeColumns[nodeId] = res.columns
    if (seq === previewSeq) preview.value = res
  } catch (e) {
    if (seq === previewSeq) {
      preview.value = null
      previewError.value = errMessage(e)
    }
  } finally {
    await skeletonPad(t0) // skeleton visibile almeno il minimo: niente flash
    if (seq === previewSeq) previewLoading.value = false
  }
}

// ── Valori distinti di una colonna all'INPUT del nodo selezionato ─────────
// (per il picker del filtro in/not_in: unique sulla catena a monte, cache inclusa)
async function fetchDistinctValues(column: string): Promise<any[]> {
  const nodeId = selectedId.value
  if (!nodeId) return []
  const node = findNode(nodeId)
  const inc = buildIncoming(getEdges.value)
  const leftId =
    inc.get(nodeId)?.left ?? (node?.parentNode ? inc.get(node.parentNode)?.left : undefined)
  if (!leftId) return []
  const { sourceNode, operations: ops } = resolveChain(getNodes.value, getEdges.value, leftId)
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
  })
  return res.rows.map((r) => r[column]).filter((v) => v !== null)
}

// ── Grafico: esegue la catena del nodo + aggregazioni extra sull'engine ──
async function chartQuery(extraOps: Operation[], limit = 1000): Promise<PreviewResult | null> {
  const nodeId = selectedId.value ?? leafNodeId(getNodes.value, getEdges.value)
  const { sourceNode, operations: ops } = resolveChain(getNodes.value, getEdges.value, nodeId)
  if (!sourceNode?.data?.parquetKey) return null
  return await apiPreview({
    bucket: sourceNode.data.bucket ?? bucket,
    input_key: sourceNode.data.parquetKey,
    operations: [...ops, ...extraOps],
    limit,
  })
}

// ── Export (download csv/xlsx del nodo selezionato) ─────────────────────
async function exportSelected(format: 'csv' | 'xlsx') {
  const nodeId = selectedId.value ?? leafNodeId(getNodes.value, getEdges.value)
  const { sourceNode, operations: ops } = resolveChain(getNodes.value, getEdges.value, nodeId)
  if (!sourceNode?.data?.parquetKey) {
    setStatus(t('flowEditor.exportNoData'), 'error')
    return
  }
  const node = findNode(nodeId)
  const label = node?.type === 'source' ? 'sorgente' : (node?.data?.opType ?? 'nodo')
  const filename = `${flowName.value.trim() || 'tabularia'}_${label}.${format}`
    .toLowerCase().replace(/[^a-z0-9._-]+/g, '_')

  busy.value = true
  setStatus(t('flowEditor.exporting', { format: format.toUpperCase() }), 'busy')
  try {
    const blob = await api.exportData({
      bucket: sourceNode.data.bucket ?? bucket,
      input_key: sourceNode.data.parquetKey,
      operations: ops,
      format,
      filename,
    })
    // scarica il blob come file (link temporaneo)
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = filename
    a.click()
    URL.revokeObjectURL(url)
    setStatus(t('flowEditor.downloaded', { filename }), 'ok')
  } catch (e) {
    setStatus(t('flowEditor.exportFailed', { error: errMessage(e) }), 'error')
  } finally {
    busy.value = false
  }
}

// ── Run (async via Celery) ──────────────────────────────────────────────
// Il click su Esegui apre il dialog; il lancio vero passa dal GATEWAY quando
// il flusso è salvato → cronologia dei run. Se il canvas ha nodi Output, ogni
// output diventa un run (la destinazione è configurata sul nodo, alla Tableau
// Prep); senza nodi Output resta il percorso classico (pubblicazione opzionale
// dal dialog).
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

// nodi di controllo (refresh/runflow): richiedono l'orchestrazione server-side
const hasControlNodes = () =>
  getNodes.value.some((n) => n.type === 'refresh' || n.type === 'runflow')

function run() {
  // flussi con nodi di controllo → orchestrazione server (refresh→output→runflow),
  // niente dialog: la configurazione sta sui nodi
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
    // gli output richiedono il gateway (cronologia, RBAC, credenziali)
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
    const { sourceNode } = resolveChain(getNodes.value, getEdges.value, leaf)
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
  const { sourceNode, operations: ops } = resolveChain(getNodes.value, getEdges.value, leaf)
  if (!sourceNode?.data?.parquetKey) {
    runDialogOpen.value = false
    return
  }
  runDialogError.value = ''
  busy.value = true // solo per la durata del LANCIO: il canvas resta usabile durante il run
  pollToken++ // nuovo ciclo: i poll dei run precedenti si fermano
  try {
    if (flowId.value !== null) {
      // percorso con cronologia: il gateway registra il run e pubblica l'output
      const launched = await runsApi.launch(flowId.value, {
        bucket: sourceNode.data.bucket ?? bucket,
        input_key: sourceNode.data.parquetKey,
        operations: ops,
        publish,
      })
      runDialogOpen.value = false // chiuso SOLO a lancio riuscito
      setStatus(t('flowEditor.runStarted', { id: launched.id }), 'busy')
      pollRun(launched.id) // in background: niente lock sull'editor
    } else {
      // flusso non salvato: run diretto, senza cronologia né pubblicazione.
      // La chiave di output la assegna il SERVER (out/<uuid> write-once): qui
      // non la scegliamo più, si polla solo il task_id.
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
    // 409 nome duplicato / 403 permessi: il dialog resta aperto con l'input intatto
    runDialogError.value = errMessage(e)
  } finally {
    busy.value = false
  }
}

// orchestrazione server-side (flussi con nodi di controllo): il gateway fa
// refresh → output → runflow. Torna subito, si polla la cronologia del flusso.
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

// polla il RUN DI ORCHESTRAZIONE per id (lo crea run-now): traccia l'intera
// esecuzione anche per flussi senza nodo Output, che non producono run propri.
async function pollOrchestration(runId: number) {
  const token = pollToken
  for (let i = 0; i < 150; i++) {
    await new Promise((r) => setTimeout(r, 2000))
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
      setStatus(t('flowEditor.orchestrationError', { error: run.error ?? t('flowEditor.unknownError') }), 'error')
      return
    }
  }
  setStatus(t('flowEditor.orchestrationTimeout'), 'info')
}

// un run per ogni nodo Output: la catena di ciascuno è il suo input sinistro
async function executeOutputRuns() {
  runDialogError.value = ''
  busy.value = true
  pollToken++ // nuovo ciclo: i poll dei run precedenti si fermano
  try {
    for (const node of outputNodes()) {
      const label = describeOutput(node).label
      const { sourceNode, operations: ops } = resolveChain(getNodes.value, getEdges.value, node.id)
      if (!sourceNode?.data?.parquetKey) {
        runDialogError.value = `${label}: ${t('flowEditor.outputMissingChain')}`
        selectedId.value = node.id
        return // dialog aperto: gli output già lanciati proseguono comunque
      }
      const d = node.data
      const destType = d.destType ?? 'datasource'
      let publish = null
      let destination = null
      if (destType === 'database') {
        destination = {
          type: 'database' as const,
          connection_id: d.connectionId,
          table: d.table.trim(),
          mode: d.mode ?? 'append',
          post_sql: d.postSql ?? '',
        }
      } else if (destType === 's3') {
        destination = {
          type: 's3' as const,
          connection_id: d.connectionId,
          bucket: d.s3Bucket?.trim() ?? '',
          key: d.s3Key.trim(),
          format: d.s3Format ?? 'parquet',
          partition_by: d.partitionBy ?? [],
        }
      } else {
        publish = {
          name: d.name.trim(),
          project_id: d.projectId,
          description: d.description ?? '',
          overwrite: !!d.overwrite,
        }
      }
      try {
        const launched = await runsApi.launch(flowId.value!, {
          bucket: sourceNode.data.bucket ?? bucket,
          input_key: sourceNode.data.parquetKey,
          operations: ops,
          publish,
          destination,
        })
        setStatus(`${label}: ${t('flowEditor.outputRunStarted', { id: launched.id })}`, 'busy')
        pollRun(launched.id, label)
      } catch (e) {
        // 409 nome duplicato / 403 permessi: il dialog resta aperto sull'errore
        runDialogError.value = `${label}: ${errMessage(e)}`
        selectedId.value = node.id
        return
      }
    }
    runDialogOpen.value = false // tutti lanciati
  } finally {
    busy.value = false
  }
}

// token di cancellazione: un nuovo CICLO di run (o l'unmount) invalida i poll
// precedenti; i poll dello stesso ciclo (un run per nodo Output) convivono
let pollToken = 0
onUnmounted(() => {
  pollToken++
})

async function pollRun(runId: number, label = '') {
  const token = pollToken // cattura il ciclo corrente (bumpato da executeRun*)
  const prefix = label ? `${label}: ` : ''
  // ~1.5s * 2400 ≈ 1h, allineato al task_time_limit dell'engine
  for (let i = 0; i < 2400; i++) {
    await new Promise((r) => setTimeout(r, 1500))
    if (token !== pollToken) return // pagina chiusa o nuovo ciclo di run: stop
    let run
    try {
      run = await runsApi.get(runId) // il GET riconcilia lo stato lato gateway
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
            ? ` → s3://${d.bucket}/${d.key}`
            : ` → ${d.db_type} ${d.database ? d.database + '.' : ''}${d.table}`
        } catch { /* riassunto illeggibile: resta il messaggio base */ }
      }
      setStatus(`${prefix}${t('flowEditor.completedRows', { rows: run.rows_written })}${target}`, 'ok')
      if (run.datasource_id) refreshDatasources() // subito usabile nel picker
      return
    }
    if (run.status === 'FAILURE') {
      setStatus(`${prefix}${t('flowEditor.errorInline', { error: run.error })}`, 'error')
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
  <div class="app" :style="appStyle">
    <Toolbar
      class="toolbar"
      :status="status"
      :status-kind="statusKind"
      :busy="busy"
      :can-run="canRun"
      :flow-name="flowName"
      :projects="projectsList"
      :project-id="projectId"
      :engine="flowEngine"
      @upload="onUpload"
      @add-op="addOperation"
      @add-source="addSource"
      @run="run"
      @save="saveFlow"
      @update:flow-name="flowName = $event"
      @update:project-id="projectId = $event"
    />

    <div class="sidebar">
      <OpSidebar :operations="operations" />
    </div>

    <div class="canvas" @drop="onCanvasDrop" @dragover="onCanvasDragOver">
      <VueFlow
        :nodes="initialNodes"
        fit-view-on-init
        :delete-key-code="null"
        :default-edge-options="{ animated: true }"
      >
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
        <template #node-comment="props">
          <CommentNode :id="props.id" :data="props.data" />
        </template>
        <Background pattern-color="#2a2f3a" :gap="16" />
        <Controls>
          <ControlButton :title="$t('flowEditor.orderFlowTitle')" @click="autoLayout">
            <Wand2 :size="13" />
          </ControlButton>
        </Controls>
      </VueFlow>
    </div>

    <div class="panel">
      <div class="panel-resizer" :title="$t('flowEditor.resizeHint')" @mousedown="startPanelResize" />
      <NodePanel
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
        @update="patchSelected"
        @delete="deleteSelected"
        @export="exportSelected"
        @preview="previewSelected"
      />
    </div>

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
      <div class="viewtabs">
        <button :class="{ active: viewTab === 'table' }" @click="viewTab = 'table'">
          <Table2 :size="13" /> {{ $t('flowEditor.tableTab') }}
        </button>
        <button :class="{ active: viewTab === 'chart' }" @click="viewTab = 'chart'">
          <BarChart3 :size="13" /> {{ $t('flowEditor.chartTab') }}
        </button>
      </div>
      <div class="viewbody" :class="{ scroll: viewTab === 'table' }">
        <DataGrid v-if="viewTab === 'table'" :result="preview" :loading="previewLoading" :error="previewError" />
        <ChartPanel v-else :columns="preview?.columns ?? []" :query="chartQuery" />
      </div>
    </div>
  </div>
</template>
