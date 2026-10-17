<script setup lang="ts">
// Tab Lineage: grafo di provenienza/impatto tra flussi, datasource, connessioni
// e destinazioni esterne. Si sceglie un oggetto su cui centrare (o si vede tutto);
// nothing loads until the user searches an object and applies it, or asks for the
// whole graph: with many objects the whole graph is the heavy case, not the start.
// il grafo mostra a monte (da dove vengono i dati) e a valle (chi si rompe se lo
// tocco). Il grafo è renderizzato con Vue Flow (client-only), layout a livelli.
import { ref, computed, watch, nextTick } from 'vue'
import { VueFlow, useVueFlow, Handle, Position } from '@vue-flow/core'
import type { Node, Edge } from '@vue-flow/core'
import { Background } from '@vue-flow/background'
import { Controls } from '@vue-flow/controls'
import {
  Share2, Workflow, Database, Plug, CloudUpload, HardDriveDownload, Search,
  AlertTriangle, Clock, ExternalLink, X, Crosshair, Server,
} from 'lucide-vue-next'
import { useApi, errMessage, type LineageGraph, type LineageNode, type LineageEdge } from '~/composables/useApi'
import { useSearch, type SearchHit } from '~/composables/useSearch'
import { useFolderPaths } from '~/composables/useFolderPaths'
import { useI18n } from 'vue-i18n'
import { useFormat } from '~/composables/useFormat'

const api = useApi()
const searchApi = useSearch()
const folderPaths = useFolderPaths()
const { fitView } = useVueFlow()
const { t } = useI18n()
const fmtx = useFormat()

const graph = ref<LineageGraph>({ nodes: [], edges: [] })
const loading = ref(false)
const error = ref('')

// selezione: oggetto centrale + parametri di traversata
type CenterType = 'flow' | 'datasource' | 'connection'
const centerType = ref<CenterType | null>(null)
const centerId = ref<number | null>(null)
const direction = ref<'both' | 'upstream' | 'downstream'>('both')
const depth = ref(3)
const q = ref('') // ricerca nel picker
const selected = ref<LineageNode | null>(null) // nodo aperto nel pannello dettaglio

// filtri per tipo di nodo (tutti attivi di default)
const ALL_TYPES = ['flow', 'datasource', 'connection', 'db_sink', 's3_sink', 'sftp_sink'] as const
const enabledTypes = ref<Set<string>>(new Set(ALL_TYPES))
function toggleType(t: string) {
  const s = new Set(enabledTypes.value)
  s.has(t) ? s.delete(t) : s.add(t)
  enabledTypes.value = s
}

// ── etichette/colori per tipo di nodo e tipo di arco ─────────────────────────
const NODE_META = computed<Record<string, { label: string; icon: any; color: string }>>(() => ({
  flow: { label: t('lineage.nodeFlow'), icon: Workflow, color: '#4f8cff' },
  datasource: { label: t('lineage.nodeDatasource'), icon: Database, color: '#6ee7b7' },
  connection: { label: t('lineage.nodeConnection'), icon: Plug, color: '#a78bfa' },
  db_sink: { label: t('lineage.nodeDbSink'), icon: HardDriveDownload, color: '#fbbf24' },
  s3_sink: { label: t('lineage.nodeS3Sink'), icon: CloudUpload, color: '#38bdf8' },
  sftp_sink: { label: t('sftp.lineageNode'), icon: Server, color: '#f472b6' },
}))
const EDGE_META = computed<Record<string, { label: string; color: string; dashed?: boolean }>>(() => ({
  read: { label: t('lineage.edgeRead'), color: '#4f8cff' },
  publish: { label: t('lineage.edgePublish'), color: '#6ee7b7' },
  ingest: { label: t('lineage.edgeIngest'), color: '#a78bfa' },
  write: { label: t('lineage.edgeWrite'), color: '#fbbf24' },
  refresh: { label: t('lineage.edgeRefresh'), color: '#8b93a7', dashed: true },
  orchestrate: { label: t('lineage.edgeOrchestrate'), color: '#c084fc', dashed: true },
}))

// ── picker: search the catalog, choose, then Apply ───────────────────────────
const CENTER_OF_KIND: Record<string, CenterType> = { flow: 'flow', datasource: 'datasource', connection: 'connection' }
const NODE_PREFIX: Record<CenterType, string> = { flow: 'flow', datasource: 'ds', connection: 'conn' }
const hits = ref<SearchHit[]>([])
const searching = ref(false)
const chosen = ref<{ type: CenterType; id: number } | null>(null)
const showingAll = ref(false)
let searchTimer: ReturnType<typeof setTimeout> | null = null
let searchSeq = 0

