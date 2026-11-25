<script setup lang="ts">
// The canvas of a Bayesian model (docs/specs/bayes-graph-editor.md, «The canvas»): a second Vue Flow
// canvas, never FlowEditor.vue itself. The graph document is the truth and this draws it: boxes as
// parent nodes nested by `parent`, nodes in the box of their plate, the mirror's edges left to right
// (a source's right handle to its target's left one).
//
// Drawing (lot G1): a node or a box is dragged, and where it is dropped decides its box (the deepest
// box under its centre, under the middle of a box's header); a box is resized by its edges; a kind is
// dropped from the palette; an edge is drawn from a node's right handle to another node, and a menu
// lists the target's slots the mirror accepts it in; an edge selected is removed with Delete. Every
// gesture is told to the page, which changes the graph (one step of the history); this component
// only draws what the graph then holds. The keyboard reaches all of it: Shift with an arrow moves,
// `m` opens a node's commands (link it, remove a link, duplicate, remove), Delete removes.
import { computed, nextTick, onBeforeUnmount, onMounted, provide, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { VueFlow, useVueFlow, type Connection } from '@vue-flow/core'
import { Background } from '@vue-flow/background'
import { Controls, ControlButton } from '@vue-flow/controls'
import { MiniMap } from '@vue-flow/minimap'
import { AudioWaveform, Cable, ChartScatter, CircleDot, Clock, Copy, Database, Hash, List, Maximize, Minus, PanelRight, Plus, Sigma, SquareFunction, Trash2, Unlink, Users, Variable } from 'lucide-vue-next'
import BayesNode from '~/components/bayes/BayesNode.vue'
import BayesPlate from '~/components/bayes/BayesPlate.vue'
import RowMenu, { type RowMenuItem } from '~/components/ui/RowMenu.vue'
import { bayesBoxes, bayesCanvasEdges, bayesCanvasNodes, bayesDropPlate, bayesFitViewport, bayesNearestNode, BAYES_NODE_H, BAYES_NODE_W, type BayesRect } from '~/composables/bayesCanvasModel'
import { BAYES_CANVAS_VIEW, type BayesNodeView, type BayesPlateView, type BayesResizeParams } from '~/composables/bayesCanvasView'
import { bayesBoundsText, bayesPriorText } from '~/composables/bayesEditorText'
import { bayesEdgeChoices, bayesDropTarget, bayesIsBox, type BayesEdgeChoice, type BayesPaletteKind } from '~/composables/bayesGraphEdits'
import { bayesDistribution } from '~/composables/bayesCatalog'
import { bayesArrowDirection, bayesKeyAction, bayesKeyScope, bayesMoveStep } from '~/composables/bayesEditorKeys'
import { platformIsMac } from '~/composables/appKeys'
import { focusNodeOf, focusOnCanvas } from '~/composables/nodeKeys'
import type { BayesModel, BayesGraphFinding } from '~/composables/bayesGraph'
import type { BayesTemplateGraph, BayesTemplateNode, BayesTemplateSlot } from '~/composables/bayesTemplates'

const props = defineProps<{
  doc: BayesTemplateGraph
  model: BayesModel | null
  findings: BayesGraphFinding[]
  selected: string | null
  /** Bumped when the whole document is replaced (opened, undone, redone): the canvas draws it anew. */
  restored: number
  /** A version looked at: nothing is dragged, linked, added or removed; the view still moves. */
  readonly?: boolean
}>()
const emit = defineEmits<{
  (e: 'select', id: string | null): void
  (e: 'form', plate: string, nonCentred: boolean): void
  (e: 'series', on: boolean): void
  /** A drag that stopped: the item's new rectangle (canvas coordinates); its box follows. */
  (e: 'drop', id: string, rect: BayesRect): void
  /** Moved from the keyboard: the new top left corner, the box unchanged. */
  (e: 'move', id: string, at: { x: number, y: number }): void
  (e: 'resize', id: string, rect: BayesRect): void
  /** A kind of the palette dropped at `at` (its top left corner) in `plate`. */
  (e: 'add', kind: BayesPaletteKind, at: { x: number, y: number }, plate: string | null): void
  (e: 'connect', target: string, choice: BayesEdgeChoice): void
  (e: 'disconnect', source: string, target: string): void
  (e: 'remove', id: string): void
  (e: 'duplicate', id: string): void
  (e: 'inspect', id: string): void
  (e: 'palette'): void
}>()
const { t, locale } = useI18n()

const FLOW_ID = 'bayes-canvas'
const root = ref<HTMLElement | null>(null)
const {
  fitView, zoomIn: vfZoomIn, zoomOut: vfZoomOut, viewport, setViewport, addSelectedNodes, removeSelectedNodes, getSelectedNodes,
  getSelectedEdges, removeSelectedEdges, onNodeClick, onPaneClick, onNodesInitialized, findNode, getNodes, onNodeDrag, onNodeDragStop,
  onConnect, onConnectStart, onConnectEnd, onEdgeClick, onNodeContextMenu, screenToFlowCoordinate, onMoveEnd,
} = useVueFlow(FLOW_ID)

// ---------------------------------------------------------------- what each node says

const owner = (id: string): string => { const i = id.indexOf('.'); return i < 0 ? id : id.slice(0, i) }
const num = (v: number): string => new Intl.NumberFormat(locale.value, { maximumFractionDigits: 6 }).format(v)
const slotText = (s: BayesTemplateSlot | undefined): string => (s?.expr ?? s?.ref ?? '').trim()
const byId = computed(() => new Map(props.doc.nodes.map(n => [n.id, n])))
const plateLabel = (id: string): string => props.doc.plates.find(p => p.id === id)?.label ?? id
const nameOf = (id: string): string => (byId.value.get(id)?.name as string | undefined) ?? id
const labelOf = (id: string): string => byId.value.get(id)?.label ?? props.doc.plates.find(p => p.id === id)?.label ?? id
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
  else if (type === 'group_index') parts.push(t('bayes.editor.dataType.group_index', { plate: n.target ? plateLabel(String(n.target)) : '…' }))
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
      return { ...base, icon: SquareFunction, lines: [slotText(n.value as BayesTemplateSlot) || '…'] }
    case 'linear_predictor': {
      const terms = ((n.terms ?? []) as Array<{ data: string, coef: BayesTemplateSlot }>).map(x => `${slotText(x.coef) || '…'} × ${nameOf(x.data)}`)
      const sum = [slotText(n.intercept as BayesTemplateSlot) || '…', ...terms].join(' + ')
      return { ...base, icon: Sigma, lines: [sum] }
    }
    case 'likelihood': {
      const order = bayesDistribution(String(n.dist))?.params.map(x => x.name) ?? []
      return { ...base, icon: ChartScatter, lines: [`${n.observed ? nameOf(String(n.observed)) : '…'} ~ ${bayesPriorText(String(n.dist), n.args ?? {}, order)}`] }
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

/** The box a dragged item would land in, when it is not the one it is in (drawn as a drop target). */
const dropHint = ref<string | null>(null)

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
      keyName: p.kind === 'group' && p.key ? nameOf(String(p.key)) : '',
      nonCentred: (p.form ?? 'non_centred') !== 'centred',
      series: !!p.series,
      errors: c.errors,
      warnings: c.warnings,
      selected: props.selected === p.id,
      nested: !!p.parent,
      target: dropHint.value === p.id,
    })
  }
  return m
})

