<script setup lang="ts">
// Explore: FOLDER navigation with a single list of resources.
//
// Before there were two boxes: a fixed 320px tree on the left and, on the right, up to
// seven stacked sections of which FOUR were the very same table (flows, datasources,
// views, connections). The result was crowded and without rhythm: nothing said at a
// glance what a folder held.
//
// Now: a breadcrumb for the path, the subfolders as rows of the list together with
// everything else, a kind selector with the counts, and a search by name. The folder's
// administration (permissions, subfolders, deletion) leaves the data's scrolling and
// lives in a sliding side panel: the destructive actions are no longer met by chance
// while reading one's own flows.
//
// The search here is LOCAL (it filters what is loaded already): instant and without
// network. The one crossing the folders is `/search` (see useSearch.ts).
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { knownError } from '~/composables/knownErrors'
import { useFormat } from '~/composables/useFormat'
import { useFolderPaths } from '~/composables/useFolderPaths'
import {
  ChevronRight,
  Folder,
  RefreshCw,
  User as UserIcon,
  Users as UsersIcon,
  X,
  Trash2,
  Plus,
  Workflow,
  FolderInput,
  Database,
  History,
  CheckCircle2,
  XCircle,
  LoaderCircle,
  Plug,
  Pencil,
  Bookmark,
  Search,
  Settings2,
  Home,
  ScrollText,
  Rows3,
  Copy,
  PencilLine,
  Lock,
  FileCode2,
  DatabaseSearch,
  NotebookPen,
} from 'lucide-vue-next'
import { errMessage, useApi } from '~/composables/useApi'
import { useFlows, type FlowSummary } from '~/composables/useFlows'
import { useRuns, type RunInfo } from '~/composables/useRuns'
import RunProgressText from '~/components/ui/RunProgressText.vue'
import { usePopover } from '~/composables/usePopover'
import { useDialogA11y } from '~/composables/useDialogA11y'
import { useDatasources, type ApiDatasourceDraft, type DatasourceInfo, type DbDatasourceDraft, type S3DatasourceDraft, type SharePointDatasourceDraft, type SftpDatasourceDraft } from '~/composables/useDatasources'
import { runNotes, notesText } from '~/composables/usePlaceholders'
import { useConnections, type ConnectionInfo, type ConnectionDraft } from '~/composables/useConnections'
import { connectionChanges } from '~/composables/connectionChanges'
import { useSavedViews, type SavedView } from '~/composables/useSavedViews'
import { useFlowGroups, type FlowGroup } from '~/composables/useFlowGroups'
import { useSavedScripts, type SavedScript } from '~/composables/useSavedScripts'
import { useScriptingEditor } from '~/composables/useScripting'
import { useSqlScripts, type SqlScript } from '~/composables/useSqlScripts'
import { useNotebooks, type Notebook } from '~/composables/useNotebooks'
import ContractBadge from '~/components/ui/ContractBadge.vue'
import ContractDialog from '~/components/ui/ContractDialog.vue'
import {
  useProjects,
  CAPABILITIES,
  type Project,
  type Permission,
  type GroupOut,
  type UserOut,
} from '~/composables/useProjects'

const api = useProjects()
const coreApi = useApi()
const { preferredEngine } = usePreferredEngine()
const flowsApi = useFlows()
const runsApi = useRuns()
const dsApi = useDatasources()
const connApi = useConnections()
const viewsApi = useSavedViews()
const groupsApi = useFlowGroups()
const scriptsApi = useSavedScripts()
const sqlApi = useSqlScripts()
const notebooksApi = useNotebooks()
// «New → Script» only for whoever may use a scripting environment (Kubernetes)
const scriptingEditor = useScriptingEditor()
onMounted(() => { scriptingEditor.load() })
// «New → Notebook» the same, with a Python environment among them (marimo is Python)
const notebooksAvailable = computed(() => scriptingEditor.available.value
  && !!scriptingEditor.state.value?.environments.some((e) => e.language === 'python'))
const { user } = useAuth()
const toast = useToast()
const { t } = useI18n()
const fmtx = useFormat()
const folderPaths = useFolderPaths()
const route = useRoute()

const projects = ref<Project[]>([])
const currentId = ref<number | null>(null)
const error = ref('')

const flows = ref<FlowSummary[]>([])
const flowGroups = ref<FlowGroup[]>([])
const dsList = ref<DatasourceInfo[]>([])
const contractFor = ref<DatasourceInfo | null>(null) // the datasource whose data contract is open
const savedViews = ref<SavedView[]>([])
const scripts = ref<SavedScript[]>([])
const sqlScripts = ref<SqlScript[]>([])
const notebooks = ref<Notebook[]>([])
const connections = ref<ConnectionInfo[]>([])
const canConnect = ref(false)
const canManage = ref(false)
const permissions = ref<Permission[]>([])
const groups = ref<GroupOut[]>([])
const users = ref<UserOut[]>([])
const loading = ref(false)

const isSuper = computed(() => !!user.value?.is_superuser)
const current = computed(() => projects.value.find((p) => p.id === currentId.value) ?? null)

// ── Path and navigation ──────────────────────────────────────────────────
const byId = computed(() => new Map(projects.value.map((p) => [p.id, p])))

/**
 * The root → current folder chain. It replaces the tree: the path reads in one line
 * instead of in a 320px-wide column.
 */
const breadcrumb = computed<Project[]>(() => {
  const out: Project[] = []
  let cur = current.value
  const seen = new Set<number>()
  while (cur && !seen.has(cur.id)) {
    out.unshift(cur)
    seen.add(cur.id)
    cur = cur.parent_id != null ? byId.value.get(cur.parent_id) ?? null : null
  }
  return out
})

function childrenOf(parentId: number | null): Project[] {
  const ids = byId.value
  return projects.value
    .filter((p) => (parentId === null ? p.parent_id === null || !ids.has(p.parent_id) : p.parent_id === parentId))
    .sort((a, b) => a.name.localeCompare(b.name))
}

function openFolder(id: number | null) {
  currentId.value = id
  // the path is in the URL: reloading the page or going back with the browser brings back
  // where one was, and `/search` can link a folder
  navigateTo(id == null ? '/' : `/?folder=${id}`, { replace: true })
  loadFolder(id)
}

// ── Unified row: one kind of row for six kinds of resource ────────────
type Kind = 'folder' | 'flow' | 'group' | 'datasource' | 'view' | 'script' | 'sql' | 'notebook' | 'connection'
interface Row {
  kind: Kind
  id: number
  name: string
  meta: string
  /** import in progress with its progress: the cell shows it with its clock */
  progressRun?: RunInfo
  /** set = the row is a link; otherwise it is only text */
  to?: string
  /** a flow this person cannot open (a container registry they have no access to):
   *  listed, greyed out, with the reason; no link, no action */
  locked?: string
  raw: any
}

const KIND_ICON: Record<Kind, any> = {
  folder: Folder,
  flow: Workflow,
  group: Rows3,
  datasource: Database,
  view: Bookmark,
  script: FileCode2,
  sql: DatabaseSearch,
  notebook: NotebookPen,
  connection: Plug,
}

function fmtDate(iso: string | null): string {
  return iso ? fmtx.dateTime(iso) : ''
}

