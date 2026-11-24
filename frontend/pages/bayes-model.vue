<script setup lang="ts">
// The editor of a Bayesian model (docs/specs/bayes-graph-editor.md). Its addresses:
//   ?id=<model>                 a saved model, its current version: drawn, saved as a new version;
//   ?id=<model>&version=<v>     a version, read only, with «Restore» and the notice of a newer one;
//   ?project=<folder>           a new model in that folder: the templates' gallery on the empty canvas;
//   ?template=<id>              a template on local data, nothing saved (lot G0's preview, kept for the
//                               browser checks that run with no gateway).
//
// Lot G0 drew the template and gave a parameter's prior its chart; lot G1 draws (palette, boxes,
// edges, history); lot G2 gives every kind its fields, the Problems tab and the glossary. Lot G3 adds
// the store: the name and the folder in the top bar, Save (Ctrl+S, ⌘S) — a graph with an error is
// refused by the gateway, whose findings then stand on the nodes and in Problems, and «Not saved: 1
// error» in the bar leads there; a stale save offers to reload, the person's graph one Undo away —,
// the Versions tab (open one read only, restore one with its program), the gallery of the templates
// on an empty canvas, and the question before leaving with unsaved changes (as the flow editor).
//
// The graph document is the truth: every edit changes it (useBayesEditor), and the mirror
// (bayesGraph.ts) reads it again at once — the findings on the nodes, the expressions' errors at
// their span, the prior plan the chart draws from.
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { onBeforeRouteLeave, onBeforeRouteUpdate, type LocationQuery } from 'vue-router'
import {
  ArrowLeft, CircleAlert, Code, Eye, FolderClosed, History, Info, LayoutTemplate, LoaderCircle, Redo2, RotateCcw, Save, SlidersHorizontal, TriangleAlert, Undo2,
} from 'lucide-vue-next'
import BrandMark from '~/components/ui/BrandMark.vue'
import GridKeys from '~/components/ui/GridKeys.vue'
import Modal from '~/components/ui/Modal.vue'
import Select from '~/components/ui/Select.vue'
import BayesCanvas from '~/components/bayes/BayesCanvas.vue'
import BayesCodePanel from '~/components/bayes/BayesCodePanel.vue'
import BayesGlossary from '~/components/bayes/BayesGlossary.vue'
import BayesInspector from '~/components/bayes/BayesInspector.vue'
import BayesPalette from '~/components/bayes/BayesPalette.vue'
import BayesProblems from '~/components/bayes/BayesProblems.vue'
import BayesTemplateDialog from '~/components/bayes/BayesTemplateDialog.vue'
import BayesTemplateGallery from '~/components/bayes/BayesTemplateGallery.vue'
import BayesVersions from '~/components/bayes/BayesVersions.vue'
import { bayesKeyAction, bayesKeyScope } from '~/composables/bayesEditorKeys'
import { bayesStanKey, bayesStanView } from '~/composables/bayesStanCode'
import { bayesStanProgramOf, createBayesStanProgram, type BayesStanProgram } from '~/composables/bayesStanProgram'
import { useKeyGroups, useKeyHelp } from '~/composables/useKeyHelp'
import { useToast } from '~/composables/useToast'
import { useFormat } from '~/composables/useFormat'
import { useFolderPaths } from '~/composables/useFolderPaths'
import { useProjects, type Project } from '~/composables/useProjects'
import { useModelGroups } from '~/composables/useModelGroups'
import { useBayesEditor } from '~/composables/useBayesEditor'
import { useBayesModels, type BayesModelDetail, type BayesVersionListed } from '~/composables/useBayesModels'
import { bayesFetchError, bayesGatewayFindings, bayesNewerVersion, bayesSaveModel } from '~/composables/bayesModelSave'
import { bayesBoxes, BAYES_NODE_W, type BayesRect } from '~/composables/bayesCanvasModel'
import {
  bayesAddItem, bayesDisconnect, bayesDropItem, bayesDropTarget, bayesDuplicateNode, bayesFreeSpot, bayesIsBox, bayesMoveItem, bayesRemoveItem,
  bayesResizeBox, bayesSetForm, bayesSetKey, bayesSetLabel, bayesSetName, bayesNestInBox, bayesPlaceInBox, bayesSetSeries,
  bayesSetSeriesPart, bayesSetTarget, bayesWriteSlot, type BayesDoc, type BayesEdgeChoice, type BayesPaletteKind,
} from '~/composables/bayesGraphEdits'
import { bayesOpenTemplate, bayesTemplate, type BayesOpenOptions, type BayesTemplate } from '~/composables/bayesTemplates'

const { t, te } = useI18n()
const route = useRoute()
const router = useRouter()
const toast = useToast()
const fmt = useFormat()
const api = useBayesModels()
const projectsApi = useProjects()
const folderPaths = useFolderPaths()
const modelGroups = useModelGroups()

const editor = useBayesEditor()
// the Stan program of the graph on the canvas, and the last saved version's (the tab's two sources)
const stan = createBayesStanProgram({ check: g => api.check(g) })
const storedProgram = ref<BayesStanProgram | null>(null)
const { doc, selected, model, findings, plan, canUndo, canRedo, restored, readonly, dirty: graphDirty, fromGateway } = editor
onBeforeUnmount(() => editor.dispose())

// ---------------------------------------------------------------- the address

const num = (v: unknown): number | null => (typeof v === 'string' && /^\d+$/.test(v) ? Number(v) : null)
type Mode = 'model' | 'new' | 'preview' | 'none'
const modelId = ref<number | null>(null)
const mode = ref<Mode>('none')
const previewTemplate = ref<BayesTemplate | undefined>(undefined)

// ---------------------------------------------------------------- the model as stored