// ---------------------------------------------------------------- resizing a box

// Vue Flow's resizer moves a box's corner by its position, and a child's place is relative to its
// parent: resized from its left or top edge, the box would carry what it holds along. Each step of
// the resize moves the children back by the same amount, so they stay where they are on the canvas.
const resizeFrom = new Map<string, { x: number, y: number }>()
function resizing (id: string, p: BayesResizeParams) {
  const before = resizeFrom.get(id) ?? (() => { const n = findNode(id); return n ? { ...n.position } : { x: p.x, y: p.y } })()
  const dx = p.x - before.x
  const dy = p.y - before.y
  if (dx || dy) {
    for (const child of getNodes.value) {
      if (child.parentNode === id) child.position = { x: child.position.x - dx, y: child.position.y - dy }
    }
  }
  resizeFrom.set(id, { x: p.x, y: p.y })
}
function resized (id: string, p: BayesResizeParams) {
  resizeFrom.delete(id)
  const n = findNode(id)
  const parent = n?.parentNode ? findNode(n.parentNode) : undefined
  const px = parent?.computedPosition.x ?? 0
  const py = parent?.computedPosition.y ?? 0
  emit('resize', id, { x: px + p.x, y: py + p.y, w: p.width, h: p.height })
}

provide(BAYES_CANVAS_VIEW, {
  readonly: () => !!props.readonly,
  node: id => nodeViews.value.get(id),
  plate: id => plateViews.value.get(id),
  setForm: (plate, nonCentred) => emit('form', plate, nonCentred),
  setSeries: on => emit('series', on),
  resizing,
  resized,
})