const rows = computed<Row[]>(() => {
  const out: Row[] = []
  for (const p of childrenOf(currentId.value)) {
    out.push({ kind: 'folder', id: p.id, name: p.name, meta: p.description || '', raw: p })
  }
  for (const f of flows.value) {
    out.push(f.locked_reason
      ? { kind: 'flow', id: f.id, name: f.name, meta: fmtDate(f.updated_at), locked: f.locked_reason, raw: f }
      : { kind: 'flow', id: f.id, name: f.name, meta: fmtDate(f.updated_at), to: `/editor?flow=${f.id}`, raw: f })
  }
  for (const g of flowGroups.value) {
    const steps = g.definition.steps.length
    const meta = [t('projectBrowser.groupSteps', { n: steps }, steps), fmtDate(g.updated_at)].join(' · ')
    out.push({ kind: 'group', id: g.id, name: g.name, meta, to: `/flow-group?id=${g.id}`, raw: g })
  }
  for (const d of dsList.value) {
    const state = ingestRuns.value[d.id]
    const running = !!state && !isTerminal(state)
    const meta = running
      ? t('projectBrowser.importingLabel')
      : state?.status === 'FAILURE'
        ? t('projectBrowser.importFailedLabel')
        : [d.rows != null ? t('projectBrowser.rowsCount', { n: fmtx.count(d.rows) }) : '', fmtDate(d.refreshed_at ?? d.updated_at),
            state?.status === 'SUCCESS' && runNotes(state.notes) ? notesText(runNotes(state.notes)!, t) : '']
            .filter(Boolean)
            .join(' · ')
    out.push({ kind: 'datasource', id: d.id, name: d.name, meta, raw: d, progressRun: running && state?.progress ? state : undefined })
  }
  for (const v of savedViews.value) {
    out.push({ kind: 'view', id: v.id, name: v.name, meta: v.datasource_name ?? '', to: `/viewer?view=${v.id}`, raw: v })
  }
  for (const sc of scripts.value) {
    const meta = [sc.environment_name ?? '', `v${sc.current_version}`, fmtDate(sc.updated_at)].filter(Boolean).join(' · ')
    out.push({ kind: 'script', id: sc.id, name: sc.name, meta, to: `/script?id=${sc.id}`, raw: sc })
  }
  for (const q of sqlScripts.value) {
    const meta = [`v${q.current_version}`, fmtDate(q.updated_at)].filter(Boolean).join(' · ')
    out.push({ kind: 'sql', id: q.id, name: q.name, meta, to: `/sql-script?id=${q.id}`, raw: q })
  }
  for (const n of notebooks.value) {
    const meta = [n.environment_name ?? '', `v${n.current_version}`, fmtDate(n.updated_at)].filter(Boolean).join(' · ')
    out.push({ kind: 'notebook', id: n.id, name: n.name, meta, to: `/notebook?id=${n.id}`, raw: n })
  }
  for (const c of connections.value) {
    out.push({
      kind: 'connection', id: c.id, name: c.name,
      meta: `${c.db_type}${c.database ? ' · ' + c.database : ''}`, raw: c,
    })
  }
  return out
})

// ── Local search + kind filter ────────────────────────────────────────
const q = ref('')
const kindFilter = ref<Kind | null>(null)

const matching = computed(() => {
  const ago = q.value.trim().toLowerCase()
  return ago ? rows.value.filter((r) => r.name.toLowerCase().includes(ago)) : rows.value
})
/**
 * The counts follow the SEARCH but not the kind filter: clicking a segment, the others'
 * numbers must not disappear.
 */
const counts = computed(() => {
  const c: Record<string, number> = {}
  for (const r of matching.value) c[r.kind] = (c[r.kind] ?? 0) + 1
  return c
})
const visibleRows = computed(() =>
  kindFilter.value ? matching.value.filter((r) => r.kind === kindFilter.value) : matching.value,
)
const segments = computed(() =>
  (['folder', 'flow', 'group', 'datasource', 'view', 'script', 'sql', 'notebook', 'connection'] as Kind[])
    .filter((k) => counts.value[k])
    .map((k) => ({ kind: k, n: counts.value[k] })),
)

// ── Loading ─────────────────────────────────────────────────────────────
async function loadProjects() {
  error.value = ''
  try {
    projects.value = await api.list()
  } catch (e) {
    error.value = errMessage(e)
  }
}

async function loadFolder(id: number | null) {
  flows.value = []
  flowGroups.value = []
  dsList.value = []
  savedViews.value = []
  scripts.value = []
  sqlScripts.value = []
  notebooks.value = []
  connections.value = []
  permissions.value = []
  canManage.value = false
  canConnect.value = false
  expandedFlowId.value = null
  ingestToken++ // stops the previous folder's polls
  ingestRuns.value = {}
  if (id == null) return

  loading.value = true
  try {
    try {
      flows.value = await flowsApi.listByProject(id)
    } catch (e) {
      error.value = errMessage(e)
    }
    try {
      dsList.value = await dsApi.listByProject(id)
      for (const d of dsList.value) {
        // the server says `refreshing`: it holds for a refresh started from another page, by
        // another user or by the scheduler too
        if (d.kind === 'database' && (d.rows == null || d.refreshing)) pollIngest(d.id, ingestToken)
      }
    } catch { dsList.value = [] }
    try { flowGroups.value = await groupsApi.listByProject(id) } catch { flowGroups.value = [] }
    try { savedViews.value = await viewsApi.listByProject(id) } catch { savedViews.value = [] }
    // only the scripts this person may see; none without scripting
    try { scripts.value = await scriptsApi.listByProject(id) } catch { scripts.value = [] }
    try { sqlScripts.value = await sqlApi.listByProject(id) } catch { sqlScripts.value = [] }
    try { notebooks.value = await notebooksApi.listByProject(id) } catch { notebooks.value = [] }
    // connections require CONNECT: the refusal is information, not an error
    try {
      connections.value = await connApi.listByProject(id)
      canConnect.value = true
    } catch { connections.value = []; canConnect.value = false }
    // if the permissions can be read, we have MANAGE
    try {
      permissions.value = await api.permissions(id)
      canManage.value = true
      if (!groups.value.length) groups.value = await api.groups()
      if (isSuper.value && !users.value.length) users.value = await api.users()
    } catch { canManage.value = false }
  } finally {
    loading.value = false
  }
}

onMounted(async () => {
  await loadProjects()
  const linked = Number(route.query.folder)
  currentId.value = Number.isInteger(linked) && linked > 0 ? linked : null
  await loadFolder(currentId.value)
  try { engines.value = await coreApi.engines() } catch { /* Polars stays */ }
})

// creating or editing in another tab, coming back here reloads
function onVisible() {
  if (document.visibilityState !== 'visible') return
  loadProjects()
  loadFolder(currentId.value)
}
onMounted(() => document.addEventListener('visibilitychange', onVisible))
onUnmounted(() => {
  document.removeEventListener('visibilitychange', onVisible)
  expandedFlowId.value = null
  ingestToken++
})

// ── Run history (expandable, auto-refreshing) ────────────────────
const expandedFlowId = ref<number | null>(null)
const flowRuns = ref<RunInfo[]>([])
const runsLoading = ref(false)
const isTerminal = (r: RunInfo) => r.status === 'SUCCESS' || r.status === 'FAILURE'

async function loadRuns(flowId: number) {
  try {
    const rows = await runsApi.listByFlow(flowId) // the GET reconciles the states
    if (expandedFlowId.value !== flowId) return // stale answer
    flowRuns.value = rows
    if (rows.some((r) => !isTerminal(r))) {
      setTimeout(() => { if (expandedFlowId.value === flowId) loadRuns(flowId) }, 2500)
    }
  } catch (e) {
    if (expandedFlowId.value === flowId) error.value = errMessage(e)
  } finally {
    runsLoading.value = false
  }
}

function toggleRuns(flow: FlowSummary) {
  if (expandedFlowId.value === flow.id) { expandedFlowId.value = null; return }
  expandedFlowId.value = flow.id
  flowRuns.value = []
  runsLoading.value = true
  loadRuns(flow.id)
}

function fmtDuration(run: RunInfo): string {
  if (!run.started_at || !run.finished_at) return '—'
  const ms = new Date(run.finished_at).getTime() - new Date(run.started_at).getTime()
  if (ms < 1000) return '<1s'
  const s = Math.round(ms / 1000)
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`
}

// ── Import/refresh of the database datasources ──────────────────────────────
const ingestRuns = ref<Record<number, RunInfo>>({})
let ingestToken = 0
// imports seen running here: their end gets a message (one found already over does not)
const watchedIngest = new Set<number>()

async function pollIngest(dsId: number, token: number) {
  try {
    const runs = await dsApi.listRuns(dsId)
    if (token !== ingestToken) return
    const last = runs[0]
    if (!last) return
    ingestRuns.value = { ...ingestRuns.value, [dsId]: last }
    if (!isTerminal(last)) {
      watchedIngest.add(dsId)
      setTimeout(() => { if (token === ingestToken) pollIngest(dsId, token) }, 2500)
      return
    }
    if (watchedIngest.delete(dsId)) {
      const name = dsList.value.find((d) => d.id === dsId)?.name ?? `#${dsId}`
      if (last.status === 'SUCCESS') {
        toast.success(t('datasources.announceDone', { name, n: fmtx.count(last.rows_written ?? 0) }))
        // a range whose files were not all there, keys on several rows: imported, but said
        const notes = runNotes(last.notes)
        if (notes) toast.warning(t('placeholders.notesToast', { name, notes: notesText(notes, t) }))
      } else toast.error(last.error ? `${t('datasources.announceFailed', { name })}: ${last.error}` : t('datasources.announceFailed', { name }))
    }
    if (last.status === 'SUCCESS' && currentId.value) {
      dsList.value = await dsApi.listByProject(currentId.value)
    }
  } catch { /* transient: it will retry at the next refresh */ }
}