const detail = ref<BayesModelDetail | null>(null)
const versions = ref<BayesVersionListed[]>([])
const versionsLoading = ref(false)
/** the version on the canvas (null: a model never saved) */
const shownVersion = ref<number | null>(null)
/** the version a save is made from */
const baseVersion = ref<number | null>(null)
const name = ref('')
const projectId = ref<number | null>(null)
const savedName = ref<string | null>(null)
const savedProjectId = ref<number | null>(null)
const loadError = ref('')
const loading = ref(false)
/** the template a new model was opened from (the store keeps it, the family does not follow it) */
const openedTemplate = ref<{ id: string, version: number } | null>(null)

const editable = computed(() => mode.value === 'new' || (!!detail.value?.editable && !readonly.value))
const metaDirty = computed(() => mode.value === 'model' && !readonly.value && (name.value.trim() !== savedName.value || projectId.value !== savedProjectId.value))
/** what leaving would lose: the graph or the name and the folder of a saved model */
const unsaved = computed(() => mode.value !== 'preview' && !readonly.value && (graphDirty.value || metaDirty.value))

const projects = ref<Project[]>([])
const folderOptions = computed(() => projects.value.map(p => ({ value: p.id, label: p.name, hint: folderPaths.pathOf(p.parent_id) })))

function emptyModel (): BayesDoc {
  return { format_version: 1, plates: [{ id: 'rows', kind: 'rows', label: t('bayes.editor.newNode.rows') }], nodes: [], layout: { rows: { x: 260, y: 40, w: 760, h: 420 } } }
}

function reset () {
  detail.value = null
  versions.value = []
  shownVersion.value = null
  baseVersion.value = null
  savedName.value = null
  savedProjectId.value = null
  loadError.value = ''
  openedTemplate.value = null
  storedProgram.value = null
  stan.reset()
  origin.value = null
  dialogTemplate.value = null
  galleryDismissed.value = false
  refused.value = 0
  editor.readonly.value = false
}

/** What the address asks: a saved model (or one of its versions), a new one, a template's preview. */
async function openAddress (q: LocationQuery) {
  reset()
  const id = num(q.id)
  const version = num(q.version)
  const project = num(q.project)
  const tpl = typeof q.template === 'string' ? q.template : ''
  if (id !== null) {
    mode.value = 'model'
    modelId.value = id
    // the list's «Versions» opens the model on that tab
    if (route.hash === '#versions') tab.value = 'versions'
    await loadModel(id, version)
  } else if (project !== null) {
    mode.value = 'new'
    modelId.value = null
    projectId.value = project
    name.value = t('bayes.editor.store.newName')
    editor.load(emptyModel())
    tab.value = 'glossary'
  } else if (tpl && bayesTemplate(tpl)) {
    mode.value = 'preview'
    previewTemplate.value = bayesTemplate(tpl)
    editor.load(null)
    dialogTemplate.value = previewTemplate.value ?? null
  } else {
    mode.value = 'none'
    editor.load(null)
  }
}

async function loadModel (id: number, version: number | null) {
  loading.value = true
  try {
    const d = await api.get(id)
    detail.value = d
    name.value = savedName.value = d.name
    projectId.value = savedProjectId.value = d.project_id
    baseVersion.value = d.current_version
    let v = d.version
    if (version !== null && version !== d.current_version) {
      v = await api.version(id, version)
      editor.readonly.value = true
    }
    shownVersion.value = v.version
    editor.load(v.graph as BayesDoc)
    storedProgram.value = bayesStanProgramOf(v)
    stan.adopt(bayesStanKey(doc.value), storedProgram.value)
    // the stored version's findings (warnings only) are the gateway's word on this graph
    editor.answer(editor.text.value, bayesGatewayFindings(v.findings))
    galleryDismissed.value = true
    nextTick(() => canvas.value?.refit())
    loadVersions()
  } catch (e) {
    const { status } = bayesFetchError(e)
    loadError.value = status === 404 ? t('bayes.editor.store.notFound') : t('bayes.editor.store.loadFailed', { error: String((e as Error)?.message ?? '') })
    editor.load(null)
  } finally {
    loading.value = false
  }
}

async function loadVersions () {
  if (modelId.value === null) return
  versionsLoading.value = true
  try { versions.value = await api.versions(modelId.value) } catch { versions.value = [] } finally { versionsLoading.value = false }
}

// another address of this page (a version opened, a restore, a model just created) opens it in place
watch(() => route.query, (q, was) => {
  if (JSON.stringify(q) === JSON.stringify(was)) return
  if (quietRoute) { quietRoute = false; return }
  openAddress(q)
})
onMounted(() => {
  openAddress(route.query)
  modelGroups.load()
  folderPaths.load()
  projectsApi.list().then((l) => { projects.value = l }).catch(() => {})
})

// ---------------------------------------------------------------- the templates

const dialogTemplate = ref<BayesTemplate | null>(null)
const dialogOpen = computed(() => !!dialogTemplate.value)
const galleryDismissed = ref(false)
const galleryAsked = ref(false)
/** The gallery stands over an empty canvas, or over a full one when the palette asked for it. */
const galleryOpen = computed(() => mode.value !== 'preview' && !!doc.value && !readonly.value && (galleryAsked.value || (doc.value.nodes.length === 0 && !galleryDismissed.value)))
const replaceAsk = ref<string | null>(null)

