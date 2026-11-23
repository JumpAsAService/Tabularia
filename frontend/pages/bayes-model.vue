<script setup lang="ts">
// The editor of a Bayesian model (docs/specs/bayes-graph-editor.md). /bayes-model?template=<id> opens
// a template on local data — the dialog asks what opening it asks (the outcome's typical value and
// spread, the AR order) — and /bayes-model?template=empty a model with only its rows' box; nothing is
// saved (the model store comes with lot G3), and the page is reached by its address only.
//
// Lot G0 drew the template and gave a parameter's prior its chart; lot G1 draws: the palette adds
// nodes and boxes, the canvas moves them into boxes and out, resizes and nests boxes, draws edges by
// hand and removes them, and every change is a step of the history (undo, redo). The side panel's
// width follows what it shows (600 px for a chart where the canvas keeps 560 px) until the person
// sets it with its handle (320 to 640 px).
//
// Lot G2 gives every kind of node its own fields in the inspector, the Problems tab (every finding,
// «Show in the graph») and the glossary; the side panel's tabs are a row of tabs `,` and `.` walk.
//
// The graph document is the truth: every edit changes it (useBayesEditor), and the mirror
// (bayesGraph.ts) reads it again at once — the findings on the nodes, the expressions' errors at
// their span, the prior plan the chart draws from.
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { Info, LayoutTemplate, Redo2, SlidersHorizontal, TriangleAlert, Undo2 } from 'lucide-vue-next'
import BrandMark from '~/components/ui/BrandMark.vue'
import GridKeys from '~/components/ui/GridKeys.vue'
import BayesCanvas from '~/components/bayes/BayesCanvas.vue'
import BayesGlossary from '~/components/bayes/BayesGlossary.vue'
import BayesInspector from '~/components/bayes/BayesInspector.vue'
import BayesPalette from '~/components/bayes/BayesPalette.vue'
import BayesProblems from '~/components/bayes/BayesProblems.vue'
import BayesTemplateDialog from '~/components/bayes/BayesTemplateDialog.vue'
import { bayesKeyAction, bayesKeyScope } from '~/composables/bayesEditorKeys'
import { useKeyGroups, useKeyHelp } from '~/composables/useKeyHelp'
import { useToast } from '~/composables/useToast'
import { useBayesEditor } from '~/composables/useBayesEditor'
import { bayesBoxes, BAYES_NODE_W, type BayesRect } from '~/composables/bayesCanvasModel'
import {
  bayesAddItem, bayesDisconnect, bayesDropItem, bayesDropTarget, bayesDuplicateNode, bayesFreeSpot, bayesIsBox, bayesMoveItem, bayesRemoveItem,
  bayesResizeBox, bayesSetForm, bayesSetKey, bayesSetLabel, bayesSetName, bayesNestInBox, bayesPlaceInBox, bayesSetSeries,
  bayesSetSeriesPart, bayesSetTarget, bayesWriteSlot, type BayesDoc, type BayesEdgeChoice, type BayesPaletteKind,
} from '~/composables/bayesGraphEdits'
import { bayesOpenTemplate, bayesTemplate, bayesTemplates, type BayesOpenOptions } from '~/composables/bayesTemplates'

const { t, te } = useI18n()
const route = useRoute()
const toast = useToast()

const EMPTY = 'empty'
const templateId = computed(() => (typeof route.query.template === 'string' ? route.query.template : ''))
const template = computed(() => (templateId.value && templateId.value !== EMPTY ? bayesTemplate(templateId.value) : undefined))
const families = computed(() => (['glm', 'hierarchical', 'series'] as const).map(f => ({ id: f, items: bayesTemplates().filter(x => x.family === f) })))

const editor = useBayesEditor()
const { doc, selected, model, findings, plan, canUndo, canRedo, restored } = editor
onBeforeUnmount(() => editor.dispose())

const dialogOpen = ref(false)
// the template as it was opened: its explanation of a node stands while the node is as it wrote it
const origin = ref<BayesDoc | null>(null)
function emptyModel (): BayesDoc {
  return { format_version: 1, plates: [{ id: 'rows', kind: 'rows', label: t('bayes.editor.newNode.rows') }], nodes: [], layout: { rows: { x: 260, y: 40, w: 760, h: 420 } } }
}
watch(templateId, (id) => {
  dialogOpen.value = !!template.value
  origin.value = null
  editor.load(id === EMPTY ? emptyModel() : null)
}, { immediate: true })

