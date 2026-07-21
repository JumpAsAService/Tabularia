<script setup lang="ts">
// Tab Lineage: grafo di provenienza/impatto tra flussi, datasource, connessioni
// e destinazioni esterne. Si sceglie un oggetto su cui centrare (o si vede tutto);
// il grafo mostra a monte (da dove vengono i dati) e a valle (chi si rompe se lo
// tocco). Il grafo è renderizzato con Vue Flow (client-only), layout a livelli.
import { ref, computed, watch, nextTick } from 'vue'
import { VueFlow, useVueFlow, Handle, Position } from '@vue-flow/core'
import type { Node, Edge } from '@vue-flow/core'
import { Background } from '@vue-flow/background'
import { Controls } from '@vue-flow/controls'
import {
  Share2, Workflow, Database, Plug, CloudUpload, HardDriveDownload, Search,
  AlertTriangle, Clock, ExternalLink, X, Crosshair,
} from 'lucide-vue-next'
import { useApi, errMessage, type LineageGraph, type LineageNode, type LineageEdge } from '~/composables/useApi'

const api = useApi()
const { fitView } = useVueFlow()

const graph = ref<LineageGraph>({ nodes: [], edges: [] })
const loading = ref(false)
const error = ref('')

// selezione: oggetto centrale + parametri di traversata
const centerType = ref<'flow' | 'datasource' | null>(null)
const centerId = ref<number | null>(null)
const direction = ref<'both' | 'upstream' | 'downstream'>('both')
const depth = ref(3)
const q = ref('') // ricerca nel picker
const selected = ref<LineageNode | null>(null) // nodo aperto nel pannello dettaglio

// filtri per tipo di nodo (tutti attivi di default)
const ALL_TYPES = ['flow', 'datasource', 'connection', 'db_sink', 's3_sink'] as const
const enabledTypes = ref<Set<string>>(new Set(ALL_TYPES))
function toggleType(t: string) {
  const s = new Set(enabledTypes.value)
  s.has(t) ? s.delete(t) : s.add(t)
  enabledTypes.value = s
}

// ── etichette/colori per tipo di nodo e tipo di arco ─────────────────────────
const NODE_META: Record<string, { label: string; icon: any; color: string }> = {
  flow: { label: 'Flusso', icon: Workflow, color: '#4f8cff' },
  datasource: { label: 'Datasource', icon: Database, color: '#6ee7b7' },
  connection: { label: 'Connessione', icon: Plug, color: '#a78bfa' },
  db_sink: { label: 'Tabella DB', icon: HardDriveDownload, color: '#fbbf24' },
  s3_sink: { label: 'Oggetto S3', icon: CloudUpload, color: '#38bdf8' },
}
const EDGE_META: Record<string, { label: string; color: string; dashed?: boolean }> = {
  read: { label: 'legge', color: '#4f8cff' },
  publish: { label: 'produce', color: '#6ee7b7' },
  ingest: { label: 'ingest', color: '#a78bfa' },
  write: { label: 'scrive', color: '#fbbf24' },
  refresh: { label: 'rinfresca', color: '#8b93a7', dashed: true },
  orchestrate: { label: 'orchestra', color: '#c084fc', dashed: true },
}

// ── picker: flussi e datasource presenti nel grafo completo ──────────────────
const fullGraph = ref<LineageGraph>({ nodes: [], edges: [] })
const pickable = computed(() =>
  fullGraph.value.nodes
    .filter((n) => (n.type === 'flow' || n.type === 'datasource') && !n.restricted)
    .filter((n) => !q.value || n.label.toLowerCase().includes(q.value.toLowerCase()))
    .sort((a, b) => a.label.localeCompare(b.label)),
)

const centerNodeId = computed(() =>
  centerType.value && centerId.value != null
    ? `${centerType.value === 'flow' ? 'flow' : 'ds'}:${centerId.value}`
    : null,
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
const COL_W = 250
const ROW_H = 92
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
    const m = EDGE_META[e.kind]
    return {
      id: `e${i}`,
      source: e.source,
      target: e.target,
      label: m?.label ?? e.kind,
      animated: e.kind === 'read' || e.kind === 'publish',
      style: { stroke: m?.color ?? '#8b93a7', strokeWidth: 1.8, strokeDasharray: m?.dashed ? '5 5' : undefined },
      labelStyle: { fill: m?.color ?? '#8b93a7', fontSize: 10, fontWeight: 600 },
      labelBgStyle: { fill: 'var(--panel)', fillOpacity: 0.85 },
    } as Edge
  }),
)