async function refreshDatasource(ds: DatasourceInfo) {
  try {
    const run = await dsApi.refresh(ds.id)
    ingestRuns.value = { ...ingestRuns.value, [ds.id]: run }
    pollIngest(ds.id, ingestToken)
  } catch (e) {
    toast.error(errMessage(e))
  }
}

// ── Deletions and moving ──────────────────────────────────────────────
// Every kind of row moves to another folder; the gateway checks the rights on both
// folders. The row leaves this list in place, with no reload of the folder (a
// blanked list reads as a crash).
const moving = ref<string | null>(null) // `${kind}-${id}` of the row whose picker is open
const rowKey = (r: Row) => `${r.kind}-${r.id}`

function descendantsOf(id: number): Set<number> {
  const out = new Set<number>([id])
  let grew = true
  while (grew) {
    grew = false
    for (const p of projects.value) {
      if (p.parent_id != null && out.has(p.parent_id) && !out.has(p.id)) {
        out.add(p.id)
        grew = true
      }
    }
  }
  return out
}

// where a row can go: any folder but its own, and for a folder not itself nor
// anything inside it; same-named folders are told apart by their path
function moveTargets(row: Row) {
  const from = row.kind === 'folder' ? row.raw.parent_id : row.raw.project_id
  const banned = row.kind === 'folder' ? descendantsOf(row.id) : new Set<number>()
  return projects.value
    .filter((p) => p.id !== from && !banned.has(p.id))
    .map((p) => ({ value: p.id, label: p.name, hint: folderPaths.pathOf(p.parent_id) }))
}

async function moveRow(row: Row, target: number | null) {
  moving.value = null
  const from = row.kind === 'folder' ? row.raw.parent_id : row.raw.project_id
  if (!target || target === from) return
  try {
    if (row.kind === 'flow') {
      await flowsApi.update(row.id, { project_id: target })
      flows.value = flows.value.filter((f) => f.id !== row.id)
    } else if (row.kind === 'group') {
      await groupsApi.update(row.id, { project_id: target })
      flowGroups.value = flowGroups.value.filter((g) => g.id !== row.id)
    } else if (row.kind === 'datasource') {
      await dsApi.update(row.id, { project_id: target })
      dsList.value = dsList.value.filter((d) => d.id !== row.id)
    } else if (row.kind === 'view') {
      await viewsApi.update(row.id, { project_id: target })
      savedViews.value = savedViews.value.filter((v) => v.id !== row.id)
    } else if (row.kind === 'script') {
      await scriptsApi.update(row.id, { project_id: target })
      scripts.value = scripts.value.filter((x) => x.id !== row.id)
    } else if (row.kind === 'sql') {
      await sqlApi.update(row.id, { project_id: target })
      sqlScripts.value = sqlScripts.value.filter((x) => x.id !== row.id)
    } else if (row.kind === 'notebook') {
      await notebooksApi.update(row.id, { project_id: target })
      notebooks.value = notebooks.value.filter((x) => x.id !== row.id)
    } else if (row.kind === 'connection') {
      await connApi.update(row.id, { project_id: target })
      connections.value = connections.value.filter((c) => c.id !== row.id)
    } else {
      await api.update(row.id, { parent_id: target })
      projects.value = projects.value.map((p) => (p.id === row.id ? { ...p, parent_id: target } : p))
      folderPaths.load(true) // the paths under this folder changed
    }
    const folder = projects.value.find((p) => p.id === target)?.name ?? `#${target}`
    toast.success(t('projectBrowser.movedTo', { name: row.name, folder }))
  } catch (e) {
    toast.error(errMessage(e))
  }
}

// rename in place, as a file manager does: Enter or leaving the field keeps the
// name, Escape gives up; a refused name (taken, empty) keeps the field open
const renaming = ref<string | null>(null)
const renameDraft = ref('')
const renameBusy = ref(false)
const RENAMABLE = new Set<Kind>(['folder', 'flow', 'group', 'datasource', 'view', 'script', 'sql', 'notebook'])

function startRename(row: Row) {
  renaming.value = rowKey(row)
  renameDraft.value = row.name
  nextTick(() => {
    const input = document.querySelector<HTMLInputElement>('input.rename-input')
    input?.focus()
    input?.select()
  })
}

function cancelRename() {
  if (!renameBusy.value) renaming.value = null
}

async function commitRename(row: Row) {
  if (renameBusy.value || renaming.value !== rowKey(row)) return
  const name = renameDraft.value.trim()
  if (!name || name === row.name) {
    renaming.value = null
    return
  }
  renameBusy.value = true
  const rename = <T extends { id: number; name: string }>(list: T[]) =>
    list.map((x) => (x.id === row.id ? { ...x, name } : x)).sort(byName)
  try {
    if (row.kind === 'flow') {
      await flowsApi.update(row.id, { name })
      flows.value = rename(flows.value)
    } else if (row.kind === 'group') {
      await groupsApi.update(row.id, { name })
      flowGroups.value = rename(flowGroups.value)
    } else if (row.kind === 'datasource') {
      await dsApi.update(row.id, { name })
      dsList.value = rename(dsList.value)
      // the views beside it name it in their row
      savedViews.value = savedViews.value.map((v) => (v.datasource_id === row.id ? { ...v, datasource_name: name } : v))
    } else if (row.kind === 'view') {
      await viewsApi.update(row.id, { name })
      savedViews.value = rename(savedViews.value)
    } else if (row.kind === 'script') {
      await scriptsApi.update(row.id, { name })
      scripts.value = rename(scripts.value)
    } else if (row.kind === 'sql') {
      await sqlApi.update(row.id, { name })
      sqlScripts.value = rename(sqlScripts.value)
    } else if (row.kind === 'notebook') {
      await notebooksApi.update(row.id, { name })
      notebooks.value = rename(notebooks.value)
    } else if (row.kind === 'folder') {
      await api.update(row.id, { name })
      projects.value = projects.value.map((p) => (p.id === row.id ? { ...p, name } : p))
      folderPaths.load(true) // the paths under this folder changed
    }
    renaming.value = null
    toast.success(t('projectBrowser.renamed', { name }))
  } catch (e) {
    toast.error(errMessage(e))
    nextTick(() => document.querySelector<HTMLInputElement>('input.rename-input')?.focus())
  } finally {
    renameBusy.value = false
  }
}

// a copy in the same folder, «name (i)», as a file manager makes one; a datasource's
// data is copied on the storage, which takes a moment for a big one
const duplicating = ref<string | null>(null)
const byName = <T extends { name: string }>(a: T, b: T) => a.name.localeCompare(b.name)

function writesOutputs(definition: string): boolean {
  try {
    return (JSON.parse(definition)?.nodes ?? []).some((n: { type?: string }) => n.type === 'output')
  } catch {
    return false
  }
}