// ---------------------------------------------------------------- the drawing

// The nodes are placed again whenever the drawing changes (a place, a box, a box's parent, a
// likelihood's observed node) or the whole document is replaced; what a node says is read from the
// views above, so typing never rebuilds them.
const shape = computed(() => JSON.stringify([
  props.restored,
  props.doc.layout,
  props.doc.plates.map(p => [p.id, p.kind, p.parent ?? null]),
  props.doc.nodes.map(n => [n.id, n.kind, n.plate ?? null, n.observed ?? null]),
]))
const draw = () => bayesCanvasNodes(props.doc).map(n => ({ ...n, selected: n.id === props.selected }))
const nodes = ref(draw())
watch(shape, () => { nodes.value = draw() })

const selectedEdge = ref<string | null>(null)
const edges = computed(() => {
  const lines = bayesCanvasEdges(props.model?.edges ?? [])
  return lines.map(l => {
    const hi = (props.selected !== null && (l.source === props.selected || l.target === props.selected)) || selectedEdge.value === l.id
    const key = l.kinds.every(k => k === 'index')
    return {
      id: l.id,
      source: l.source,
      target: l.target,
      sourceHandle: l.sourceHandle,
      targetHandle: l.targetHandle,
      selectable: true,
      focusable: true,
      ariaLabel: t('bayes.editor.edge.label', { from: labelOf(l.source), to: labelOf(l.target) }),
      class: [hi ? 'hi' : '', key ? 'key' : '', selectedEdge.value === l.id ? 'sel' : ''].filter(Boolean).join(' '),
    }
  })
})

// The model is fitted whole when it opens, never above 1 and never under a zoom whose words can be
// read (bayesFitViewport: a larger model shows its left column and the rest scrolls). While the view stays as fitted, a change
// of the canvas' size (the panel widening for a chart, the panel's handle) fits it again, so nothing
// stays under the panel; once the person has panned or zoomed, the view keeps its centre and brings
// the selected node back into sight.
let fittedAt: { x: number, y: number, zoom: number } | null = null
const fit = async () => {
  const all = getNodes.value.filter(n => !n.hidden)
  const w = root.value?.clientWidth ?? 0
  const h = root.value?.clientHeight ?? 0
  if (!all.length || !w || !h) { await fitView({ padding: 0.08, maxZoom: 1 }); fittedAt = { ...viewport.value }; return }
  let x0 = Infinity; let y0 = Infinity; let x1 = -Infinity; let y1 = -Infinity
  for (const n of all) {
    const nw = n.dimensions.width || BAYES_NODE_W
    const nh = n.dimensions.height || BAYES_NODE_H
    x0 = Math.min(x0, n.computedPosition.x); y0 = Math.min(y0, n.computedPosition.y)
    x1 = Math.max(x1, n.computedPosition.x + nw); y1 = Math.max(y1, n.computedPosition.y + nh)
  }
  await setViewport(bayesFitViewport({ x: x0, y: y0, w: x1 - x0, h: y1 - y0 }, w, h, { padding: 0.08, maxZoom: 1 }))
  fittedAt = { ...viewport.value }
}
const stillFitted = () => !!fittedAt && Math.abs(fittedAt.x - viewport.value.x) < 1 && Math.abs(fittedAt.y - viewport.value.y) < 1 && Math.abs(fittedAt.zoom - viewport.value.zoom) < 1e-3
onMoveEnd(() => { if (!stillFitted()) fittedAt = null })
// fitted once when the model opens (refit: another model opened in its place), never after a drop
let fittedOnce = false
onNodesInitialized(() => { if (!fittedOnce) { fittedOnce = true; fit() } })
function refit () {
  fittedOnce = false
  setTimeout(() => { if (!fittedOnce) { fittedOnce = true; fit() } }, 150)
}
const zoomIn = () => vfZoomIn()
const zoomOut = () => vfZoomOut()
const zoomText = computed(() => `${Math.round(viewport.value.zoom * 100)} %`)