// the template as it was opened: its explanation of a node stands while the node is as it wrote it
const origin = ref<BayesDoc | null>(null)
const texts = (key: string): string | undefined => (te(key) ? t(key) : undefined)
function chooseTemplate (id: string) {
  const tpl = bayesTemplate(id)
  if (!tpl) return
  if (doc.value && doc.value.nodes.length > 0 && mode.value !== 'preview') { replaceAsk.value = id; return }
  dialogTemplate.value = tpl
}
function confirmReplace () {
  const id = replaceAsk.value
  replaceAsk.value = null
  const tpl = id ? bayesTemplate(id) : undefined
  if (tpl) dialogTemplate.value = tpl
}
function openModel (options: BayesOpenOptions) {
  const tpl = dialogTemplate.value
  if (!tpl) return
  const opened = bayesOpenTemplate(tpl.id, options, texts)
  origin.value = JSON.parse(JSON.stringify(opened)) as BayesDoc
  // on a model's canvas the template is one step of the history (Undo brings back what was there);
  // the preview opens it as its document
  if (mode.value === 'preview') editor.load(opened)
  else editor.adopt(opened)
  if (mode.value === 'new') {
    openedTemplate.value = { id: tpl.id, version: tpl.version }
    if (name.value.trim() === t('bayes.editor.store.newName')) name.value = t(`bayes.templates.${tpl.id}.title`)
  }
  dialogTemplate.value = null
  galleryAsked.value = false
  galleryDismissed.value = true
  tab.value = 'inspector'
  nextTick(() => canvas.value?.refit())
}
function cancelDialog () {
  dialogTemplate.value = null
}
function emptyChosen () {
  galleryAsked.value = false
  galleryDismissed.value = true
  nextTick(() => palette.value?.focusFirst())
}
/** The template's `why.<node>` while the node's slots still equal the template's (the templates spec). */
const whyTemplate = computed(() => previewTemplate.value?.id ?? openedTemplate.value?.id ?? '')
function why (id: string): string | undefined {
  const key = whyTemplate.value ? `bayes.templates.${whyTemplate.value}.why.${id}` : ''
  if (!key || !te(key)) return undefined
  const strip = (n: object | undefined) => (n ? JSON.stringify({ ...n, label: '', note: '' }) : '')
  const was = origin.value?.nodes.find(n => n.id === id)
  return was && strip(was) === strip(nodeOf(id)) ? t(key) : undefined
}

const title = computed(() => {
  if (mode.value === 'preview' && previewTemplate.value) return t(`bayes.templates.${previewTemplate.value.id}.title`)
  return name.value.trim() || t('bayes.editor.title')
})
useHead({ title: computed(() => `${title.value} · ${t('bayes.editor.title')}`) })

// ---------------------------------------------------------------- saving

const saving = ref(false)
/** errors of the last refused save, while the graph is still the one refused */
const refused = ref(0)
const notSaved = computed(() => (fromGateway.value && refused.value > 0 ? findings.value.filter(f => f.severity === 'error').length : 0))
const stale = ref<number | null>(null)
let quietRoute = false

async function save (): Promise<boolean> {
  if (saving.value || readonly.value || mode.value === 'preview' || mode.value === 'none' || !doc.value) return false
  if (projectId.value === null) return false
  if (!name.value.trim()) { toast.error(t('bayes.editor.store.nameNeeded')); return false }
  saving.value = true
  try {
    const out = await bayesSaveModel(editor, api, {
      id: modelId.value,
      projectId: projectId.value,
      baseVersion: baseVersion.value,
      name: name.value,
      savedName: savedName.value,
      savedProjectId: savedProjectId.value,
      templateId: openedTemplate.value?.id ?? null,
      templateVersion: openedTemplate.value?.version ?? null,
    })
    if (out.kind === 'saved') {
      const d = out.detail
      const was = baseVersion.value
      detail.value = d
      refused.value = 0
      name.value = savedName.value = d.name
      projectId.value = savedProjectId.value = d.project_id
      baseVersion.value = shownVersion.value = d.current_version
      storedProgram.value = bayesStanProgramOf(d.version)
      if (!graphDirty.value) stan.adopt(bayesStanKey(doc.value), storedProgram.value)
      if (out.created) {
        mode.value = 'model'
        modelId.value = d.id
        quietRoute = true
        await router.replace({ path: '/bayes-model', query: { id: String(d.id) } })
        toast.success(t('bayes.editor.store.created', { name: d.name }))
      } else if (d.current_version === was) {
        toast.info(t('bayes.editor.store.unchanged'))
      } else {
        toast.success(t('bayes.editor.store.saved', { n: d.current_version }))
      }
      loadVersions()
      return true
    }
    if (out.kind === 'refused') {
      refused.value = out.errors
      tab.value = 'problems'
      toast.error(t(out.errors === 1 ? 'bayes.editor.store.refusedOne' : 'bayes.editor.store.refusedMany', { n: out.errors }))
    } else if (out.kind === 'stale') {
      stale.value = out.current
    } else if (out.kind === 'name') {
      toast.error(t('bayes.editor.store.nameTaken', { name: name.value.trim() }))
    } else {
      toast.error(t('bayes.editor.store.failed', { error: out.detail || String(out.status) }))
    }
    return false
  } finally {
    saving.value = false
  }
}

/** «Reload» after a stale save: the version saved meanwhile, as ONE step — Undo brings the person's graph back. */
async function reloadStale () {
  const id = modelId.value
  stale.value = null
  if (id === null) return
  try {
    const d = await api.get(id)
    detail.value = d
    baseVersion.value = shownVersion.value = d.current_version
    savedName.value = d.name
    savedProjectId.value = d.project_id
    editor.adopt(d.version.graph as BayesDoc)
    storedProgram.value = bayesStanProgramOf(d.version)
    stan.adopt(bayesStanKey(doc.value), storedProgram.value)
    editor.markSaved(editor.text.value)
    editor.answer(editor.text.value, bayesGatewayFindings(d.version.findings))
    refused.value = 0
    toast.info(t('bayes.editor.store.reloaded', { n: d.current_version, undo: undoKeys.value }))
    loadVersions()
  } catch (e) {
    toast.error(t('bayes.editor.store.loadFailed', { error: String((e as Error)?.message ?? '') }))
  }
}

// ---------------------------------------------------------------- versions