async function duplicateRow(row: Row) {
  if (duplicating.value) return
  duplicating.value = rowKey(row)
  try {
    let name = ''
    let outputs = false
    if (row.kind === 'flow') {
      const copy = await flowsApi.duplicate(row.id)
      flows.value = [...flows.value, copy].sort(byName)
      name = copy.name
      outputs = writesOutputs(copy.definition)
    } else if (row.kind === 'group') {
      const copy = await groupsApi.duplicate(row.id)
      flowGroups.value = [...flowGroups.value, copy].sort(byName)
      name = copy.name
    } else if (row.kind === 'datasource') {
      const copy = await dsApi.duplicate(row.id)
      dsList.value = [...dsList.value, copy].sort(byName)
      name = copy.name
    } else if (row.kind === 'view') {
      const copy = await viewsApi.duplicate(row.id)
      savedViews.value = [...savedViews.value, copy].sort(byName)
      name = copy.name
    } else if (row.kind === 'script') {
      const copy = await scriptsApi.duplicate(row.id)
      scripts.value = [...scripts.value, copy].sort(byName)
      name = copy.name
    } else if (row.kind === 'sql') {
      const copy = await sqlApi.duplicate(row.id)
      sqlScripts.value = [...sqlScripts.value, copy].sort(byName)
      name = copy.name
    } else if (row.kind === 'notebook') {
      const copy = await notebooksApi.duplicate(row.id)
      notebooks.value = [...notebooks.value, copy].sort(byName)
      name = copy.name
    } else if (row.kind === 'connection') {
      const copy = await connApi.duplicate(row.id)
      connections.value = [...connections.value, copy].sort(byName)
      name = copy.name
    } else {
      return
    }
    toast.success(t(outputs ? 'projectBrowser.duplicatedFlow' : 'projectBrowser.duplicated', { name }))
  } catch (e) {
    toast.error(errMessage(e))
  } finally {
    duplicating.value = null
  }
}

async function removeRow(row: Row) {
  const ask = (k: string) => confirm(t(k, { name: row.name }))
  try {
    if (row.kind === 'flow') {
      if (!ask('projectBrowser.confirmDeleteFlow')) return
      await flowsApi.remove(row.id)
      flows.value = flows.value.filter((f) => f.id !== row.id)
      toast.success(t('projectBrowser.flowDeleted', { name: row.name }))
    } else if (row.kind === 'group') {
      if (!ask('projectBrowser.confirmDeleteFlowGroup')) return
      await groupsApi.remove(row.id)
      flowGroups.value = flowGroups.value.filter((g) => g.id !== row.id)
      toast.success(t('projectBrowser.flowGroupDeleted', { name: row.name }))
    } else if (row.kind === 'datasource') {
      if (!ask('projectBrowser.confirmDeleteDatasource')) return
      await dsApi.remove(row.id)
      dsList.value = dsList.value.filter((d) => d.id !== row.id)
      toast.success(t('projectBrowser.datasourceDeleted', { name: row.name }))
    } else if (row.kind === 'view') {
      if (!ask('projectBrowser.confirmDeleteSavedView')) return
      await viewsApi.remove(row.id)
      savedViews.value = savedViews.value.filter((v) => v.id !== row.id)
      toast.success(t('projectBrowser.savedViewDeleted', { name: row.name }))
    } else if (row.kind === 'script') {
      if (!ask('projectBrowser.confirmDeleteScript')) return
      await scriptsApi.remove(row.id)
      scripts.value = scripts.value.filter((x) => x.id !== row.id)
      toast.success(t('projectBrowser.scriptDeleted', { name: row.name }))
    } else if (row.kind === 'sql') {
      if (!ask('projectBrowser.confirmDeleteSqlScript')) return
      await sqlApi.remove(row.id)
      sqlScripts.value = sqlScripts.value.filter((x) => x.id !== row.id)
      toast.success(t('projectBrowser.sqlScriptDeleted', { name: row.name }))
    } else if (row.kind === 'notebook') {
      if (!ask('projectBrowser.confirmDeleteNotebook')) return
      await notebooksApi.remove(row.id)
      notebooks.value = notebooks.value.filter((x) => x.id !== row.id)
      toast.success(t('projectBrowser.notebookDeleted', { name: row.name }))
    } else if (row.kind === 'connection') {
      if (!ask('projectBrowser.confirmDeleteConnection')) return
      await connApi.remove(row.id)
      connections.value = connections.value.filter((c) => c.id !== row.id)
      toast.success(t('projectBrowser.connectionDeleted', { name: row.name }))
    } else if (row.kind === 'folder') {
      if (!ask('projectBrowser.confirmDeleteFolder')) return
      await api.remove(row.id)
      await loadProjects()
      toast.success(t('projectBrowser.folderDeleted', { name: row.name }))
    }
  } catch (e) { toast.error(errMessage(e)) }
}

// ── Creation: a single entry point ─────────────────────────────────────
interface EngineOpt { id: string; label: string; available: boolean; description: string; optional?: boolean; disabled_by_admin?: boolean }
const engines = ref<EngineOpt[]>([{ id: 'polars', label: 'Polars', available: true, description: '' }])
const newMenu = ref(false)
const engineStep = ref(false) // the menu's second level: choosing the engine
const newFolderName = ref('')

function closeNew() { newMenu.value = false; engineStep.value = false; newFolderName.value = '' }
const newPop = ref<HTMLElement | null>(null)
usePopover(newMenu, newPop, closeNew)

function createFlowWith(engineId: string) {
  closeNew()
  const where = currentId.value != null ? `&project=${currentId.value}` : ''
  navigateTo(`/editor?engine=${engineId}${where}`)
}

function createFlowGroup() {
  closeNew()
  if (currentId.value != null) navigateTo(`/flow-group?project=${currentId.value}`)
}

function createScript() {
  closeNew()
  if (currentId.value != null) navigateTo(`/script?project=${currentId.value}`)
}

function createNotebook() {
  closeNew()
  if (currentId.value != null) navigateTo(`/notebook?project=${currentId.value}`)
}

function createSqlScript() {
  closeNew()
  if (currentId.value != null) navigateTo(`/sql-script?project=${currentId.value}`)
}

async function createFolder() {
  const folderName = newFolderName.value.trim()
  if (!folderName) return
  try {
    const p = await api.create({ name: folderName, parent_id: currentId.value })
    closeNew()
    await loadProjects()
    openFolder(p.id)
  } catch (e) { toast.error(errMessage(e)) }
}

// ── Side panel: the folder's administration ───────────────────────
const settingsOpen = ref(false)
// the drawer has a backdrop covering the page: it is modal like the dialogs
const drawerEl = ref<HTMLElement | null>(null)
useDialogA11y(drawerEl, () => settingsOpen.value && canManage.value && !!current.value, () => { settingsOpen.value = false })
const grantSubjectType = ref<'group' | 'user'>('group')
const grantGroupId = ref<number | null>(null)
const grantUserId = ref<number | null>(null)
const grantCapability = ref<string>('view')

const groupName = (id: number | null) =>
  groups.value.find((g) => g.id === id)?.name ?? (id != null ? t('projectBrowser.groupFallback', { id }) : '')
const userLabel = (id: number | null) =>
  users.value.find((u) => u.id === id)?.email ?? (id != null ? t('projectBrowser.userFallback', { id }) : '')

async function grant() {
  if (!currentId.value) return
  const body: any = { capability: grantCapability.value }
  if (grantSubjectType.value === 'group') {
    if (!grantGroupId.value) return
    body.group_id = grantGroupId.value
  } else {
    if (!grantUserId.value) return
    body.user_id = grantUserId.value
  }
  try {
    await api.grant(currentId.value, body)
    permissions.value = await api.permissions(currentId.value)
  } catch (e) { toast.error(errMessage(e)) }
}

async function revoke(perm: Permission) {
  try {
    await api.revoke(perm.id)
    permissions.value = permissions.value.filter((p) => p.id !== perm.id)
  } catch (e) { toast.error(errMessage(e)) }
}

async function deleteCurrentFolder() {
  const p = current.value
  if (!p || !confirm(t('projectBrowser.confirmDeleteFolder', { name: p.name }))) return
  try {
    await api.remove(p.id)
    settingsOpen.value = false
    const parent = p.parent_id ?? null
    await loadProjects()
    openFolder(parent)
    toast.success(t('projectBrowser.folderDeleted', { name: p.name }))
  } catch (e) { toast.error(errMessage(e)) }
}