watch(q, (text) => {
  if (searchTimer) clearTimeout(searchTimer)
  if (!text.trim()) {
    searchSeq++
    hits.value = []
    searching.value = false
    return
  }
  searchTimer = setTimeout(runSearch, 250)
})
async function runSearch() {
  const seq = ++searchSeq
  searching.value = true
  try {
    const res = await searchApi.query(q.value.trim(), { limit: 100 })
    if (seq === searchSeq) hits.value = res.items.filter((h) => h.kind in CENTER_OF_KIND)
  } catch {
    if (seq === searchSeq) hits.value = []
  } finally {
    if (seq === searchSeq) searching.value = false
  }
}
function choose(h: SearchHit) {
  chosen.value = { type: CENTER_OF_KIND[h.kind]!, id: h.id }
}
const isChosen = (h: SearchHit) => chosen.value?.type === CENTER_OF_KIND[h.kind] && chosen.value?.id === h.id
function apply() {
  if (!chosen.value) return
  showingAll.value = false
  selected.value = null
  centerType.value = chosen.value.type
  centerId.value = chosen.value.id
}
const hitType = (h: SearchHit) =>
  h.kind === 'flow' ? t('lineage.typeFlow') : h.kind === 'datasource' ? t('lineage.typeDatasource') : t('lineage.typeConnection')

const centerNodeId = computed(() =>
  centerType.value && centerId.value != null ? `${NODE_PREFIX[centerType.value]}:${centerId.value}` : null,
)

// ── liste "a monte / a valle" relative al centro (dal grafo mostrato) ─────────
function neighbors(dir: 'up' | 'down'): LineageNode[] {
  const c = centerNodeId.value
  if (!c) return []
  const ids = new Set<string>()
  for (const e of graph.value.edges) {
    if (dir === 'up' && e.target === c) ids.add(e.source)
    if (dir === 'down' && e.source === c) ids.add(e.target)
  }
  return graph.value.nodes.filter((n) => ids.has(n.id))
}
const upstream = computed(() => neighbors('up'))
const downstream = computed(() => neighbors('down'))

// ── layout a livelli (longest-path da radici) ────────────────────────────────
// distanza tra i livelli (orizzontale) e tra i nodi di uno stesso livello
// (verticale). COL_W poco più della larghezza nodo (180px) → colonne ravvicinate.
const COL_W = 210
const ROW_H = 80
function layout(nodes: LineageNode[], edges: LineageEdge[]): Map<string, { x: number; y: number }> {
  const layer = new Map<string, number>()
  nodes.forEach((n) => layer.set(n.id, 0))
  const outs = new Map<string, string[]>()
  edges.forEach((e) => outs.set(e.source, [...(outs.get(e.source) ?? []), e.target]))
  // rilassamento longest-path, iterazioni limitate (robusto ai cicli orchestrate)
  for (let i = 0; i < nodes.length; i++) {
    let changed = false
    for (const e of edges) {
      const s = layer.get(e.source) ?? 0
      if ((layer.get(e.target) ?? 0) < s + 1) {
        layer.set(e.target, s + 1)
        changed = true
      }
    }
    if (!changed) break
  }
  // raggruppa per livello e impila verticalmente
  const byLayer = new Map<number, string[]>()
  nodes.forEach((n) => {
    const l = layer.get(n.id) ?? 0
    byLayer.set(l, [...(byLayer.get(l) ?? []), n.id])
  })
  const pos = new Map<string, { x: number; y: number }>()
  for (const [l, ids] of byLayer) {
    ids.forEach((id, i) => {
      pos.set(id, { x: l * COL_W, y: i * ROW_H - ((ids.length - 1) * ROW_H) / 2 })
    })
  }
  return pos
}

// ── nodi/archi effettivamente mostrati (dopo i filtri per tipo) ──────────────
const shownNodes = computed(() => graph.value.nodes.filter((n) => enabledTypes.value.has(n.type)))
const shownEdges = computed(() => {
  const ids = new Set(shownNodes.value.map((n) => n.id))
  return graph.value.edges.filter((e) => ids.has(e.source) && ids.has(e.target))
})

// problemi nel grafo mostrato: staleness + riferimenti non risolti
const problems = computed(() => {
  const stale = shownNodes.value.filter((n) => n.meta?.stale).length
  const restricted = shownNodes.value.filter((n) => n.restricted).length
  const neverRun = shownNodes.value.filter((n) => n.meta?.never_run).length
  return { stale, restricted, neverRun, total: stale + restricted }
})