const texts = (key: string): string | undefined => (te(key) ? t(key) : undefined)
function openModel (options: BayesOpenOptions) {
  if (!template.value) return
  const opened = bayesOpenTemplate(template.value.id, options, texts)
  origin.value = JSON.parse(JSON.stringify(opened)) as BayesDoc
  editor.load(opened)
  dialogOpen.value = false
  canvas.value?.refit()
}
/** The template's `why.<node>` while the node's slots still equal the template's (the templates spec). */
function why (id: string): string | undefined {
  const key = template.value ? `bayes.templates.${template.value.id}.why.${id}` : ''
  if (!key || !te(key)) return undefined
  const strip = (n: object | undefined) => (n ? JSON.stringify({ ...n, label: '', note: '' }) : '')
  const was = origin.value?.nodes.find(n => n.id === id)
  return was && strip(was) === strip(nodeOf(id)) ? t(key) : undefined
}

const title = computed(() => (template.value ? t(`bayes.templates.${template.value.id}.title`) : templateId.value === EMPTY ? t('bayes.editor.start.emptyTitle') : t('bayes.editor.title')))
useHead({ title: computed(() => (template.value || templateId.value === EMPTY ? `${title.value} · ${t('bayes.editor.title')}` : t('bayes.editor.title'))) })

// ---------------------------------------------------------------- the edits

const canvas = ref<InstanceType<typeof BayesCanvas> | null>(null)
const palette = ref<InstanceType<typeof BayesPalette> | null>(null)
const nodeOf = (id: string) => doc.value?.nodes.find(n => n.id === id)
const labelOf = (id: string): string => nodeOf(id)?.label ?? doc.value?.plates.find(p => p.id === id)?.label ?? id

/** The label field of the inspector takes the focus (a node just added, «Open in the inspector»). */
async function focusInspector (id: string) {
  await nextTick()
  await nextTick()
  const el = document.getElementById(`bi-${id}-label`) as HTMLInputElement | null
  el?.focus()
  el?.select()
}
function backToCanvas () {
  if (selected.value) canvas.value?.focusNode(selected.value)
}

/**
 * A kind added: dropped from the palette (`at` and `plate` from the drop), or with Enter or a click
 * (in the selected box, or at the centre of the view). Selected, its inspector open on the label.
 */
function add (kind: BayesPaletteKind, at: { x: number, y: number } | null, plate: string | null) {
  const d = doc.value
  if (!d) return
  let place = at
  let box = plate
  if (!place) {
    const sel = selected.value && bayesIsBox(d, selected.value) ? selected.value : null
    box = kind === 'group' ? (sel && d.plates.find(p => p.id === sel)?.kind === 'group' ? sel : null) : sel
    const within = box ? bayesBoxes(d).find(b => b.id === box) ?? null : null
    const centre = canvas.value?.viewCentre() ?? { x: 0, y: 0 }
    place = bayesFreeSpot(d, { x: centre.x - 90, y: centre.y - 28 }, within)
    // at the centre of the view a node is in the box it lands in, as if dropped there
    if (!box && kind !== 'group') box = bayesDropTarget(d, '', { ...place, w: BAYES_NODE_W, h: 56 })
  }
  const name = t(`bayes.editor.palette.item.${kind}.name`)
  const id = editor.edit(g => bayesAddItem(g, kind, place!, box, {
    label: name,
    components: [t('bayes.editor.newNode.componentA'), t('bayes.editor.newNode.componentB')],
    correlation: t('bayes.editor.newNode.correlation'),
  }))
  if (!id) return
  selected.value = id
  tab.value = 'inspector'
  focusInspector(id)
  // in sight once Vue Flow has measured it
  setTimeout(() => canvas.value?.bringIntoView(id), 120)
}

