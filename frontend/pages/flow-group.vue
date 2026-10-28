<script setup lang="ts">
// The flow group editor: a grid where each row is a step and the cells of a row
// run at the same time (a flow to run, a database datasource to refresh, a saved
// script to run, a SQL script's datasource to write, docs/specs/sql-scripts.md, or an
// image of a container registry, docs/specs/saved-scripts.md). A
// Container cell holds its own settings, set in the Container node's panel.
// Flows and datasources come from the panel on the right, by drag and drop, or
// from the picker inside an empty cell (the keyboard way). A filled cell can be
// dragged onto another cell: the two swap.
//
// The page also shows the group's runs: the state of every cell in the run being
// looked at (the latest by default), polled while a run is queued or running.
//
// /flow-group?id=N opens a group; /flow-group?project=P starts a new one in folder P.
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { onBeforeRouteLeave, onBeforeRouteUpdate } from 'vue-router'
import {
  Rows3, Workflow, DatabaseZap, GripVertical, Plus, X, Play, CalendarClock, Save, Search, History,
  CircleCheck, CircleX, LoaderCircle, Hourglass, SkipForward, CircleSlash, EyeOff, Ban, Check, FileCode2,
  Container as ContainerIcon, Settings2, Mail, DatabaseSearch,
} from 'lucide-vue-next'
import { errMessage } from '~/composables/useApi'
import { knownError } from '~/composables/knownErrors'
import { useFormat } from '~/composables/useFormat'
import { useFolderPaths } from '~/composables/useFolderPaths'
import { useFlows, type FlowSummary } from '~/composables/useFlows'
import { useDatasources, type DatasourceInfo } from '~/composables/useDatasources'
import { useSavedScripts, type SavedScript } from '~/composables/useSavedScripts'
import { useSqlScripts, type SqlScript } from '~/composables/useSqlScripts'
import { useRegistriesEditor } from '~/composables/useRegistries'
import ContainerNodePanel from '~/components/containers/ContainerNodePanel.vue'
import PushmailCellPanel from '~/components/containers/PushmailCellPanel.vue'
import { useConnections, type ConnectionInfo } from '~/composables/useConnections'
import { useReports, type Report } from '~/composables/useReports'
import Modal from '~/components/ui/Modal.vue'
import {
  useFlowGroups, MAX_GROUP_COLUMNS, MAX_GROUP_STEPS,
  type FlowGroupCell, type FlowGroupCellKind, type FlowGroupCellLabel, type FlowGroupDetail, type FlowGroupRun, type PushmailConfig,
} from '~/composables/useFlowGroups'

const { t } = useI18n()
const fmtx = useFormat()
const toast = useToast()
const route = useRoute()
const groupsApi = useFlowGroups()
const flowsApi = useFlows()
const dsApi = useDatasources()
const scriptsApi = useSavedScripts()
const sqlScriptsApi = useSqlScripts()
const registriesEditor = useRegistriesEditor()
const connApi = useConnections()
const reportsApi = useReports()
const folderPaths = useFolderPaths()

type Grid = (FlowGroupCell | null)[][]

const groupId = ref<number | null>(Number(route.query.id) || null)
const projectId = ref<number | null>(Number(route.query.project) || null)
const detail = ref<FlowGroupDetail | null>(null)
const name = ref('')
const grid = ref<Grid>([[null, null]])
const loading = ref(!!groupId.value)
const loadError = ref('')
const saving = ref(false)

// a new group is the author's to edit; an existing one says what the reader may do
const canEdit = computed(() => (detail.value ? detail.value.can_edit : true))
const canRun = computed(() => !!detail.value?.can_run)
const folderPath = computed(() => folderPaths.pathOf(projectId.value) ?? '')

// ── the grid: always rectangular on screen, saved as drawn ──────────────────
const cols = computed(() => Math.max(1, ...grid.value.map((row) => row.length)))

function rectangular(steps: Grid): Grid {
  const width = Math.max(1, ...steps.map((row) => row.length))
  const rows = steps.length ? steps : [[]]
  // a container or pushmail cell keeps its settings
  return rows.map((row) => [...row.map((c) => (c ? copyCell(c) : null)), ...Array(width - row.length).fill(null)])
}
function copyCell(c: FlowGroupCell): FlowGroupCell {
  if (c.kind === 'container') return { kind: c.kind, id: c.id, container: { ...(c.container ?? {}) } }
  if (c.kind === 'pushmail') return { kind: c.kind, id: c.id, pushmail: JSON.parse(JSON.stringify(c.pushmail ?? {})) }
  return { kind: c.kind, id: c.id }
}

const snapshot = (n: string, g: Grid) => JSON.stringify([n.trim(), g])
const savedAs = ref(snapshot('', grid.value))
const dirty = computed(() => snapshot(name.value, grid.value) !== savedAs.value)

const keyOf = (c: { kind: FlowGroupCellKind; id: number }) => `${c.kind}:${c.id}`

function addStep() {
  if (grid.value.length >= MAX_GROUP_STEPS) return
  grid.value.push(Array(cols.value).fill(null))
}
function addColumn() {
  if (cols.value >= MAX_GROUP_COLUMNS) return
  const width = cols.value
  for (const row of grid.value) {
    while (row.length < width) row.push(null)
    row.push(null)
  }
}
function removeStep(r: number) {
  grid.value.splice(r, 1)
  if (!grid.value.length) grid.value.push(Array(cols.value).fill(null))
}
function removeColumn(c: number) {
  for (const row of grid.value) row.splice(c, 1)
  if (grid.value.every((row) => !row.length)) for (const row of grid.value) row.push(null)
}
function clearCell(r: number, c: number) {
  grid.value[r]![c] = null
}

// the same object twice in one step is refused by the gateway: say it here, at once
function inStepElsewhere(r: number, c: number, key: string): boolean {
  return grid.value[r]!.some((cell, i) => i !== c && !!cell && keyOf(cell) === key)
}