// ── traduzione grafo → nodi/archi Vue Flow ───────────────────────────────────
const rfNodes = computed<Node[]>(() => {
  const pos = layout(shownNodes.value, shownEdges.value)
  return shownNodes.value.map((n) => ({
    id: n.id,
    type: 'default',
    position: pos.get(n.id) ?? { x: 0, y: 0 },
    data: { node: n },
    class: [
      'ln-node',
      `ln-${n.type}`,
      n.restricted ? 'ln-restricted' : '',
      n.id === centerNodeId.value ? 'ln-center' : '',
    ].join(' '),
    sourcePosition: 'right' as any,
    targetPosition: 'left' as any,
  }))
})
const rfEdges = computed<Edge[]>(() =>
  shownEdges.value.map((e, i) => {
    const m = EDGE_META.value[e.kind]
    return {
      id: `e${i}`,
      source: e.source,
      target: e.target,
      label: m?.label ?? e.kind,
      animated: e.kind === 'read' || e.kind === 'publish',
      style: { stroke: m?.color ?? '#8b93a7', strokeWidth: 1.8, strokeDasharray: m?.dashed ? '5 5' : undefined },
      // l'etichetta è TESTO: il colore dell'arco mescolato col testo del tema quanto basta per l'AA
      labelStyle: { fill: `color-mix(in srgb, ${m?.color ?? '#8b93a7'} var(--enc-text-mix), var(--text))`, fontSize: 10, fontWeight: 600 },
      labelBgStyle: { fill: 'var(--panel)', fillOpacity: 0.85 },
    } as Edge
  }),
)

// ── caricamento ──────────────────────────────────────────────────────────────
async function load() {
  if (!centerNodeId.value && !showingAll.value) {
    graph.value = { nodes: [], edges: [] }  // nothing asked for yet
    return
  }
  loading.value = true
  error.value = ''
  try {
    if (centerType.value && centerId.value != null) {
      graph.value = await api.lineage({
        type: centerType.value, id: centerId.value, direction: direction.value, depth: depth.value,
      })
    } else {
      graph.value = await api.lineage()
    }
    await nextTick()
    setTimeout(() => fitView({ padding: 0.2 }), 50)
  } catch (e) {
    error.value = errMessage(e)
    graph.value = { nodes: [], edges: [] }
  } finally {
    loading.value = false
  }
}

function pick(n: LineageNode) {
  const prefix = n.id.split(':')[0]
  const type = (Object.keys(NODE_PREFIX) as CenterType[]).find((k) => NODE_PREFIX[k] === prefix)
  if (!type) return
  showingAll.value = false
  centerType.value = type
  centerId.value = Number(n.id.split(':')[1])
}
function showAll() {
  centerType.value = null
  centerId.value = null
  selected.value = null
  showingAll.value = true
  load()
}

// click su un nodo → apre il pannello di dettaglio (il ricentro è un'azione lì)
function onNodeClick(ev: any) {
  const n: LineageNode | undefined = ev?.node?.data?.node
  if (n) selected.value = n
}

// azioni del pannello: ricentra sul nodo, oppure aprilo nel suo strumento
function recenterOn(n: LineageNode) {
  pick(n)
}
function openNode(n: LineageNode) {
  const id = Number(n.id.split(':')[1])
  if (n.type === 'flow') navigateTo(`/editor?flow=${id}`)
  else if (n.type === 'datasource') navigateTo('/datasources')
}
const canRecenter = (n: LineageNode | null) =>
  !!n && (n.type === 'flow' || n.type === 'datasource' || n.type === 'connection') && !n.restricted
const canOpen = (n: LineageNode | null) =>
  !!n && (n.type === 'flow' || n.type === 'datasource') && !n.restricted

function fmtDate(iso?: string | null) {
  return fmtx.dateTime(iso)
}

// motivo di un riferimento non risolto
function restrictedLabel(n: LineageNode) {
  return n.meta?.reason === 'removed' ? t('lineage.reasonRemoved') : t('lineage.reasonNotAccessible')
}