function remove (id: string) {
  const r = editor.edit(g => bayesRemoveItem(g, id))
  if (r && !r.ok && r.reason === 'rows') { toast.info(t('bayes.editor.edge.rowsBox')); return }
  if (selected.value === id) selected.value = null
}
function duplicate (id: string) {
  const nid = editor.edit(g => bayesDuplicateNode(g, id))
  if (!nid) return
  selected.value = nid
  canvas.value?.focusNode(nid)
}
function connect (target: string, choice: BayesEdgeChoice) {
  editor.edit(g => bayesWriteSlot(g, target, choice.slot.path, choice.value))
  canvas.value?.focusNode(target)
}
function disconnect (source: string, target: string) {
  const r = editor.edit(g => bayesDisconnect(g, model.value?.edges ?? [], source, target))
  if (!r || r.ok) return
  // the slot holds more than the reference: the person edits the expression, the inspector opens on it
  const slot = t(`bayes.editor.slot.${r.slot.words.key}`, r.slot.words.params)
  toast.warning(t('bayes.editor.edge.refused', { name: r.name, slot, label: labelOf(r.node) }))
  selected.value = r.node
}
const drop = (id: string, rect: BayesRect) => editor.edit(g => bayesDropItem(g, id, rect))
const move = (id: string, at: { x: number, y: number }) => editor.edit(g => bayesMoveItem(g, id, at))
const resize = (id: string, rect: BayesRect) => editor.edit(g => bayesResizeBox(g, id, rect))
const setForm = (plate: string, on: boolean) => editor.edit(g => bayesSetForm(g, plate, on))
const setSeries = (on: boolean) => editor.edit(g => bayesSetSeries(g, on))
const setSeriesPart = (part: 'time' | 'by', v: string | null) => editor.edit(g => bayesSetSeriesPart(g, part, v))
const setLabel = (id: string, text: string) => editor.edit(g => bayesSetLabel(g, id, text, t('bayes.editor.newNode.fallbackName')), false)
const setName = (id: string, text: string) => editor.edit(g => bayesSetName(g, id, text), false)
const setPlate = (id: string, plate: string | null) => editor.edit(g => bayesPlaceInBox(g, id, plate))
const setParent = (id: string, parent: string | null) => editor.edit(g => bayesNestInBox(g, id, parent))
const setKey = (id: string, key: string) => editor.edit(g => bayesSetKey(g, id, key))
const setTarget = (id: string, target: string) => editor.edit(g => bayesSetTarget(g, id, target))
/** A change of a node's own fields (the inspector's): a choice is a step at once, typing one when it stops. */
const change = (fn: (d: BayesDoc) => unknown, step: boolean) => editor.edit(fn, step)
function undo () { editor.undo() }
function redo () { editor.redo() }

// ---------------------------------------------------------------- the panel

type PanelTab = 'inspector' | 'problems' | 'glossary'
const tab = ref<PanelTab>('inspector')
const errorCount = computed(() => findings.value.filter(f => f.severity === 'error').length)
const problemCount = computed(() => findings.value.length)
/** A node selected on the canvas opens its inspector. */
function selectFromCanvas (id: string | null) {
  selected.value = id
  if (id) tab.value = 'inspector'
}
/** «Show in the graph»: the node selected and brought into sight; the list stays open. */
function showInGraph (id: string) {
  selected.value = id
  nextTick(() => canvas.value?.bringIntoView(id))
}