let lastWidth = 0
let observer: ResizeObserver | null = null
function onCanvasResize () {
  const w = root.value?.clientWidth ?? 0
  const dw = w - lastWidth
  const was = lastWidth
  lastWidth = w
  if (!was || !dw) return
  if (stillFitted()) { fit(); return }
  // keep the centre: the content moves by half the change
  const vp = viewport.value
  setViewport({ x: vp.x + dw / 2, y: vp.y, zoom: vp.zoom })
  nextTick(() => bringIntoView(props.selected))
}
/** Pans the least that brings a node into sight (a node added, the selected node under a wider panel). */
function bringIntoView (id: string | null) {
  const n = id ? findNode(id) : undefined
  if (!n || !root.value) return
  const vp = viewport.value
  const shift = (lo: number, hi: number, size: number, margin: number) => (hi > size - margin ? -(hi - size + margin) : lo < margin ? margin - lo : 0)
  const left = n.computedPosition.x * vp.zoom + vp.x
  const top = n.computedPosition.y * vp.zoom + vp.y
  const dx = shift(left, left + (n.dimensions.width || BAYES_NODE_W) * vp.zoom, root.value.clientWidth, 24)
  // the bottom keeps room for the minimap and the zoom buttons
  const dy = shift(top, top + (n.dimensions.height || BAYES_NODE_H) * vp.zoom, root.value.clientHeight, 130)
  if (dx || dy) setViewport({ x: vp.x + dx, y: vp.y + dy, zoom: vp.zoom })
}

// ---------------------------------------------------------------- selection

onNodeClick(({ node }) => { selectedEdge.value = null; emit('select', node.id) })
onPaneClick(() => { selectedEdge.value = null; menu.value = null; emit('select', null) })
onEdgeClick(({ edge }) => {
  selectedEdge.value = edge.id
  if (props.selected !== null) emit('select', null)
})
// Tab then Enter selects through Vue Flow's own keys: its selection is followed as a click is
watch(() => (getSelectedNodes.value.length === 1 ? getSelectedNodes.value[0]!.id : null), (id) => {
  if (id !== null && id !== props.selected) { selectedEdge.value = null; emit('select', id) }
})
watch(() => (getSelectedEdges.value.length === 1 ? getSelectedEdges.value[0]!.id : null), (id) => {
  if (id !== null && id !== selectedEdge.value) { selectedEdge.value = id; if (props.selected !== null) emit('select', null) }
})
watch(() => props.selected, (id) => {
  if (id !== null) { selectedEdge.value = null; removeSelectedEdges(getSelectedEdges.value) }
  const now = getSelectedNodes.value.map(n => n.id)
  if (id === null ? now.length === 0 : now.length === 1 && now[0] === id) return
  removeSelectedNodes(getSelectedNodes.value)
  const n = id ? findNode(id) : undefined
  if (n) addSelectedNodes([n])
})

/** Puts the focus on a node of the canvas (the inspector's Esc, Alt with an arrow, a node just added). */
function focusNode (id: string): void {
  if (!focusNodeOf(root.value, id)) {
    // a node Vue Flow has just added is drawn hidden until measured: try again on the next frames
    let tries = 0
    const again = () => { if (!focusNodeOf(root.value, id) && ++tries < 10) requestAnimationFrame(again) }
    requestAnimationFrame(again)
  }
}

// ---------------------------------------------------------------- dragging and dropping

function rectOfFlowNode (id: string): BayesRect | null {
  const n = findNode(id)
  if (!n) return null
  const box = bayesIsBox(props.doc, id)
  return {
    x: n.computedPosition.x,
    y: n.computedPosition.y,
    w: n.dimensions.width || (box ? 0 : BAYES_NODE_W),
    h: n.dimensions.height || (box ? 0 : BAYES_NODE_H),
  }
}
function currentBox (id: string): string | null {
  const p = props.doc.plates.find(x => x.id === id)
  if (p) return typeof p.parent === 'string' ? p.parent : null
  const n = byId.value.get(id)
  return typeof n?.plate === 'string' ? n.plate : null
}
onNodeDrag(({ node }) => {
  const r = rectOfFlowNode(node.id)
  if (!r || byId.value.get(node.id)?.kind === 'likelihood') { dropHint.value = null; return }
  const target = bayesDropTarget(props.doc, node.id, r)
  dropHint.value = target !== currentBox(node.id) ? target : null
})
onNodeDragStop(({ node }) => {
  dropHint.value = null
  const r = rectOfFlowNode(node.id)
  if (r) emit('drop', node.id, r)
})