// blast radius: tutto ciò che sta a VALLE del nodo lungo il flusso dei dati
// (read/publish/write), not limited to the current view: the node's downstream graph
// (8 hops, the most the gateway gives) is asked for when its panel opens
const DATA_KINDS = new Set(['read', 'publish', 'write'])
const impactGraph = ref<LineageGraph | null>(null)
watch(selected, async (n) => {
  impactGraph.value = null
  const prefix = n?.id.split(':')[0]
  const type = (Object.keys(NODE_PREFIX) as CenterType[]).find((k) => NODE_PREFIX[k] === prefix)
  if (!n || n.restricted || !type) return
  try {
    const g = await api.lineage({ type, id: Number(n.id.split(':')[1]), direction: 'downstream', depth: 8 })
    if (selected.value?.id === n.id) impactGraph.value = g
  } catch { /* the panel shows no impact */ }
})
function impactOf(n: LineageNode, full: LineageGraph) {
  const down = new Map<string, string[]>()
  for (const e of full.edges) {
    if (DATA_KINDS.has(e.kind)) down.set(e.source, [...(down.get(e.source) ?? []), e.target])
  }
  const seen = new Set<string>()
  const queue = [...(down.get(n.id) ?? [])]
  while (queue.length) {
    const id = queue.shift()!
    if (seen.has(id)) continue
    seen.add(id)
    for (const nx of down.get(id) ?? []) if (!seen.has(nx)) queue.push(nx)
  }
  const byType = (t: string) =>
    full.nodes.filter((x) => seen.has(x.id) && x.type === t).length
  return {
    flows: byType('flow'),
    datasources: byType('datasource'),
    sinks: byType('db_sink') + byType('s3_sink') + byType('sftp_sink'),
    total: seen.size,
  }
}
const impact = computed(() => (selected.value && impactGraph.value ? impactOf(selected.value, impactGraph.value) : null))

watch([centerType, centerId, direction, depth], load)


const centerLabel = computed(() =>
  graph.value.nodes.find((n) => n.id === centerNodeId.value)?.label ?? null,
)
function nodeIcon(t: string) {
  return NODE_META.value[t]?.icon ?? Database
}
</script>

