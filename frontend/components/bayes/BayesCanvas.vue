<script setup lang="ts">
// The canvas of a Bayesian model (docs/specs/bayes-graph-editor.md, «The canvas»): a second Vue Flow
// canvas, never FlowEditor.vue itself. The graph document is the truth and this draws it: boxes as
// parent nodes nested by `parent`, nodes in the box of their plate, the mirror's edges left to right
// (a source's right handle to its target's left one). Lot G0: nothing is dragged or connected; a
// node or a box is selected (click, Tab and Enter, Alt with an arrow), and a group box's switch
// changes its form.
import { computed, onBeforeUnmount, onMounted, provide, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { VueFlow, useVueFlow } from '@vue-flow/core'
import { Background } from '@vue-flow/background'
import { Controls, ControlButton } from '@vue-flow/controls'
import { MiniMap } from '@vue-flow/minimap'
import { AudioWaveform, ChartScatter, CircleDot, Clock, Database, Hash, List, Maximize, Minus, Plus, Sigma, SquareFunction, Users, Variable } from 'lucide-vue-next'
import BayesNode from '~/components/bayes/BayesNode.vue'
import BayesPlate from '~/components/bayes/BayesPlate.vue'
import { bayesBoxes, bayesCanvasEdges, bayesCanvasNodes, bayesNearestNode, BAYES_NODE_H, BAYES_NODE_W, type BayesDirection } from '~/composables/bayesCanvasModel'
import { BAYES_CANVAS_VIEW, type BayesNodeView, type BayesPlateView } from '~/composables/bayesCanvasView'
import { bayesBoundsText, bayesPriorText } from '~/composables/bayesEditorText'
import { bayesDistribution } from '~/composables/bayesCatalog'
import { focusNodeOf, focusOnCanvas } from '~/composables/nodeKeys'
import type { BayesModel, BayesGraphFinding } from '~/composables/bayesGraph'
import type { BayesTemplateGraph, BayesTemplateNode, BayesTemplateSlot } from '~/composables/bayesTemplates'

const props = defineProps<{
  doc: BayesTemplateGraph
  model: BayesModel | null
  findings: BayesGraphFinding[]
  selected: string | null
}>()
const emit = defineEmits<{
  (e: 'select', id: string | null): void
  (e: 'form', plate: string, nonCentred: boolean): void
}>()
const { t, locale } = useI18n()

const FLOW_ID = 'bayes-canvas'
const root = ref<HTMLElement | null>(null)
const { fitView, zoomIn: vfZoomIn, zoomOut: vfZoomOut, viewport, addSelectedNodes, removeSelectedNodes, getSelectedNodes, onNodeClick, onPaneClick, onNodesInitialized, findNode } = useVueFlow(FLOW_ID)

// ---------------------------------------------------------------- what each node says

const owner = (id: string): string => { const i = id.indexOf('.'); return i < 0 ? id : id.slice(0, i) }
const num = (v: number): string => new Intl.NumberFormat(locale.value, { maximumFractionDigits: 6 }).format(v)
const slotText = (s: BayesTemplateSlot | undefined): string => (s?.expr ?? s?.ref ?? '').trim()
const byId = computed(() => new Map(props.doc.nodes.map(n => [n.id, n])))
const plateLabel = (id: string): string => props.doc.plates.find(p => p.id === id)?.label ?? id
const nameOf = (id: string): string => (byId.value.get(id)?.name as string | undefined) ?? id
const observedIds = computed(() => new Set(props.doc.nodes.filter(n => n.kind === 'likelihood').map(n => String(n.observed ?? ''))))

const counts = computed(() => {
  const m = new Map<string, { errors: number, warnings: number }>()
  for (const f of props.findings) {
    const id = owner(f.node)
    const c = m.get(id) ?? { errors: 0, warnings: 0 }
    if (f.severity === 'error') c.errors++
    else c.warnings++
    m.set(id, c)
  }
  return m
})

const DATA_ICONS: Record<string, unknown> = { real: Database, int: Hash, group_index: Users, category: List, time: Clock }

function dataWhat (n: BayesTemplateNode): string {
  const type = String(n.type ?? '')
  const parts: string[] = []
  if (type === 'int') parts.push(t(`bayes.editor.dataType.int_${String(n.support || 'any')}`))
  else if (type === 'group_index') parts.push(t('bayes.editor.dataType.group_index', { plate: plateLabel(String(n.target ?? '')) }))
  else parts.push(t(`bayes.editor.dataType.${type || 'real'}`))
  if (n.standardize) parts.push(t('bayes.editor.dataType.standardized'))
  if (observedIds.value.has(n.id)) parts.push(t('bayes.editor.dataType.observed'))
  return parts.join(', ')
}

function priorLine (p: { dist: string, args: Record<string, BayesTemplateSlot> } | undefined): string {
  if (!p) return ''
  const order = bayesDistribution(p.dist)?.params.map(x => x.name) ?? []
  return bayesPriorText(p.dist, p.args, order)
}

function view (n: BayesTemplateNode): BayesNodeView {
  const c = counts.value.get(n.id) ?? { errors: 0, warnings: 0 }
  const base = { id: n.id, kind: n.kind, label: n.label, name: n.name ?? '', what: '', lines: [] as string[], role: '', errors: c.errors, warnings: c.warnings, selected: props.selected === n.id }
  switch (n.kind) {
    case 'data':
      return { ...base, icon: (DATA_ICONS[String(n.type)] ?? Database) as BayesNodeView['icon'], what: dataWhat(n) }
    case 'parameter': {
      const bounds = bayesBoundsText(n.bounds as { lower?: number, upper?: number } | undefined, num)
      const role = props.model?.params.get(n.id)?.role === 'hyperprior' ? t('bayes.editor.node.hyperprior') : ''
      return { ...base, icon: CircleDot, role, lines: [priorLine(n.prior) + (bounds ? ` · ${bounds}` : '')] }
    }
    case 'transformed':
      return { ...base, icon: SquareFunction, lines: [slotText(n.value as BayesTemplateSlot)] }
    case 'linear_predictor': {
      const terms = ((n.terms ?? []) as Array<{ data: string, coef: BayesTemplateSlot }>).map(x => `${slotText(x.coef)} × ${nameOf(x.data)}`)
      const sum = [slotText(n.intercept as BayesTemplateSlot), ...terms].filter(Boolean).join(' + ')
      return { ...base, icon: Sigma, lines: [sum] }
    }
    case 'likelihood': {
      const order = bayesDistribution(String(n.dist))?.params.map(x => x.name) ?? []
      return { ...base, icon: ChartScatter, lines: [`${nameOf(String(n.observed ?? ''))} ~ ${bayesPriorText(String(n.dist), n.args ?? {}, order)}`] }
    }
    case 'state':
      return { ...base, icon: AudioWaveform, what: t('bayes.editor.node.randomWalk'), lines: [priorLine(n.init)] }
    case 'correlated_effects': {
      const lines = (n.components ?? []).map(x => `${x.name} · ${x.label}`)
      const eta = slotText(n.correlation?.eta)
      return { ...base, icon: Variable, what: t('bayes.editor.node.correlated', { eta }), lines }
    }
    default:
      return { ...base, icon: CircleDot }
  }
}

const nodeViews = computed(() => new Map(props.doc.nodes.map(n => [n.id, view(n)])))
const plateViews = computed(() => {
  const m = new Map<string, BayesPlateView>()
  for (const p of props.doc.plates) {
    if (p.kind !== 'rows' && p.kind !== 'group') continue
    const c = counts.value.get(p.id) ?? { errors: 0, warnings: 0 }
    m.set(p.id, {
      id: p.id,
      kind: p.kind,
      label: p.label,
      keyName: p.kind === 'group' ? nameOf(String(p.key ?? '')) : '',
      nonCentred: (p.form ?? 'non_centred') !== 'centred',
      series: !!p.series,
      errors: c.errors,
      warnings: c.warnings,
      selected: props.selected === p.id,
      nested: !!p.parent,
    })
  }
  return m
})

provide(BAYES_CANVAS_VIEW, {
  node: id => nodeViews.value.get(id),
  plate: id => plateViews.value.get(id),
  setForm: (plate, nonCentred) => emit('form', plate, nonCentred),
})

// ---------------------------------------------------------------- the drawing

// The nodes are placed once per document: the layout does not move in this lot, and what a node
// says is read from the views above, so typing never rebuilds them.
const nodes = ref(bayesCanvasNodes(props.doc))
watch(() => props.doc, (d) => { nodes.value = bayesCanvasNodes(d) })

const edges = computed(() => {
  const lines = bayesCanvasEdges(props.model?.edges ?? [])
  return lines.map(l => {
    const hi = props.selected !== null && (l.source === props.selected || l.target === props.selected)
    const key = l.kinds.every(k => k === 'index')
    return { id: l.id, source: l.source, target: l.target, sourceHandle: l.sourceHandle, targetHandle: l.targetHandle, selectable: false, focusable: false, class: [hi ? 'hi' : '', key ? 'key' : ''].filter(Boolean).join(' ') }
  })
})

// The model is fitted whole when it opens, never above 1. When the panel widens for a chart the view
// stays where it is (as the approved mockup draws it): the node just clicked does not move under the
// pointer, and the rest is a pan away or in the minimap.
const fit = () => fitView({ padding: 0.08, maxZoom: 1 })
onNodesInitialized(() => { fit() })
const zoomIn = () => vfZoomIn()
const zoomOut = () => vfZoomOut()
const zoomText = computed(() => `${Math.round(viewport.value.zoom * 100)} %`)

// ---------------------------------------------------------------- selection

onNodeClick(({ node }) => emit('select', node.id))
onPaneClick(() => emit('select', null))
// Tab then Enter selects through Vue Flow's own keys: its selection is followed as a click is
watch(() => (getSelectedNodes.value.length === 1 ? getSelectedNodes.value[0]!.id : null), (id) => {
  if (id !== null && id !== props.selected) emit('select', id)
})
watch(() => props.selected, (id) => {
  const now = getSelectedNodes.value.map(n => n.id)
  if (id === null ? now.length === 0 : now.length === 1 && now[0] === id) return
  removeSelectedNodes(getSelectedNodes.value)
  const n = id ? findNode(id) : undefined
  if (n) addSelectedNodes([n])
})

/** Puts the focus on a node of the canvas (the inspector's Esc, Alt with an arrow). */
function focusNode (id: string): void { focusNodeOf(root.value, id) }
defineExpose({ focusNode, fit })

const DIRS: Record<string, BayesDirection> = { ArrowLeft: 'left', ArrowRight: 'right', ArrowUp: 'up', ArrowDown: 'down' }
function rectOf (id: string) {
  const b = bayesBoxes(props.doc).find(x => x.id === id)
  if (b) return b
  const at = props.doc.layout[id]
  return at ? { x: at.x, y: at.y, w: BAYES_NODE_W, h: BAYES_NODE_H } : null
}
function typing (el: EventTarget | null): boolean {
  const e = el as HTMLElement | null
  return !!e?.closest?.('input, textarea, select, [contenteditable="true"], .nokey')
}
function onKeys (e: KeyboardEvent) {
  // the canvas' keys work with the focus on the canvas or on nothing, never while typing or with a dialog open
  if (e.defaultPrevented || typing(e.target) || e.ctrlKey || e.metaKey) return
  if (!focusOnCanvas(document.activeElement, root.value, document.body) || document.querySelector('[role="dialog"]')) return
  const dir = DIRS[e.key]
  if (dir && e.altKey && !e.shiftKey) {
    // Alt with an arrow walks to the nearest node in that direction, from the one selected
    const from = props.selected ?? props.doc.nodes[0]?.id
    const r = from ? rectOf(from) : null
    if (!from || !r) return
    const others = props.doc.nodes.filter(n => n.id !== from).map(n => ({ id: n.id, rect: rectOf(n.id)! })).filter(o => o.rect)
    const next = bayesNearestNode(r, others, dir)
    e.preventDefault()
    if (next) { emit('select', next); focusNode(next) }
    return
  }
  if (e.altKey) return
  if (e.key === '+' || e.key === '=') { e.preventDefault(); zoomIn() } else if (e.key === '-') { e.preventDefault(); zoomOut() } else if (e.key === '0') { e.preventDefault(); fit() }
}

onMounted(() => window.addEventListener('keydown', onKeys))
onBeforeUnmount(() => window.removeEventListener('keydown', onKeys))

const miniClass = (n: { type?: string }) => (n.type === 'plate' ? 'mm-plate' : 'mm-node')
</script>

<template>
  <div ref="root" class="bayes-canvas">
    <VueFlow
      :id="FLOW_ID"
      v-model:nodes="nodes"
      :edges="edges"
      :nodes-draggable="false"
      :nodes-connectable="false"
      :elements-selectable="true"
      :delete-key-code="null"
      :selection-key-code="null"
      :multi-selection-key-code="null"
      :zoom-on-double-click="false"
      :min-zoom="0.2"
      :max-zoom="2"
      :aria-label="t('bayes.editor.canvas.label')"
    >
      <template #node-bayes="p">
        <BayesNode :id="p.id" />
      </template>
      <template #node-plate="p">
        <BayesPlate :id="p.id" />
      </template>
      <Background :gap="17" :size="1.1" />
      <Controls :show-interactive="false" :show-zoom="false" :show-fit-view="false" position="bottom-left">
        <ControlButton :title="t('canvasKeys.zoomIn')" :aria-label="t('canvasKeys.zoomIn')" @click="zoomIn()"><Plus :size="13" /></ControlButton>
        <ControlButton :title="t('canvasKeys.zoomOut')" :aria-label="t('canvasKeys.zoomOut')" @click="zoomOut()"><Minus :size="13" /></ControlButton>
        <ControlButton :title="t('canvasKeys.fitView')" :aria-label="t('canvasKeys.fitView')" @click="fit()"><Maximize :size="13" /></ControlButton>
      </Controls>
      <MiniMap
        position="bottom-right"
        :width="168"
        :height="104"
        :node-class-name="miniClass"
        :node-border-radius="3"
        mask-color="color-mix(in srgb, var(--bg) 72%, transparent)"
        mask-stroke-color="var(--accent)"
        :mask-stroke-width="1"
        :aria-label="t('bayes.editor.canvas.miniMap')"
      />
    </VueFlow>
    <span class="zoomlbl" aria-hidden="true">{{ zoomText }}</span>
  </div>
</template>

<style scoped>
.bayes-canvas {
  position: relative; width: 100%; height: 100%; min-width: 0;
  background: radial-gradient(1200px 600px at 30% 0%, color-mix(in srgb, var(--accent) 5%, transparent), transparent 60%), var(--bg);
}
.zoomlbl { position: absolute; left: 48px; bottom: 16px; z-index: 6; font-size: 11px; color: var(--muted); font-variant-numeric: tabular-nums; pointer-events: none; }
/* Vue Flow's pieces in the app's tokens: the pattern from --border-soft (never a literal), the
   minimap's nodes neutral and its boxes outlined */
.bayes-canvas :deep(.vue-flow__background pattern circle) { fill: var(--border-soft); }
.bayes-canvas :deep(.vue-flow__minimap) { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; box-shadow: var(--shadow-1); overflow: hidden; }
.bayes-canvas :deep(.vue-flow__minimap-node.mm-node) { fill: var(--panel-2); stroke: var(--edge); stroke-width: 1; }
.bayes-canvas :deep(.vue-flow__minimap-node.mm-plate) { fill: none; stroke: var(--edge); stroke-width: 1; }
.bayes-canvas :deep(.vue-flow__node-plate),
.bayes-canvas :deep(.vue-flow__node-bayes) { padding: 0; border: 0; background: none; box-shadow: none; border-radius: 10px; }
.bayes-canvas :deep(.vue-flow__node-bayes) { border-radius: 6px; }
.bayes-canvas :deep(.vue-flow__node:focus-visible) { outline: 2px solid var(--accent); outline-offset: 3px; }
.bayes-canvas :deep(.vue-flow__node-plate:focus-visible) { outline-offset: 1px; }
.bayes-canvas :deep(.vue-flow__edge.hi .vue-flow__edge-path) { stroke: var(--accent); stroke-width: 1.8; }
.bayes-canvas :deep(.vue-flow__edge.key .vue-flow__edge-path) { stroke: var(--muted); stroke-width: 1.2; stroke-dasharray: 2 4; }
.bayes-canvas :deep(.vue-flow__edge.key.hi .vue-flow__edge-path) { stroke: var(--accent); }
.bayes-canvas :deep(.vue-flow__handle) { width: 9px !important; height: 9px !important; cursor: default; }
</style>