// a kind dragged from the palette: the box under the pointer lights up, the drop adds it there
const PALETTE_TYPE = 'application/x-bayes-kind'
function paletteDrag (e: DragEvent): boolean { return !!e.dataTransfer?.types.includes(PALETTE_TYPE) }
function onDragOver (e: DragEvent) {
  if (!paletteDrag(e) || props.readonly) return
  e.preventDefault()
  if (e.dataTransfer) e.dataTransfer.dropEffect = 'copy'
  const p = screenToFlowCoordinate({ x: e.clientX, y: e.clientY })
  dropHint.value = bayesDropPlate(p, bayesBoxes(props.doc))
}
function onDragLeave (e: DragEvent) {
  if (!root.value?.contains(e.relatedTarget as Node | null)) dropHint.value = null
}
function onDrop (e: DragEvent) {
  if (!paletteDrag(e) || props.readonly) return
  e.preventDefault()
  dropHint.value = null
  const kind = e.dataTransfer!.getData(PALETTE_TYPE) as BayesPaletteKind
  if (!kind) return
  const p = screenToFlowCoordinate({ x: e.clientX, y: e.clientY })
  const boxes = bayesBoxes(props.doc)
  const plate = kind === 'group' ? bayesDropPlate(p, boxes.filter(b => b.kind === 'group')) : bayesDropPlate(p, boxes)
  // the item is centred on the pointer (a box by its header)
  const at = kind === 'group' ? { x: p.x - 140, y: p.y - 17 } : { x: p.x - BAYES_NODE_W / 2, y: p.y - 28 }
  emit('add', kind, at, plate)
}

// ---------------------------------------------------------------- menus: a node's commands, an edge's slot

interface CanvasMenu { x: number, y: number, label: string, items: RowMenuItem[], act: (id: string) => void, byKey: boolean }
const menu = ref<CanvasMenu | null>(null)
const menuOpen = ref(false)
async function showMenu (m: CanvasMenu) {
  menuOpen.value = false
  await nextTick()
  menu.value = m
  menuOpen.value = true
}
function closeMenu () { menuOpen.value = false }
function onMenuSelect (id: string) {
  const m = menu.value
  menuOpen.value = false
  m?.act(id)
}

/** Where a menu opens beside a node: at its right edge, or at its left one for an edge's target. */
function anchorOf (id: string, side: 'left' | 'right'): { x: number, y: number } {
  const el = root.value?.querySelector(`.vue-flow__node[data-id="${CSS.escape(id)}"]`)
  const r = el?.getBoundingClientRect()
  if (!r) return { x: window.innerWidth / 2, y: window.innerHeight / 2 }
  return side === 'right' ? { x: r.right + 6, y: r.top } : { x: r.left + 6, y: r.top + r.height / 2 }
}

const slotLabel = (c: BayesEdgeChoice): string => {
  const what = t(`bayes.editor.slot.${c.slot.words.key}`, c.slot.words.params)
  const value = c.shown
  const now = c.slot.text ? t('bayes.editor.menu.now', { text: c.slot.text }) : t('bayes.editor.menu.nowEmpty')
  return `${what} ← ${value} · ${now}`
}

/** The slots of `target` that can take `source`, as a menu (an edge drawn by hand, or chosen from the keyboard). */
function openSlotMenu (source: string, target: string, byKey: boolean) {
  const choices = bayesEdgeChoices(props.doc, source, target)
  const at = anchorOf(target, 'left')
  const src = labelOf(source)
  const items: RowMenuItem[] = choices.length
    ? choices.map((c, i) => ({ id: String(i), label: slotLabel(c), icon: Cable }))
    : [{ id: 'none', label: t('bayes.editor.menu.noSlot', { label: labelOf(target), name: src }), disabled: true }]
  showMenu({
    ...at,
    byKey,
    label: t('bayes.editor.menu.chooseSlot', { name: src, label: labelOf(target) }),
    items,
    act: (id) => {
      const c = choices[Number(id)]
      if (c) emit('connect', target, c)
    },
  })
}

/** A node's (or a box's) commands: `m`, Shift+F10, the context menu key, a right click. */
function openNodeMenu (id: string, byKey: boolean, at?: { x: number, y: number }) {
  const box = bayesIsBox(props.doc, id)
  const n = byId.value.get(id)
  const items: RowMenuItem[] = [{ id: 'inspect', label: t('bayes.editor.menu.inspect'), icon: PanelRight }]
  if (n && n.kind !== 'likelihood') items.push({ id: 'connect', label: t('bayes.editor.menu.connect'), icon: Cable })
  if (n) items.push({ id: 'links', label: t('bayes.editor.menu.links'), icon: Unlink })
  if (n) items.push({ id: 'duplicate', label: t('bayes.editor.menu.duplicate'), icon: Copy })
  const rows = box && props.doc.plates.find(p => p.id === id)?.kind === 'rows'
  items.push({ id: 'remove', label: t('bayes.editor.menu.remove'), icon: Trash2, danger: true, disabled: rows, sep: true })
  showMenu({
    ...(at ?? anchorOf(id, 'right')),
    byKey,
    label: t('bayes.editor.menu.node', { label: labelOf(id) }),
    items,
    act: (cmd) => {
      if (cmd === 'inspect') emit('inspect', id)
      else if (cmd === 'connect') openTargetMenu(id, byKey)
      else if (cmd === 'links') openLinksMenu(id, byKey)
      else if (cmd === 'duplicate') emit('duplicate', id)
      else if (cmd === 'remove') emit('remove', id)
    },
  })
}