// ── Dialogs ────────────────────────────────────────────────────────────────
const showConnDialog = ref(false)
const editingConn = ref<ConnectionInfo | null>(null)
const connBusy = ref(false)
const connDialogError = ref('')
const showDbDsDialog = ref(false)
const dbDsBusy = ref(false)
const dbDsError = ref('')
const usableConnections = ref<ConnectionInfo[]>([])

// ── the whole row opens what it names ──────────────────────────────────────
// The real control stays the NAME (button or link): from the keyboard and for a screen
// reader there is one point per row, and the buttons on the right stay apart. A click on
// the rest of the row does the same as the name, except when it lands on another
// control or closes a text selection.
const router = useRouter()
function apribile(r: Row): boolean {
  return r.kind === 'folder' || r.kind === 'connection' || !!r.to
}
function rowClick(e: MouseEvent, r: Row) {
  if (!apribile(r)) return
  // the actions' column does not open: between one button and the next a missed click must
  // not take one away from the page
  if ((e.target as HTMLElement | null)?.closest('button, a, input, select, textarea, label, [role="listbox"], [role="option"], .racts')) return
  if (window.getSelection()?.toString()) return
  const newTab = e.button === 1 || e.ctrlKey || e.metaKey || e.shiftKey
  if (e.type === 'auxclick' && e.button !== 1) return
  if (r.to) {
    // as the link would: Ctrl/Cmd/Shift or the middle click open elsewhere
    if (newTab) {
      e.preventDefault()
      window.open(router.resolve(r.to).href, '_blank', 'noopener')
    } else {
      navigateTo(r.to)
    }
  } else if (e.type === 'click') {
    if (r.kind === 'folder') openFolder(r.id)
    else if (r.kind === 'connection') openConnDialog(r.raw)
  }
}

function openConnDialog(conn: ConnectionInfo | null) {
  closeNew()
  editingConn.value = conn
  connDialogError.value = ''
  showConnDialog.value = true
}

async function saveConnection(draft: ConnectionDraft) {
  if (!currentId.value) return
  connBusy.value = true
  connDialogError.value = ''
  try {
    if (editingConn.value) {
      // only what the edit changed (an empty password keeps the stored one)
      await connApi.update(editingConn.value.id, connectionChanges(editingConn.value, draft))
      toast.success(t('projectBrowser.connectionUpdated', { name: draft.name }))
    } else {
      await connApi.create(currentId.value, draft)
      toast.success(t('projectBrowser.connectionCreated', { name: draft.name }))
    }
    showConnDialog.value = false
    connections.value = await connApi.listByProject(currentId.value)
  } catch (e) {
    connDialogError.value = errMessage(e)
  } finally {
    connBusy.value = false
  }
}

async function openDbDsDialog() {
  closeNew()
  dbDsError.value = ''
  try {
    // every source but mail: databases, SharePoint, SFTP, HTTP APIs, and S3 (a
    // folder of a bucket, «S3 folder»)
    usableConnections.value = (await connApi.list()).filter((c) => c.db_type !== 'smtp')
  } catch { usableConnections.value = [] }
  showDbDsDialog.value = true
}

async function createDbDatasource(draft: DbDatasourceDraft) {
  if (!currentId.value) return
  dbDsBusy.value = true
  dbDsError.value = ''
  try {
    const ds = await dsApi.createDb(currentId.value, draft)
    toast.success(t('projectBrowser.datasourceCreated', { name: draft.name }))
    showDbDsDialog.value = false
    dsList.value = await dsApi.listByProject(currentId.value)
    pollIngest(ds.id, ingestToken)
  } catch (e) {
    dbDsError.value = errMessage(e)
  } finally {
    dbDsBusy.value = false
  }
}

async function createSharePointDatasource(draft: SharePointDatasourceDraft) {
  if (!currentId.value) return
  dbDsBusy.value = true
  dbDsError.value = ''
  try {
    const ds = await dsApi.createSharePoint(currentId.value, draft)
    toast.success(t('projectBrowser.datasourceCreated', { name: draft.name }))
    showDbDsDialog.value = false
    dsList.value = await dsApi.listByProject(currentId.value)
    pollIngest(ds.id, ingestToken)
  } catch (e) {
    dbDsError.value = errMessage(e)
  } finally {
    dbDsBusy.value = false
  }
}

async function createApiDatasource(draft: ApiDatasourceDraft) {
  if (!currentId.value) return
  dbDsBusy.value = true
  dbDsError.value = ''
  try {
    const ds = await dsApi.createApi(currentId.value, draft)
    toast.success(t('projectBrowser.datasourceCreated', { name: draft.name }))
    showDbDsDialog.value = false
    dsList.value = await dsApi.listByProject(currentId.value)
    pollIngest(ds.id, ingestToken)
  } catch (e) {
    dbDsError.value = errMessage(e)
  } finally {
    dbDsBusy.value = false
  }
}

async function createS3Datasource(draft: S3DatasourceDraft) {
  if (!currentId.value) return
  dbDsBusy.value = true
  dbDsError.value = ''
  try {
    const ds = await dsApi.createS3(currentId.value, draft)
    toast.success(t('projectBrowser.datasourceCreated', { name: draft.name }))
    showDbDsDialog.value = false
    dsList.value = await dsApi.listByProject(currentId.value)
    pollIngest(ds.id, ingestToken)
  } catch (e) {
    dbDsError.value = errMessage(e)
  } finally {
    dbDsBusy.value = false
  }
}

async function createSftpDatasource(draft: SftpDatasourceDraft) {
  if (!currentId.value) return
  dbDsBusy.value = true
  dbDsError.value = ''
  try {
    const ds = await dsApi.createSftp(currentId.value, draft)
    toast.success(t('projectBrowser.datasourceCreated', { name: draft.name }))
    showDbDsDialog.value = false
    dsList.value = await dsApi.listByProject(currentId.value)
    pollIngest(ds.id, ingestToken)
  } catch (e) {
    dbDsError.value = errMessage(e)
  } finally {
    dbDsBusy.value = false
  }
}

// changing folder resets search and filter: they are questions about the current
// content, not lasting preferences
watch(currentId, () => { q.value = ''; kindFilter.value = null; settingsOpen.value = false })
</script>