function place(item: { kind: FlowGroupCellKind; id: number }, r: number, c: number, from?: [number, number]) {
  if (!canEdit.value) return
  const key = keyOf(item)
  if (from) {
    const [fr, fc] = from
    if (fr === r && fc === c) return
    const target = grid.value[r]![c] ?? null
    if (fr !== r) {
      // across steps the two cells swap: neither may land next to a twin
      const twin = inStepElsewhere(r, c, key) ? item : target && inStepElsewhere(fr, fc, keyOf(target)) ? target : null
      if (twin) {
        toast.error(t('flowGroups.duplicateInStep', { name: labelFor(twin).name ?? '#' + twin.id }))
        return
      }
    }
    const moving = grid.value[fr]![fc] ?? { kind: item.kind, id: item.id }
    grid.value[fr]![fc] = target
    grid.value[r]![c] = moving // a container cell moves with its settings
    return
  }
  if (item.kind === 'container') {
    // a new Container cell: the first registry and image one may use, then its panel
    const first = registriesEditor.state.value?.registries[0]
    const ids = grid.value.flat().filter((x): x is FlowGroupCell => !!x && x.kind === 'container').map((x) => x.id)
    grid.value[r]![c] = {
      kind: 'container', id: Math.max(0, ...ids) + 1,
      container: {
        label: t('containers.node.title'), registryId: first?.id ?? null, registryName: first?.name ?? '',
        repository: first?.repositories.find((x) => !x.includes('*')) ?? '', tag: '', command: [], args: [], variables: [],
        cpu: '500m', memory: '512Mi', timeoutSeconds: 900, retries: 0, retryDelaySeconds: 60,
      },
    }
    editing.value = [r, c]
    return
  }
  if (item.kind === 'pushmail') {
    // a new Pushmail cell: the first SMTP connection, then its panel
    const ids = grid.value.flat().filter((x): x is FlowGroupCell => !!x && x.kind === 'pushmail').map((x) => x.id)
    grid.value[r]![c] = {
      kind: 'pushmail', id: Math.max(0, ...ids) + 1,
      pushmail: {
        label: '', connectionId: smtpConnections.value[0]?.id ?? null, to: [], cc: [], subject: '{flow} {today:yyyy-MM-dd}',
        body: '', bodyIsHtml: false, timezone: 'UTC', attachments: [{ datasourceId: null, fileName: '', maxRows: 10_000 }],
        reports: [],
      },
    }
    editing.value = [r, c]
    return
  }
  if (inStepElsewhere(r, c, key)) {
    toast.error(t('flowGroups.duplicateInStep', { name: labelFor(item).name ?? '#' + item.id }))
    return
  }
  grid.value[r]![c] = { kind: item.kind, id: item.id }
}

// ── what can go in a cell: readable flows, database datasources, saved scripts ─
const flows = ref<FlowSummary[]>([])
const datasources = ref<DatasourceInfo[]>([])
const scripts = ref<SavedScript[]>([])
// the SQL scripts that write a datasource: a cell writes it
const sqlScripts = ref<SqlScript[]>([])
const KIND_ICON = { flow: Workflow, refresh: DatabaseZap, script: FileCode2, container: ContainerIcon, pushmail: Mail, sql: DatabaseSearch } as const
const KIND_LABEL = { flow: 'flowGroups.kindFlow', refresh: 'flowGroups.kindRefresh', script: 'flowGroups.kindScript', container: 'flowGroups.kindContainer', pushmail: 'pushmail.kind', sql: 'flowGroups.kindSql' } as const
const SECTION_LABEL = { flow: 'flowGroups.catalogFlows', refresh: 'flowGroups.catalogRefresh', script: 'flowGroups.catalogScripts', container: 'flowGroups.catalogImages', pushmail: 'pushmail.catalog', sql: 'flowGroups.catalogSql' } as const
// the SMTP connections one may send through: a Pushmail cell is offered when there is one
const connections = ref<ConnectionInfo[]>([])
const smtpConnections = computed(() => connections.value.filter((c) => c.db_type === 'smtp'))
// the reports one sees: a Pushmail cell can attach them
const reports = ref<Report[]>([])

// ── a Container cell's settings, in the Container node's panel ───────────────
const editing = ref<[number, number] | null>(null)
const editingCell = computed(() => (editing.value ? grid.value[editing.value[0]]?.[editing.value[1]] ?? null : null))
function editCell(r: number, c: number) {
  const kind = grid.value[r]?.[c]?.kind
  if (kind === 'container' || kind === 'pushmail') editing.value = [r, c]
}
function patchPushmail(patch: Partial<PushmailConfig>) {
  const cell = editingCell.value
  if (!cell || !editing.value) return
  grid.value[editing.value[0]]![editing.value[1]] = { ...cell, pushmail: { ...(cell.pushmail as PushmailConfig), ...patch } }
}
function patchContainer(patch: Record<string, any>) {
  const cell = editingCell.value
  if (!cell || !editing.value) return
  grid.value[editing.value[0]]![editing.value[1]] = { ...cell, container: { ...(cell.container ?? {}), ...patch } }
}
function removeEditing() {
  if (editing.value) clearCell(editing.value[0], editing.value[1])
  editing.value = null
}

interface Item { key: string; kind: FlowGroupCellKind; id: number; name: string; path: string }
const items = computed<Item[]>(() => [
  ...flows.value.map((f) => ({ key: `flow:${f.id}`, kind: 'flow' as const, id: f.id, name: f.name, path: folderPaths.pathOf(f.project_id) ?? '' })),
  ...datasources.value
    .filter((d) => d.kind === 'database')
    .map((d) => ({ key: `refresh:${d.id}`, kind: 'refresh' as const, id: d.id, name: d.name, path: folderPaths.pathOf(d.project_id) ?? '' })),
  ...scripts.value.map((x) => ({ key: `script:${x.id}`, kind: 'script' as const, id: x.id, name: x.name, path: folderPaths.pathOf(x.project_id) ?? '' })),
  ...sqlScripts.value.filter((x) => x.output).map((x) => ({
    key: `sql:${x.id}`, kind: 'sql' as const, id: x.id, name: x.name,
    path: `${folderPaths.pathOf(x.project_id) ?? ''} → ${x.output!.name}`,
  })),
  // an image of a registry one may use: a new Container cell each time
  ...(registriesEditor.available.value
    ? [{ key: 'container:new', kind: 'container' as const, id: 0, name: t('flowGroups.newContainer'), path: t('flowGroups.newContainerHint') }]
    : []),
  // an email with datasources attached: a new Pushmail cell each time
  ...(smtpConnections.value.length
    ? [{ key: 'pushmail:new', kind: 'pushmail' as const, id: 0, name: t('pushmail.new'), path: t('pushmail.newHint') }]
    : []),
])
const byKey = computed(() => new Map(items.value.map((i) => [i.key, i])))