/** «Link to another node…»: the nodes that may take this one, then the slot. */
function openTargetMenu (source: string, byKey: boolean) {
  const targets = props.doc.nodes.filter(n => n.id !== source && n.kind !== 'data')
  showMenu({
    ...anchorOf(source, 'right'),
    byKey,
    label: t('bayes.editor.menu.chooseTarget', { name: labelOf(source) }),
    items: targets.length
      ? targets.map(n => ({ id: n.id, label: n.name ? `${n.label} · ${n.name}` : n.label }))
      : [{ id: 'none', label: t('bayes.editor.menu.noTarget'), disabled: true }],
    act: (id) => { if (id !== 'none') openSlotMenu(source, id, byKey) },
  })
}

/** «Remove a link…»: the lines into and out of this node. */
function openLinksMenu (id: string, byKey: boolean) {
  const lines = bayesCanvasEdges(props.model?.edges ?? []).filter(l => l.source === id || l.target === id)
  showMenu({
    ...anchorOf(id, 'right'),
    byKey,
    label: t('bayes.editor.menu.links'),
    items: lines.length
      ? lines.map(l => ({ id: l.id, label: l.source === id ? t('bayes.editor.menu.linkTo', { label: labelOf(l.target) }) : t('bayes.editor.menu.linkFrom', { label: labelOf(l.source) }), icon: Unlink }))
      : [{ id: 'none', label: t('bayes.editor.menu.noLinks'), disabled: true }],
    act: (lid) => {
      const l = lines.find(x => x.id === lid)
      if (l) emit('disconnect', l.source, l.target)
    },
  })
}

// An edge drawn by hand: from a source handle to another node's handle, or dropped on its body.
let connectFrom: string | null = null
let connected = false
onConnectStart(({ nodeId }) => { connectFrom = nodeId ?? null; connected = false })
onConnect((c: Connection) => {
  connected = true
  if (c.source && c.target && c.source !== c.target) openSlotMenu(c.source, c.target, false)
})
onConnectEnd((e) => {
  const from = connectFrom
  connectFrom = null
  if (connected || !from || !e) return
  const p = 'clientX' in e ? { x: e.clientX, y: e.clientY } : 'changedTouches' in e && e.changedTouches[0] ? { x: e.changedTouches[0].clientX, y: e.changedTouches[0].clientY } : null
  if (!p) return
  const el = document.elementFromPoint(p.x, p.y)?.closest('.vue-flow__node-bayes')
  const target = el?.getAttribute('data-id')
  if (target && target !== from) openSlotMenu(from, target, false)
})
onNodeContextMenu(({ event, node }) => {
  const e = event as MouseEvent
  e.preventDefault()
  if (props.readonly) return
  emit('select', node.id)
  openNodeMenu(node.id, false, { x: e.clientX, y: e.clientY })
})

/** The centre of what the canvas shows, in canvas coordinates (where a node added by the keyboard goes). */
function viewCentre (): { x: number, y: number } {
  const r = root.value?.getBoundingClientRect()
  if (!r) return { x: 0, y: 0 }
  return screenToFlowCoordinate({ x: r.left + r.width / 2, y: r.top + r.height / 2 })
}

defineExpose({ focusNode, fit, refit, openNodeMenu, viewCentre, bringIntoView })

// ---------------------------------------------------------------- the keys