<template>
  <div class="explore">
    <!-- path + search + the single primary action -->
    <div class="bar">
      <nav class="crumbs" :aria-label="$t('projectBrowser.pathLabel')">
        <button class="crumb" :class="{ on: currentId === null }" @click="openFolder(null)">
          <Home :size="14" /> {{ $t('projectBrowser.rootCrumb') }}
        </button>
        <template v-for="p in breadcrumb" :key="p.id">
          <ChevronRight :size="13" class="sep" />
          <button class="crumb" :class="{ on: p.id === currentId }" @click="openFolder(p.id)">{{ p.name }}</button>
        </template>
      </nav>

      <span class="searchbox">
        <Search :size="14" />
        <input v-model="q" type="text" :placeholder="$t('projectBrowser.searchPlaceholder')" :aria-label="$t('projectBrowser.searchPlaceholder')" />
        <button v-if="q" class="x" :title="$t('projectBrowser.clearSearch')" :aria-label="$t('projectBrowser.clearSearch')" @click="q = ''">
          <X :size="12" />
        </button>
      </span>

      <button class="mini" :title="$t('projectBrowser.reloadTitle')" :aria-label="$t('projectBrowser.reloadTitle')" @click="loadProjects(); loadFolder(currentId)">
        <RefreshCw :size="13" />
      </button>

      <div class="newwrap">
        <button class="btn-link" @click="newMenu = !newMenu"><Plus :size="14" /> {{ $t('projectBrowser.newButton') }}</button>
        <div v-if="newMenu" class="menu-backdrop" @click="closeNew" />
        <div v-if="newMenu" ref="newPop" class="menu-pop">
          <template v-if="!engineStep">
            <div class="menu-label">{{ $t('projectBrowser.newFolderLabel') }}</div>
            <div class="menu-row">
              <input v-model="newFolderName" type="text" :placeholder="$t('projectBrowser.namePlaceholder')" @keyup.enter="createFolder" />
              <button class="primary" :disabled="!newFolderName.trim()" @click="createFolder">{{ $t('projectBrowser.folderButton') }}</button>
            </div>
            <template v-if="currentId !== null">
              <div class="menu-sep" />
              <button class="menu-item" @click="engineStep = true">
                <span class="mi-top"><Workflow :size="14" /> {{ $t('projectBrowser.newFlowButton') }}</span>
              </button>
              <button class="menu-item" @click="createFlowGroup">
                <span class="mi-top"><Rows3 :size="14" /> {{ $t('projectBrowser.newFlowGroupButton') }}</span>
              </button>
              <button class="menu-item" @click="createSqlScript">
                <span class="mi-top"><DatabaseSearch :size="14" /> {{ $t('projectBrowser.newSqlScriptButton') }}</span>
              </button>
              <button v-if="scriptingEditor.available.value" class="menu-item" @click="createScript">
                <span class="mi-top"><FileCode2 :size="14" /> {{ $t('projectBrowser.newScriptButton') }}</span>
              </button>
              <button v-else-if="scriptingEditor.state.value?.disabled_reason === 'kubernetes'" class="menu-item off"
                      disabled :title="$t('scripting.kubernetesOnly')">
                <span class="mi-top"><FileCode2 :size="14" /> {{ $t('projectBrowser.newScriptButton') }}</span>
                <span class="mi-desc">{{ $t('scripting.kubernetesOnly') }}</span>
              </button>
              <button v-if="notebooksAvailable" class="menu-item" @click="createNotebook">
                <span class="mi-top"><NotebookPen :size="14" /> {{ $t('projectBrowser.newNotebookButton') }}</span>
              </button>
              <button v-else-if="scriptingEditor.state.value?.disabled_reason === 'kubernetes'" class="menu-item off"
                      disabled :title="$t('scripting.kubernetesOnly')">
                <span class="mi-top"><NotebookPen :size="14" /> {{ $t('projectBrowser.newNotebookButton') }}</span>
                <span class="mi-desc">{{ $t('scripting.kubernetesOnly') }}</span>
              </button>
              <button class="menu-item" @click="openDbDsDialog">
                <span class="mi-top"><Database :size="14" /> {{ $t('projectBrowser.fromDatabaseButton') }}</span>
              </button>
              <button v-if="canConnect" class="menu-item" @click="openConnDialog(null)">
                <span class="mi-top"><Plug :size="14" /> {{ $t('projectBrowser.newConnectionButton') }}</span>
              </button>
            </template>
          </template>

          <template v-else>
            <div class="menu-label">{{ $t('projectBrowser.engineMenuLabel') }}</div>
            <button v-for="e in engines" :key="e.id" class="menu-item" :disabled="!e.available" @click="createFlowWith(e.id)">
              <span class="mi-top">
                {{ e.label }}
                <span v-if="e.id === preferredEngine && e.available" class="pref">{{ $t('projectBrowser.preferredTag') }}</span>
                <span v-if="!e.available" class="soon">
                  {{ $t(e.disabled_by_admin ? 'projectBrowser.notAllowedTag' : (e.optional ? 'projectBrowser.notConfiguredTag' : 'projectBrowser.comingSoonTag')) }}
                </span>
              </span>
              <span class="mi-desc">{{ engineDescription(e, $t, $te) }}</span>
            </button>
          </template>
        </div>
      </div>

      <button
        v-if="canManage"
        class="mini"
        :class="{ activebtn: settingsOpen }"
        :title="$t('projectBrowser.folderSettingsTitle')"
        :aria-label="$t('projectBrowser.folderSettingsTitle')"
        @click="settingsOpen = !settingsOpen"
      ><Settings2 :size="13" /></button>
    </div>

    <!-- kind filter: the numbers say what is inside, before looking -->
    <div v-if="segments.length > 1" class="segs">
      <button class="seg" :class="{ on: kindFilter === null }" @click="kindFilter = null">
        {{ $t('projectBrowser.allSegment') }} <span class="n">{{ matching.length }}</span>
      </button>
      <button
        v-for="s in segments"
        :key="s.kind"
        class="seg"
        :class="{ on: kindFilter === s.kind }"
        @click="kindFilter = kindFilter === s.kind ? null : s.kind"
      >
        {{ $t(`projectBrowser.kind_${s.kind}`) }} <span class="n">{{ s.n }}</span>
      </button>
    </div>

    <p v-if="error" class="err">{{ error }}</p>

    <SkeletonRows v-if="loading" :rows="6" />

    <p v-else-if="!rows.length" class="muted empty">
      {{ currentId === null ? $t('projectBrowser.emptyRoot') : $t('projectBrowser.emptyFolder') }}
    </p>
    <p v-else-if="!visibleRows.length" class="muted empty">{{ $t('projectBrowser.noMatches', { q }) }}</p>

    <table v-else class="rows">
      <tbody>
        <template v-for="r in visibleRows" :key="`${r.kind}-${r.id}`">
          <tr
            :class="{ folderrow: r.kind === 'folder', apribile: apribile(r), blocked: !!r.locked }"
            :title="r.locked || undefined"
            :aria-disabled="r.locked ? 'true' : undefined"
            @click="rowClick($event, r)"
            @auxclick="rowClick($event, r)"
          >
            <td class="rname">
              <span v-if="renaming === rowKey(r)" class="asname renaming">
                <component :is="KIND_ICON[r.kind]" :size="15" class="kicon" />
                <input
                  v-model="renameDraft"
                  class="rename-input"
                  maxlength="200"
                  :aria-label="$t('projectBrowser.renameLabel', { name: r.name })"
                  :disabled="renameBusy"
                  @keydown.enter.prevent="commitRename(r)"
                  @keydown.esc.prevent="cancelRename"
                  @blur="commitRename(r)"
                />
              </span>
              <button v-else-if="r.kind === 'folder'" class="asname" @click="openFolder(r.id)">
                <component :is="KIND_ICON[r.kind]" :size="15" class="kicon" /> {{ r.name }}
              </button>
              <button v-else-if="r.kind === 'connection'" class="asname" @click="openConnDialog(r.raw)">
                <component :is="KIND_ICON[r.kind]" :size="15" class="kicon" /> {{ r.name }}
              </button>
              <NuxtLink v-else-if="r.to" :to="r.to" class="asname">
                <component :is="KIND_ICON[r.kind]" :size="15" class="kicon" /> {{ r.name }}
              </NuxtLink>
              <span v-else class="asname plain">
                <component :is="KIND_ICON[r.kind]" :size="15" class="kicon" /> {{ r.name }}
                <Lock v-if="r.locked" :size="12" class="lock" :aria-label="$t('projectBrowser.lockedFlow')" />
              </span>
              <span v-if="r.kind === 'datasource' && r.raw.kind === 'database'" class="tag">db</span>
              <ContractBadge v-if="r.kind === 'datasource'" :contract="r.raw.contract" :size="15" />
            </td>
            <td class="rmeta muted">
              <span class="rmeta-text" :title="r.progressRun ? undefined : r.meta">
                <RunProgressText v-if="r.progressRun" :run="r.progressRun" :sr-label="r.meta" />
                <template v-else>{{ r.meta }}</template>
              </span>
            </td>
            <td v-if="r.locked" class="racts" />
            <td v-else class="racts">
              <button
                v-if="r.kind === 'flow'"
                class="mini"
                :class="{ activebtn: expandedFlowId === r.id }"
                :title="$t('projectBrowser.runHistoryTitle')"
                :aria-label="$t('projectBrowser.runHistoryTitle')"
                @click="toggleRuns(r.raw)"
              ><History :size="13" /></button>

              <Select
                v-if="moving === rowKey(r)"
                class="movesel"
                :model-value="null"
                :options="moveTargets(r)"
                searchable
                :placeholder="$t('projectBrowser.moveToPlaceholder')"
                @update:model-value="(v: any) => moveRow(r, v)"
                @close="moving = null"
              />
              <button
                v-else
                class="mini"
                :title="$t('projectBrowser.moveToFolderTitle')"
                :aria-label="$t('projectBrowser.moveToFolderTitle')"
                @click="moving = rowKey(r)"
              ><FolderInput :size="13" /></button>

              <button
                v-if="RENAMABLE.has(r.kind)"
                class="mini"
                :title="$t('projectBrowser.renameTitle')"
                :aria-label="$t('projectBrowser.renameTitle')"
                :disabled="renaming === rowKey(r)"
                @click="startRename(r)"
              ><PencilLine :size="13" /></button>

              <button
                v-if="r.kind !== 'folder'"
                class="mini"
                :title="$t('projectBrowser.duplicateTitle')"
                :aria-label="$t('projectBrowser.duplicateTitle')"
                :disabled="!!duplicating"
                :aria-busy="duplicating === rowKey(r)"
                @click="duplicateRow(r)"
              >
                <LoaderCircle v-if="duplicating === rowKey(r)" :size="13" class="spin" />
                <Copy v-else :size="13" />
              </button>

              <button
                v-if="r.kind === 'datasource' && r.raw.kind === 'database'"
                class="mini"
                :title="$t('projectBrowser.refreshSnapshotTitle')"
                :aria-label="$t('projectBrowser.refreshSnapshotTitle')"
                :disabled="!!ingestRuns[r.id] && !isTerminal(ingestRuns[r.id])"
                @click="refreshDatasource(r.raw)"
              ><RefreshCw :size="13" /></button>

              <button
                v-if="r.kind === 'datasource'"
                class="mini"
                :title="$t('contracts.open')"
                :aria-label="$t('contracts.open')"
                @click="contractFor = r.raw"
              ><ScrollText :size="13" /></button>

              <button
                v-if="r.kind === 'connection'"
                class="mini"
                :title="$t('projectBrowser.editConnectionTitle')"
                :aria-label="$t('projectBrowser.editConnectionTitle')"
                @click="openConnDialog(r.raw)"
              ><Pencil :size="13" /></button>

              <button class="mini danger" :title="$t('projectBrowser.deleteTitle')" :aria-label="$t('projectBrowser.deleteTitle')" @click="removeRow(r)">
                <Trash2 :size="13" />
              </button>
            </td>
          </tr>

          <tr v-if="r.kind === 'flow' && expandedFlowId === r.id" class="runsrow">
            <td colspan="3">
              <p v-if="runsLoading" class="muted runmeta">
                <LoaderCircle :size="13" class="spin" /> {{ $t('projectBrowser.loadingHistory') }}
              </p>
              <p v-else-if="!flowRuns.length" class="muted runmeta">{{ $t('projectBrowser.noRunsForFlow') }}</p>
              <ul v-else class="runlist">
                <li v-for="run in flowRuns" :key="run.id" class="runitem">
                  <CheckCircle2 v-if="run.status === 'SUCCESS'" :size="14" class="rok" />
                  <XCircle v-else-if="run.status === 'FAILURE'" :size="14" class="rko" />
                  <LoaderCircle v-else :size="14" class="spin rwip" />
                  <span class="rwhen">{{ fmtDate(run.started_at) }}</span>
                  <span class="muted">{{ fmtDuration(run) }}</span>
                  <span v-if="run.rows_written != null" class="muted">{{ $t('projectBrowser.rowsCount', { n: fmtx.count(run.rows_written) }) }}</span>
                  <span v-if="run.publish_name" class="rpub"><Database :size="12" /> {{ run.publish_name }}</span>
                  <span v-if="run.error" class="rerr" :title="knownError(run.error, t)">{{ knownError(run.error, t).slice(0, 80) }}</span>
                </li>
              </ul>
            </td>
          </tr>
        </template>
      </tbody>
    </table>

    <!-- The folder's administration: outside the data's scrolling -->
    <template v-if="settingsOpen && canManage && current">
      <div class="drawer-backdrop" @click="settingsOpen = false" />
      <aside ref="drawerEl" v-label-fields class="drawer" role="dialog" aria-modal="true" tabindex="-1" :aria-label="$t('projectBrowser.folderSettingsTitle')">
        <div class="drawer-head">
          <span class="dtitle"><Settings2 :size="15" /> {{ current.name }}</span>
          <button class="mini" :aria-label="$t('projectBrowser.closeDrawer')" @click="settingsOpen = false"><X :size="14" /></button>
        </div>

        <label class="dlabel">{{ $t('projectBrowser.permissionsLabel') }}</label>
        <table class="perm">
          <tbody>
            <tr v-for="p in permissions" :key="p.id">
              <td class="subject">
                <UsersIcon v-if="p.group_id != null" :size="14" />
                <UserIcon v-else :size="14" />
                {{ p.group_id != null ? groupName(p.group_id) : userLabel(p.user_id) }}
              </td>
              <td><span class="cap">{{ p.capability }}</span></td>
              <td class="right"><button class="mini danger" :aria-label="$t('projectBrowser.revokeTitle')" @click="revoke(p)"><X :size="13" /></button></td>
            </tr>
            <tr v-if="!permissions.length"><td colspan="3" class="muted">{{ $t('projectBrowser.noExplicitPermissions') }}</td></tr>
          </tbody>
        </table>

        <div class="grant">
          <Select
            v-model="grantSubjectType"
            :options="isSuper ? [{ value: 'group', label: $t('projectBrowser.groupOption') }, { value: 'user', label: $t('projectBrowser.userOption') }] : [{ value: 'group', label: $t('projectBrowser.groupOption') }]"
          />
          <Select
            v-if="grantSubjectType === 'group'"
            v-model="grantGroupId"
            :options="groups.map((g) => ({ value: g.id, label: g.name }))"
            :placeholder="$t('projectBrowser.groupPlaceholder')"
          />
          <Select
            v-else
            v-model="grantUserId"
            :options="users.map((u) => ({ value: u.id, label: u.email }))"
            :placeholder="$t('projectBrowser.userPlaceholder')"
          />
          <Select v-model="grantCapability" :options="[...CAPABILITIES]" />
          <button class="primary" @click="grant">{{ $t('projectBrowser.grantButton') }}</button>
        </div>

        <div class="dangerzone">
          <button class="danger" @click="deleteCurrentFolder">
            <Trash2 :size="14" /> {{ $t('projectBrowser.deleteFolderButton') }}
          </button>
        </div>
      </aside>
    </template>

    <ConnectionDialog
      :open="showConnDialog"
      :project-id="currentId ?? 0"
      :existing="editingConn"
      :error="connDialogError"
      :busy="connBusy"
      @confirm="saveConnection"
      @cancel="showConnDialog = false"
    />
    <ContractDialog :open="contractFor !== null" :datasource="contractFor" @close="contractFor = null" @changed="loadFolder(currentId)" />
    <DbDatasourceDialog
      :open="showDbDsDialog"
      :connections="usableConnections"
      :error="dbDsError"
      :busy="dbDsBusy"
      @confirm="createDbDatasource"
      @confirm-sharepoint="createSharePointDatasource"
      @confirm-sftp="createSftpDatasource"
      @confirm-s3="createS3Datasource"
      @confirm-api="createApiDatasource"
      @cancel="showDbDsDialog = false"
    />
  </div>