// the labels the gateway resolved for the saved cells: the only source for a cell
// the reader cannot see, or one deleted since
const savedLabels = computed(() => {
  const out = new Map<string, FlowGroupCellLabel>()
  const d = detail.value
  if (!d) return out
  d.definition.steps.forEach((row, r) => row.forEach((cell, c) => {
    const label = d.cells[r]?.[c]
    if (cell && label) out.set(keyOf(cell), label)
  }))
  return out
})

interface CellLabel { name: string | null; path: string; hidden: boolean; missing: boolean }
function labelFor(cell: { kind: FlowGroupCellKind; id: number; container?: Record<string, any> }): CellLabel {
  if (cell.kind === 'container') {
    const d = cell.container ?? {}
    const image = d.repository ? `${d.repository}:${d.tag || 'latest'}` : ''
    return { name: d.label || image || t('flowGroups.kindContainer'), path: image, hidden: false, missing: false }
  }
  if (cell.kind === 'pushmail') {
    const d = (cell as FlowGroupCell).pushmail
    const n = (d?.attachments ?? []).filter((a) => a.datasourceId != null).length
      + (d?.reports ?? []).filter((r) => r.reportId != null).length
    const to = (d?.to ?? [])[0]
    return {
      name: d?.label?.trim() || t('pushmail.kind'),
      path: [t('pushmail.files', { n }, n), to ? (d!.to.length > 1 ? `${to} +${d!.to.length - 1}` : to) : ''].filter(Boolean).join(' → '),
      hidden: false, missing: false,
    }
  }
  const key = keyOf(cell)
  const known = byKey.value.get(key)
  if (known) return { name: known.name, path: known.path, hidden: false, missing: false }
  const saved = savedLabels.value.get(key)
  if (saved?.hidden) return { name: null, path: '', hidden: true, missing: false }
  if (saved && !saved.missing && saved.name) {
    return { name: saved.name, path: folderPaths.pathOf(saved.project_id) ?? '', hidden: false, missing: false }
  }
  return { name: null, path: '', hidden: false, missing: true }
}

const used = computed(() => new Set(grid.value.flat().filter((c): c is FlowGroupCell => !!c).map(keyOf)))

const catalogQ = ref('')
const catalog = computed(() => {
  const q = catalogQ.value.trim().toLowerCase()
  const hit = (i: Item) => !q || i.name.toLowerCase().includes(q) || i.path.toLowerCase().includes(q)
  return {
    flows: items.value.filter((i) => i.kind === 'flow' && hit(i)),
    refresh: items.value.filter((i) => i.kind === 'refresh' && hit(i)),
    script: items.value.filter((i) => i.kind === 'script' && hit(i)),
    sql: items.value.filter((i) => i.kind === 'sql' && hit(i)),
    container: items.value.filter((i) => i.kind === 'container'),
    pushmail: items.value.filter((i) => i.kind === 'pushmail'),
  }
})

// the picker of an empty cell offers what is not already in that step
function optionsFor(r: number) {
  const inStep = new Set(grid.value[r]!.filter((c): c is FlowGroupCell => !!c).map(keyOf))
  return items.value
    .filter((i) => !inStep.has(i.key) || i.kind === 'container' || i.kind === 'pushmail')
    .map((i) => ({
      value: i.key,
      label: i.name,
      hint: i.path || undefined,
      group: t(SECTION_LABEL[i.kind]),
    }))
}
function pick(r: number, c: number, key: string | null) {
  const item = key ? byKey.value.get(key) : undefined
  if (item) place(item, r, c)
}

// ── drag and drop ───────────────────────────────────────────────────────────
// What is being dragged is kept here: during `dragover` the browser hides the
// data, and only a drag started on this page may drop.
interface Drag { kind: FlowGroupCellKind; id: number; from?: [number, number] }
let dragging: Drag | null = null
const dropOn = ref<string | null>(null)

function startDrag(e: DragEvent, d: Drag, label: string) {
  if (!canEdit.value || !e.dataTransfer) return
  dragging = d
  e.dataTransfer.setData('text/plain', label) // Firefox starts a drag only with data
  e.dataTransfer.effectAllowed = d.from ? 'move' : 'copy'
}
function overCell(e: DragEvent, target: string) {
  if (!dragging) return
  e.preventDefault()
  if (e.dataTransfer) e.dataTransfer.dropEffect = dragging.from ? 'move' : 'copy'
  dropOn.value = target
}
function leaveCell(e: DragEvent, target: string) {
  const into = e.relatedTarget as Node | null
  if (dropOn.value === target && !(into && (e.currentTarget as HTMLElement).contains(into))) dropOn.value = null
}
function dropCell(e: DragEvent, r: number, c: number) {
  e.preventDefault()
  const d = dragging
  endDrag()
  if (d) place(d, r, c, d.from)
}
// dropped on "add step": a new step that starts with it
function dropNewStep(e: DragEvent) {
  e.preventDefault()
  const d = dragging
  endDrag()
  if (!d || grid.value.length >= MAX_GROUP_STEPS) return
  addStep()
  place(d, grid.value.length - 1, 0, d.from)
}
function endDrag() {
  dragging = null
  dropOn.value = null
}

// ── load and save ───────────────────────────────────────────────────────────
function apply(d: FlowGroupDetail) {
  detail.value = d
  groupId.value = d.id
  projectId.value = d.project_id
  name.value = d.name
  grid.value = rectangular(d.definition.steps.length ? d.definition.steps : [[null, null]])
  savedAs.value = snapshot(d.name, grid.value)
}

async function load() {
  if (groupId.value == null) {
    loading.value = false
    return
  }
  try {
    apply(await groupsApi.get(groupId.value))
    loadRuns()
  } catch (e) {
    loadError.value = errMessage(e)
  } finally {
    loading.value = false
  }
}

async function loadCatalog() {
  // a list that does not load leaves the panel empty, not the page broken
  const [f, d, sc, , cn, sq, rp] = await Promise.allSettled([
    flowsApi.list(), dsApi.list(), scriptsApi.list(), registriesEditor.load(), connApi.list(), sqlScriptsApi.list(),
    reportsApi.list(),
  ])
  connections.value = cn.status === 'fulfilled' ? cn.value : []
  reports.value = rp.status === 'fulfilled' ? rp.value : []
  sqlScripts.value = sq.status === 'fulfilled' ? sq.value : []
  flows.value = f.status === 'fulfilled' ? f.value : []
  datasources.value = d.status === 'fulfilled' ? d.value : []
  // only the scripts one sees; none where scripting is off
  scripts.value = sc.status === 'fulfilled' ? sc.value : []
}