const restoreAsk = ref<number | null>(null)
const restoring = ref<number | null>(null)
const newer = computed(() => (readonly.value && shownVersion.value !== null ? bayesNewerVersion(versions.value, shownVersion.value) : null))
function openVersion (v: number) {
  if (modelId.value === null) return
  const current = detail.value?.current_version
  navigateTo({ path: '/bayes-model', query: v === current ? { id: String(modelId.value) } : { id: String(modelId.value), version: String(v) } })
}
async function restore () {
  const v = restoreAsk.value
  restoreAsk.value = null
  const id = modelId.value
  if (v === null || id === null) return
  restoring.value = v
  try {
    const d = await api.restore(id, v)
    leaveConfirmed = true
    await router.replace({ path: '/bayes-model', query: { id: String(id) } })
    leaveConfirmed = false
    // the same address (restored from the current one's page): open it again by hand
    if (route.query.version === undefined && shownVersion.value !== d.current_version) await openAddress(route.query)
    toast.success(t('bayes.editor.versions.restored', { from: v, n: d.current_version }))
  } catch (e) {
    const { data } = bayesFetchError(e)
    toast.error(t('bayes.editor.store.failed', { error: typeof data.detail === 'string' ? data.detail : String((e as Error)?.message ?? '') }))
  } finally {
    restoring.value = null
    leaveConfirmed = false
  }
}

const savedWords = computed(() => {
  if (mode.value === 'new') return t('bayes.editor.store.neverSaved')
  if (!detail.value || shownVersion.value === null) return ''
  if (readonly.value) return t('bayes.editor.store.readonlyVersion', { n: shownVersion.value, current: detail.value.current_version })
  const v = versions.value.find(x => x.version === shownVersion.value)
  const when = fmt.ago(v?.created_at ?? detail.value.updated_at)
  return v?.created_by
    ? t('bayes.editor.store.savedBy', { n: shownVersion.value, when, who: v.created_by })
    : t('bayes.editor.store.savedAt', { n: shownVersion.value, when })
})

// ---------------------------------------------------------------- leaving with unsaved changes

// Leaving with unsaved changes asks what to do: stay, leave without saving, save and leave (as the
// flow editor). Every navigation of the app goes through here; closing the tab or reloading goes
// through the browser's warning; a sign-out the account wanted does not ask.
const leaveOpen = ref(false)
let decideLeaving: ((leave: boolean) => void) | null = null
let leaveConfirmed = false
function askToLeave (): Promise<boolean> {
  editor.flush()
  if (!unsaved.value) return Promise.resolve(true)
  leaveOpen.value = true
  return new Promise((resolve) => { decideLeaving = resolve })
}
function answerLeaving (leave: boolean) {
  leaveOpen.value = false
  if (leave) leaveConfirmed = true
  decideLeaving?.(leave)
  decideLeaving = null
}
async function saveAndLeave () {
  const ok = await save()
  // a refused save keeps the person here, its findings in sight
  answerLeaving(ok && !unsaved.value)
}
const wantedLeave = useLeaving()
onBeforeRouteLeave(() => (leaveConfirmed || wantedLeave.value ? true : askToLeave()))
// another model or version of this page replaces what is on screen: the same question
onBeforeRouteUpdate((to, from) => {
  const same = ['id', 'version', 'project', 'template'].every(k => to.query[k] === from.query[k])
  if (same || quietRoute || leaveConfirmed || wantedLeave.value) return true
  return askToLeave().then((ok) => { leaveConfirmed = false; return ok })
})
function browserWarning (e: BeforeUnloadEvent) {
  editor.flush()
  if (!unsaved.value) return
  e.preventDefault()
  e.returnValue = ''
}
onMounted(() => window.addEventListener('beforeunload', browserWarning))
onBeforeUnmount(() => window.removeEventListener('beforeunload', browserWarning))

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
  if (!d || readonly.value) return
  galleryAsked.value = false
  galleryDismissed.value = true
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
  const label = t(`bayes.editor.palette.item.${kind}.name`)
  const id = editor.edit(g => bayesAddItem(g, kind, place!, box, {
    label,
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

type PanelTab = 'inspector' | 'code' | 'problems' | 'versions' | 'glossary'
const tab = ref<PanelTab>('inspector')
const errorCount = computed(() => findings.value.filter(f => f.severity === 'error').length)
const problemCount = computed(() => findings.value.length)
watch(() => mode.value, (m) => {
  if (m !== 'model' && tab.value === 'versions') tab.value = 'inspector'
  if (m === 'preview' && tab.value === 'code') tab.value = 'inspector'
})
watch(tab, (v) => { if (v === 'versions' && !versions.value.length) loadVersions() })
/** A node selected on the canvas opens its inspector. */
function selectFromCanvas (id: string | null) {
  selected.value = id
  // on the Stan code tab a node chosen finds its rows and its lines there
  if (id && tab.value !== 'code') tab.value = 'inspector'
}
/** «Show in the graph»: the node selected and brought into sight; the list stays open. */
function showInGraph (id: string) {
  selected.value = id
  nextTick(() => canvas.value?.bringIntoView(id))
}
function toProblems () {
  tab.value = 'problems'
  nextTick(() => document.getElementById('bt-problems')?.focus())
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
const codeWide = computed(() => tab.value === 'code' && roomy.value)
const panelWidth = computed(() => setWidth.value ?? (codeWide.value ? 640 : wide.value ? 600 : 420))
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

// ---------------------------------------------------------------- the Stan code

const stanKey = computed(() => bayesStanKey(doc.value))
/** The gateway's program is asked for while the tab is open: at once on opening, 600 ms after a change, never for a graph with an error. */
function askProgram (now: boolean) {
  if (tab.value !== 'code' || !doc.value || mode.value === 'none' || mode.value === 'preview' || readonly.value) return
  if (errorCount.value > 0) stan.hold()
  else stan.want(doc.value as Record<string, any>, stanKey.value, now)
}
watch(tab, (v) => { if (v === 'code') askProgram(true); else stan.hold() })
watch([stanKey, errorCount, readonly], () => askProgram(false))
// an error the gateway finds and the mirror did not stands where a save's findings stand
watch(() => stan.refusal.value, (r) => {
  if (r && r.key === stanKey.value && tab.value === 'code') editor.answer(editor.text.value, bayesGatewayFindings(r.findings as any))
})
const stanView = computed(() => bayesStanView({ errors: errorCount.value, readonly: readonly.value, hasLive: !!stan.program.value, hasStored: !!storedProgram.value }))
const stanShown = computed(() => (stanView.value === 'live' ? stan.program.value : stanView.value === 'stored' ? storedProgram.value : null))

// ---------------------------------------------------------------- the keys

const canvasKeys = ref<InstanceType<typeof GridKeys> | null>(null)
const keyHelp = useKeyHelp()
keyHelp.host(() => canvasKeys.value?.toggle())
const undoKeys = computed(() => keyHelp.names.chord(keyHelp.names.mod, 'Z'))
const redoKeys = computed(() => {
  const k = keyHelp.names
  return k.ways({ pc: [k.chord(k.mod, 'Y')], any: [k.chord(k.mod, k.shift, 'Z')] })
})
const saveKeys = computed(() => keyHelp.names.chord(keyHelp.names.mod, 'S'))
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
      [saveKeys.value, t('bayes.editor.keys.save')],
      ['+ · - · 0', t('bayes.editor.keys.zoom')],
      [', · .', t('bayes.editor.keys.tabs')],
      ['↑ · ↓', t('bayes.editor.keys.codeRows')],
      ['Esc', t('bayes.editor.keys.escape')],
    ],
  }]
})