</template>

<style scoped>
.menu-item.off { opacity: .45; cursor: not-allowed; }
.explore { display: flex; flex-direction: column; min-height: 0; }

/* ── bar: path, search, action ─────────────────────────────────────── */
.bar { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; }
.crumbs { display: flex; align-items: center; gap: 2px; min-width: 0; flex: 1; overflow: hidden; }
.crumb {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 8px;
  border: none;
  background: none;
  color: var(--muted);
  font-size: 13.5px;
  border-radius: 7px;
  white-space: nowrap;
}
.crumb:hover { background: var(--panel-2); color: var(--text); box-shadow: none; }
.crumb.on { color: var(--text); font-weight: 600; }
.sep { color: var(--muted); flex-shrink: 0; }
.searchbox {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  padding: 5px 9px;
  border: 1px solid var(--control-border);
  border-radius: 8px;
  background: var(--panel-2);
  color: var(--muted);
  flex-shrink: 0;
}
.searchbox input { border: none; background: transparent; outline: none; color: var(--text); width: 190px; font-size: 13px; }
.searchbox .x { padding: 1px 5px; min-height: 0; }
.newwrap { position: relative; flex-shrink: 0; }
/* `.btn-link` lives in assets/listpage.css, which is NOT imported here: with scoped
   styles that rule does not arrive, and without this the button would fall on the base
   `button` rule losing the accent fill, that is the visual weight of the page's only
   primary action. Values copied from listpage.css so that Explore's `New` is identical
   to the Flows and Datasources ones. */