function rectOf (id: string) {
  const b = bayesBoxes(props.doc).find(x => x.id === id)
  if (b) return b
  const at = props.doc.layout[id]
  return at ? { x: at.x, y: at.y, w: BAYES_NODE_W, h: BAYES_NODE_H } : null
}
function typing (el: EventTarget | null): boolean {
  const e = el as HTMLElement | null
  return !!e?.closest?.('input, textarea, select, [contenteditable="true"], .nokey, [role="combobox"]')
}
function focusedEdge (): string | null {
  const el = (document.activeElement as HTMLElement | null)?.closest?.('.vue-flow__edge')
  return el?.getAttribute('data-id') ?? null
}
// On a node, before Vue Flow sees the key: Vue Flow's own keyboard moves a selected node by a few
// pixels on a bare arrow, behind the graph's back. Here a bare arrow moves nothing, Shift with an
// arrow moves the node (one step of the history), Alt with an arrow walks (as FlowEditor.vue does).
function onArrowCapture (e: KeyboardEvent) {
  if (!bayesArrowDirection(e.key) || typing(e.target) || !(e.target as HTMLElement | null)?.closest?.('.vue-flow__node')) return
  e.stopPropagation()
  e.preventDefault()
  if (!e.ctrlKey && !e.metaKey) doKey(e)
}
function onKeys (e: KeyboardEvent) {
  // the canvas' keys work with the focus on the canvas or on nothing, never while typing or with a dialog or a menu open
  if (e.defaultPrevented || typing(e.target) || e.ctrlKey || e.metaKey) return
  if (!focusOnCanvas(document.activeElement, root.value, document.body) || document.querySelector('[role="dialog"], [role="menu"], .sel-panel')) return
  doKey(e)
}
// What a key asks is bayesEditorKeys' to say; this does it with what the canvas holds.
function doKey (e: KeyboardEvent) {
  const act = bayesKeyAction(e, platformIsMac())
  if (!act || bayesKeyScope(act.kind) !== 'canvas') return
  // a version looked at: the view moves and the nodes are walked, nothing changes
  if (props.readonly && (act.kind === 'move' || act.kind === 'remove' || act.kind === 'menu' || act.kind === 'add')) return
  if (act.kind === 'walk') {
    // Alt with an arrow walks to the nearest node in that direction, from the one selected
    const from = props.selected ?? props.doc.nodes[0]?.id
    const r = from ? rectOf(from) : null
    if (!from || !r) return
    const others = props.doc.nodes.filter(n => n.id !== from).map(n => ({ id: n.id, rect: rectOf(n.id)! })).filter(o => o.rect)
    const next = bayesNearestNode(r, others, act.dir)
    e.preventDefault()
    if (next) { emit('select', next); focusNode(next) }
  } else if (act.kind === 'move') {
    // Shift with an arrow moves the selected node or box 10 px; its box changes only by the inspector
    if (!props.selected) return
    const r = rectOf(props.selected)
    if (!r) return
    e.preventDefault()
    const [dx, dy] = bayesMoveStep(act.dir)
    emit('move', props.selected, { x: r.x + dx, y: r.y + dy })
  } else if (act.kind === 'remove') {
    const edge = selectedEdge.value ?? focusedEdge()
    const line = edge ? edges.value.find(x => x.id === edge) : undefined
    if (line) { e.preventDefault(); emit('disconnect', line.source, line.target); return }
    if (props.selected) { e.preventDefault(); emit('remove', props.selected) }
  } else if (act.kind === 'menu') {
    if (!props.selected) return
    e.preventDefault()
    openNodeMenu(props.selected, true)
  } else if (act.kind === 'add') {
    e.preventDefault()
    emit('palette')
  } else if (act.kind === 'zoomIn') { e.preventDefault(); zoomIn() } else if (act.kind === 'zoomOut') { e.preventDefault(); zoomOut() } else if (act.kind === 'fit') { e.preventDefault(); fit() }
}

onMounted(() => {
  window.addEventListener('keydown', onKeys)
  root.value?.addEventListener('keydown', onArrowCapture, true)
  lastWidth = root.value?.clientWidth ?? 0
  if (typeof ResizeObserver !== 'undefined' && root.value) {
    observer = new ResizeObserver(() => onCanvasResize())
    observer.observe(root.value)
  }
})
onBeforeUnmount(() => { window.removeEventListener('keydown', onKeys); root.value?.removeEventListener('keydown', onArrowCapture, true); observer?.disconnect() })

const miniClass = (n: { type?: string }) => (n.type === 'plate' ? 'mm-plate' : 'mm-node')
</script>