function typing (el: EventTarget | null): boolean {
  const e = el as HTMLElement | null
  return !!e?.closest?.('input, textarea, select, [contenteditable="true"], .nokey')
}
const overlayOpen = () => !!document.querySelector('[role="dialog"], [role="menu"], .sel-panel')
// Save from anywhere on the page, a field included (what was typed is saved with it), never from a
// dialog; undo, redo and duplicate from anywhere but a text field (whose own undo is the browser's)
// or a dialog. ⌘Y is the browser's history on a Mac: redo is ⌘⇧Z there.
function onPageKeys (e: KeyboardEvent) {
  if (e.defaultPrevented || e.altKey || !(e.ctrlKey || e.metaKey)) return
  const act = bayesKeyAction(e, keyHelp.names.mac)
  if (!act || bayesKeyScope(act.kind) !== 'page') return
  if (act.kind === 'save') {
    // the browser's «save the page» is never what an editor's Ctrl+S means
    e.preventDefault()
    if (overlayOpen()) return
    if (mode.value === 'preview') { toast.info(t('bayes.editor.preview')); return }
    save()
    return
  }
  if (!doc.value || typing(e.target) || overlayOpen()) return
  if (act.kind === 'undo') { e.preventDefault(); undo() } else if (act.kind === 'redo') { e.preventDefault(); redo() } else if (act.kind === 'duplicate' && selected.value && nodeOf(selected.value)) { e.preventDefault(); duplicate(selected.value) }
}
onMounted(() => window.addEventListener('keydown', onPageKeys))
onBeforeUnmount(() => window.removeEventListener('keydown', onPageKeys))
</script>

