<script setup lang="ts">
import { ref, reactive, computed, onMounted, onUnmounted } from 'vue'
import { VueFlow, useVueFlow } from '@vue-flow/core'
import type { Node, Connection } from '@vue-flow/core'
import { Background } from '@vue-flow/background'
import { Controls } from '@vue-flow/controls'

import { Table2, BarChart3 } from 'lucide-vue-next'
import { useApi, errMessage } from '~/composables/useApi'
import type { PreviewResult, ColumnInfo, Operation } from '~/composables/useApi'
import { SOURCE_ID, buildIncoming, resolveChain, leafNodeId, defaultParams } from '~/composables/useFlowModel'
import { useFlows } from '~/composables/useFlows'
import { useProjects } from '~/composables/useProjects'
import { useRuns, type PublishSpec } from '~/composables/useRuns'
import { useDatasources, type DatasourceInfo } from '~/composables/useDatasources'

const api = useApi()
const flowsApi = useFlows()
const projectsApi = useProjects()
const runsApi = useRuns()
const dsApi = useDatasources()
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
// placeholder {{colonna}} disponibili per un nodo DENTRO un container foreach
const placeholders = ref<string[]>([])
const nodeColumns = reactive<Record<string, ColumnInfo[]>>({}) // cache output per nodo

let opCounter = 0
let sourceCounter = 0

const viewTab = ref<'table' | 'chart'>('table') // vista sotto il canvas

// ── Pannello destro ridimensionabile (larghezza ricordata) ────────────────
const PANEL_MIN = 280
const PANEL_MAX = 640
const panelWidth = ref(
  Math.min(PANEL_MAX, Math.max(PANEL_MIN, Number(localStorage.getItem('tabularia.panelWidth')) || 340)),
)
const appStyle = computed(() => ({ gridTemplateColumns: `200px 1fr ${panelWidth.value}px` }))

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
const status = ref('Carica un file per iniziare.')
// tipo di stato → icona nella toolbar (spinner / check / errore)
const statusKind = ref<'info' | 'ok' | 'error' | 'busy'>('info')
function setStatus(msg: string, kind: 'info' | 'ok' | 'error' | 'busy' = 'info') {
  status.value = msg
  statusKind.value = kind
}
const busy = ref(false)
const preview = ref<PreviewResult | null>(null)
const previewLoading = ref(false)
const previewError = ref('')

const selectedNode = computed(() => (selectedId.value ? findNode(selectedId.value) ?? null : null))
const firstSource = computed(() => getNodes.value.find((n) => n.type === 'source'))
const canRun = computed(() => !!firstSource.value?.data?.parquetKey)

// ── Flusso salvato (persistenza nel gateway) ─────────────────────────────
const flowId = ref<number | null>(route.query.flow ? Number(route.query.flow) : null)
const projectId = ref<number | null>(route.query.project ? Number(route.query.project) : null)
const flowName = ref('Flusso senza nome')
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
  const def = JSON.parse(f.definition || '{}')
  // normalizza: sorgenti salvate senza bucket (flussi vecchi/esterni) ricevono
  // quello di default, altrimenti preview/run partirebbero senza bucket (422)
  let nodes = (def.nodes ?? []).map((n: any) => {
    if (n.type === 'source') return { ...n, data: { ...n.data, bucket: n.data?.bucket ?? bucket } }
    if (n.parentNode) return { ...n, extent: 'parent' } // figli restano nel container
    return n
  })
  // Vue Flow richiede i genitori PRIMA dei figli nell'array
  nodes = [...nodes.filter((n: any) => !n.parentNode), ...nodes.filter((n: any) => n.parentNode)]
  setNodes(nodes)
  setEdges(def.edges ?? [])
  // riallinea i contatori agli id caricati per evitare collisioni sui nuovi nodi
  const maxN = (prefix: string) =>
    Math.max(0, ...(def.nodes ?? [])
      .filter((n: any) => String(n.id).startsWith(prefix))
      .map((n: any) => Number(String(n.id).slice(prefix.length)) || 0))
  opCounter = maxN('op-')
  sourceCounter = maxN('src-')
  selectedId.value = null
  setStatus(`Flusso "${f.name}" caricato`, 'ok')
}

async function saveFlow() {
  if (projectId.value === null) {
    setStatus('Scegli la cartella di destinazione per salvare', 'error')
    return
  }
  if (!flowName.value.trim()) {
    setStatus('Dai un nome al flusso', 'error')
    return
  }
  try {
    if (flowId.value !== null) {
      await flowsApi.update(flowId.value, { name: flowName.value.trim(), definition: serializeCanvas() })
    } else {
      const f = await flowsApi.create(projectId.value, {
        name: flowName.value.trim(),
        definition: serializeCanvas(),
      })
      flowId.value = f.id
      router.replace({ query: { flow: String(f.id) } }) // l'URL ora punta al flusso salvato
    }
    setStatus(`Flusso "${flowName.value.trim()}" salvato`, 'ok')
  } catch (e) {
    setStatus(`Salvataggio fallito: ${errMessage(e)}`, 'error')
  }
}