const selectedKind = computed(() => (selected.value ? nodeOf(selected.value)?.kind : undefined))
// the panel opens at 600 px for a node with a chart, where the canvas keeps 560 px (a window of 1400 px or more)
const roomy = ref(false)
let roomyQuery: MediaQueryList | null = null
const onRoomy = () => { roomy.value = !!roomyQuery?.matches }
onMounted(() => { roomyQuery = window.matchMedia('(min-width: 1400px)'); onRoomy(); roomyQuery.addEventListener('change', onRoomy) })
onBeforeUnmount(() => roomyQuery?.removeEventListener('change', onRoomy))
const CHARTED = ['parameter', 'state', 'correlated_effects']
const wide = computed(() => tab.value === 'inspector' && CHARTED.includes(selectedKind.value ?? '') && roomy.value)
// a width the person set by the handle wins until the page is left
const PANEL_MIN = 320
const PANEL_MAX = 640
const setWidth = ref<number | null>(null)
const panelWidth = computed(() => setWidth.value ?? (wide.value ? 600 : 420))
const clampPanel = (w: number) => Math.round(Math.min(PANEL_MAX, Math.max(PANEL_MIN, w)))
function startPanelResize (ev: MouseEvent) {
  ev.preventDefault()
  const onMove = (e: MouseEvent) => { setWidth.value = clampPanel(window.innerWidth - e.clientX) }
  const onUp = () => {
    window.removeEventListener('mousemove', onMove)
    window.removeEventListener('mouseup', onUp)
    document.body.style.cursor = ''
    document.body.style.userSelect = ''
  }
  window.addEventListener('mousemove', onMove)
  window.addEventListener('mouseup', onUp)
  document.body.style.cursor = 'col-resize'
  document.body.style.userSelect = 'none'
}
function panelKeys (e: KeyboardEvent) {
  const d = e.key === 'ArrowLeft' ? 16 : e.key === 'ArrowRight' ? -16 : 0
  if (!d) return
  e.preventDefault()
  setWidth.value = clampPanel(panelWidth.value + d * (e.shiftKey ? 5 : 1))
}

// ---------------------------------------------------------------- the keys

const canvasKeys = ref<InstanceType<typeof GridKeys> | null>(null)
const keyHelp = useKeyHelp()
keyHelp.host(() => canvasKeys.value?.toggle())
const undoKeys = computed(() => keyHelp.names.chord(keyHelp.names.mod, 'Z'))
const redoKeys = computed(() => {
  const k = keyHelp.names
  return k.ways({ pc: [k.chord(k.mod, 'Y')], any: [k.chord(k.mod, k.shift, 'Z')] })
})
useKeyGroups(() => {
  const k = keyHelp.names
  return [{
    title: t('bayes.editor.keys.group'),
    keys: [
      [`Tab · ${k.enter}`, t('bayes.editor.keys.tabEnter')],
      [`${k.chord(k.alt, '←')} · ${k.chord(k.alt, '↑')} · ${k.chord(k.alt, '→')} · ${k.chord(k.alt, '↓')}`, t('bayes.editor.keys.walk')],
      [k.chord(k.shift, '← ↑ → ↓'), t('bayes.editor.keys.move')],
      ['n', t('bayes.editor.keys.add')],
      [k.ways({ pc: [k.chord(k.shift, 'F10')], any: ['m'] }), t('bayes.editor.keys.menu')],
      [k.ways({ pc: ['Del'], any: [k.backspace] }), t('bayes.editor.keys.remove')],
      [k.chord(k.mod, 'D'), t('bayes.editor.keys.duplicate')],
      [undoKeys.value, t('bayes.editor.keys.undo')],
      [redoKeys.value, t('bayes.editor.keys.redo')],
      ['+ · - · 0', t('bayes.editor.keys.zoom')],
      [', · .', t('bayes.editor.keys.tabs')],
      ['Esc', t('bayes.editor.keys.escape')],
    ],
  }]
})

function typing (el: EventTarget | null): boolean {
  const e = el as HTMLElement | null
  return !!e?.closest?.('input, textarea, select, [contenteditable="true"], .nokey')
}
// Undo, redo and duplicate: from anywhere on the page but a text field (whose own undo is the
// browser's) or a dialog. ⌘Y is the browser's history on a Mac: redo is ⌘⇧Z there.
function onPageKeys (e: KeyboardEvent) {
  if (!doc.value || e.defaultPrevented || typing(e.target) || e.altKey) return
  if (!(e.ctrlKey || e.metaKey) || document.querySelector('[role="dialog"], [role="menu"], .sel-panel')) return
  const act = bayesKeyAction(e, keyHelp.names.mac)
  if (!act || bayesKeyScope(act.kind) !== 'page') return
  if (act.kind === 'undo') { e.preventDefault(); undo() } else if (act.kind === 'redo') { e.preventDefault(); redo() } else if (act.kind === 'duplicate' && selected.value && nodeOf(selected.value)) { e.preventDefault(); duplicate(selected.value) }
}
onMounted(() => window.addEventListener('keydown', onPageKeys))
onBeforeUnmount(() => window.removeEventListener('keydown', onPageKeys))
</script>