<template>
  <AppShell fluid>
    <div class="lineage">
      <div class="page-head">
        <h1><Share2 :size="18" /> {{ $t('lineage.title') }}</h1>
        <span class="muted sub">{{ $t('lineage.subtitle') }}</span>
      </div>

      <div class="ln-body">
        <!-- sidebar: picker + parametri + liste a monte/valle -->
        <aside class="ln-side">
          <div class="ln-block">
            <label class="ln-label">{{ $t('lineage.centerOn') }}</label>
            <div class="searchbox"><Search :size="14" /><input v-model="q" type="text" :placeholder="$t('lineage.searchPlaceholder')" :aria-label="$t('lineage.searchPlaceholder')" /></div>
            <div v-if="q.trim()" class="ln-picklist" role="listbox" :aria-label="$t('lineage.centerOn')">
              <button
                v-for="h in hits"
                :key="`${h.kind}-${h.id}`"
                class="ln-pick"
                :class="{ on: isChosen(h) }"
                role="option"
                :aria-selected="isChosen(h)"
                @click="choose(h)"
                @dblclick="choose(h); apply()"
              >
                <component :is="nodeIcon(h.kind)" :size="13" />
                <span class="ln-pick-text">
                  <span class="ln-pick-label">{{ h.name }}</span>
                  <span v-if="folderPaths.pathOf(h.project_id) || h.project_name" class="ln-pick-path">{{ folderPaths.pathOf(h.project_id) || h.project_name }}</span>
                </span>
                <span class="ln-pick-type">{{ hitType(h) }}</span>
              </button>
              <p v-if="!hits.length && !searching" class="muted empty">{{ $t('lineage.noObjects') }}</p>
            </div>
            <div class="ln-apply-row">
              <button class="primary" :disabled="!chosen" @click="apply">{{ $t('lineage.apply') }}</button>
              <button class="ln-all" :class="{ on: showingAll }" @click="showAll">{{ $t('lineage.wholeGraph') }}</button>
            </div>
          </div>

          <div v-if="centerNodeId" class="ln-block">
            <label class="ln-label">{{ $t('lineage.direction') }}</label>
            <div class="ln-seg">
              <button :class="{ on: direction === 'upstream' }" @click="direction = 'upstream'">{{ $t('lineage.upstream') }}</button>
              <button :class="{ on: direction === 'both' }" @click="direction = 'both'">{{ $t('lineage.both') }}</button>
              <button :class="{ on: direction === 'downstream' }" @click="direction = 'downstream'">{{ $t('lineage.downstream') }}</button>
            </div>
            <label class="ln-label" style="margin-top: 10px">{{ $t('lineage.depth', { n: depth }) }}</label>
            <input v-model.number="depth" type="range" min="1" max="8" class="ln-range" />
          </div>

          <div v-if="centerNodeId" class="ln-block">
            <label class="ln-label">{{ $t('lineage.upstream') }} <span class="muted">({{ upstream.length }})</span></label>
            <div class="ln-neigh">
              <span v-for="n in upstream" :key="n.id" class="ln-chip" :class="`c-${n.type}`">
                <component :is="nodeIcon(n.type)" :size="11" /> {{ n.label }}
              </span>
              <span v-if="!upstream.length" class="muted empty">{{ $t('lineage.none') }}</span>
            </div>
            <label class="ln-label" style="margin-top: 10px">{{ $t('lineage.downstream') }} <span class="muted">({{ downstream.length }})</span></label>
            <div class="ln-neigh">
              <span v-for="n in downstream" :key="n.id" class="ln-chip" :class="`c-${n.type}`">
                <component :is="nodeIcon(n.type)" :size="11" /> {{ n.label }}
              </span>
              <span v-if="!downstream.length" class="muted empty">{{ $t('lineage.none') }}</span>
            </div>
          </div>

          <!-- salute / problemi nel grafo mostrato -->
          <div v-if="problems.total || problems.neverRun" class="ln-block ln-problems">
            <label class="ln-label"><AlertTriangle :size="12" /> {{ $t('lineage.health') }}</label>
            <span v-if="problems.stale" class="ln-prob stale"><Clock :size="12" /> {{ $t('lineage.staleItems', { n: problems.stale }) }}</span>
            <span v-if="problems.neverRun" class="ln-prob never"><AlertTriangle :size="12" /> {{ $t('lineage.neverRunItems', { n: problems.neverRun }) }}</span>
            <span v-if="problems.restricted" class="ln-prob restr"><AlertTriangle :size="12" /> {{ $t('lineage.unresolvedRefs', { n: problems.restricted }) }}</span>
          </div>

          <!-- filtri per tipo -->
          <div class="ln-block">
            <label class="ln-label">{{ $t('lineage.show') }}</label>
            <div class="ln-filters">
              <button
                v-for="t in ALL_TYPES"
                :key="t"
                class="ln-filter"
                :class="{ off: !enabledTypes.has(t) }"
                :style="{ '--c': NODE_META[t].color }"
                @click="toggleType(t)"
              >
                <i :style="{ background: NODE_META[t].color }" /> {{ NODE_META[t].label }}
              </button>
            </div>
          </div>

          <!-- legenda archi -->
          <div class="ln-block">
            <label class="ln-label">{{ $t('lineage.relationships') }}</label>
            <div class="ln-legend">
              <span v-for="(m, k) in EDGE_META" :key="k" class="ln-leg">
                <i class="edge" :style="{ background: m.color, borderTop: m.dashed ? `2px dashed ${m.color}` : 'none' }" /> {{ m.label }}
              </span>
            </div>
          </div>
        </aside>

        <!-- canvas -->
        <div class="ln-canvas">
          <div v-if="error" class="ln-msg err">{{ error }}</div>
          <div v-else-if="loading" class="ln-msg muted">{{ $t('lineage.computingGraph') }}</div>
          <div v-else-if="!centerNodeId && !showingAll" class="ln-msg muted">{{ $t('lineage.pickHint') }}</div>
          <div v-else-if="!graph.nodes.length" class="ln-msg muted">{{ $t('lineage.noElements') }}</div>
          <ClientOnly>
            <VueFlow
              :nodes="rfNodes"
              :edges="rfEdges"
              :nodes-draggable="true"
              :nodes-connectable="false"
              :elements-selectable="true"
              fit-view-on-init
              :min-zoom="0.2"
              @node-click="onNodeClick"
            >
              <template #node-default="{ data }">
                <Handle type="target" :position="Position.Left" />
                <div class="ln-inner">
                  <component :is="nodeIcon(data.node.type)" :size="14" class="ln-ic" />
                  <div class="ln-txt">
                    <span class="ln-name">{{ data.node.label }}</span>
                    <span class="ln-meta">{{ NODE_META[data.node.type]?.label }}<template v-if="data.node.kind"> · {{ data.node.kind }}</template></span>
                  </div>
                  <span v-if="data.node.meta?.stale" class="ln-badge stale" :title="data.node.meta?.stale_reason === 'upstream' ? $t('lineage.staleInherited') : $t('lineage.staleDataNewer')"><Clock :size="11" /></span>
                  <span v-else-if="data.node.meta?.never_run" class="ln-badge never" :title="$t('lineage.neverRun')"><AlertTriangle :size="11" /></span>
                  <span v-else-if="data.node.restricted" class="ln-badge restr" :title="$t('lineage.unresolvedRefTitle')"><AlertTriangle :size="11" /></span>
                </div>
                <Handle type="source" :position="Position.Right" />
              </template>
              <Background pattern-color="var(--edge)" :gap="18" />
              <Controls />
            </VueFlow>
          </ClientOnly>

          <!-- pannello di dettaglio del nodo selezionato -->
          <div v-if="selected" class="ln-detail">
            <button class="ln-detail-x" :title="$t('lineage.close')" :aria-label="$t('lineage.close')" @click="selected = null"><X :size="14" /></button>
            <div class="ln-detail-head">
              <component :is="nodeIcon(selected.type)" :size="16" :style="{ color: NODE_META[selected.type]?.color }" />
              <div>
                <div class="ln-detail-name">{{ selected.label }}</div>
                <div class="ln-detail-type">{{ NODE_META[selected.type]?.label }}<template v-if="selected.kind"> · {{ selected.kind }}</template></div>
              </div>
            </div>

            <div v-if="selected.meta?.stale" class="ln-detail-warn stale">
              <Clock :size="13" />
              <span v-if="selected.meta?.stale_reason === 'upstream'">{{ $t('lineage.staleInheritedDetail') }}</span>
              <span v-else>{{ $t('lineage.staleDetail') }}</span>
            </div>
            <div v-else-if="selected.meta?.never_run" class="ln-detail-warn never"><AlertTriangle :size="13" /> {{ $t('lineage.neverRun') }}</div>
            <div v-else-if="selected.restricted" class="ln-detail-warn restr"><AlertTriangle :size="13" /> {{ $t('lineage.unresolvedRef') }} · {{ restrictedLabel(selected) }}</div>

            <dl class="ln-detail-meta">
              <template v-if="selected.type === 'flow'">
                <dt>{{ $t('lineage.engine') }}</dt><dd>{{ selected.meta?.engine ?? '—' }}</dd>
                <dt>{{ $t('lineage.lastRun') }}</dt><dd>{{ selected.meta?.last_run_status ?? $t('lineage.never') }} · {{ fmtDate(selected.meta?.last_run_at) }}</dd>
                <dt>{{ $t('lineage.scheduled') }}</dt><dd>{{ selected.meta?.scheduled ? $t('lineage.yes') : $t('lineage.no') }}</dd>
              </template>
              <template v-else-if="selected.type === 'datasource'">
                <dt>{{ $t('lineage.rows') }}</dt><dd>{{ selected.meta?.rows ?? '—' }}</dd>
                <dt>{{ $t('lineage.refreshed') }}</dt><dd>{{ fmtDate(selected.meta?.refreshed_at) }}</dd>
              </template>
              <template v-else-if="selected.type === 'connection'">
                <dt>{{ $t('lineage.type') }}</dt><dd>{{ selected.meta?.db_type ?? '—' }}</dd>
                <dt>{{ $t('lineage.database') }}</dt><dd>{{ selected.meta?.database || '—' }}</dd>
              </template>
              <template v-else-if="selected.type === 'db_sink'">
                <dt>{{ $t('lineage.nodeConnection') }}</dt><dd>{{ selected.meta?.connection_name ?? '—' }}</dd>
                <dt>{{ $t('lineage.mode') }}</dt><dd>{{ selected.meta?.mode ?? '—' }}</dd>
              </template>
              <template v-else-if="selected.type === 's3_sink'">
                <dt>{{ $t('lineage.bucket') }}</dt><dd>{{ selected.meta?.bucket || '—' }}</dd>
                <dt>{{ $t('lineage.format') }}</dt><dd>{{ selected.meta?.format ?? '—' }}</dd>
              </template>
              <template v-else-if="selected.type === 'sftp_sink'">
                <dt>{{ $t('lineage.nodeConnection') }}</dt><dd>{{ selected.meta?.connection_name ?? '—' }}</dd>
                <dt>{{ $t('sftp.outFolderLabel') }}</dt><dd>{{ selected.meta?.folder || '—' }}</dd>
                <dt>{{ $t('lineage.format') }}</dt><dd>{{ selected.meta?.format ?? '—' }}</dd>
              </template>
            </dl>

            <!-- blast radius: cosa si tocca a valle se cambio questo nodo -->
            <div v-if="impact && impact.total" class="ln-impact">
              <span class="ln-impact-h">{{ $t('lineage.downstreamImpact') }}</span>
              <div class="ln-impact-row">
                <span v-if="impact.flows"><Workflow :size="11" /> {{ $t('lineage.impactFlows', { n: impact.flows }) }}</span>
                <span v-if="impact.datasources"><Database :size="11" /> {{ $t('lineage.impactDatasources', { n: impact.datasources }) }}</span>
                <span v-if="impact.sinks"><HardDriveDownload :size="11" /> {{ $t('lineage.impactSinks', { n: impact.sinks }) }}</span>
              </div>
            </div>

            <div class="ln-detail-actions">
              <button v-if="canRecenter(selected)" @click="recenterOn(selected)"><Crosshair :size="13" /> {{ $t('lineage.centerHere') }}</button>
              <button v-if="canOpen(selected)" class="primary" @click="openNode(selected)"><ExternalLink :size="13" /> {{ $t('lineage.open') }}</button>
            </div>
          </div>
        </div>
      </div>
    </div>
  </AppShell>