<template>
  <div class="ed" :style="{ '--panel-w': `${panelWidth}px` }" :class="{ bare: !doc }">
    <header class="etb">
      <NuxtLink to="/" class="brand" :title="t('nav.home')"><BrandMark :size="20" /></NuxtLink>
      <span class="sep" aria-hidden="true" />
      <template v-if="mode === 'preview'">
        <h1 class="mname">
          <LayoutTemplate :size="15" aria-hidden="true" />
          <span>{{ title }}</span>
        </h1>
        <span class="preview"><Info :size="13" aria-hidden="true" />{{ t('bayes.editor.preview') }}</span>
      </template>
      <template v-else>
        <NuxtLink to="/bayes-models" class="back"><ArrowLeft :size="13" aria-hidden="true" />{{ t('nav.bayesModels') }}</NuxtLink>
        <h1 class="sr-only">{{ title }}</h1>
        <template v-if="doc">
          <input
            v-model="name"
            class="namein"
            type="text"
            maxlength="200"
            spellcheck="false"
            :readonly="!editable"
            :aria-label="t('bayes.editor.store.name')"
            :placeholder="t('bayes.editor.store.name')"
          >
          <div class="folder">
            <Select
              :model-value="projectId"
              :options="folderOptions"
              :disabled="!editable"
              searchable
              :placeholder="t('bayes.editor.store.folder')"
              :aria-label="t('bayes.editor.store.folder')"
              @update:model-value="(v: number) => (projectId = v)"
            />
            <FolderClosed :size="13" class="fic" aria-hidden="true" />
          </div>
          <span class="ver">{{ savedWords }}</span>
          <button v-if="notSaved > 0" type="button" class="notsaved" @click="toProblems">
            <CircleAlert :size="13" aria-hidden="true" />{{ t(notSaved === 1 ? 'bayes.editor.store.notSavedOne' : 'bayes.editor.store.notSavedMany', { n: notSaved }) }}
          </button>
          <span v-else-if="unsaved" class="unsaved" role="status">{{ t('bayes.editor.store.unsaved') }}</span>
        </template>
      </template>
      <span class="grow" />
      <template v-if="doc">
        <button type="button" class="icon-btn" :disabled="!canUndo || readonly" :aria-label="t('bayes.editor.toolbar.undo')" :title="`${t('bayes.editor.toolbar.undo')} (${undoKeys})`" @click="undo">
          <Undo2 :size="15" aria-hidden="true" />
        </button>
        <button type="button" class="icon-btn" :disabled="!canRedo || readonly" :aria-label="t('bayes.editor.toolbar.redo')" :title="`${t('bayes.editor.toolbar.redo')} (${redoKeys})`" @click="redo">
          <Redo2 :size="15" aria-hidden="true" />
        </button>
        <button
          v-if="mode === 'new' || (mode === 'model' && !readonly && detail?.editable)"
          type="button"
          class="primary savebtn"
          :disabled="saving"
          :aria-busy="saving"
          :title="`${t('bayes.editor.store.saveHint')} (${saveKeys})`"
          aria-keyshortcuts="Control+S Meta+S"
          @click="save"
        >
          <LoaderCircle v-if="saving" :size="15" class="spin" aria-hidden="true" /><Save v-else :size="15" aria-hidden="true" />
          {{ saving ? t('bayes.editor.store.saving') : t('bayes.editor.store.save') }}
        </button>
        <button v-else-if="readonly && detail?.editable && shownVersion !== null" type="button" class="primary savebtn" :disabled="restoring !== null" @click="restoreAsk = shownVersion">
          <RotateCcw :size="15" aria-hidden="true" />{{ t('bayes.editor.versions.restoreThis', { n: shownVersion }) }}
        </button>
        <span class="sep" aria-hidden="true" />
      </template>
      <button v-if="mode === 'preview' && previewTemplate && doc" type="button" class="mini" @click="dialogTemplate = previewTemplate ?? null">
        <SlidersHorizontal :size="13" aria-hidden="true" />{{ t('bayes.editor.reopen') }}
      </button>
      <GridKeys ref="canvasKeys" :groups="keyHelp.groups.value" :title="t('appKeys.title')" :label="t('canvasKeys.button')" wide />
    </header>

    <ClientOnly>
      <template v-if="doc">
        <div class="pl">
          <BayesPalette v-if="!readonly" ref="palette" :templates="mode !== 'preview'" @add="(k: BayesPaletteKind) => add(k, null, null)" @templates="galleryAsked = true" />
          <div v-else class="ro">
            <Eye :size="15" aria-hidden="true" />
            <p>{{ t('bayes.editor.versions.readonlyPalette') }}</p>
          </div>
        </div>
        <main class="cv">
          <BayesCanvas
            ref="canvas"
            :doc="doc"
            :model="model"
            :findings="findings"
            :selected="selected"
            :restored="restored"
            :readonly="readonly"
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
          <div v-if="readonly && shownVersion !== null" class="rnote" role="status">
            <History :size="14" aria-hidden="true" />
            <span>
              {{ t('bayes.editor.versions.lookingAt', { n: shownVersion }) }}
              <template v-if="newer">
                {{ newer.layoutOnly ? t('bayes.editor.versions.newerLayout', { n: newer.current }) : t('bayes.editor.versions.newer', { n: newer.current }) }}
              </template>
            </span>
            <button type="button" class="mini" @click="openVersion(detail?.current_version ?? 0)">{{ t('bayes.editor.versions.openCurrent') }}</button>
          </div>
          <div v-if="galleryOpen" class="galwrap">
            <BayesTemplateGallery :chosen="dialogTemplate?.id ?? null" @choose="chooseTemplate" @empty="emptyChosen" />
            <button v-if="galleryAsked" type="button" class="mini galclose" @click="galleryAsked = false">{{ t('bayes.editor.gallery.close') }}</button>
          </div>
        </main>
        <aside class="side" :aria-label="t('bayes.editor.panel.tabs')">
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
 <button v-if="mode !== 'preview'" id="bt-code" type="button" role="tab" class="tab" :aria-selected="tab === 'code'" aria-controls="bp-code" @click="tab = 'code'">
              <Code :size="13" aria-hidden="true" />{{ t('bayes.editor.stan.tab') }}
            </button>
            <button id="bt-problems" type="button" role="tab" class="tab" :aria-selected="tab === 'problems'" aria-controls="bp-problems" @click="tab = 'problems'">
              <TriangleAlert :size="13" aria-hidden="true" />{{ t('bayes.editor.problems.title') }}
              <span v-if="problemCount" class="cnt" :class="{ e: errorCount > 0 }" :aria-label="t('bayes.editor.problems.count', { n: problemCount })">{{ problemCount }}</span>
            </button>
            <button v-if="mode === 'model'" id="bt-versions" type="button" role="tab" class="tab" :aria-selected="tab === 'versions'" aria-controls="bp-versions" @click="tab = 'versions'">
              <History :size="13" aria-hidden="true" />{{ t('bayes.editor.versions.title') }}
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
            <p v-if="notSaved > 0" class="refused" role="status">
              <CircleAlert :size="14" aria-hidden="true" />
              <span>{{ t(notSaved === 1 ? 'bayes.editor.store.refusedOne' : 'bayes.editor.store.refusedMany', { n: notSaved }) }} {{ t('bayes.editor.store.refusedWhy') }}</span>
            </p>
            <BayesProblems :doc="doc" :findings="findings" :selected="selected" @show="showInGraph" />
          </div>
          <div v-else-if="tab === 'versions' && mode === 'model'" id="bp-versions" class="sbody" role="tabpanel" aria-labelledby="bt-versions">
            <BayesVersions
              :versions="versions"
              :shown="shownVersion"
              :editable="!!detail?.editable"
              :loading="versionsLoading"
              :restoring="restoring"
              @open="openVersion"
              @restore="(v: number) => (restoreAsk = v)"
            />
          </div>
          <div v-else-if="tab === 'code' && mode !== 'preview'" id="bp-code" class="sbody" role="tabpanel" aria-labelledby="bt-code">
            <BayesCodePanel
              :doc="doc"
              :view="stanView"
              :program="stanShown"
              :current="stan.key.value === stanKey"
              :status="stan.status.value"
              :failure="stan.failure.value"
              :findings="findings"
              :selected="selected"
              :stored-version="storedProgram?.version ?? null"
              @select="(id: string) => (selected = id)"
              @problems="toProblems"
            />
          </div>
          <div v-else-if="tab === 'glossary'" id="bp-glossary" class="sbody" role="tabpanel" aria-labelledby="bt-glossary">
            <BayesGlossary />
          </div>
          <div v-else id="bp-inspector" class="sbody" role="tabpanel" aria-labelledby="bt-inspector">
            <fieldset class="ins" :disabled="readonly">
              <legend class="sr-only">{{ t('bayes.editor.inspector.title') }}</legend>
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
            </fieldset>
          </div>
        </aside>
      </template>
      <main v-else class="start">
        <div class="card">
          <template v-if="loading">
            <p class="m"><LoaderCircle :size="14" class="spin" aria-hidden="true" /> {{ t('bayes.editor.store.loading') }}</p>
          </template>
          <template v-else-if="loadError">
            <h2>{{ t('bayes.editor.title') }}</h2>
            <p class="m">{{ loadError }}</p>
            <NuxtLink to="/bayes-models" class="btn-link"><ArrowLeft :size="14" aria-hidden="true" />{{ t('nav.bayesModels') }}</NuxtLink>
          </template>
          <template v-else-if="mode === 'preview' && previewTemplate">
            <h2>{{ t(`bayes.templates.${previewTemplate.id}.title`) }}</h2>
            <p class="m">{{ t('bayes.editor.start.what') }}</p>
            <button type="button" class="primary" @click="dialogTemplate = previewTemplate ?? null">
              <LayoutTemplate :size="14" aria-hidden="true" />{{ t('bayes.editor.dialog.open') }}
            </button>
          </template>
          <template v-else-if="mode === 'none'">
            <h2>{{ t('bayes.editor.title') }}</h2>
            <p class="m">{{ t('bayes.editor.store.noAddress') }}</p>
            <NuxtLink to="/bayes-models" class="btn-link"><ArrowLeft :size="14" aria-hidden="true" />{{ t('nav.bayesModels') }}</NuxtLink>
          </template>
        </div>
      </main>
      <BayesTemplateDialog v-if="dialogTemplate" :template="dialogTemplate" :open="dialogOpen" @open="openModel" @cancel="cancelDialog" />

      <Modal :open="leaveOpen" :title="t('bayes.editor.store.leaveTitle')" :width="460" @close="answerLeaving(false)">
        <p class="mbody">{{ t('bayes.editor.store.leaveBody', { name: name.trim() || '—' }) }}</p>
        <template #footer>
          <button type="button" @click="answerLeaving(false)">{{ t('bayes.editor.store.leaveStay') }}</button>
          <button type="button" class="danger" @click="answerLeaving(true)">{{ t('bayes.editor.store.leaveDiscard') }}</button>
          <button type="button" class="primary" :disabled="saving" @click="saveAndLeave">{{ t('bayes.editor.store.leaveSave') }}</button>
        </template>
      </Modal>
      <Modal :open="stale !== null" :title="t('bayes.editor.store.staleTitle')" :width="480" @close="stale = null">
        <p class="mbody">{{ t('bayes.editor.store.staleBody', { n: stale ?? 0 }) }}</p>
        <p class="mbody m">{{ t('bayes.editor.store.staleKeep', { n: stale ?? 0, undo: undoKeys }) }}</p>
        <template #footer>
          <button type="button" @click="stale = null">{{ t('bayes.editor.store.cancel') }}</button>
          <button type="button" class="primary" @click="reloadStale">{{ t('bayes.editor.store.reload') }}</button>
        </template>
      </Modal>
      <Modal :open="restoreAsk !== null" :title="t('bayes.editor.versions.restoreTitle', { n: restoreAsk ?? 0 })" :width="480" @close="restoreAsk = null">
        <p class="mbody">{{ t('bayes.editor.versions.restoreBody', { n: restoreAsk ?? 0, next: (detail?.current_version ?? 0) + 1 }) }}</p>
        <p v-if="unsaved" class="mbody m">{{ t('bayes.editor.versions.restoreDiscards') }}</p>
        <template #footer>
          <button type="button" @click="restoreAsk = null">{{ t('bayes.editor.store.cancel') }}</button>
          <button type="button" class="primary" @click="restore"><RotateCcw :size="14" aria-hidden="true" />{{ t('bayes.editor.versions.restore') }}</button>
        </template>
      </Modal>
      <Modal :open="replaceAsk !== null" :title="t('bayes.editor.gallery.replaceTitle')" :width="460" @close="replaceAsk = null">
        <p class="mbody">{{ t('bayes.editor.gallery.replaceBody', { undo: undoKeys }) }}</p>
        <template #footer>
          <button type="button" @click="replaceAsk = null">{{ t('bayes.editor.store.cancel') }}</button>
          <button type="button" class="primary" @click="confirmReplace">{{ t('bayes.editor.gallery.replace') }}</button>
        </template>
      </Modal>
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
.brand { display: inline-flex; align-items: center; color: var(--text); }
.sep { width: 1px; height: 22px; background: var(--border); flex: none; margin: 0 2px; }
.mname { margin: 0; display: inline-flex; align-items: center; gap: 7px; min-width: 0; font-size: 14px; font-weight: 600; }
.mname span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.mname :deep(svg) { flex: none; color: var(--muted); }
.preview {
  display: inline-flex; align-items: center; gap: 5px; flex: none; padding: 2px 9px; border-radius: 999px;
  font-size: 11.5px; color: var(--muted); border: 1px solid var(--border); background: var(--panel-2); white-space: nowrap;
}
/* «← Bayesian models»: the way back to the list, a quiet chip */
.back {
  display: inline-flex; align-items: center; gap: 6px; flex: none; padding: 5px 10px; font-size: 12.5px; white-space: nowrap;
  color: var(--text); text-decoration: none; border: 1px solid var(--border); border-radius: 8px; background: var(--panel-2);
  transition: border-color var(--dur-hover);
}
@media (hover: hover) and (pointer: fine) { .back:hover { border-color: var(--accent); } .back:hover :deep(svg) { transform: translateX(-2px); } }
.back :deep(svg) { transition: transform var(--dur-icon) var(--ease-out); }
.back:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.namein { width: 210px; flex: 0 1 210px; min-width: 120px; padding: 5px 10px; font-weight: 600; font-size: 13px; }
.namein[readonly] { border-color: var(--border); background: transparent; }
.folder { position: relative; width: 230px; flex: 0 1 230px; min-width: 130px; }
.folder :deep(.sel-trigger) { padding-left: 28px; font-size: 12.5px; }
.fic { position: absolute; left: 10px; top: 50%; transform: translateY(-50%); color: var(--muted); pointer-events: none; }
.ver { font-size: 12px; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; min-width: 0; flex: 0 1 auto; font-variant-numeric: tabular-nums; }
.unsaved {
  flex: none; font-size: 11.5px; padding: 2px 8px; border-radius: 999px; white-space: nowrap;
  color: var(--text); border: 1px solid color-mix(in srgb, var(--warning) 55%, var(--border)); background: color-mix(in srgb, var(--warning) 10%, transparent);
}
/* «Not saved: 1 error»: a refused save; it leads to the Problems tab */
.notsaved {
  flex: none; display: inline-flex; align-items: center; gap: 5px; padding: 2px 9px; min-height: 24px; font-size: 12px; font-weight: 500; white-space: nowrap;
  color: var(--danger-text); border: 1px solid color-mix(in srgb, var(--danger) 50%, var(--border)); border-radius: 999px; background: var(--tint-danger);
  text-decoration: underline; text-decoration-color: color-mix(in srgb, var(--danger) 45%, transparent); text-underline-offset: 3px;
}
.notsaved::before { display: none; }
@media (hover: hover) and (pointer: fine) { .notsaved:hover { border-color: var(--danger); text-decoration-color: currentColor; } }
.grow { flex: 1 1 0; min-width: 0; }
.icon-btn {
  display: inline-grid; place-items: center; width: 30px; height: 30px; padding: 0; flex: none;
  border: 1px solid var(--border); border-radius: 8px; background: var(--panel-2); color: var(--text);
  transition: border-color var(--dur-1), color var(--dur-1);
}
.icon-btn:disabled { color: var(--muted); opacity: 0.55; cursor: default; }
@media (hover: hover) and (pointer: fine) { .icon-btn:not(:disabled):hover { border-color: var(--accent); } }
.savebtn { flex: none; white-space: nowrap; }
.pl { grid-area: palette; min-height: 0; min-width: 0; background: var(--panel); }
.ro { display: flex; flex-direction: column; gap: 8px; padding: 14px 12px; color: var(--muted); }
.ro p { margin: 0; font-size: 12px; line-height: 1.5; }
.cv { grid-area: canvas; min-width: 0; min-height: 0; position: relative; }
/* a version looked at: the notice on the canvas' top edge */
.rnote {
  position: absolute; top: 10px; left: 12px; right: 12px; margin: 0 auto; width: fit-content; z-index: 8;
  display: flex; align-items: center; gap: 9px; padding: 7px 8px 7px 12px; font-size: 12.5px; line-height: 1.4;
  color: var(--text); background: var(--panel); border: 1px solid color-mix(in srgb, var(--accent) 45%, var(--border));
  border-radius: 10px; box-shadow: var(--shadow-2);
}
.rnote :deep(svg) { flex: none; color: var(--accent-hi); }
.rnote .mini { flex: none; }
/* the gallery stands over the canvas, on the canvas' own dotted ground */
.galwrap {
  position: absolute; inset: 0; z-index: 9; overflow-y: auto;
  background: radial-gradient(circle, var(--border-soft) 1.1px, transparent 1.3px) 0 0 / 17px 17px, var(--bg);
}
.galclose { position: absolute; top: 14px; right: 16px; }
.side { grid-area: panel; position: relative; display: flex; flex-direction: column; min-width: 0; min-height: 0; background: var(--panel); }
.tabs { display: flex; align-items: flex-end; gap: 4px; padding: 8px 10px 0; border-bottom: 1px solid var(--border-soft); flex: none; }
/* the editor's view tabs: a small button whose bottom edge turns accent over a tint when chosen */
.tab {
  display: inline-flex; align-items: center; gap: 6px; padding: 5px 10px; font-size: 12px; color: var(--muted);
  border: 1px solid transparent; border-bottom: 2px solid transparent; border-radius: 6px 6px 0 0; background: none;
  transition: color var(--dur-1), background var(--dur-1), border-color var(--dur-1); white-space: nowrap;
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
.ins { border: 0; margin: 0; padding: 0; min-width: 0; }
.refused {
  margin: 0 0 12px; display: flex; gap: 8px; align-items: flex-start; padding: 9px 11px; font-size: 12.5px; line-height: 1.45;
  color: var(--text); border: 1px solid color-mix(in srgb, var(--danger) 45%, var(--border)); border-radius: 8px; background: var(--tint-danger);
}
.refused :deep(svg) { flex: none; margin-top: 2px; color: var(--danger-text); }
.start { grid-column: 1 / -1; grid-row: 2; background: var(--bg); overflow-y: auto; padding: 40px 24px; }
.card { max-width: 760px; margin: 0 auto; display: flex; flex-direction: column; gap: 12px; }
.card h2 { margin: 0; font-size: 19px; font-weight: 700; }
.card .m { margin: 0; font-size: 13px; line-height: 1.5; color: var(--muted); max-width: 68ch; display: flex; align-items: center; gap: 8px; }
.card .primary, .card .btn-link { align-self: flex-start; }
.mbody { margin: 0 0 8px; line-height: 1.5; }
.mbody.m { color: var(--muted); font-size: 12.5px; }
@media (max-width: 1399px) {
  /* five tabs in 420 px: closer, so none breaks its words */
  .tabs { gap: 2px; padding-inline: 8px; }
  .tab { padding: 5px 7px; gap: 5px; }
  .namein { flex-basis: 170px; width: 170px; }
  .folder { flex-basis: 190px; width: 190px; }
}
</style>