async function save() {
  if (saving.value || !canEdit.value) return
  const n = name.value.trim()
  if (!n) {
    toast.error(t('flowGroups.nameRequired'))
    return
  }
  saving.value = true
  const definition = { steps: grid.value }
  try {
    if (groupId.value != null) {
      apply(await groupsApi.update(groupId.value, { name: n, definition }))
      toast.success(t('flowGroups.saved', { name: n }))
    } else if (projectId.value != null) {
      const d = await groupsApi.create(projectId.value, { name: n, definition })
      apply(d)
      navigateTo(`/flow-group?id=${d.id}`, { replace: true })
      toast.success(t('flowGroups.created', { name: n }))
    }
  } catch (e) {
    toast.error(errMessage(e))
  } finally {
    saving.value = false
  }
}

function onKey(e: KeyboardEvent) {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
    e.preventDefault()
    // a field of a cell's panel that records its value on «change» gives it first (as in
    // the flow editor): saved at once, the group kept the value from before
    const field = document.activeElement as HTMLElement | null
    if (field && ['INPUT', 'TEXTAREA', 'SELECT'].includes(field.tagName.toUpperCase())) {
      field.blur()
      field.focus()
    }
    nextTick(() => save())
  }
}

// unsaved changes are not lost by a click elsewhere, nor by opening another group
// from the search (the same page, another id)
onBeforeRouteLeave(() => !dirty.value || confirm(t('flowGroups.leaveConfirm')))
onBeforeRouteUpdate((to) => (Number(to.query.id) || null) === groupId.value || !dirty.value || confirm(t('flowGroups.leaveConfirm')))
watch(() => route.query.id, (id) => {
  const next = Number(id) || null
  if (next === groupId.value) return // a group just created took its address
  if (pollTimer) clearTimeout(pollTimer)
  pollTimer = null
  watchedLive.clear()
  runs.value = []
  shownRunId.value = null
  historyOpen.value = false
  detail.value = null
  loadError.value = ''
  groupId.value = next
  projectId.value = Number(route.query.project) || null
  name.value = ''
  grid.value = [[null, null]]
  savedAs.value = snapshot('', grid.value)
  loading.value = next != null
  load()
})
function beforeUnload(e: BeforeUnloadEvent) {
  if (!dirty.value) return
  e.preventDefault()
  e.returnValue = ''
}

onMounted(() => {
  window.addEventListener('keydown', onKey)
  window.addEventListener('beforeunload', beforeUnload)
  load()
  loadCatalog()
})
onUnmounted(() => {
  mounted = false
  window.removeEventListener('keydown', onKey)
  window.removeEventListener('beforeunload', beforeUnload)
  if (pollTimer) clearTimeout(pollTimer)
  pollTimer = null
})

// ── runs ────────────────────────────────────────────────────────────────────
const runs = ref<FlowGroupRun[]>([])
const shownRunId = ref<number | null>(null) // null = the latest
const historyOpen = ref(false)
let pollTimer: ReturnType<typeof setTimeout> | null = null
let mounted = true
const watchedLive = new Set<number>() // runs seen live here: their end gets a message

const short = (text: string) => (text.length > 160 ? text.slice(0, 159) + '…' : text)
const isLive = (run: FlowGroupRun) => run.status === 'PENDING' || run.status === 'STARTED'
const shownRun = computed(() => runs.value.find((r) => r.id === shownRunId.value) ?? runs.value[0] ?? null)

async function loadRuns() {
  const id = groupId.value
  if (id == null || !mounted) return
  if (pollTimer) clearTimeout(pollTimer)
  pollTimer = null
  try {
    const answer = await groupsApi.runs(id)
    // the page left, or shows another group, while the answer travelled: drop it
    if (!mounted || id !== groupId.value) return
    runs.value = answer
  } catch {
    if (!mounted || id !== groupId.value) return
    /* transient: the next load tries again */
  }
  for (const run of runs.value) {
    if (isLive(run)) watchedLive.add(run.id)
    else if (watchedLive.delete(run.id)) {
      if (run.status === 'SUCCESS') toast.success(t('flowGroups.runSucceeded', { name: name.value }))
      // the whole error is in the run bar: a message only says which step and why, in short
      else toast.error(t('flowGroups.runFailed', { name: name.value }) + (run.error ? `: ${short(run.error)}` : ''))
    }
  }
  if (runs.value.some(isLive)) pollTimer = setTimeout(loadRuns, 2500)
}

type ShownState = 'waiting' | 'running' | 'succeeded' | 'failed' | 'skipped' | 'interrupted' | 'notRun'
const STATE_ICON: Record<ShownState, any> = {
  waiting: Hourglass, running: LoaderCircle, succeeded: CircleCheck, failed: CircleX,
  skipped: SkipForward, interrupted: CircleSlash, notRun: SkipForward,
}

// the state of a cell in the run shown, only if that run ran this same object
// here: a grid changed since would otherwise borrow another cell's outcome
function stateOf(r: number, c: number) {
  const run = shownRun.value
  const cell = grid.value[r]?.[c]
  const rc = run?.cells?.[r]?.[c]
  if (!run || !cell || !rc || rc.kind !== cell.kind || rc.id !== cell.id) return null
  let state: ShownState = rc.state
  // a run that ended with cells still open was cut short (its process died)
  if (!isLive(run) && state === 'running') state = 'interrupted'
  if (!isLive(run) && state === 'waiting') state = 'notRun'
  return { state, error: rc.error, waited: rc.waited }
}