<template>
  <div class="ed" :style="{ '--panel-w': `${panelWidth}px` }" :class="{ bare: !doc }">
    <header class="etb">
      <span class="brand"><BrandMark :size="20" /></span>
      <span class="sep" aria-hidden="true" />
      <h1 class="mname">
        <LayoutTemplate :size="15" aria-hidden="true" />
        <span>{{ title }}</span>
      </h1>
      <span class="preview"><Info :size="13" aria-hidden="true" />{{ t('bayes.editor.preview') }}</span>
      <span class="grow" />
      <template v-if="doc">
        <button type="button" class="icon-btn" :disabled="!canUndo" :aria-label="t('bayes.editor.toolbar.undo')" :title="`${t('bayes.editor.toolbar.undo')} (${undoKeys})`" @click="undo">
          <Undo2 :size="15" aria-hidden="true" />
        </button>
        <button type="button" class="icon-btn" :disabled="!canRedo" :aria-label="t('bayes.editor.toolbar.redo')" :title="`${t('bayes.editor.toolbar.redo')} (${redoKeys})`" @click="redo">
          <Redo2 :size="15" aria-hidden="true" />
        </button>
        <span class="sep" aria-hidden="true" />
      </template>
      <button v-if="template && doc" type="button" class="mini" @click="dialogOpen = true">
        <SlidersHorizontal :size="13" aria-hidden="true" />{{ t('bayes.editor.reopen') }}
      </button>
      <GridKeys ref="canvasKeys" :groups="keyHelp.groups.value" :title="t('appKeys.title')" :label="t('canvasKeys.button')" wide />
    </header>

    <ClientOnly>
      <template v-if="doc">
        <div class="pl">
          <BayesPalette ref="palette" @add="(k: BayesPaletteKind) => add(k, null, null)" />
        </div>
        <main class="cv">
          <BayesCanvas
            ref="canvas"
            :doc="doc"
            :model="model"
            :findings="findings"
            :selected="selected"
            :restored="restored"
            @select="selectFromCanvas"
            @form="setForm"
            @series="setSeries"
            @drop="drop"
            @move="move"
            @resize="resize"
            @add="add"
            @connect="connect"
            @disconnect="disconnect"
            @remove="remove"
            @duplicate="duplicate"
            @inspect="(id: string) => { selected = id; tab = 'inspector'; focusInspector(id) }"
            @palette="palette?.focusFirst()"
          />
        </main>
        <aside class="side" :aria-label="t('bayes.editor.inspector.title')">
          <div
            class="panel-resizer"
            role="separator"
            aria-orientation="vertical"
            tabindex="0"
            :aria-valuenow="panelWidth"
            :aria-valuemin="PANEL_MIN"
            :aria-valuemax="PANEL_MAX"
            :aria-label="t('bayes.editor.panel.resize')"
            :title="t('bayes.editor.panel.resizeHint')"
            @mousedown="startPanelResize"
            @keydown="panelKeys"
          />
          <div v-roving.main class="tabs" role="tablist" :aria-label="t('bayes.editor.panel.tabs')">
            <button id="bt-inspector" type="button" role="tab" class="tab" :aria-selected="tab === 'inspector'" aria-controls="bp-inspector" @click="tab = 'inspector'">
              <SlidersHorizontal :size="13" aria-hidden="true" />{{ t('bayes.editor.inspector.title') }}
            </button>
            <button id="bt-problems" type="button" role="tab" class="tab" :aria-selected="tab === 'problems'" aria-controls="bp-problems" @click="tab = 'problems'">
              <TriangleAlert :size="13" aria-hidden="true" />{{ t('bayes.editor.problems.title') }}
              <span v-if="problemCount" class="cnt" :class="{ e: errorCount > 0 }" :aria-label="t('bayes.editor.problems.count', { n: problemCount })">{{ problemCount }}</span>
            </button>
            <button
              id="bt-glossary"
              type="button"
              role="tab"
              class="tab gl"
              :aria-selected="tab === 'glossary'"
              aria-controls="bp-glossary"
              :aria-label="t('bayes.editor.glossary.title')"
              :title="t('bayes.editor.glossary.title')"
              @click="tab = 'glossary'"
            >
              <Info :size="14" aria-hidden="true" />
            </button>
          </div>
          <div v-if="tab === 'problems'" id="bp-problems" class="sbody" role="tabpanel" aria-labelledby="bt-problems">
            <BayesProblems :doc="doc" :findings="findings" :selected="selected" @show="showInGraph" />
          </div>
          <div v-else-if="tab === 'glossary'" id="bp-glossary" class="sbody" role="tabpanel" aria-labelledby="bt-glossary">
            <BayesGlossary />
          </div>
          <div v-else id="bp-inspector" class="sbody" role="tabpanel" aria-labelledby="bt-inspector">
            <BayesInspector
              :doc="doc"
              :model="model"
              :findings="findings"
              :plan="plan"
              :selected="selected"
              :why="why"
              @change="change"
              @form="setForm"
              @select="(id: string) => (selected = id)"
              @escape="backToCanvas"
              @label="setLabel"
              @name="setName"
              @plate="setPlate"
              @parent="setParent"
              @key="setKey"
              @target="setTarget"
              @series="setSeries"
              @series-part="setSeriesPart"
            />
          </div>
        </aside>
      </template>
      <main v-else class="start">
        <div class="card">
          <h2>{{ template ? t(`bayes.templates.${template.id}.title`) : t('bayes.editor.start.title') }}</h2>
          <p v-if="templateId && !template" class="m">{{ t('bayes.editor.start.unknown', { id: templateId }) }}</p>
          <p class="m">{{ t('bayes.editor.start.what') }}</p>
          <button v-if="template" type="button" class="primary" @click="dialogOpen = true">
            <LayoutTemplate :size="14" aria-hidden="true" />{{ t('bayes.editor.dialog.open') }}
          </button>
          <nav v-else class="list" :aria-label="t('bayes.editor.start.title')">
            <section>
              <h3>{{ t('bayes.editor.start.emptyTitle') }}</h3>
              <ul>
                <li>
                  <NuxtLink :to="{ path: '/bayes-model', query: { template: EMPTY } }">{{ t('bayes.editor.start.emptyTitle') }}</NuxtLink>
                  <span>{{ t('bayes.editor.start.emptyWhat') }}</span>
                </li>
              </ul>
            </section>
            <section v-for="f in families" :key="f.id">
              <h3>{{ t(`bayes.editor.family.${f.id}`) }}</h3>
              <ul>
                <li v-for="x in f.items" :key="x.id">
                  <NuxtLink :to="{ path: '/bayes-model', query: { template: x.id } }">{{ t(`bayes.templates.${x.id}.title`) }}</NuxtLink>
                  <span>{{ t(`bayes.templates.${x.id}.what`) }}</span>
                </li>
              </ul>
            </section>
          </nav>
        </div>
      </main>
      <BayesTemplateDialog v-if="template" :template="template" :open="dialogOpen" @open="openModel" @cancel="dialogOpen = false" />
    </ClientOnly>
  </div>