.btn-link {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 7px 14px;
  border-radius: 8px;
  text-decoration: none;
  background: var(--grad-accent);
  color: var(--on-accent);
  font-size: 13px;
  font-weight: 500;
  white-space: nowrap;
}
.menu-backdrop { position: fixed; inset: 0; z-index: 40; }
.menu-pop {
  position: absolute;
  right: 0;
  top: calc(100% + 6px);
  z-index: 41;
  min-width: 280px;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  box-shadow: var(--shadow-2);
  padding: 6px;
}
.menu-label { font-size: 11px; text-transform: uppercase; letter-spacing: 0.04em; color: var(--muted); padding: 6px 8px 4px; }
.menu-row { display: flex; gap: 6px; padding: 0 6px 6px; }
.menu-row input { flex: 1; min-width: 0; }
.menu-sep { height: 1px; background: var(--border-soft); margin: 4px 6px; }
.menu-item {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 2px;
  width: 100%;
  text-align: left;
  padding: 8px 9px;
  border: none;
  background: transparent;
  border-radius: 7px;
}
.menu-item:hover:not(:disabled) { background: var(--panel-2); box-shadow: none; }
.menu-item:disabled { opacity: 0.55; }
.mi-top { display: inline-flex; align-items: center; gap: 7px; font-weight: 600; font-size: 13px; color: var(--text); }
.mi-desc { font-size: 11.5px; color: var(--muted); line-height: 1.35; }
.pref, .soon { font-size: 10px; text-transform: uppercase; letter-spacing: 0.04em; color: var(--muted); }

/* ── kind filter ───────────────────────────────────────────────────────── */
.segs { display: flex; gap: 6px; margin-bottom: 10px; flex-wrap: wrap; }
.seg {
  padding: 4px 11px;
  font-size: 12.5px;
  color: var(--muted);
  background: transparent;
  border: 1px solid var(--border);
  border-radius: 999px;
}
.seg:hover { color: var(--text); box-shadow: none; }
.seg.on { color: var(--text); background: var(--tint-accent); border-color: var(--accent); }
.seg .n { font-variant-numeric: tabular-nums; opacity: 0.75; margin-left: 4px; }

/* ── single list ───────────────────────────────────────────────────────── */
/* no line under EVERY row: the hover separates, not a grid of hairlines */
/* `separate` and not `collapse`: with collapsed cells the rounded corners are not drawn,
   and the clickable row must read as a button */
table.rows { width: 100%; border-collapse: separate; border-spacing: 0; font-size: 13px; }
table.rows td { padding: 7px 8px; transition: background 0.12s; }
table.rows tr:hover td { background: var(--row-hover); }
table.rows tr > td:first-child { border-radius: 8px 0 0 8px; }
table.rows tr > td:last-child { border-radius: 0 8px 8px 0; }
/* the row that opens something: taken whole, it answers the click and the focus */
table.rows tr.apribile { cursor: pointer; }
table.rows tr.blocked { opacity: 0.45; cursor: not-allowed; }
table.rows tr.blocked:hover td { background: none; }
.lock { margin-left: 6px; vertical-align: -1px; color: var(--muted); }
table.rows tr.apribile:hover td,
table.rows tr.apribile:has(.asname:focus-visible) td { background: var(--tint-accent); }
table.rows tr.apribile:hover .asname,
table.rows tr.apribile:has(.asname:focus-visible) .asname { color: var(--accent); }
table.rows tr.apribile:active td { background: color-mix(in srgb, var(--accent) 18%, transparent); }
/* the controls inside the row keep their pointer */
table.rows tr.apribile .racts { cursor: default; }
tr.folderrow .kicon { color: var(--accent-2); }
.kicon { color: var(--muted); flex-shrink: 0; }
.rname { width: 55%; }
.rname :deep(.contract-badge) { margin-left: 5px; }
.asname {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  color: var(--text);
  text-decoration: none;
  border: none;
  background: none;
  padding: 0;
  font-size: 13px;
}
.asname:hover:not(.plain) { color: var(--accent); box-shadow: none; }
.asname.plain { cursor: default; }
.asname.renaming { width: min(100%, 440px); }
.rename-input {
  flex: 1;
  min-width: 0;
  max-width: 420px;
  font: inherit;
  color: var(--text);
  background: var(--panel);
  border: 1px solid var(--accent);
  border-radius: 6px;
  padding: 3px 8px;
  outline: none;
  box-shadow: 0 0 0 3px var(--tint-accent);
}
.rmeta { font-size: 12px; white-space: nowrap; }
/* a long description must not widen the table to push the actions off screen (between
   721 and ~1170px it happened): it is cut, whole in the tooltip */
.rmeta-text { display: block; max-width: 36ch; overflow: hidden; text-overflow: ellipsis; }
.racts { text-align: right; white-space: nowrap; display: flex; gap: 4px; justify-content: flex-end; }
.tag {
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  padding: 0 6px;
  margin-left: 7px;
  border-radius: 8px;
  background: var(--panel-2);
  border: 1px solid var(--border);
  color: var(--muted);
}
.empty { padding: 28px 4px; }
/* as `.btn-link`: it lives in listpage.css, not imported here. Without it, the error
   message would come out in the normal text's colour, that is it would not look like an
   error. */
.err { color: var(--danger); }
.movesel { width: 130px; font-size: 12px; }
.activebtn { border-color: var(--accent); }

.runsrow td { background: var(--bg-soft); }
.runmeta { display: flex; align-items: center; gap: 6px; font-size: 12px; margin: 4px 2px; }
.runlist { list-style: none; margin: 2px 0; padding: 0; display: flex; flex-direction: column; gap: 3px; }
.runitem { display: flex; align-items: center; gap: 10px; font-size: 12px; padding: 2px 4px; }
.rok { color: var(--accent-2); }
.rko { color: var(--danger); }
.rwip { color: var(--accent); }
.rwhen { min-width: 110px; font-variant-numeric: tabular-nums; }
.rpub { display: inline-flex; align-items: center; gap: 4px; color: var(--accent-hi); }
.rerr { color: var(--danger); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

/* ── side panel ────────────────────────────────────────────────────────── */
.drawer-backdrop { position: fixed; inset: 0; background: var(--scrim); z-index: 120; }
.drawer {
  position: fixed;
  top: 0;
  right: 0;
  bottom: 0;
  z-index: 121;
  width: 380px;
  max-width: 92vw;
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 16px;
  overflow-y: auto;
  background: var(--panel);
  border-left: 1px solid var(--border);
  box-shadow: var(--shadow-2);
}
.drawer-head { display: flex; align-items: center; justify-content: space-between; }
.dtitle { display: inline-flex; align-items: center; gap: 8px; font-weight: 600; }
.dlabel { font-size: 12px; color: var(--muted); }
table.perm { width: 100%; border-collapse: collapse; font-size: 13px; }
table.perm td { padding: 5px 6px; border-bottom: 1px solid var(--border-soft); }
table.perm td.subject { display: flex; align-items: center; gap: 6px; }
table.perm td.right { text-align: right; width: 32px; }
.cap { font-size: 11px; padding: 1px 8px; border-radius: 10px; background: var(--panel-2); border: 1px solid var(--border); }
.grant { display: flex; gap: 6px; flex-wrap: wrap; }
/* the deletion sits at the bottom of the panel, apart: it is not met by chance */
.dangerzone { margin-top: auto; padding-top: 14px; border-top: 1px solid var(--border-soft); }
button.mini { padding: 2px 8px; min-height: 24px; }
button.danger, .mini.danger { border-color: var(--danger); color: var(--danger); }
button.danger:hover { background: var(--danger-fill); color: var(--on-danger); }

@media (max-width: 720px) {
  .bar { flex-wrap: wrap; }
  /* the path on a line of its own, scrolled sideways if long: it was cut to «All f» */
  .crumbs { flex-basis: 100%; overflow-x: auto; }
  .searchbox input { width: 120px; }
  .rmeta { display: none; }
}
</style>