function duration(run: FlowGroupRun): string {
  if (!run.started_at) return ''
  const end = run.finished_at ? new Date(run.finished_at).getTime() : Date.now()
  const s = Math.max(0, Math.round((end - new Date(run.started_at).getTime()) / 1000))
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`
}
const when = (iso: string | null) => (iso ? fmtx.dateTime(iso) : '')

const running = computed(() => runs.value.some(isLive))
const starting = ref(false)
async function runNow() {
  if (groupId.value == null || dirty.value || starting.value) return
  starting.value = true
  try {
    await groupsApi.runNow(groupId.value)
    toast.success(t('flowGroups.runQueued', { name: name.value }))
    shownRunId.value = null
    await loadRuns()
  } catch (e) {
    toast.error(errMessage(e))
  } finally {
    starting.value = false
  }
}

// ── schedule ────────────────────────────────────────────────────────────────
const scheduleOpen = ref(false)
const savingSchedule = ref(false)
async function saveSchedule(cron: string) {
  if (groupId.value == null) return
  savingSchedule.value = true
  try {
    const d = await groupsApi.setSchedule(groupId.value, cron)
    // only the schedule changed: unsaved edits to the grid stay on screen
    if (detail.value) detail.value = { ...detail.value, run_schedule: d.run_schedule, run_scheduled_by: d.run_scheduled_by, next_run_at: d.next_run_at }
    scheduleOpen.value = false
    toast.success(d.run_schedule ? t('flowGroups.scheduleSaved') : t('flowGroups.scheduleCleared'))
  } catch (e) {
    toast.error(errMessage(e))
  } finally {
    savingSchedule.value = false
  }
}
</script>

<template>
  <AppShell fluid>
    <div class="fg">
      <div class="page-head">
        <h1>
          <Rows3 :size="18" />
          <input
            v-model="name"
            class="gname"
            type="text"
            maxlength="200"
            :readonly="!canEdit"
            :placeholder="$t('flowGroups.namePlaceholder')"
            :aria-label="$t('flowGroups.nameLabel')"
          />
        </h1>
        <span v-if="folderPath" class="where muted">{{ $t('flowGroups.inFolder', { path: folderPath }) }}</span>
        <div class="head-actions">
          <span v-if="dirty && canEdit" class="tag unsaved">{{ $t('flowGroups.unsaved') }}</span>
          <button
            v-if="groupId != null && canRun"
            :title="detail?.run_schedule ? $t('flowGroups.scheduleTitle', { cron: detail.run_schedule }) : $t('flowGroups.schedule')"
            @click="scheduleOpen = true"
          >
            <CalendarClock :size="14" />
            <span v-if="detail?.run_schedule" class="cron">{{ detail.run_schedule }}</span>
            <template v-else>{{ $t('flowGroups.schedule') }}</template>
          </button>
          <button
            v-if="groupId != null && canRun"
            :disabled="dirty || running || starting"
            :title="dirty ? $t('flowGroups.saveFirst') : running ? $t('flowGroups.alreadyRunning') : undefined"
            @click="runNow"
          >
            <LoaderCircle v-if="starting" :size="14" class="spin" />
            <Play v-else :size="14" />
            {{ $t('flowGroups.runNow') }}
          </button>
          <button v-if="canEdit" class="primary" :disabled="saving || (!dirty && groupId != null)" @click="save">
            <LoaderCircle v-if="saving" :size="14" class="spin" />
            <Save v-else :size="14" />
            {{ saving ? $t('flowGroups.saving') : $t('flowGroups.save') }}
          </button>
        </div>
      </div>

      <p v-if="loadError" class="err" role="alert">{{ loadError }}</p>
      <SkeletonRows v-else-if="loading" :rows="5" />

      <div v-else class="work" :class="{ readonly: !canEdit }">
        <section class="board" :aria-label="$t('flowGroups.gridLabel')">
          <!-- the run being looked at, and the way to the others -->
          <div v-if="groupId != null" class="runbar">
            <template v-if="shownRun">
              <span class="rstate" :class="shownRun.status.toLowerCase()">
                <CircleCheck v-if="shownRun.status === 'SUCCESS'" :size="14" />
                <CircleX v-else-if="shownRun.status === 'FAILURE'" :size="14" />
                <LoaderCircle v-else :size="14" class="spin" />
                {{ $t(`flowGroups.status_${shownRun.status}`) }}
              </span>
              <span class="muted">
                {{ shownRun === runs[0] ? $t('flowGroups.lastRun') : $t('flowGroups.pastRun') }}
                · {{ when(shownRun.started_at) }}
                <template v-if="duration(shownRun)"> · {{ duration(shownRun) }}</template>
                · {{ $t(`flowGroups.trigger_${shownRun.trigger_type}`) }}
              </span>
              <span v-if="shownRun.error" class="rerr" :title="knownError(shownRun.error, t)">{{ knownError(shownRun.error, t) }}</span>
            </template>
            <span v-else class="muted">{{ $t('flowGroups.neverRun') }}</span>
            <span v-if="detail?.next_run_at && detail.run_schedule" class="muted next">
              <CalendarClock :size="13" /> {{ $t('flowGroups.nextRun', { when: when(detail.next_run_at) }) }}
            </span>
            <button
              v-if="runs.length"
              class="mini histbtn"
              :class="{ activebtn: historyOpen }"
              :aria-expanded="historyOpen"
              @click="historyOpen = !historyOpen"
            >
              <History :size="13" /> {{ $t('flowGroups.history') }} <span class="n">{{ runs.length }}</span>
            </button>
          </div>

          <ul v-if="historyOpen && runs.length" class="history">
            <li v-for="run in runs" :key="run.id">
              <button class="hrun" :class="{ on: shownRun?.id === run.id }" @click="shownRunId = run.id">
                <CircleCheck v-if="run.status === 'SUCCESS'" :size="13" class="ok" />
                <CircleX v-else-if="run.status === 'FAILURE'" :size="13" class="ko" />
                <LoaderCircle v-else :size="13" class="spin wip" />
                <span class="hwhen">{{ when(run.started_at) || $t('flowGroups.status_PENDING') }}</span>
                <span class="muted">{{ duration(run) }}</span>
                <span class="muted">{{ $t(`flowGroups.trigger_${run.trigger_type}`) }}</span>
                <span v-if="run.error" class="rerr" :title="knownError(run.error, t)">{{ knownError(run.error, t) }}</span>
              </button>
            </li>
          </ul>

          <p v-if="canEdit" class="lead muted">{{ $t('flowGroups.lead') }}</p>

          <div class="gridwrap">
            <div class="steps" :style="{ gridTemplateColumns: `76px repeat(${cols}, minmax(200px, 280px)) ${canEdit ? '34px' : ''}` }">
              <!-- column heads: where a column is removed -->
              <div class="corner" />
              <div v-for="c in cols" :key="`h${c}`" class="colhead">
                <span>{{ $t('flowGroups.column', { n: c }) }}</span>
                <button
                  v-if="canEdit && cols > 1"
                  class="mini ghost"
                  :title="$t('flowGroups.removeColumn', { n: c })"
                  :aria-label="$t('flowGroups.removeColumn', { n: c })"
                  @click="removeColumn(c - 1)"
                ><X :size="12" /></button>
              </div>
              <div v-if="canEdit" class="addcol">
                <button
                  class="mini"
                  :disabled="cols >= MAX_GROUP_COLUMNS"
                  :title="cols >= MAX_GROUP_COLUMNS ? $t('flowGroups.limitColumns', { n: MAX_GROUP_COLUMNS }) : $t('flowGroups.addColumn')"
                  :aria-label="$t('flowGroups.addColumn')"
                  @click="addColumn"
                ><Plus :size="13" /></button>
              </div>

              <template v-for="(row, r) in grid" :key="`r${r}`">
                <div class="stephead">
                  <span class="stepno">{{ $t('flowGroups.step', { n: r + 1 }) }}</span>
                  <button
                    v-if="canEdit && grid.length > 1"
                    class="mini ghost"
                    :title="$t('flowGroups.removeStep', { n: r + 1 })"
                    :aria-label="$t('flowGroups.removeStep', { n: r + 1 })"
                    @click="removeStep(r)"
                  ><X :size="12" /></button>
                </div>

                <div
                  v-for="c in cols"
                  :key="`c${r}-${c}`"
                  class="cell"
                  :class="{
                    filled: !!row[c - 1],
                    over: dropOn === `${r}-${c - 1}`,
                    [`st-${stateOf(r, c - 1)?.state}`]: !!stateOf(r, c - 1),
                  }"
                  @dragover="canEdit && overCell($event, `${r}-${c - 1}`)"
                  @dragleave="leaveCell($event, `${r}-${c - 1}`)"
                  @drop="canEdit && dropCell($event, r, c - 1)"
                >
                  <template v-if="row[c - 1]">
                    <div
                      class="card"
                      :draggable="canEdit"
                      @dragstart="startDrag($event, { ...row[c - 1]!, from: [r, c - 1] }, labelFor(row[c - 1]!).name ?? '')"
                      @dragend="endDrag"
                    >
                      <div class="ctop">
                        <GripVertical v-if="canEdit" :size="13" class="grip" />
                        <component :is="KIND_ICON[row[c - 1]!.kind]" :size="14" class="kicon" :class="row[c - 1]!.kind" />
                        <span v-if="labelFor(row[c - 1]!).hidden" class="cname muted"><EyeOff :size="13" /> {{ $t('flowGroups.hiddenCell') }}</span>
                        <span v-else-if="labelFor(row[c - 1]!).missing" class="cname gone"><Ban :size="13" /> {{ $t('flowGroups.missingCell') }}</span>
                        <span v-else class="cname" :title="labelFor(row[c - 1]!).name ?? ''">{{ labelFor(row[c - 1]!).name }}</span>
                        <button
                          v-if="row[c - 1]!.kind === 'container' || row[c - 1]!.kind === 'pushmail'"
                          class="mini ghost"
                          :title="row[c - 1]!.kind === 'pushmail' ? $t('pushmail.edit') : $t('flowGroups.editContainer')"
                          :aria-label="row[c - 1]!.kind === 'pushmail' ? $t('pushmail.edit') : $t('flowGroups.editContainer')"
                          @click="editCell(r, c - 1)"
                        ><Settings2 :size="12" /></button>
                        <button
                          v-if="canEdit"
                          class="mini ghost cdel"
                          :title="$t('flowGroups.removeCell')"
                          :aria-label="$t('flowGroups.removeCell')"
                          @click="clearCell(r, c - 1)"
                        ><X :size="12" /></button>
                      </div>
                      <div class="cpath muted">
                        {{ $t(KIND_LABEL[row[c - 1]!.kind]) }}<template v-if="labelFor(row[c - 1]!).path"> · {{ labelFor(row[c - 1]!).path }}</template>
                      </div>
                      <div v-if="stateOf(r, c - 1)" class="cstate">
                        <component
                          :is="STATE_ICON[stateOf(r, c - 1)!.state]"
                          :size="13"
                          :class="{ spin: stateOf(r, c - 1)!.state === 'running' }"
                        />
                        {{ $t(`flowGroups.state_${stateOf(r, c - 1)!.state}`) }}
                        <span v-if="stateOf(r, c - 1)!.waited" class="muted">· {{ $t('flowGroups.waited') }}</span>
                      </div>
                      <div v-if="stateOf(r, c - 1)?.error" class="cerr" :title="stateOf(r, c - 1)!.error ?? ''">
                        {{ stateOf(r, c - 1)!.error }}
                      </div>
                    </div>
                  </template>
                  <Select
                    v-else-if="canEdit"
                    class="cellpick"
                    :model-value="null"
                    :options="optionsFor(r)"
                    searchable
                    :placeholder="$t('flowGroups.pickPlaceholder')"
                    :aria-label="$t('flowGroups.pickLabel', { step: r + 1, col: c })"
                    @update:model-value="(v: any) => pick(r, c - 1, v)"
                  />
                </div>
                <div v-if="canEdit" />
              </template>
            </div>

            <button
              v-if="canEdit"
              class="addstep"
              :class="{ over: dropOn === 'new' }"
              :disabled="grid.length >= MAX_GROUP_STEPS"
              :title="grid.length >= MAX_GROUP_STEPS ? $t('flowGroups.limitSteps', { n: MAX_GROUP_STEPS }) : undefined"
              @click="addStep"
              @dragover="overCell($event, 'new')"
              @dragleave="leaveCell($event, 'new')"
              @drop="dropNewStep"
            >
              <Plus :size="14" /> {{ $t('flowGroups.addStep') }}
            </button>
          </div>
        </section>

        <!-- what can go in a cell -->
        <aside v-if="canEdit" class="catalog" :aria-label="$t('flowGroups.catalogTitle')">
          <div class="cat-head">{{ $t('flowGroups.catalogTitle') }}</div>
          <span class="searchbox">
            <Search :size="14" />
            <input v-model="catalogQ" type="text" :placeholder="$t('flowGroups.catalogSearch')" :aria-label="$t('flowGroups.catalogSearch')" />
            <button v-if="catalogQ" class="x" :aria-label="$t('flowGroups.clearSearch')" @click="catalogQ = ''"><X :size="12" /></button>
          </span>
          <p class="cat-hint muted">{{ $t('flowGroups.catalogHint') }}</p>

          <template v-for="section in ([
            'flows', 'refresh', ...(scripts.length ? ['script'] as const : []),
            ...(sqlScripts.some((x) => x.output) ? ['sql'] as const : []), ...(catalog.container.length ? ['container'] as const : []),
            ...(catalog.pushmail.length ? ['pushmail'] as const : []),
          ] as const)" :key="section">
            <div class="cat-group">
              {{ $t(section === 'flows' ? SECTION_LABEL.flow : SECTION_LABEL[section]) }}
              <span class="n">{{ catalog[section].length }}</span>
            </div>
            <ul class="cat-list">
              <li
                v-for="i in catalog[section]"
                :key="i.key"
                class="cat-item"
                draggable="true"
                :title="$t('flowGroups.dragHint', { name: i.name })"
                @dragstart="startDrag($event, { kind: i.kind, id: i.id }, i.name)"
                @dragend="endDrag"
              >
                <GripVertical :size="13" class="grip" />
                <component :is="KIND_ICON[i.kind]" :size="14" class="kicon" :class="i.kind" />
                <span class="ci-text">
                  <span class="ci-name">{{ i.name }}</span>
                  <span v-if="i.path" class="ci-path muted">{{ i.path }}</span>
                </span>
                <Check v-if="used.has(i.key)" :size="13" class="ci-used" :aria-label="$t('flowGroups.inGroup')" />
              </li>
              <li v-if="!catalog[section].length" class="cat-none muted">
                {{ catalogQ ? $t('flowGroups.catalogNoMatch', { q: catalogQ }) : $t('flowGroups.catalogNothing') }}
              </li>
            </ul>
          </template>
        </aside>
      </div>

      <Modal :open="editingCell?.kind === 'pushmail'" :title="$t('pushmail.title')" :width="620" @close="editing = null">
        <PushmailCellPanel
          v-if="editingCell?.kind === 'pushmail' && editingCell.pushmail"
          :config="editingCell.pushmail"
          :connections="smtpConnections"
          :datasources="datasources"
          :reports="reports"
          :group-name="name"
          :read-only="!canEdit"
          @update="patchPushmail"
          @delete="removeEditing"
        />
      </Modal>
      <Modal :open="editingCell?.kind === 'container'" :title="$t('flowGroups.containerTitle')" :width="560" @close="editing = null">
        <ContainerNodePanel
          v-if="editingCell?.kind === 'container'"
          :node="{ id: `container-cell-${editingCell.id}`, position: { x: 0, y: 0 }, data: editingCell.container ?? {} } as any"
          :read-only="!canEdit"
          in-group
          @update="patchContainer"
          @delete="removeEditing"
        />
      </Modal>

      <ScheduleDialog
        :open="scheduleOpen"
        :title="name"
        :subtitle="$t('flowGroups.scheduleSubtitle')"
        :current="detail?.run_schedule ?? null"
        :busy="savingSchedule"
        @save="saveSchedule"
        @cancel="scheduleOpen = false"
      />
    </div>
  </AppShell>
</template>

<style scoped>
.fg { display: flex; flex-direction: column; min-height: 0; }

/* ── head: name, where it lives, the actions ─────────────────────────────── */
.page-head { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; }
.page-head h1 { display: inline-flex; align-items: center; gap: 8px; margin: 0; flex: 0 1 380px; min-width: 0; }
.gname { font-size: 16px; font-weight: 600; padding: 4px 9px; }
.gname[readonly] { border-color: transparent; background: transparent; padding-left: 0; }
.where { font-size: 12.5px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; min-width: 0; flex: 1; }
.head-actions { display: flex; align-items: center; gap: 8px; margin-left: auto; flex-shrink: 0; }
.head-actions button { white-space: nowrap; }
.cron { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
.tag {
  font-size: 10px; text-transform: uppercase; letter-spacing: 0.04em; padding: 1px 7px;
  border-radius: 8px; background: var(--panel-2); border: 1px solid var(--border); color: var(--muted);
}
.err { color: var(--danger); }

/* ── work area: the board and, beside it, the catalog ────────────────────── */
.work { display: grid; grid-template-columns: minmax(0, 1fr) 300px; gap: 16px; align-items: start; }
.work.readonly { grid-template-columns: minmax(0, 1fr); }
.board { min-width: 0; display: flex; flex-direction: column; gap: 10px; }

.runbar { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; font-size: 12.5px; min-height: 26px; }
.rstate { display: inline-flex; align-items: center; gap: 5px; font-weight: 600; }
.rstate.success { color: var(--accent-2); }
.rstate.failure { color: var(--danger); }
.rstate.pending, .rstate.started { color: var(--accent); }
.rerr { color: var(--danger); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 48ch; }
.next { display: inline-flex; align-items: center; gap: 5px; }
.histbtn { margin-left: auto; display: inline-flex; align-items: center; gap: 5px; }
.histbtn .n { opacity: 0.7; font-variant-numeric: tabular-nums; }
.activebtn { border-color: var(--accent); }

.history { list-style: none; margin: 0; padding: 4px; display: flex; flex-direction: column; gap: 2px; border: 1px solid var(--border); border-radius: 10px; background: var(--panel); max-height: 220px; overflow-y: auto; }
.hrun { display: flex; align-items: center; justify-content: flex-start; gap: 10px; width: 100%; text-align: left; font-size: 12px; padding: 4px 8px; border: none; background: transparent; border-radius: 6px; min-height: 0; }
.hrun:hover { background: var(--panel-2); box-shadow: none; }
.hrun.on { background: var(--tint-accent); }
.hwhen { min-width: 120px; font-variant-numeric: tabular-nums; }
.ok { color: var(--accent-2); }
.ko { color: var(--danger); }
.wip { color: var(--accent); }

.lead { font-size: 12.5px; margin: 0; }

/* ── the grid ────────────────────────────────────────────────────────────── */
/* room for the halo of the cells at the edges: the scrolling box would cut it */
.gridwrap { overflow-x: auto; padding: 6px 18px 8px 4px; }
.steps { display: grid; gap: 8px; align-items: stretch; min-width: min-content; }
.corner { min-height: 0; }
.colhead, .stephead {
  display: flex; align-items: center; gap: 4px;
  font-size: 11.5px; font-weight: 600; letter-spacing: 0.05em; text-transform: uppercase; color: var(--muted);
}
.colhead { justify-content: space-between; padding: 0 2px; min-height: 22px; }
.stephead { justify-content: space-between; padding-right: 2px; }
.stepno { white-space: nowrap; }
.addcol { display: flex; align-items: center; }
button.mini { padding: 2px 8px; min-height: 24px; }
.mini.ghost { border-color: transparent; background: transparent; color: var(--muted); padding: 2px 4px; }
.mini.ghost:hover { color: var(--text); border-color: var(--border); }

.cell {
  min-height: 74px; border: 1px dashed var(--border); border-radius: 10px;
  display: flex; align-items: stretch; transition: background 0.12s, border-color 0.12s;
}
.cell.filled { border-style: solid; border-color: transparent; }
.cell.over { border: 1px dashed var(--accent); background: var(--tint-accent); }
.cell :deep(.cellpick) { flex: 1; align-self: center; margin: 0 10px; border-style: dashed; background: transparent; font-size: 12.5px; }

.card {
  flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 3px;
  padding: 8px 10px; border: 1px solid var(--border); border-radius: 10px; background: var(--panel);
}
.card[draggable='true'] { cursor: grab; }
.card[draggable='true']:active { cursor: grabbing; }
/* the outcome of a cell reads at a glance across the grid: a halo in the colour of
   its state, next to the words that say it (colour is never alone) */
.card { --halo: transparent; transition: border-color 0.2s, box-shadow 0.2s; }
.st-succeeded .card { --halo: var(--accent-2); }
.st-failed .card, .st-interrupted .card { --halo: var(--danger); }
.st-running .card { --halo: var(--accent); animation: halo-breathe 1.8s ease-in-out infinite; }
.st-succeeded .card, .st-failed .card, .st-interrupted .card, .st-running .card {
  border-color: var(--halo);
  box-shadow: 0 0 0 1px color-mix(in srgb, var(--halo) 40%, transparent),
              0 0 14px 1px color-mix(in srgb, var(--halo) 38%, transparent);
}
/* running: the halo breathes, so a live cell is told from a finished one */
@keyframes halo-breathe {
  50% {
    box-shadow: 0 0 0 1px color-mix(in srgb, var(--halo) 25%, transparent),
                0 0 6px 0 color-mix(in srgb, var(--halo) 20%, transparent);
  }
}
@media (prefers-reduced-motion: reduce) {
  .st-running .card { animation: none; }
}
.ctop { display: flex; align-items: center; gap: 6px; min-width: 0; }
.grip { color: var(--muted); flex-shrink: 0; opacity: 0.7; }
.kicon { flex-shrink: 0; }
.kicon.flow { color: var(--accent); }
.kicon.refresh { color: var(--violet); }
.kicon.script { color: var(--warning); }
.kicon.sql { color: var(--accent-2); }
.kicon.container { color: #38bdf8; }
.kicon.pushmail { color: #f472b6; }
/* the name is never cut short: two flows may differ only at its end */
.cname { flex: 1; min-width: 0; font-weight: 500; overflow-wrap: anywhere; display: inline-flex; align-items: center; gap: 5px; }
.cname.gone { color: var(--danger); }
.cdel { margin-left: auto; opacity: 0; }
.card:hover .cdel, .cdel:focus-visible { opacity: 1; }
.cpath { font-size: 11.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.cstate { display: inline-flex; align-items: center; gap: 5px; font-size: 12px; color: var(--muted); }
.st-running .cstate { color: var(--accent); }
.st-succeeded .cstate { color: var(--accent-2); }
.st-failed .cstate, .st-interrupted .cstate { color: var(--danger); }
.cerr { font-size: 11.5px; color: var(--danger); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.addstep {
  margin-top: 8px; margin-left: 84px; display: inline-flex; align-items: center; gap: 6px;
  border-style: dashed; background: transparent; color: var(--muted);
}
.addstep:hover:not(:disabled), .addstep.over { color: var(--text); border-color: var(--accent); background: var(--tint-accent); }

/* ── the catalog ─────────────────────────────────────────────────────────── */
.catalog {
  position: sticky; top: 68px; max-height: calc(100vh - 92px); overflow-y: auto;
  display: flex; flex-direction: column; gap: 8px;
  padding: 12px; border: 1px solid var(--border); border-radius: 10px; background: var(--panel);
}
.cat-head { font-weight: 600; font-size: 13px; }
.searchbox {
  display: flex; align-items: center; gap: 7px; padding: 5px 9px;
  border: 1px solid var(--control-border); border-radius: 8px; background: var(--bg-soft); color: var(--muted);
}
.searchbox input { border: none; background: transparent; outline: none; color: var(--text); flex: 1; min-width: 0; font-size: 13px; padding: 0; box-shadow: none; }
.searchbox .x { padding: 1px 5px; min-height: 0; }
.cat-hint { font-size: 11.5px; line-height: 1.4; margin: 0; }
.cat-group {
  margin-top: 6px; font-size: 11.5px; font-weight: 600; letter-spacing: 0.05em; text-transform: uppercase; color: var(--muted);
}
.cat-group .n { font-variant-numeric: tabular-nums; opacity: 0.75; margin-left: 4px; }
.cat-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 2px; }
.cat-item {
  display: flex; align-items: center; gap: 7px; padding: 6px 6px; border-radius: 8px; cursor: grab;
  border: 1px solid transparent;
}
.cat-item:hover { background: var(--panel-2); border-color: var(--border); }
.cat-item:active { cursor: grabbing; }
.ci-text { display: flex; flex-direction: column; min-width: 0; flex: 1; }
/* the name is never cut short: it wraps, it is what tells two items apart */
.ci-name { font-size: 13px; overflow-wrap: anywhere; }
.ci-path { font-size: 11.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ci-used { color: var(--accent-2); flex-shrink: 0; }
.cat-none { font-size: 12px; padding: 4px 6px; }

@media (max-width: 900px) {
  .work { grid-template-columns: minmax(0, 1fr); }
  .catalog { position: static; max-height: none; }
  .page-head { flex-wrap: wrap; }
  .where { flex-basis: 100%; order: 3; }
}
</style>