<template>
  <div ref="root" class="bayes-canvas" @dragover="onDragOver" @dragleave="onDragLeave" @drop="onDrop">
    <VueFlow
      :id="FLOW_ID"
      v-model:nodes="nodes"
      :edges="edges"
      :nodes-draggable="!readonly"
      :nodes-connectable="!readonly"
      :elements-selectable="true"
      :elevate-nodes-on-select="false"
      :delete-key-code="null"
      :selection-key-code="null"
      :multi-selection-key-code="null"
      :zoom-on-double-click="false"
      :connection-radius="28"
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
    <RowMenu
      :open="menuOpen && !!menu"
      :x="menu?.x ?? 0"
      :y="menu?.y ?? 0"
      :items="menu?.items ?? []"
      :label="menu?.label"
      :by-key="menu?.byKey"
      @select="onMenuSelect"
      @close="closeMenu"
    />
  </div>
</template>

<style scoped>
.bayes-canvas {
  position: relative; width: 100%; height: 100%; min-width: 0;
  /* the app's --edge falls under 3:1 against the canvas in three themes; an edge is what the graph says, so it reads at 3:1 at least */
  --bayes-edge: color-mix(in srgb, var(--muted) 80%, var(--bg));
  background: radial-gradient(1200px 600px at 30% 0%, color-mix(in srgb, var(--accent) 5%, transparent), transparent 60%), var(--bg);
}
.zoomlbl { position: absolute; left: 48px; bottom: 16px; z-index: 6; font-size: 11px; color: var(--muted); font-variant-numeric: tabular-nums; pointer-events: none; }
/* Vue Flow's pieces in the app's tokens: the pattern from --border-soft (never a literal), the
   minimap's nodes neutral and its boxes outlined */
.bayes-canvas :deep(.vue-flow__background pattern circle) { fill: var(--border-soft); }
.bayes-canvas :deep(.vue-flow__minimap) { background: var(--panel); border: 1px solid var(--border); border-radius: var(--radius-control); box-shadow: var(--shadow-1); overflow: hidden; }
.bayes-canvas :deep(.vue-flow__minimap-node.mm-node) { fill: var(--panel-2); stroke: var(--edge); stroke-width: 1; }
.bayes-canvas :deep(.vue-flow__minimap-node.mm-plate) { fill: none; stroke: var(--edge); stroke-width: 1; }
.bayes-canvas :deep(.vue-flow__node-plate),
.bayes-canvas :deep(.vue-flow__node-bayes) { padding: 0; border: 0; background: none; box-shadow: none; border-radius: var(--radius); }
.bayes-canvas :deep(.vue-flow__node-bayes) { border-radius: var(--radius-small); }
.bayes-canvas :deep(.vue-flow__node:focus-visible) { outline: 2px solid var(--accent); outline-offset: 3px; }
.bayes-canvas :deep(.vue-flow__node-plate:focus-visible) { outline-offset: 1px; }
.bayes-canvas :deep(.vue-flow__edge .vue-flow__edge-path) { stroke: var(--bayes-edge); stroke-width: 1.4; transition: stroke var(--dur-hover); }
@media (hover: hover) and (pointer: fine) { .bayes-canvas :deep(.vue-flow__edge:hover .vue-flow__edge-path) { stroke: var(--accent); } }
.bayes-canvas :deep(.vue-flow__edge.hi .vue-flow__edge-path) { stroke: var(--accent); stroke-width: 1.8; }
.bayes-canvas :deep(.vue-flow__edge.sel .vue-flow__edge-path) { stroke: var(--accent); stroke-width: 2.6; }
.bayes-canvas :deep(.vue-flow__edge.key .vue-flow__edge-path) { stroke: var(--muted); stroke-width: 1.2; stroke-dasharray: 2 4; }
.bayes-canvas :deep(.vue-flow__edge.key.hi .vue-flow__edge-path) { stroke: var(--accent); }
.bayes-canvas :deep(.vue-flow__edge:focus-visible) { outline: none; }
.bayes-canvas :deep(.vue-flow__edge:focus-visible .vue-flow__edge-path) { stroke: var(--accent); stroke-width: 2.6; }
/* the line being drawn by hand, in accent */
.bayes-canvas :deep(.vue-flow__connection-path) { stroke: var(--accent); stroke-width: 1.8; stroke-dasharray: 5 4; }
.bayes-canvas :deep(.vue-flow__handle) { width: 9px !important; height: 9px !important; }
.bayes-canvas :deep(.vue-flow__handle-right) { cursor: crosshair; }
@media (hover: hover) and (pointer: fine) {
  .bayes-canvas :deep(.vue-flow__handle-right:hover) { transform: translate(50%, -50%) scale(1.35); }
}
.bayes-canvas :deep(.vue-flow__handle.connectingto), .bayes-canvas :deep(.vue-flow__handle.valid) { background: var(--accent); }
</style>