onMounted(async () => {
  try {
    operations.value = await api.operations()
  } catch (e) {
    setStatus(`Backend non raggiungibile: ${errMessage(e)}`, 'error')
    return
  }
  try {
    // la lista progetti serve al selettore in toolbar E al dialog di run
    projectsList.value = await projectsApi.list()
  } catch {
    projectsList.value = []
  }
  try {
    if (flowId.value !== null) await loadFlow(flowId.value)
  } catch (e) {
    setStatus(`Caricamento flusso fallito: ${errMessage(e)}`, 'error')
  }
  refreshDatasources()
})

// ── Catalogo datasources (per il picker del nodo sorgente) ────────────────
const datasources = ref<DatasourceInfo[]>([])
async function refreshDatasources() {
  try {
    datasources.value = await dsApi.list()
  } catch {
    datasources.value = []
  }
}

// ── Eventi canvas ─────────────────────────────────────────────────────────
onConnect((conn: Connection) => {
  const handle = (conn.targetHandle as string) || 'left'
  // un solo arco per (target, handle)
  const dup = getEdges.value.filter(
    (e) => e.target === conn.target && ((e.targetHandle as string) || 'left') === handle,
  )
  if (dup.length) removeEdges(dup.map((e) => e.id))
  addEdges({
    id: `e-${conn.source}-${handle}-${conn.target}`,
    source: conn.source!,
    target: conn.target!,
    sourceHandle: conn.sourceHandle ?? undefined,
    targetHandle: conn.targetHandle ?? undefined,
  })
  invalidateColumns()
  refreshForNode(conn.target!)
})

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
  setStatus(`Caricamento ${file.name}…`, 'busy')
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
      setStatus(`Pronto: ${res.dataset?.rows} righe`, 'ok')
      await refreshForNode(sid)
    } else {
      // file grande: conversione async su Celery → aspetta il completamento
      setStatus(`Conversione in corso (task ${res.task_id})…`, 'busy')
      await pollConversion(sid, res.task_id!)
    }
  } catch (e) {
    setStatus(`Upload fallito: ${errMessage(e)}`, 'error')
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
      setStatus(`Pronto: ${info.rows} righe`, 'ok')
      await refreshForNode(sid)
      return
    }
    if (st.status === 'FAILURE') {
      setStatus(`Conversione fallita: ${st.error}`, 'error')
      return
    }
    setStatus(`Conversione in corso… (${st.status})`, 'busy')
  }
  setStatus('Timeout conversione.', 'error')
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
  refreshForNode(selectedId.value)
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
  const res = await api.preview({
    bucket: sourceNode.data.bucket ?? bucket,
    input_key: sourceNode.data.parquetKey,
    operations: ops,
    limit: 1,
  })
  nodeColumns[nodeId] = res.columns
  return res.columns
}

async function refreshForNode(nodeId: string) {
  const inc = buildIncoming(getEdges.value)
  const node = findNode(nodeId)

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

  // colonne del lato destro = ramo destro del join, o driver del foreach
  // (per il foreach sono i placeholder {{colonna}} disponibili nel corpo)
  if (node?.data?.opType === 'join' || node?.type === 'foreach') {
    const rightId = inc.get(nodeId)?.right
    try {
      rightColumns.value = rightId ? await ensureColumns(rightId) : []
    } catch {
      rightColumns.value = []
    }
  } else {
    rightColumns.value = []
  }

  await runPreview(nodeId)
}