// ── caricamento ──────────────────────────────────────────────────────────────
async function load() {
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
  centerType.value = n.type === 'flow' ? 'flow' : 'datasource'
  centerId.value = Number(n.id.split(':')[1])
}
function showAll() {
  centerType.value = null
  centerId.value = null
  selected.value = null
}

// click su un nodo → apre il pannello di dettaglio (il ricentro è un'azione lì)
function onNodeClick(ev: any) {
  const n: LineageNode | undefined = ev?.node?.data?.node
  if (n) selected.value = n
}

// azioni del pannello: ricentra sul nodo, oppure aprilo nel suo strumento
function recenterOn(n: LineageNode) {
  if (n.type === 'flow' || n.type === 'datasource') pick(n)
}
function openNode(n: LineageNode) {
  const id = Number(n.id.split(':')[1])
  if (n.type === 'flow') navigateTo(`/editor?flow=${id}`)
  else if (n.type === 'datasource') navigateTo('/datasources')
}
const canRecenter = (n: LineageNode | null) =>
  !!n && (n.type === 'flow' || n.type === 'datasource') && !n.restricted
const canOpen = (n: LineageNode | null) =>
  !!n && (n.type === 'flow' || n.type === 'datasource') && !n.restricted

function fmtDate(iso?: string | null) {
  if (!iso) return '—'
  try { return new Date(iso).toLocaleString('it-IT', { dateStyle: 'medium', timeStyle: 'short' }) } catch { return iso }
}

// motivo di un riferimento non risolto
function restrictedLabel(n: LineageNode) {
  return n.meta?.reason === 'removed' ? 'Rimosso' : 'Non accessibile'
}