</template>

<style scoped>
.lineage { display: flex; flex-direction: column; flex: 1; min-height: 0; gap: 12px; }
.page-head { display: flex; align-items: baseline; gap: 12px; }
.page-head h1 { display: inline-flex; align-items: center; gap: 8px; margin: 0; font-size: 1.5em; }
.sub { font-size: 12.5px; }

.ln-body { display: flex; gap: 12px; flex: 1; min-height: 0; }
.ln-side {
  width: 290px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  gap: 12px;
  overflow-y: auto;
}
.ln-block {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.ln-label { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.03em; }
.searchbox { display: flex; align-items: center; gap: 6px; background: var(--bg-soft); border: 1px solid var(--border); border-radius: 8px; padding: 0 8px; }
.searchbox input { border: none; background: transparent; padding: 6px 0; }
.searchbox input:focus { box-shadow: none; }

.ln-all { justify-content: flex-start; font-size: 12.5px; }
.ln-all.on { border-color: var(--accent); background: var(--tint-accent); color: var(--text); }
.ln-picklist { display: flex; flex-direction: column; gap: 3px; max-height: 260px; overflow-y: auto; }
.ln-pick {
  display: flex; align-items: center; gap: 7px;
  justify-content: flex-start; text-align: left;
  padding: 6px 8px; font-size: 12.5px; width: 100%;
  background: transparent; border: 1px solid transparent;
}
.ln-pick:hover { background: var(--panel-2); border-color: var(--border); }
.ln-pick.on { border-color: var(--accent); background: var(--tint-accent); }
.ln-pick-text { flex: 1; min-width: 0; display: flex; flex-direction: column; text-align: left; }
/* the name is never cut short: two objects may differ only at its end */
.ln-pick-label { overflow-wrap: anywhere; }
.ln-pick-path { font-size: 11px; color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ln-apply-row { display: flex; gap: 6px; margin-top: 6px; }
.ln-apply-row .ln-all { flex: 1; }
.ln-pick-type { font-size: 10px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.03em; }

.ln-seg { display: flex; gap: 4px; }
.ln-seg button { flex: 1; font-size: 11.5px; padding: 5px 6px; }
.ln-seg button.on { border-color: var(--accent); background: var(--tint-accent); color: var(--text); }
.ln-range { width: 100%; }

.ln-neigh { display: flex; flex-wrap: wrap; gap: 5px; }
.ln-chip {
  display: inline-flex; align-items: center; gap: 4px;
  font-size: 11px; padding: 2px 8px; border-radius: 999px;
  background: var(--panel-2); border: 1px solid var(--border);
}
.ln-chip.c-flow { border-color: rgba(79, 140, 255, 0.4); }
.ln-chip.c-datasource { border-color: rgba(110, 231, 183, 0.4); }
.ln-chip.c-connection { border-color: rgba(167, 139, 250, 0.4); }
.ln-chip.c-db_sink { border-color: rgba(251, 191, 36, 0.4); }
.ln-chip.c-s3_sink { border-color: rgba(56, 189, 248, 0.4); }
.ln-chip.c-sftp_sink { border-color: rgba(244, 114, 182, 0.4); }
.empty { font-size: 12px; }

.ln-legend { display: flex; flex-direction: column; gap: 5px; }
.ln-leg { display: inline-flex; align-items: center; gap: 7px; font-size: 11.5px; color: var(--muted); }
.ln-leg i { width: 10px; height: 10px; border-radius: 3px; display: inline-block; }
.ln-leg i.edge { width: 16px; height: 2px; border-radius: 0; }

/* filtri per tipo */
.ln-filters { display: flex; flex-wrap: wrap; gap: 5px; }
.ln-filter {
  display: inline-flex; align-items: center; gap: 5px;
  font-size: 11.5px; padding: 4px 9px; border-radius: 999px;
}
.ln-filter i { width: 9px; height: 9px; border-radius: 3px; display: inline-block; }
.ln-filter.off { opacity: 0.4; text-decoration: line-through; }

/* salute / problemi */
.ln-problems { gap: 6px; }
.ln-prob { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; }
.ln-prob.stale { color: color-mix(in srgb, #d97706 var(--enc-text-mix), var(--text)); }
.ln-prob.never { color: var(--muted); }
.ln-prob.restr { color: var(--danger); }

/* badge di salute sui nodi del grafo */
:deep(.ln-badge) {
  flex-shrink: 0; display: inline-flex; align-items: center; justify-content: center;
  width: 18px; height: 18px; border-radius: 5px;
}
:deep(.ln-badge.stale) { color: color-mix(in srgb, #d97706 var(--enc-text-mix), var(--text)); background: rgba(217, 119, 6, 0.14); }
:deep(.ln-badge.never) { color: var(--muted); background: var(--panel-2); }
:deep(.ln-badge.restr) { color: var(--danger); background: rgba(255, 107, 107, 0.14); }

/* pannello di dettaglio */
.ln-detail {
  position: absolute; top: 12px; right: 12px; z-index: 8;
  width: 260px;
  background: var(--panel); border: 1px solid var(--border);
  border-radius: 11px; box-shadow: var(--shadow-2);
  padding: 14px; display: flex; flex-direction: column; gap: 10px;
}
.ln-detail-x { position: absolute; top: 8px; right: 8px; padding: 4px 6px; }
.ln-detail-head { display: flex; align-items: flex-start; gap: 9px; padding-right: 20px; }
.ln-detail-name { font-size: 13.5px; font-weight: 700; line-height: 1.25; }
.ln-detail-type { font-size: 10.5px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.03em; margin-top: 2px; }
.ln-detail-warn {
  display: flex; align-items: center; gap: 7px;
  font-size: 11.5px; padding: 7px 9px; border-radius: 8px; line-height: 1.3;
}
.ln-detail-warn.stale { color: color-mix(in srgb, #d97706 var(--enc-text-mix), var(--text)); background: rgba(217, 119, 6, 0.12); }
.ln-detail-warn.never { color: var(--muted); background: var(--panel-2); }
.ln-detail-warn.restr { color: var(--danger); background: rgba(255, 107, 107, 0.12); }
.ln-detail-meta { display: grid; grid-template-columns: auto 1fr; gap: 4px 12px; margin: 0; }
.ln-detail-meta dt { font-size: 11px; color: var(--muted); }
.ln-detail-meta dd { font-size: 12px; margin: 0; text-align: right; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ln-impact { display: flex; flex-direction: column; gap: 5px; padding: 8px 9px; background: var(--panel-2); border-radius: 8px; }
.ln-impact-h { font-size: 10.5px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.03em; }
.ln-impact-row { display: flex; flex-wrap: wrap; gap: 4px 10px; }
.ln-impact-row span { display: inline-flex; align-items: center; gap: 4px; font-size: 12px; }
.ln-detail-actions { display: flex; gap: 6px; }
.ln-detail-actions button { flex: 1; font-size: 12px; padding: 6px 8px; }

.ln-canvas {
  flex: 1;
  min-width: 0;
  position: relative;
  background: var(--bg-soft);
  border: 1px solid var(--border);
  border-radius: 10px;
  overflow: hidden;
}
.ln-msg { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; z-index: 5; pointer-events: none; }
.ln-msg.err { color: var(--danger); }

/* nodi del grafo */
:deep(.ln-node) {
  background: var(--panel);
  border: 1px solid var(--border);
  border-left: 3px solid var(--muted);
  border-radius: 9px;
  padding: 0;
  width: 180px;
  box-shadow: var(--shadow-1);
  /* il tema default di Vue Flow imposta un colore testo scuro sui nodi 'default':
     lo forziamo al testo del tema, altrimenti è illeggibile su fondo scuro */
  color: var(--text);
}
:deep(.ln-node.ln-center) { box-shadow: 0 0 0 2px var(--accent), var(--shadow-2); }
:deep(.ln-node.ln-restricted) { border-style: dashed; opacity: 0.65; }
:deep(.ln-flow) { border-left-color: #4f8cff; }
:deep(.ln-datasource) { border-left-color: #6ee7b7; }
:deep(.ln-connection) { border-left-color: #a78bfa; }
:deep(.ln-db_sink) { border-left-color: #fbbf24; }
:deep(.ln-s3_sink) { border-left-color: #38bdf8; }
:deep(.ln-sftp_sink) { border-left-color: #f472b6; }
.ln-inner { display: flex; align-items: center; gap: 8px; padding: 8px 10px; }
.ln-ic { flex-shrink: 0; color: var(--muted); }
.ln-txt { display: flex; flex-direction: column; min-width: 0; }
.ln-name { font-size: 12.5px; font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ln-meta { font-size: 10px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.02em; }
</style>