</template>

<style scoped>
.ed {
  --pal-w: 200px;
  display: grid; height: 100vh; gap: 1px; background: var(--border-soft);
  grid-template-rows: auto minmax(0, 1fr);
  grid-template-columns: var(--pal-w) minmax(0, 1fr) var(--panel-w);
  grid-template-areas: "toolbar toolbar toolbar" "palette canvas panel";
}
/* at 1280 px the same columns, the palette 168 px; below 1100 px it folds to its icons */
@media (max-width: 1399px) { .ed { --pal-w: 168px; } }
@media (max-width: 1099px) { .ed { --pal-w: 52px; } }
.etb {
  grid-area: toolbar; display: flex; align-items: center; gap: 8px; min-width: 0; padding: 8px 12px;
  background: var(--topbar-bg); backdrop-filter: blur(8px); border-bottom: 1px solid var(--border-soft);
}
.brand { display: inline-flex; align-items: center; }
.sep { width: 1px; height: 22px; background: var(--border); flex: none; margin: 0 2px; }
.mname { margin: 0; display: inline-flex; align-items: center; gap: 7px; min-width: 0; font-size: 14px; font-weight: 600; }
.mname span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.mname :deep(svg) { flex: none; color: var(--muted); }
.preview {
  display: inline-flex; align-items: center; gap: 5px; flex: none; padding: 2px 9px; border-radius: 999px;
  font-size: 11.5px; color: var(--muted); border: 1px solid var(--border); background: var(--panel-2); white-space: nowrap;
}
.grow { flex: 1 1 0; min-width: 0; }
.icon-btn {
  display: inline-grid; place-items: center; width: 30px; height: 30px; padding: 0; flex: none;
  border: 1px solid var(--border); border-radius: 8px; background: var(--panel-2); color: var(--text);
  transition: border-color var(--dur-1), color var(--dur-1);
}
.icon-btn:disabled { color: var(--muted); opacity: 0.55; cursor: default; }
@media (hover: hover) and (pointer: fine) { .icon-btn:not(:disabled):hover { border-color: var(--accent); } }
.pl { grid-area: palette; min-height: 0; min-width: 0; background: var(--panel); }
.cv { grid-area: canvas; min-width: 0; min-height: 0; position: relative; }
.side { grid-area: panel; position: relative; display: flex; flex-direction: column; min-width: 0; min-height: 0; background: var(--panel); }
.tabs { display: flex; align-items: flex-end; gap: 4px; padding: 8px 10px 0; border-bottom: 1px solid var(--border-soft); flex: none; }
/* the editor's view tabs: a small button whose bottom edge turns accent over a tint when chosen */
.tab {
  display: inline-flex; align-items: center; gap: 6px; padding: 5px 10px; font-size: 12px; color: var(--muted);
  border: 1px solid transparent; border-bottom: 2px solid transparent; border-radius: 6px 6px 0 0; background: none;
  transition: color var(--dur-1), background var(--dur-1), border-color var(--dur-1);
}
.tab[aria-selected="true"] { color: var(--text); border-color: var(--border); border-bottom-color: var(--accent); background: var(--tint-accent); }
@media (hover: hover) and (pointer: fine) { .tab:not([aria-selected="true"]):hover { color: var(--text); } }
.tab:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
.tab .cnt {
  font-size: 10.5px; font-weight: 600; line-height: 15px; padding: 0 5px; border-radius: 999px;
  background: var(--panel); border: 1px solid var(--border); color: var(--muted); font-variant-numeric: tabular-nums;
}
.tab .cnt.e { color: var(--danger-text); border-color: color-mix(in srgb, var(--danger) 50%, var(--border)); }
.tab.gl { margin-left: auto; padding: 5px 8px; }
/* the panel's body is the container the inspector's two columns read (600 px: beside; less: under) */
.sbody { flex: 1; min-height: 0; overflow-y: auto; padding: 14px 16px 18px; container: bayes-panel / inline-size; }
.start { grid-column: 1 / -1; grid-row: 2; background: var(--bg); overflow-y: auto; padding: 40px 24px; }
.card { max-width: 760px; margin: 0 auto; display: flex; flex-direction: column; gap: 12px; }
.card h2 { margin: 0; font-size: 19px; font-weight: 700; }
.card .m { margin: 0; font-size: 13px; line-height: 1.5; color: var(--muted); max-width: 68ch; }
.card .primary { align-self: flex-start; }
.list { display: flex; flex-direction: column; gap: 18px; margin-top: 8px; }
.list h3 { margin: 0 0 6px; font-size: 11.5px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); }
.list ul { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; border: 1px solid var(--border); border-radius: 10px; background: var(--panel); }
.list li { display: flex; flex-direction: column; gap: 2px; padding: 10px 14px; border-top: 1px solid var(--border-soft); }
.list li:first-child { border-top: 0; }
.list a { font-weight: 500; color: var(--text); text-decoration: none; }
@media (hover: hover) and (pointer: fine) { .list a:hover { color: var(--accent); text-decoration: underline; text-underline-offset: 3px; } }
.list li span { font-size: 12px; line-height: 1.45; color: var(--muted); }
</style>