async function runPreview(nodeId: string) {
  const { sourceNode, operations: ops } = resolveChain(getNodes.value, getEdges.value, nodeId)
  if (!sourceNode?.data?.parquetKey) {
    preview.value = null
    return
  }
  previewError.value = ''
  previewLoading.value = true
  try {
    const res = await api.preview({
      bucket: sourceNode.data.bucket ?? bucket,
      input_key: sourceNode.data.parquetKey,
      operations: ops,
      limit: 100,
    })
    preview.value = res
    nodeColumns[nodeId] = res.columns
  } catch (e) {
    preview.value = null
    previewError.value = errMessage(e)
  } finally {
    previewLoading.value = false
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
  const res = await api.preview({
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
  return await api.preview({
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
    setStatus('Il nodo non ha dati a monte da esportare', 'error')
    return
  }
  const node = findNode(nodeId)
  const label = node?.type === 'source' ? 'sorgente' : (node?.data?.opType ?? 'nodo')
  const filename = `${flowName.value.trim() || 'tabularia'}_${label}.${format}`
    .toLowerCase().replace(/[^a-z0-9._-]+/g, '_')

  busy.value = true
  setStatus(`Esportazione ${format.toUpperCase()}…`, 'busy')
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
    setStatus(`Scaricato ${filename}`, 'ok')
  } catch (e) {
    setStatus(`Export fallito: ${errMessage(e)}`, 'error')
  } finally {
    busy.value = false
  }
}

// ── Run (async via Celery) ──────────────────────────────────────────────
// Il click su Esegui apre il dialog (con pubblicazione opzionale); il lancio
// vero passa dal GATEWAY quando il flusso è salvato → cronologia dei run.
const runDialogOpen = ref(false)
const runDialogError = ref('')

function run() {
  const leaf = leafNodeId(getNodes.value, getEdges.value)
  const { sourceNode } = resolveChain(getNodes.value, getEdges.value, leaf)
  if (!sourceNode?.data?.parquetKey) return
  runDialogError.value = ''
  runDialogOpen.value = true
}

async function executeRun(publish: PublishSpec | null) {
  const leaf = leafNodeId(getNodes.value, getEdges.value)
  const { sourceNode, operations: ops } = resolveChain(getNodes.value, getEdges.value, leaf)
  if (!sourceNode?.data?.parquetKey) {
    runDialogOpen.value = false
    return
  }
  runDialogError.value = ''
  busy.value = true // solo per la durata del LANCIO: il canvas resta usabile durante il run
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
      setStatus(`Run #${launched.id} avviato…`, 'busy')
      pollRun(launched.id) // in background: niente lock sull'editor
    } else {
      // flusso non salvato: run diretto, senza cronologia né pubblicazione
      const outputKey = `out/${sourceNode.data.datasetId ?? 'run'}_${Date.now()}.parquet`
      const res = await api.transform({
        bucket: sourceNode.data.bucket ?? bucket,
        input_key: sourceNode.data.parquetKey,
        output_key: outputKey,
        operations: ops,
      })
      runDialogOpen.value = false
      setStatus(`Task ${res.task_id} avviato…`, 'busy')
      pollTask(res.task_id, outputKey)
    }
  } catch (e) {
    // 409 nome duplicato / 403 permessi: il dialog resta aperto con l'input intatto
    runDialogError.value = errMessage(e)
  } finally {
    busy.value = false
  }
}

// token di cancellazione: un nuovo poll (o l'unmount) invalida i precedenti
let pollToken = 0
onUnmounted(() => {
  pollToken++
})

async function pollRun(runId: number) {
  const token = ++pollToken
  // ~1.5s * 2400 ≈ 1h, allineato al task_time_limit dell'engine
  for (let i = 0; i < 2400; i++) {
    await new Promise((r) => setTimeout(r, 1500))
    if (token !== pollToken) return // pagina chiusa o nuovo run: stop
    let run
    try {
      run = await runsApi.get(runId) // il GET riconcilia lo stato lato gateway
    } catch {
      continue
    }
    if (token !== pollToken) return
    if (run.status === 'SUCCESS') {
      const published = run.publish_name ? ` → datasource “${run.publish_name}”` : ''
      setStatus(`Completato: ${run.rows_written} righe${published}`, 'ok')
      if (run.datasource_id) refreshDatasources() // subito usabile nel picker
      return
    }
    if (run.status === 'FAILURE') {
      setStatus(`Errore: ${run.error}`, 'error')
      return
    }
  }
  setStatus('Timeout in attesa del run.', 'error')
}

async function pollTask(id: string, outputKey: string) {
  const token = ++pollToken
  for (let i = 0; i < 120; i++) {
    await new Promise((r) => setTimeout(r, 1000))
    if (token !== pollToken) return
    const st = await api.taskStatus(id)
    if (token !== pollToken) return
    if (st.status === 'SUCCESS') {
      setStatus(`Completato: ${st.result?.rows_written} righe → ${outputKey}`, 'ok')
      return
    }
    if (st.status === 'FAILURE') {
      setStatus(`Errore: ${st.error}`, 'error')
      return
    }
  }
  setStatus('Timeout in attesa del task.', 'error')
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
        <Background pattern-color="#2a2f3a" :gap="16" />
        <Controls />
      </VueFlow>
    </div>

    <div class="panel">
      <div class="panel-resizer" title="Trascina per ridimensionare" @mousedown="startPanelResize" />
      <NodePanel
        :node="selectedNode"
        :operations="operations"
        :input-columns="inputColumns"
        :right-columns="rightColumns"
        :placeholders="placeholders"
        :fetch-distinct="fetchDistinctValues"
        :datasources="datasources"
        @update="patchSelected"
        @delete="deleteSelected"
        @export="exportSelected"
      />
    </div>

    <RunDialog
      :open="runDialogOpen"
      :can-publish="flowId !== null"
      :projects="projectsList"
      :default-project-id="projectId"
      :error="runDialogError"
      :busy="busy"
      @confirm="executeRun"
      @cancel="runDialogOpen = false"
    />

    <div class="grid">
      <div class="viewtabs">
        <button :class="{ active: viewTab === 'table' }" @click="viewTab = 'table'">
          <Table2 :size="13" /> Tabella
        </button>
        <button :class="{ active: viewTab === 'chart' }" @click="viewTab = 'chart'">
          <BarChart3 :size="13" /> Grafico
        </button>
      </div>
      <div class="viewbody" :class="{ scroll: viewTab === 'table' }">
        <DataGrid v-if="viewTab === 'table'" :result="preview" :loading="previewLoading" :error="previewError" />
        <ChartPanel v-else :columns="preview?.columns ?? []" :query="chartQuery" />
      </div>
    </div>
  </div>
</template>