// blast radius: tutto ciò che sta a VALLE del nodo lungo il flusso dei dati
// (read/publish/write), calcolato sul grafo COMPLETO (non sulla vista corrente)
const DATA_KINDS = new Set(['read', 'publish', 'write'])
function impactOf(n: LineageNode) {
  const down = new Map<string, string[]>()
  for (const e of fullGraph.value.edges) {
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
    fullGraph.value.nodes.filter((x) => seen.has(x.id) && x.type === t).length
  return {
    flows: byType('flow'),
    datasources: byType('datasource'),
    sinks: byType('db_sink') + byType('s3_sink'),
    total: seen.size,
  }
}
const impact = computed(() => (selected.value ? impactOf(selected.value) : null))

watch([centerType, centerId, direction, depth], load)

onMounted(async () => {
  try {
    fullGraph.value = await api.lineage() // per il picker
  } catch (e) {
    error.value = errMessage(e)
  }
  await load()
})

const centerLabel = computed(() =>
  graph.value.nodes.find((n) => n.id === centerNodeId.value)?.label ?? null,
)
function nodeIcon(t: string) {
  return NODE_META[t]?.icon ?? Database
}
</script>

<template>
  <AppShell fluid>
    <div class="lineage">
      <div class="page-head">
        <h2><Share2 :size="18" /> Lineage</h2>
        <span class="muted sub">Provenienza e impatto tra flussi, datasource, connessioni e destinazioni</span>
      </div>

      <div class="ln-body">
        <!-- sidebar: picker + parametri + liste a monte/valle -->
        <aside class="ln-side">
          <div class="ln-block">
            <label class="ln-label">Centra su</label>
            <div class="searchbox"><Search :size="14" /><input v-model="q" type="text" placeholder="Cerca flusso o datasource…" /></div>
            <button class="ln-all" :class="{ on: !centerNodeId }" @click="showAll">Tutto il grafo</button>
            <div class="ln-picklist">
              <button
                v-for="n in pickable"
                :key="n.id"
                class="ln-pick"
                :class="{ on: n.id === centerNodeId }"
                @click="pick(n)"
              >
                <component :is="nodeIcon(n.type)" :size="13" />
                <span class="ln-pick-label">{{ n.label }}</span>
                <span class="ln-pick-type">{{ n.type === 'flow' ? 'flusso' : 'datasource' }}</span>
              </button>
              <p v-if="!pickable.length" class="muted empty">Nessun oggetto.</p>
            </div>
          </div>

          <div v-if="centerNodeId" class="ln-block">
            <label class="ln-label">Direzione</label>
            <div class="ln-seg">
              <button :class="{ on: direction === 'upstream' }" @click="direction = 'upstream'">A monte</button>
              <button :class="{ on: direction === 'both' }" @click="direction = 'both'">Entrambe</button>
              <button :class="{ on: direction === 'downstream' }" @click="direction = 'downstream'">A valle</button>
            </div>
            <label class="ln-label" style="margin-top: 10px">Profondità: {{ depth }}</label>
            <input v-model.number="depth" type="range" min="1" max="8" class="ln-range" />
          </div>

          <div v-if="centerNodeId" class="ln-block">
            <label class="ln-label">A monte <span class="muted">({{ upstream.length }})</span></label>
            <div class="ln-neigh">
              <span v-for="n in upstream" :key="n.id" class="ln-chip" :class="`c-${n.type}`">
                <component :is="nodeIcon(n.type)" :size="11" /> {{ n.label }}
              </span>
              <span v-if="!upstream.length" class="muted empty">— nessuno</span>
            </div>
            <label class="ln-label" style="margin-top: 10px">A valle <span class="muted">({{ downstream.length }})</span></label>
            <div class="ln-neigh">
              <span v-for="n in downstream" :key="n.id" class="ln-chip" :class="`c-${n.type}`">
                <component :is="nodeIcon(n.type)" :size="11" /> {{ n.label }}
              </span>
              <span v-if="!downstream.length" class="muted empty">— nessuno</span>
            </div>
          </div>

          <!-- salute / problemi nel grafo mostrato -->
          <div v-if="problems.total || problems.neverRun" class="ln-block ln-problems">
            <label class="ln-label"><AlertTriangle :size="12" /> Salute</label>
            <span v-if="problems.stale" class="ln-prob stale"><Clock :size="12" /> {{ problems.stale }} elemento/i stale (dati superati)</span>
            <span v-if="problems.neverRun" class="ln-prob never"><AlertTriangle :size="12" /> {{ problems.neverRun }} mai eseguito/i</span>
            <span v-if="problems.restricted" class="ln-prob restr"><AlertTriangle :size="12" /> {{ problems.restricted }} riferimento/i non risolto/i</span>
          </div>

          <!-- filtri per tipo -->
          <div class="ln-block">
            <label class="ln-label">Mostra</label>
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
            <label class="ln-label">Relazioni</label>
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
          <div v-else-if="loading" class="ln-msg muted">Calcolo del grafo…</div>
          <div v-else-if="!graph.nodes.length" class="ln-msg muted">Nessun elemento da mostrare.</div>
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
                  <span v-if="data.node.meta?.stale" class="ln-badge stale" :title="data.node.meta?.stale_reason === 'upstream' ? 'Stale ereditata da monte' : 'Stale: dati più recenti dell\'ultimo run'"><Clock :size="11" /></span>
                  <span v-else-if="data.node.meta?.never_run" class="ln-badge never" title="Mai eseguito"><AlertTriangle :size="11" /></span>
                  <span v-else-if="data.node.restricted" class="ln-badge restr" title="Riferimento non risolto (rimosso o non accessibile)"><AlertTriangle :size="11" /></span>
                </div>
                <Handle type="source" :position="Position.Right" />
              </template>
              <Background pattern-color="var(--edge)" :gap="18" />
              <Controls />
            </VueFlow>
          </ClientOnly>

          <!-- pannello di dettaglio del nodo selezionato -->
          <div v-if="selected" class="ln-detail">
            <button class="ln-detail-x" title="Chiudi" @click="selected = null"><X :size="14" /></button>
            <div class="ln-detail-head">
              <component :is="nodeIcon(selected.type)" :size="16" :style="{ color: NODE_META[selected.type]?.color }" />
              <div>
                <div class="ln-detail-name">{{ selected.label }}</div>
                <div class="ln-detail-type">{{ NODE_META[selected.type]?.label }}<template v-if="selected.kind"> · {{ selected.kind }}</template></div>
              </div>
            </div>

            <div v-if="selected.meta?.stale" class="ln-detail-warn stale">
              <Clock :size="13" />
              <span v-if="selected.meta?.stale_reason === 'upstream'">Stale ereditata: dipende da un elemento a monte con dati superati — rieseguire la catena</span>
              <span v-else>Stale: legge dati rinfrescati dopo l'ultimo run riuscito — rieseguire</span>
            </div>
            <div v-else-if="selected.meta?.never_run" class="ln-detail-warn never"><AlertTriangle :size="13" /> Mai eseguito</div>
            <div v-else-if="selected.restricted" class="ln-detail-warn restr"><AlertTriangle :size="13" /> Riferimento non risolto · {{ restrictedLabel(selected) }}</div>

            <dl class="ln-detail-meta">
              <template v-if="selected.type === 'flow'">
                <dt>Motore</dt><dd>{{ selected.meta?.engine ?? '—' }}</dd>
                <dt>Ultimo run</dt><dd>{{ selected.meta?.last_run_status ?? 'mai' }} · {{ fmtDate(selected.meta?.last_run_at) }}</dd>
                <dt>Schedulato</dt><dd>{{ selected.meta?.scheduled ? 'sì' : 'no' }}</dd>
              </template>
              <template v-else-if="selected.type === 'datasource'">
                <dt>Righe</dt><dd>{{ selected.meta?.rows ?? '—' }}</dd>
                <dt>Rinfrescata</dt><dd>{{ fmtDate(selected.meta?.refreshed_at) }}</dd>
              </template>
              <template v-else-if="selected.type === 'connection'">
                <dt>Tipo</dt><dd>{{ selected.meta?.db_type ?? '—' }}</dd>
                <dt>Database</dt><dd>{{ selected.meta?.database || '—' }}</dd>
              </template>
              <template v-else-if="selected.type === 'db_sink'">
                <dt>Connessione</dt><dd>{{ selected.meta?.connection_name ?? '—' }}</dd>
                <dt>Modalità</dt><dd>{{ selected.meta?.mode ?? '—' }}</dd>
              </template>
              <template v-else-if="selected.type === 's3_sink'">
                <dt>Bucket</dt><dd>{{ selected.meta?.bucket || '—' }}</dd>
                <dt>Formato</dt><dd>{{ selected.meta?.format ?? '—' }}</dd>
              </template>
            </dl>

            <!-- blast radius: cosa si tocca a valle se cambio questo nodo -->
            <div v-if="impact && impact.total" class="ln-impact">
              <span class="ln-impact-h">Impatto a valle</span>
              <div class="ln-impact-row">
                <span v-if="impact.flows"><Workflow :size="11" /> {{ impact.flows }} flussi</span>
                <span v-if="impact.datasources"><Database :size="11" /> {{ impact.datasources }} datasource</span>
                <span v-if="impact.sinks"><HardDriveDownload :size="11" /> {{ impact.sinks }} destinazioni</span>
              </div>
            </div>

            <div class="ln-detail-actions">
              <button v-if="canRecenter(selected)" @click="recenterOn(selected)"><Crosshair :size="13" /> Centra qui</button>
              <button v-if="canOpen(selected)" class="primary" @click="openNode(selected)"><ExternalLink :size="13" /> Apri</button>
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
.page-head h2 { display: inline-flex; align-items: center; gap: 8px; margin: 0; }
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
.ln-pick-label { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
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
.ln-prob.stale { color: #d97706; }
.ln-prob.never { color: var(--muted); }
.ln-prob.restr { color: var(--danger); }

/* badge di salute sui nodi del grafo */
:deep(.ln-badge) {
  flex-shrink: 0; display: inline-flex; align-items: center; justify-content: center;
  width: 18px; height: 18px; border-radius: 5px;
}
:deep(.ln-badge.stale) { color: #d97706; background: rgba(217, 119, 6, 0.14); }
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
.ln-detail-warn.stale { color: #d97706; background: rgba(217, 119, 6, 0.12); }
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
}
:deep(.ln-node.ln-center) { box-shadow: 0 0 0 2px var(--accent), var(--shadow-2); }
:deep(.ln-node.ln-restricted) { border-style: dashed; opacity: 0.65; }
:deep(.ln-flow) { border-left-color: #4f8cff; }
:deep(.ln-datasource) { border-left-color: #6ee7b7; }
:deep(.ln-connection) { border-left-color: #a78bfa; }
:deep(.ln-db_sink) { border-left-color: #fbbf24; }
:deep(.ln-s3_sink) { border-left-color: #38bdf8; }
.ln-inner { display: flex; align-items: center; gap: 8px; padding: 8px 10px; }
.ln-ic { flex-shrink: 0; color: var(--muted); }
.ln-txt { display: flex; flex-direction: column; min-width: 0; }
.ln-name { font-size: 12.5px; font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ln-meta { font-size: 10px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.02em; }
</style>
