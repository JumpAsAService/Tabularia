<script setup lang="ts">
// The dashboard designer (docs/specs/dashboards.md, «On the screen»): the three parts of the report
// designer, elements and data on the left, the grid with its data in the middle, the properties on
// the right. Its panels are the report designer's, on its store, which holds a dashboard written as
// a report's (dashboardDesignModel.ts). Saving makes a new version; a definition the server refuses
// comes back as problems at their tile and property.
//
// The numbers are live (dashboardLiveView.ts): after a pause in the editing the server checks the
// definition, then gives the numbers of what changed, and only of that. A tile's place asks nothing.
//
// /dashboard-design?id=N. It needs EDIT on the dashboard's folder: with VIEW only, the viewer
// (/dashboard?id=N) opens instead.
import { computed, nextTick, onBeforeUnmount, onMounted, provide, ref, shallowRef, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { onBeforeRouteLeave } from 'vue-router'
import { ArrowLeft, Check, CircleAlert, LayoutDashboard, LoaderCircle, MonitorSmartphone, Redo2, RefreshCw, Save, Undo2 } from 'lucide-vue-next'
import BrandMark from '~/components/ui/BrandMark.vue'
import DdBoardProps from '~/components/dashboard/designer/DdBoardProps.vue'
import DdCanvas from '~/components/dashboard/designer/DdCanvas.vue'
import DdTileFields from '~/components/dashboard/designer/DdTileFields.vue'
import ReportParamsBar from '~/components/report/ReportParamsBar.vue'
import RdDatasetDialog from '~/components/report/designer/RdDatasetDialog.vue'
import RdParameterDialog from '~/components/report/designer/RdParameterDialog.vue'
import RdProperties from '~/components/report/designer/RdProperties.vue'
import RdSidebar from '~/components/report/designer/RdSidebar.vue'
import { dashboardCtx } from '~/composables/dashboardCharts'
import {
  boardOf, definitionOf, draftOf, duplicateTile, fieldOnTile, newTile, placeTile, problemsOfBoard, readyToAsk, sizeOfNew, tilesOf, withoutTiles,
  type DBoard, type DTile, type TileKind,
} from '~/composables/dashboardDesignModel'
import { boxMoved, freeSpot, pushed } from '~/composables/dashboardGrid'
import { createLiveView } from '~/composables/dashboardLiveView'
import type { TileSlots } from '~/composables/dashboardTiles'
import { DESIGN, ON_A_BOARD, createDesignStore } from '~/composables/reportDesignStore'
import { createLiveCheck } from '~/composables/reportLiveCheck'
import { dropParamFilters, fieldKind, stillReported, usesOfParam, type DDataset, type DField, type DItem, type DesignProblem } from '~/composables/reportDesignModel'
import { rowCountOf, sourceKey, type RowCount } from '~/composables/reportRowCount'
import { errMessage } from '~/composables/useApi'
import { useSessionPresent } from '~/composables/useAuth'
import { useChartTheme } from '~/composables/useChartTheme'
import { formOfQuery, useDashboards, type DashboardDefinition } from '~/composables/useDashboards'
import { useFolderPaths } from '~/composables/useFolderPaths'
import { paramsOfForm, withChoices, type ReportFormValue, type ReportParameter } from '~/composables/useReports'

const { t } = useI18n()
const route = useRoute()
const toast = useToast()
const api = useDashboards()
const folderPaths = useFolderPaths()
const { ui } = useChartTheme()

const made = createDesignStore()
// a copy goes where it fits on the grid, with its own id: the panels call it as the report's
const store = { ...made, duplicate: duplicate }
provide(DESIGN, store)
provide(ON_A_BOARD, true)
const board = () => store.doc.value as DBoard
const tiles = computed(() => tilesOf(store.doc.value))

const dashboardId = ref<number | null>(Number(route.query.id) || null)
const projectId = ref<number | null>(null)
const name = ref('')
const savedName = ref('')
const savedText = ref('')
const version = ref<number | null>(null)
const loading = ref(true)
const loadError = ref('')
const saving = ref(false)
const announce = ref('')
const ctx = computed(() => dashboardCtx(ui.value, store.doc.value.locale || 'en', t))
// a newer request knocks down an older one of this tab only: two tabs on a dashboard both answer
const tab = Math.random().toString(36).slice(2, 12)

const dirty = computed(() => !loading.value && (store.text.value !== savedText.value || name.value.trim() !== savedName.value))

async function load() {
  loading.value = true
  loadError.value = ''
  if (dashboardId.value == null) {
    loadError.value = t('dashboardDesigner.notFound')
    loading.value = false
    return
  }
  try {
    const d = await api.get(dashboardId.value)
    if (!d.editable) {
      // reading is all this person may do: the viewer
      await navigateTo(`/dashboard?id=${d.id}`, { replace: true })
      return
    }
    projectId.value = d.project_id
    name.value = d.name
    savedName.value = d.name
    version.value = d.current_version
    store.reset(boardOf(d.content))
    savedText.value = store.text.value
  } catch (e: any) {
    loadError.value = (e?.statusCode ?? e?.response?.status) === 404 ? t('dashboardDesigner.notFound') : errMessage(e)
  } finally {
    loading.value = false
  }
  if (!loadError.value) afterEditing()
}

// ── saving ───────────────────────────────────────────────────────────────────
/** A new version. On the version this editor opened (`base_version`): one saved meanwhile by someone
 *  else, or in another tab, is not undone without a word: asked first. */
async function save(force = false): Promise<boolean> {
  if (saving.value || dashboardId.value == null || loading.value) return false
  const n = name.value.trim()
  if (!n) {
    toast.error(t('dashboardDesigner.nameRequired'))
    return false
  }
  store.record()
  saving.value = true
  const text = store.text.value
  try {
    const d = await api.update(dashboardId.value, {
      ...(n !== savedName.value ? { name: n } : {}), content: definitionOf(board()),
      ...(force || version.value == null ? {} : { base_version: version.value }),
    })
    savedName.value = d.name
    name.value = d.name
    version.value = d.current_version
    savedText.value = text
    store.problems.value = []
    if (store.text.value === text) store.liveProblems.value = [] // the server took it as it is
    toast.success(t('dashboardDesigner.saved', { n: d.current_version }))
    return true
  } catch (e: any) {
    const code = e?.statusCode ?? e?.response?.status
    const detail = e?.data?.detail
    if (code === 422 && detail && typeof detail === 'object' && !Array.isArray(detail)) {
      showProblems(problemsOfBoard(store.doc.value, detail))
    } else {
      const now = code === 409 && !force && version.value != null ? await newerVersion() : null
      if (now == null) {
        toast.error(errMessage(e))
      } else if (confirm(t('dashboardDesigner.savedElsewhere', { mine: version.value!, now }))) {
        saving.value = false
        return save(true)
      } // «Cancel» keeps the changes here, with nothing more to say
    }
    return false
  } finally {
    saving.value = false
  }
}

/** A 409 of a save: the version that became the current one since this editor opened the dashboard. A
 *  409 for the name (another dashboard has it) is not this: null. */
async function newerVersion(): Promise<number | null> {
  let now: number | null = null
  try { now = (await api.get(dashboardId.value!)).current_version } catch { return null }
  return now === version.value ? null : now
}

const problemsOpen = ref(false)
/** The problems of a refused save: the first one's tile is selected and its field shows the message;
 *  a dataset's or a parameter's bring the Data tab forward. */
function showProblems(list: DesignProblem[]) {
  store.setProblems(list)
  const first = list[0]
  if (!first) return
  if (first.item && store.find(first.item)) store.select(first.item)
  else if (first.dataset || first.parameter) store.sideTab.value = 'data'
  else store.select(null)
  problemsOpen.value = list.length > 1 || (!first.item && !first.dataset && !first.parameter)
  toast.error(list.length > 1 ? t('dashboardDesigner.problemsN', { n: list.length, first: first.message }) : first.message)
}
function goToProblem(p: DesignProblem) {
  problemsOpen.value = false
  if (p.item && store.find(p.item)) store.select(p.item)
  else if (p.dataset) { store.sideTab.value = 'data'; datasetDialog.value = { open: true, name: p.dataset } }
  else if (p.parameter) { store.sideTab.value = 'data'; parameterDialog.value = { open: true, name: p.parameter } }
  else store.select(null)
}
function problemWhere(p: DesignProblem): string {
  if (p.item) return p.path ? `${p.item} · ${p.path}` : p.item
  if (p.dataset) return t('reportDesigner.problemDataset', { name: p.dataset })
  if (p.parameter) return t('reportDesigner.problemParameter', { name: p.parameter })
  return p.path || t('dashboardDesigner.problemWhole')
}

// ── the parameters of the live view: their form, with the values the tiles are read with ──
const parameters = ref<ReportParameter[]>([])
const form = ref<Record<string, ReportFormValue>>({})
const chosen = ref<Record<string, ReportFormValue>>({})
const fieldErrors = ref<Record<string, string>>({})
const paramsRead = ref('')
const toParams = (values: Record<string, ReportFormValue>) => paramsOfForm(parameters.value, values)
const missing = computed(() => parameters.value.filter((p) => p.required && toParams(chosen.value)[p.name] === undefined).map((p) => p.name))
const needsValues = computed(() => missing.value.length > 0)
const paramsKey = () => JSON.stringify([board().parameters, board().datasets.map((d) => [d.name, d.source])])
let paramsSeq = 0
/** The form of the definition being edited; what was chosen stays for the parameters still there. */
async function readParameters() {
  const key = paramsKey()
  if (key === paramsRead.value) return
  const mine = ++paramsSeq
  if (!board().parameters.length) {
    parameters.value = []
    form.value = {}
    chosen.value = {}
    paramsRead.value = key
    return
  }
  try {
    const read = await api.parametersOf(dashboardId.value!, withFormOnly(), toParams(chosen.value))
    if (mine !== paramsSeq) return
    parameters.value = read
    const fresh = formOfQuery(read, {})
    const kept = Object.fromEntries(read.filter((p) => p.name in chosen.value).map((p) => [p.name, chosen.value[p.name]]))
    form.value = { ...fresh, ...kept }
    chosen.value = { ...form.value }
    fieldErrors.value = {}
    paramsRead.value = key
  } catch {
    // the check says what is wrong with them; the form keeps what it has
    if (mine === paramsSeq) parameters.value = parameters.value.filter((p) => board().parameters.some((x) => x.name === p.name))
  }
}
/** What the form needs of the definition: the tiles may be half done. */
const withFormOnly = () => ({ ...definitionFrom(board()), refresh_seconds: 0, tiles: [] }) as DashboardDefinition
function applyValues() {
  const empty = parameters.value.filter((p) => p.required && toParams(form.value)[p.name] === undefined).map((p) => p.name)
  fieldErrors.value = Object.fromEntries(empty.map((n) => [n, t('report.needsValue')]))
  if (empty.length) return
  chosen.value = { ...form.value }
  view.run()
  // a list that follows another value
  if (parameters.value.length > 1 && board().parameters.some((p) => p.choices && 'dataset' in p.choices)) {
    api.parametersOf(dashboardId.value!, withFormOnly(), toParams(chosen.value)).then((fresh) => { parameters.value = withChoices(parameters.value, fresh) }, () => {})
  }
}

// ── the numbers ──────────────────────────────────────────────────────────────
const slots = shallowRef<TileSlots>({})
const wholeProblem = ref('')
const tileReady = (tile: Record<string, any>) => readyToAsk(store.doc.value, tile)
/** The definition to ask about: without the tiles half made, whose first empty field would hide every
 *  other problem, images as a pixel. */
function draftToAsk() {
  const d = draftOf(board())
  return withoutTiles(d, new Set(tiles.value.filter((tile) => !tileReady(tile)).map((tile) => tile.id)))
}
const view = createLiveView({
  build: () => draftOf(board()),
  values: () => toParams(chosen.value),
  slots: () => slots.value,
  setSlots: (s) => { slots.value = s },
  ready: tileReady,
  // an image is drawn from the definition, and half a megabyte does not go at every pause
  local: (tile) => {
    if (tile.type !== 'image') return null
    const own = tiles.value.find((x) => x.id === tile.id)
    return { type: 'image', title: own?.title ?? '', computed_at: '', cached: true, src: own?.asset, fit: own?.fit ?? 'contain' }
  },
  say: (message) => { wholeProblem.value = message },
  call: async (definition, ids, values, signal) => {
    try {
      return await api.preview(dashboardId.value!, definition, { params: values as any, tiles: ids }, tab, signal)
    } catch (e: any) {
      const status = e?.statusCode ?? e?.response?.status
      const detail = e?.data?.detail
      if (status === 422 && detail && typeof detail === 'object') e.problems = problemsOfBoard(store.doc.value, detail)
      if (status === 400 && typeof detail?.parameter === 'string') fieldErrors.value = { ...fieldErrors.value, [detail.parameter]: errMessage(e) }
      throw e
    }
  },
  errorOf: errMessage,
})

// the server's check after a pause in the editing (reportLiveCheck.ts): its problems go to their
// fields and tiles, and then the numbers of what changed are asked. It says nothing while the same
// definition is on screen; a change of place is checked and asks for no number
function askNumbers(found?: DesignProblem[]) {
  if (!needsValues.value) view.run(found)
}
async function onChecked(list: unknown[]) {
  const found = problemsOfBoard(store.doc.value, { problems: list })
  store.setLiveProblems(found)
  store.problems.value = stillReported(store.problems.value, found)
  if (!found.some((p) => !p.item)) await readParameters()
  askNumbers(found)
}
const live = createLiveCheck({
  call: (definition, signal) => api.check(dashboardId.value!, definition, signal),
  current: () => store.text.value,
  onProblems: (list) => { void onChecked(list) },
  onClear: () => {
    store.liveProblems.value = []
    askNumbers()
  },
})
function afterEditing() {
  live.schedule(store.text.value, draftToAsk)
}
watch([() => store.text.value, () => store.gesture.value], ([, moving]) => {
  if (loading.value || loadError.value || dashboardId.value == null || moving) return
  afterEditing()
})
function refreshNumbers() {
  if (!needsValues.value) view.run(undefined, { again: true })
}

// ── tiles ────────────────────────────────────────────────────────────────────
function addTile(tile: DTile, at?: { x: number; y: number }) {
  store.record() // what came before is a step of its own, however quickly this one follows
  const layout = at ? pushed([...tiles.value, tile], tile.id, tile.grid) : null
  store.doc.value.sections.body.items.push(tile as unknown as DItem)
  if (layout) placeTile(store.doc.value, layout)
  store.select(tile.id)
}
function insert(kind: TileKind, at?: { x: number; y: number }) {
  if (tiles.value.length >= 40) {
    toast.error(t('dashboardDesigner.tooManyTiles', { n: 40 }))
    return
  }
  const size = sizeOfNew(kind)
  addTile(newTile(store.doc.value, kind, at ?? freeSpot(tiles.value, size.w, size.h)), at)
}
function duplicate(id: string) {
  const hit = tiles.value.find((x) => x.id === id)
  if (!hit) return
  if (tiles.value.length >= 40) {
    toast.error(t('dashboardDesigner.tooManyTiles', { n: 40 }))
    return
  }
  addTile(duplicateTile(store.doc.value, hit))
}
/** A field clicked: it joins the tile selected, as what it can be there; otherwise a new tile: a number
 *  a KPI that adds it up, anything else a chart along it. */
function useField(dataset: string, field: DField) {
  const sel = store.selected.value?.item as unknown as DTile | undefined
  if (sel && fieldOnTile(sel, dataset, field)) return
  newFromField(dataset, field)
}
function newFromField(dataset: string, field: DField, at?: { x: number; y: number }) {
  const number = fieldKind(field.type) === 'number'
  const kind: TileKind = number ? 'kpi' : 'chart'
  const size = sizeOfNew(kind)
  const tile = newTile(store.doc.value, kind, at ?? freeSpot(tiles.value, size.w, size.h), dataset)
  fieldOnTile(tile, dataset, field)
  if (number) tile.label = field.name
  addTile(tile, at)
}
function dropField(dataset: string, field: DField, at: { x: number; y: number }, on: string | null) {
  const tile = on ? tiles.value.find((x) => x.id === on) : null
  if (tile) {
    if (fieldOnTile(tile, dataset, field)) store.select(tile.id)
    return
  }
  newFromField(dataset, field, at)
}

// ── dialogs and the data panel ───────────────────────────────────────────────
const datasetDialog = ref<{ open: boolean; name: string | null }>({ open: false, name: null })
const parameterDialog = ref<{ open: boolean; name: string | null }>({ open: false, name: null })
const refreshing = ref<string | null>(null)
const fieldErrorsOfData = ref<Record<string, string>>({})
const waiting: string[] = []
const readFields = (dataset: string, definition: any) => api.fields(dashboardId.value!, dataset, { ...definitionFrom(definition), tiles: [] })
/** What the dialog sends is the store's, written as a report's. */
function definitionFrom(doc: DBoard) {
  return { v: 1, locale: doc.locale, timezone: doc.timezone, parameters: doc.parameters, datasets: doc.datasets }
}
async function refreshFields(dataset: string) {
  if (dashboardId.value == null) return
  // one read at a time; the others wait their turn instead of being dropped
  if (refreshing.value) {
    if (refreshing.value !== dataset && !waiting.includes(dataset)) waiting.push(dataset)
    return
  }
  refreshing.value = dataset
  try {
    const got = await readFields(dataset, board())
    const ds = store.doc.value.datasets.find((d) => d.name === dataset)
    if (ds) store.quietly(() => { ds.fields = got.fields })
    keepRowCount(dataset, rowCountOf(got), ds?.source)
    const { [dataset]: _, ...rest } = fieldErrorsOfData.value
    fieldErrorsOfData.value = rest
    toast.success(t('reportDesigner.data.fieldsRead', { n: got.fields.length, name: dataset }, got.fields.length))
  } catch (e) {
    fieldErrorsOfData.value = { ...fieldErrorsOfData.value, [dataset]: errMessage(e) }
    toast.error(errMessage(e))
  } finally {
    refreshing.value = null
    const next = waiting.shift()
    if (next && store.doc.value.datasets.some((d) => d.name === next)) refreshFields(next)
  }
}
function keepRowCount(dataset: string, count: RowCount | null, source: unknown) {
  const { [dataset]: _, ...rest } = store.rowCounts.value
  store.rowCounts.value = count ? { ...rest, [dataset]: { ...count, source: sourceKey(source) } } : rest
}
function onDatasetApplied(dataset: string) {
  const ds = store.doc.value.datasets.find((d) => d.name === dataset)
  if (ds && !(ds.fields ?? []).length) refreshFields(dataset)
  store.sideTab.value = 'data'
}
function removeDataset(dataset: string) {
  const users = tiles.value.filter((it) => it.dataset === dataset).map((it) => it.id)
  const msg = users.length ? t('reportDesigner.data.removeDatasetUsed', { name: dataset, items: users.join(', ') }) : t('reportDesigner.data.removeDatasetConfirm', { name: dataset })
  if (!confirm(msg)) return
  store.doc.value.datasets = store.doc.value.datasets.filter((d) => d.name !== dataset)
  keepRowCount(dataset, null, null)
}
function removeParameter(param: string) {
  const uses = usesOfParam(store.doc.value, param)
  const said = [
    t('reportDesigner.data.removeParameterConfirm', { name: param }),
    ...(uses.filters.length ? [t('reportDesigner.data.removeParameterFilters', { datasets: uses.filters.join(', ') })] : []),
    ...(uses.sql.length ? [t('reportDesigner.data.removeParameterSql', { datasets: uses.sql.join(', ') })] : []),
  ]
  if (!confirm(said.join(' '))) return
  store.doc.value.parameters = store.doc.value.parameters.filter((p) => p.name !== param)
  dropParamFilters(store.doc.value, param)
}
function sourceLabel(d: DDataset): string {
  const s = d.source
  if (s.kind === 'sql') return s.connection_id == null ? t('reportDesigner.data.srcCatalog') : t('reportDesigner.data.srcConnection')
  if (s.kind === 'datasource') return t('reportDesigner.data.srcDatasource')
  if (s.kind === 'view') return t('reportDesigner.data.srcView')
  return t('reportDesigner.data.srcScript')
}
// what the editor did on its own is said once
watch(() => store.notice.value, (n) => { if (n) toast.info(n) })

async function done() {
  if (dashboardId.value == null) return
  if (dirty.value && !(await save())) return
  savedText.value = store.text.value
  navigateTo(`/dashboard?id=${dashboardId.value}`)
}

// ── keys, with the report designer's rules ───────────────────────────────────
function typing(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null
  return !!el?.closest?.('input, textarea, select, [contenteditable="true"], .monaco-editor, [role="combobox"], .sel-panel')
}
// after a press on what takes no focus (a panel's empty space, a button in Firefox) the keys come
// from the body: they act on the grid only if that last press was on it, or a Backspace beside the
// fields would remove the selected tile
let pressedAway = false
function onPress(e: PointerEvent) {
  pressedAway = !(e.target as HTMLElement | null)?.closest?.('.ddc-stage')
}
function onCanvas(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null
  if (!el || el === document.body) return !pressedAway
  return !!el.closest?.('.ddc-stage')
}
const dialogOpen = () => datasetDialog.value.open || parameterDialog.value.open || !!document.querySelector('[role="dialog"][aria-modal="true"]')

function onKey(e: KeyboardEvent) {
  const mod = e.ctrlKey || e.metaKey
  const key = e.key.toLowerCase()
  if (mod && !e.altKey && key === 's') {
    e.preventDefault()
    save()
    return
  }
  if (loading.value || dialogOpen() || typing(e.target) || e.defaultPrevented) return
  if (mod && key === 'z') {
    e.preventDefault()
    if (e.shiftKey) store.redo()
    else store.undo()
    return
  }
  if (mod && key === 'y') {
    e.preventDefault()
    store.redo()
    return
  }
  // what acts on the tiles acts where they are: the grid, not a button of a panel
  if (!onCanvas(e.target)) return
  const sel = store.selected.value?.item as unknown as DTile | undefined
  if (!sel) return
  if (e.key === 'Delete' || e.key === 'Backspace') {
    e.preventDefault()
    store.remove(sel.id)
  } else if (mod && key === 'd') {
    e.preventDefault()
    store.duplicate(sel.id)
  } else if (e.key === 'Escape') {
    store.select(null)
  } else if (e.key.startsWith('Arrow')) {
    e.preventDefault()
    nudge(sel, e.key, e.shiftKey ? 5 : 1)
  }
}
/** A tile moved with the arrows, pushing what it lands on, as a drag does. */
function nudge(tile: DTile, key: string, step: number) {
  const dx = key === 'ArrowLeft' ? -step : key === 'ArrowRight' ? step : 0
  const dy = key === 'ArrowUp' ? -step : key === 'ArrowDown' ? step : 0
  const box = boxMoved(tile.grid, dx, dy)
  const onTile = (document.activeElement as HTMLElement | null)?.dataset?.tile === tile.id
  placeTile(store.doc.value, pushed(tiles.value, tile.id, box))
  announce.value = t('dashboardDesigner.at', { col: box.x + 1, row: box.y + 1 })
  // the page's order follows the reading order: the keys stay on the tile if that moved it
  if (onTile) nextTick(() => document.querySelector<HTMLElement>(`[data-tile="${CSS.escape(tile.id)}"]`)?.focus({ preventScroll: true }))
}

// ── leaving ──────────────────────────────────────────────────────────────────
function onBeforeUnload(e: BeforeUnloadEvent) {
  if (!dirty.value) return
  e.preventDefault()
  e.returnValue = ''
}
const signedIn = useSessionPresent()
onBeforeRouteLeave(() => !dirty.value || confirm(t(signedIn.value ? 'dashboardDesigner.leaveConfirm' : 'dashboardDesigner.sessionGone')))

onMounted(() => {
  document.addEventListener('keydown', onKey)
  document.addEventListener('pointerdown', onPress, true)
  window.addEventListener('beforeunload', onBeforeUnload)
  folderPaths.load()
  load()
})
onBeforeUnmount(() => {
  live.stop()
  view.stop()
  document.removeEventListener('keydown', onKey)
  document.removeEventListener('pointerdown', onPress, true)
  window.removeEventListener('beforeunload', onBeforeUnload)
})

const folderPath = computed(() => folderPaths.pathOf(projectId.value) ?? '')
useHead(() => ({ title: name.value ? `${name.value} · ${t('dashboardDesigner.titleSuffix')}` : t('dashboardDesigner.titleSuffix') }))
</script>

<template>
  <div class="ddz">
    <header class="ddz-top">
      <NuxtLink :to="projectId ? `/?folder=${projectId}` : '/'" class="ddz-back" :title="$t('reportDesigner.backToExplore')" :aria-label="$t('reportDesigner.backToExplore')">
        <ArrowLeft :size="15" />
      </NuxtLink>
      <BrandMark :size="20" />
      <div class="ddz-name">
        <LayoutDashboard :size="15" class="ddz-nicon" />
        <input v-model="name" type="text" maxlength="200" :aria-label="$t('dashboardDesigner.nameLabel')" :placeholder="$t('dashboardDesigner.nameLabel')" :disabled="loading" />
        <span v-if="folderPath" class="ddz-folder" :title="folderPath">{{ folderPath }}</span>
      </div>
      <span class="ddz-state" role="status" aria-live="polite">
        <template v-if="saving"><LoaderCircle :size="13" class="spin" /> {{ $t('reportDesigner.saving') }}</template>
        <template v-else-if="dirty"><span class="ddz-dot" aria-hidden="true" /> {{ $t('reportDesigner.unsaved') }}</template>
        <template v-else-if="version != null"><Check :size="13" /> {{ $t('reportDesigner.savedState', { n: version }) }}</template>
      </span>
      <button v-if="store.problems.value.length" type="button" class="mini ddz-problems" :aria-expanded="problemsOpen" @click="problemsOpen = !problemsOpen">
        <CircleAlert :size="13" /> {{ $t('reportDesigner.problems', { n: store.problems.value.length }, store.problems.value.length) }}
      </button>

      <span class="ddz-grow" />

      <div class="ddz-tools">
        <button type="button" class="ddz-icon" :disabled="!store.canUndo.value" :title="`${$t('reportDesigner.undo')} (Ctrl+Z)`" :aria-label="$t('reportDesigner.undo')" @click="store.undo()"><Undo2 :size="15" /></button>
        <button type="button" class="ddz-icon" :disabled="!store.canRedo.value" :title="`${$t('reportDesigner.redo')} (Ctrl+Shift+Z)`" :aria-label="$t('reportDesigner.redo')" @click="store.redo()"><Redo2 :size="15" /></button>
        <span class="ddz-sep" aria-hidden="true" />
        <button type="button" class="ddz-icon" :title="$t('dashboardDesigner.refreshNumbers')" :aria-label="$t('dashboardDesigner.refreshNumbers')" :disabled="!tiles.length" @click="refreshNumbers"><RefreshCw :size="15" /></button>
      </div>

      <button type="button" class="primary ddz-save" :disabled="saving || loading || !!loadError || (!dirty && version != null)" :title="`${$t('reportDesigner.save')} (Ctrl+S)`" @click="save()">
        <LoaderCircle v-if="saving" :size="14" class="spin" /><Save v-else :size="14" /> {{ $t('reportDesigner.save') }}
      </button>
      <button type="button" class="ddz-done" :disabled="loading || !!loadError || saving" :title="$t('dashboardDesigner.doneTitle')" @click="done">
        {{ $t('reportDesigner.done') }}
      </button>
    </header>

    <div v-if="problemsOpen && store.problems.value.length" class="ddz-plist" role="region" :aria-label="$t('reportDesigner.problemsLabel')">
      <ul>
        <li v-for="(p, i) in store.problems.value" :key="i">
          <button type="button" @click="goToProblem(p)">
            <CircleAlert :size="13" />
            <span class="ddz-pwhere">{{ problemWhere(p) }}</span>
            <span class="ddz-pmsg">{{ p.message }}</span>
          </button>
        </li>
      </ul>
    </div>

    <span class="sr-only" role="status" aria-live="polite">{{ announce }}</span>
    <!-- a phone: the grid and its panels do not fit, and the viewer does -->
    <div class="ddz-small" role="status">
      <MonitorSmartphone :size="26" aria-hidden="true" />
      <h1>{{ $t('dashboardDesigner.smallTitle') }}</h1>
      <p>{{ $t('dashboardDesigner.smallBody') }}</p>
      <NuxtLink v-if="dashboardId != null" :to="`/dashboard?id=${dashboardId}`" class="ddz-open">{{ $t('dashboardDesigner.smallOpen') }}</NuxtLink>
    </div>
    <p v-if="loadError" class="ddz-error" role="alert">{{ loadError }}</p>
    <div v-else-if="loading" class="ddz-loading"><SkeletonRows :rows="6" /></div>
    <div v-else class="ddz-work">
      <RdSidebar
        class="ddz-left"
        board
        :refreshing="refreshing"
        :field-errors="fieldErrorsOfData"
        :source-label="sourceLabel"
        @edit-dataset="datasetDialog = { open: true, name: $event }"
        @remove-dataset="removeDataset"
        @refresh-fields="refreshFields"
        @edit-parameter="parameterDialog = { open: true, name: $event }"
        @remove-parameter="removeParameter"
        @insert="insert($event as TileKind)"
        @use-field="useField"
      />
      <main class="ddz-center">
        <div v-if="wholeProblem" class="ddz-whole" role="alert"><CircleAlert :size="14" aria-hidden="true" /> <span>{{ wholeProblem }}</span></div>
        <div v-if="parameters.length" class="ddz-params">
          <ReportParamsBar
            :parameters="parameters"
            :model-value="form"
            :errors="fieldErrors"
            :busy="false"
            :submit-label="$t('dashboard.apply')"
            @update:model-value="form = $event"
            @submit="applyValues"
          />
        </div>
        <p v-if="needsValues" class="ddz-waiting" role="status">{{ $t('dashboard.needsValuesBody') }}</p>
        <DdCanvas
          class="ddz-canvas"
          :slots="slots"
          :ctx="ctx"
          :ready="tileReady"
          @add-dataset="datasetDialog = { open: true, name: null }"
          @insert="insert"
          @drop-field="dropField"
          @said="announce = $event"
        />
      </main>
      <RdProperties class="ddz-right">
        <template #layout="{ item }"><DdTileFields :item="item" /></template>
        <template #nothing><DdBoardProps /></template>
      </RdProperties>
    </div>

    <RdDatasetDialog
      :open="datasetDialog.open"
      :name="datasetDialog.name"
      :report-id="dashboardId"
      :read-fields="readFields"
      @close="datasetDialog = { open: false, name: null }"
      @applied="onDatasetApplied"
    />
    <RdParameterDialog :open="parameterDialog.open" :name="parameterDialog.name" @close="parameterDialog = { open: false, name: null }" />
  </div>
</template>

<style scoped>
.ddz { height: 100vh; display: flex; flex-direction: column; background: var(--bg); color: var(--text); overflow: hidden; }
.ddz-top {
  display: flex; align-items: center; gap: 10px; padding: 7px 12px; min-width: 0; flex: none;
  background: var(--topbar-bg); backdrop-filter: blur(8px); border-bottom: 1px solid var(--border-soft); position: relative; z-index: 10;
}
.ddz-back { display: grid; place-items: center; width: 28px; height: 28px; border-radius: 8px; color: var(--muted); flex: none; }
.ddz-back:hover { color: var(--text); background: var(--panel-2); }
.ddz-back:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.ddz-name { display: flex; align-items: center; gap: 6px; min-width: 0; flex: 0 1 380px; }
.ddz-nicon { color: var(--accent); flex: none; }
.ddz-name input {
  font-size: 15px; font-weight: 600; padding: 4px 8px; min-width: 120px; flex: 1 1 auto;
  border-color: transparent; background: transparent;
}
.ddz-name input:hover { border-color: var(--control-border); }
.ddz-name input:focus { background: var(--bg-soft); }
.ddz-folder { font-size: 12px; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; min-width: 0; flex: 0 1 auto; max-width: 180px; }
.ddz-state { display: inline-flex; align-items: center; gap: 5px; font-size: 12px; color: var(--muted); white-space: nowrap; flex-shrink: 0; }
.ddz-state :deep(svg) { color: var(--accent-2); }
.ddz-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--warning); }
.ddz-state:has(.ddz-dot) { color: var(--warning); }
.ddz-problems { color: var(--danger); border-color: color-mix(in srgb, var(--danger) 45%, var(--border)); white-space: nowrap; flex-shrink: 0; display: inline-flex; gap: 5px; align-items: center; }
.ddz-grow { flex: 1; min-width: 8px; }
.ddz-tools { display: flex; align-items: center; gap: 2px; flex-shrink: 0; }
.ddz-icon { border: 0; background: transparent; box-shadow: none; padding: 5px 6px; color: var(--muted); border-radius: 7px; min-height: 0; }
.ddz-icon:hover:not(:disabled) { color: var(--text); background: var(--panel-2); box-shadow: none; }
.ddz-sep { width: 1px; height: 18px; background: var(--border); margin: 0 6px; }
.ddz-save, .ddz-done { white-space: nowrap; flex-shrink: 0; }
.ddz-plist { flex: none; border-bottom: 1px solid var(--border-soft); background: var(--panel); max-height: 30vh; overflow: auto; }
.ddz-plist ul { list-style: none; margin: 0; padding: 6px 12px; display: flex; flex-direction: column; gap: 2px; }
.ddz-plist button {
  width: 100%; justify-content: flex-start; align-items: baseline; gap: 8px; border: 0; background: transparent; box-shadow: none; padding: 5px 8px;
  font-size: 12.5px; text-align: left;
}
.ddz-plist button:hover { background: var(--panel-2); box-shadow: none; }
.ddz-plist button :deep(svg) { color: var(--danger); flex: none; }
.ddz-pwhere { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; color: var(--muted); flex: 0 1 auto; max-width: 38%; overflow-wrap: anywhere; }
.ddz-pmsg { flex: 1; min-width: 0; overflow-wrap: anywhere; line-height: 1.4; }
.ddz-error { margin: 32px; color: var(--danger); }
.ddz-loading { padding: 24px; }
.ddz-work { flex: 1; min-height: 0; display: grid; grid-template-columns: 252px minmax(0, 1fr) 320px; gap: 1px; background: var(--border-soft); }
.ddz-left, .ddz-right { min-height: 0; overflow: hidden; }
.ddz-center { min-width: 0; min-height: 0; display: flex; flex-direction: column; background: var(--bg); }
.ddz-canvas { flex: 1; min-height: 0; }
.ddz-params { flex: none; padding: 8px 14px 0; }
.ddz-waiting { flex: none; margin: 8px 14px 0; font-size: 12.5px; color: var(--muted); }
.ddz-whole {
  flex: none; margin: 8px 14px 0; padding: 6px 10px; display: flex; align-items: center; gap: 8px; font-size: 12.5px; line-height: 1.4; overflow-wrap: anywhere;
  color: var(--text); background: var(--panel); border: 1px solid color-mix(in srgb, var(--danger) 45%, var(--border)); border-radius: 8px;
}
.ddz-whole svg { color: var(--danger); flex-shrink: 0; }
.ddz-whole span { flex: 1; min-width: 0; }
.ddz-small { display: none; }
/* a laptop (1280 to 1440 px): the grid gets what the panels can spare */
@media (max-width: 1440px) {
  .ddz-work { grid-template-columns: 228px minmax(0, 1fr) 300px; }
  .ddz-folder { max-width: 140px; }
}
@media (max-width: 1180px) {
  .ddz-folder { display: none; }
  .ddz-work { grid-template-columns: 216px minmax(0, 1fr) 288px; }
}
@media (max-width: 899px) {
  .ddz-work, .ddz-tools, .ddz-save, .ddz-state, .ddz-folder, .ddz-loading, .ddz-plist, .ddz-problems { display: none; }
  .ddz-small { flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 8px; padding: 24px 16px; text-align: center; color: var(--muted); }
  .ddz-small svg { color: var(--accent); }
  .ddz-small h1 { margin: 4px 0 0; font-size: 16px; color: var(--text); }
  .ddz-small p { margin: 0; line-height: 1.5; max-width: 420px; }
  .ddz-open { margin-top: 8px; color: var(--accent-hi); }
}
</style>
