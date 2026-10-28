<script setup lang="ts">
// A SQL script: SQL kept in a folder, with its versions, run on the datasources the
// person may read or on a database connection (docs/specs/sql-scripts.md). On the catalog
// the SQL names a datasource by its name in double quotes, with its folder's in front when
// two share it; on a connection it is the database's own SQL. «Run» (Ctrl/Cmd+Enter) runs
// the selection or the whole SQL and shows the first rows. Whoever may only read the folder
// runs it and tries changes, and saves nothing; a script on a connection runs only for
// whoever may use that connection. A script may name a datasource: the whole result of
// its saved version is written there now («Refresh now»), on its schedule, or by a flow
// group's cell.
//
// /sql-script?id=N opens a script; /sql-script?project=P starts a new one in folder P.
import { computed, defineAsyncComponent, onMounted, onUnmounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { onBeforeRouteLeave, onBeforeRouteUpdate } from 'vue-router'
import {
  CalendarClock, ChevronRight, CircleCheck, CircleX, Database, DatabaseSearch, Download, FileSpreadsheet, History,
  LoaderCircle, Play, RefreshCw, RotateCcw, Save, Search, Square, Table2, X,
} from 'lucide-vue-next'
import { DB_TYPES, useConnections, type ConnectionInfo } from '~/composables/useConnections'
import { errMessage, useApi } from '~/composables/useApi'
import { useFormat } from '~/composables/useFormat'
import { useFolderPaths } from '~/composables/useFolderPaths'
import { useProjects, type Project } from '~/composables/useProjects'
import { FIRST_PAUSE_MS, nextPause } from '~/composables/waits'
import { usePreferredEngine } from '~/composables/useEngine'
import { versionNote } from '~/composables/useSavedScripts'
import {
  QUERYABLE_DB_TYPES, catalogDialect, sqlIdent, sqlName, sqlTableRef, useSqlScripts,
  type SqlCatalogItem, type SqlRunResult, type SqlScriptDetail, type SqlScriptRun, type SqlScriptVersion, type SqlTable,
} from '~/composables/useSqlScripts'
import Select from '~/components/ui/Select.vue'
import ScheduleDialog from '~/components/ui/ScheduleDialog.vue'
import DataGrid from '~/components/ui/DataGrid.vue'

// Monaco weighs a few MB: loaded with the editor, not with the page
const SqlEditor = defineAsyncComponent(() => import('~/components/sql/SqlEditor.vue'))

const { t } = useI18n()
const fmtx = useFormat()
const toast = useToast()
const route = useRoute()
const api = useSqlScripts()
const connectionsApi = useConnections()
const folderPaths = useFolderPaths()
const { engineCatalog, loadCatalog, defaultEngine } = usePreferredEngine()

const scriptId = ref<number | null>(Number(route.query.id) || null)
const projectId = ref<number | null>(Number(route.query.project) || null)
const detail = ref<SqlScriptDetail | null>(null)
const loading = ref(true)
const loadError = ref('')
const saving = ref(false)

// what is on screen; `savedAs` is the same thing as last saved, to tell unsaved changes
const name = ref('')
const description = ref('')
const sql = ref('')
const engine = ref<string | null>(null)
const connectionId = ref<number | null>(null) // null = the catalog
// the datasource the whole result is written into: an empty name = none
const outName = ref('')
const outProject = ref<number | null>(null)
const savedAs = ref('')
const snapshot = () => JSON.stringify([name.value.trim(), description.value.trim(), sql.value, engine.value, connectionId.value,
  outName.value.trim(), outName.value.trim() ? outProject.value : null])
const dirty = computed(() => snapshot() !== savedAs.value)

const canEdit = computed(() => (detail.value ? detail.value.editable : true))
const folderPath = computed(() => folderPaths.pathOf(projectId.value) ?? '')

// the engines the administrator lets one choose; «default» is the person's favourite among them
const usable = computed(() => engineCatalog.value.filter((e) => e.available && !e.disabled_by_admin))
const fallbackEngine = computed(() => defaultEngine(usable.value))
const engineOptions = computed(() => [
  { value: null, label: t('sqlScript.engineDefault', { engine: labelOf(fallbackEngine.value) }) },
  ...usable.value.map((e) => ({ value: e.id, label: e.label })),
])
function labelOf(id: string): string {
  return engineCatalog.value.find((e) => e.id === id)?.label ?? id
}

// ── where the SQL runs: the catalog, or a database connection ────────────────
// the connections this person may use (CONNECT on their folder), databases only
const connections = ref<ConnectionInfo[]>([])
async function loadConnections() {
  try {
    connections.value = (await connectionsApi.list()).filter((c) => QUERYABLE_DB_TYPES.includes(c.db_type))
  } catch { connections.value = [] }
}
const dbLabel = (dbType: string) => DB_TYPES.find((d) => d.value === dbType)?.label ?? dbType
const connection = computed(() => connections.value.find((c) => c.id === connectionId.value) ?? null)
// the script's connection when this person cannot use it, or when it was deleted
const foreignConnection = computed(() => {
  const c = detail.value?.connection
  return c && c.id === connectionId.value && !connection.value ? c : null
})
const dbType = computed(() => connection.value?.db_type ?? foreignConnection.value?.db_type ?? null)
const sourceOptions = computed(() => [
  { value: null, label: t('sqlScript.sourceCatalog'), hint: t('sqlScript.sourceCatalogHint') },
  ...connections.value.map((c) => ({ value: c.id, label: c.name, hint: dbLabel(c.db_type) })),
  ...(foreignConnection.value
    ? [{ value: foreignConnection.value.id, label: foreignConnection.value.name || t('sqlScript.connectionGone'),
         hint: foreignConnection.value.db_type ? dbLabel(foreignConnection.value.db_type) : '' }]
    : []),
])
// a script on a connection this person may not use: they read it, and do not run it
const cannotRun = computed(() => connectionId.value != null && !connection.value)

function apply(d: SqlScriptDetail) {
  detail.value = d
  scriptId.value = d.id
  projectId.value = d.project_id
  name.value = d.name
  description.value = d.description
  sql.value = d.content.sql
  engine.value = d.content.engine
  connectionId.value = d.content.connection_id
  outName.value = d.content.output?.name ?? ''
  outProject.value = d.content.output?.project_id ?? null
  savedAs.value = snapshot()
}

async function load() {
  loadError.value = ''
  viewing.value = null
  result.value = null
  runError.value = ''
  if (scriptId.value == null) {
    detail.value = null
    sql.value = `${t('sqlScript.starter')}\nSELECT *\nFROM `
    savedAs.value = ''
    loading.value = false
    return
  }
  loading.value = true
  try {
    apply(await api.get(scriptId.value))
    await Promise.all([loadVersions(), loadWrites()])
  } catch (e) {
    loadError.value = errMessage(e)
  } finally {
    loading.value = false
  }
}

async function save() {
  if (saving.value || !canEdit.value || viewing.value) return
  const n = name.value.trim()
  if (!n) {
    toast.error(t('sqlScript.nameRequired'))
    return
  }
  saving.value = true
  const output = outName.value.trim() ? { name: outName.value.trim(), project_id: outProject.value ?? projectId.value! } : null
  const content = { sql: sql.value, engine: connectionId.value == null ? engine.value : null, connection_id: connectionId.value, output }
  try {
    if (scriptId.value != null) {
      apply(await api.update(scriptId.value, { name: n, description: description.value.trim(), content }))
      await loadVersions()
      toast.success(t('sqlScript.saved', { name: n }))
    } else if (projectId.value != null) {
      const d = await api.create(projectId.value, { name: n, description: description.value.trim(), content })
      apply(d)
      navigateTo(`/sql-script?id=${d.id}`, { replace: true })
      await loadVersions()
      toast.success(t('sqlScript.created', { name: n }))
    }
  } catch (e) {
    toast.error(errMessage(e))
  } finally {
    saving.value = false
  }
}

// ── running ──────────────────────────────────────────────────────────────────
const editorRef = ref<{ runText: () => string; insert: (s: string) => void; focus: () => void } | null>(null)
const running = ref(false)
const result = ref<SqlRunResult | null>(null)
const runError = ref('')
let controller: AbortController | null = null

async function run(text?: string) {
  const query = (text ?? editorRef.value?.runText() ?? sql.value).trim()
  if (!query || cannotRun.value) return
  controller?.abort()
  controller = new AbortController()
  const mine = controller
  running.value = true
  runError.value = ''
  try {
    result.value = await api.run({
      sql: query, script_id: scriptId.value, connection_id: connectionId.value,
      engine: connectionId.value == null ? engine.value ?? fallbackEngine.value : null,
    }, mine.signal)
  } catch (e: any) {
    if (e?.name === 'AbortError' || mine.signal.aborted) return
    result.value = null
    runError.value = errMessage(e)
  } finally {
    if (controller === mine) {
      running.value = false
      controller = null
    }
  }
}

// the whole result as a file (what «Run» runs: the selection, or everything), written by a
// worker as the Viewer's export
const { followExport } = useApi()
const exporting = ref<'' | 'csv' | 'xlsx'>('')
async function exportResult(format: 'csv' | 'xlsx') {
  const query = (editorRef.value?.runText() ?? sql.value).trim()
  if (!query || cannotRun.value || exporting.value) return
  exporting.value = format
  try {
    const { task_id, filename } = await api.export({
      sql: query, format, script_id: scriptId.value, connection_id: connectionId.value,
      engine: connectionId.value == null ? engine.value ?? fallbackEngine.value : null,
    })
    await followExport(task_id, filename)
  } catch (e) {
    toast.error(errMessage(e))
  } finally {
    exporting.value = ''
  }
}

function stop() {
  controller?.abort()
  controller = null
  running.value = false
}

const resultLine = computed(() => {
  const r = result.value
  if (!r) return ''
  const rows = t('sqlScript.rows', { n: fmtx.count(r.row_count), more: r.truncated ? '+' : '' }, r.truncated ? 2 : r.row_count)
  if (r.connection) return t('sqlScript.resultLineConnection', { rows, ms: fmtx.count(r.elapsed_ms), connection: r.connection.name })
  return t('sqlScript.resultLine', {
    rows, ms: fmtx.count(r.elapsed_ms), engine: labelOf(r.engine ?? ''), read: r.datasources.map((d) => d.name).join(', '),
  })
})

// ── the catalog beside the editor ────────────────────────────────────────────
const catalog = ref<SqlCatalogItem[]>([])
const catalogQuery = ref('')
const openItem = ref<number | null>(null)
async function loadCatalogList() {
  try { catalog.value = await api.catalog() } catch { catalog.value = [] }
}
const shownCatalog = computed(() => {
  const q = catalogQuery.value.trim().toLowerCase()
  return q ? catalog.value.filter((d) => `${d.name} ${d.path}`.toLowerCase().includes(q)) : catalog.value
})
// how the catalog's names are written: BigQuery quotes them in backticks
const onCatalog = computed(() => catalogDialect(engine.value ?? fallbackEngine.value))
function insertName(d: SqlCatalogItem) {
  editorRef.value?.insert(sqlName(d, catalog.value, onCatalog.value))
}
function insertColumn(c: string) {
  editorRef.value?.insert(sqlIdent(c, connectionId.value == null ? onCatalog.value : dbType.value))
}

// ── a connection's tables beside the editor; their columns read when opened ──
const dbTables = ref<string[]>([])
const dbTablesState = ref<'idle' | 'loading' | 'error'>('idle')
const dbTablesError = ref('')
const dbColumns = ref<Record<string, { name: string; dtype: string }[] | 'loading' | { error: string }>>({})
const openTable = ref<string | null>(null)
let tablesFor: number | null = null
async function loadTables() {
  const id = connection.value?.id ?? null
  tablesFor = id
  dbTables.value = []
  dbColumns.value = {}
  openTable.value = null
  if (id == null) return
  dbTablesState.value = 'loading'
  try {
    const { tables } = await connectionsApi.tables(id)
    if (tablesFor !== id) return
    dbTables.value = tables
    dbTablesState.value = 'idle'
  } catch (e) {
    if (tablesFor !== id) return
    dbTablesError.value = errMessage(e)
    dbTablesState.value = 'error'
  }
}
watch(() => connection.value?.id ?? null, loadTables)
async function toggleTable(table: string) {
  openTable.value = openTable.value === table ? null : table
  const id = connection.value?.id
  if (openTable.value == null || id == null || Array.isArray(dbColumns.value[table])) return
  dbColumns.value = { ...dbColumns.value, [table]: 'loading' }
  try {
    const { columns } = await connectionsApi.columns(id, table)
    if (tablesFor === id) dbColumns.value = { ...dbColumns.value, [table]: columns }
  } catch (e) {
    if (tablesFor === id) dbColumns.value = { ...dbColumns.value, [table]: { error: errMessage(e) } }
  }
}
const columnsOf = (table: string) => { const c = dbColumns.value[table]; return Array.isArray(c) ? c : null }
const columnsError = (table: string) => { const c = dbColumns.value[table]; return c && typeof c === 'object' && !Array.isArray(c) ? c.error : '' }
const shownTables = computed(() => {
  const q = catalogQuery.value.trim().toLowerCase()
  return q ? dbTables.value.filter((n) => n.toLowerCase().includes(q)) : dbTables.value
})
function insertTable(table: string) {
  if (dbType.value) editorRef.value?.insert(sqlTableRef(table, dbType.value))
}

// what the editor completes: the datasources, or the connection's tables
const editorTables = computed<SqlTable[]>(() => {
  if (connectionId.value == null) {
    return catalog.value.filter((d) => d.has_data).map((d) => ({
      name: d.name, insert: sqlName(d, catalog.value, onCatalog.value), detail: d.path,
      columns: d.columns.map((c) => ({ name: c.name, insert: sqlIdent(c.name, onCatalog.value), type: c.type })),
    }))
  }
  const type = dbType.value
  if (!type) return []
  return dbTables.value.map((table) => {
    const cols = dbColumns.value[table]
    return {
      name: table.split('.').pop() ?? table, insert: sqlTableRef(table, type), detail: connection.value?.name ?? '',
      columns: Array.isArray(cols) ? cols.map((c) => ({ name: c.name, insert: sqlIdent(c.name, type), type: c.dtype })) : [],
    }
  })
})

// ── its datasource: written from the saved version now, on a schedule, by a flow group ──
const projectsApi = useProjects()
const projects = ref<Project[]>([])
async function loadProjects() {
  try { projects.value = await projectsApi.list() } catch { projects.value = [] }
}
const folderOptions = computed(() => projects.value.map((p) => ({ value: p.id, label: p.name, hint: folderPaths.pathOf(p.parent_id) })))
function onOutName() {
  // a name typed first: the script's own folder until another is chosen
  if (outName.value.trim() && outProject.value == null) outProject.value = projectId.value
}
const savedOutput = computed(() => detail.value?.content.output ?? null)

const writes = ref<SqlScriptRun[]>([])
const lastWrite = computed(() => writes.value[0] ?? null)
const writing = computed(() => lastWrite.value != null && ['PENDING', 'STARTED'].includes(lastWrite.value.status))
const launching = ref(false)
let following: number | null = null
let pageGone = false

async function loadWrites() {
  if (scriptId.value == null) {
    writes.value = []
    return
  }
  try { writes.value = await api.runs(scriptId.value, 5) } catch { /* the card keeps what it showed */ }
  if (writing.value && lastWrite.value) follow(lastWrite.value.run_id)
}

async function refreshNow() {
  if (scriptId.value == null || launching.value || writing.value) return
  if (dirty.value) {
    toast.error(t('sqlScript.saveBeforeRefresh'))
    return
  }
  launching.value = true
  try {
    const run = await api.refresh(scriptId.value)
    writes.value = [run, ...writes.value.filter((w) => w.run_id !== run.run_id)]
    follow(run.run_id)
  } catch (e) {
    toast.error(errMessage(e))
  } finally {
    launching.value = false
  }
}

// checks often at first, then less (composables/waits.ts); the datasource's line follows
async function follow(runId: number) {
  if (following === runId) return
  following = runId
  let delay = FIRST_PAUSE_MS
  const since = Date.now()
  while (!pageGone && following === runId) {
    await new Promise((r) => setTimeout(r, delay))
    delay = nextPause(delay, since, 5000)
    if (pageGone || following !== runId || scriptId.value == null) return
    try { writes.value = await api.runs(scriptId.value, 5) } catch { continue }
    const w = writes.value.find((x) => x.run_id === runId)
    if (!w || w.status === 'SUCCESS' || w.status === 'FAILURE') {
      following = null
      const target = savedOutput.value?.name ?? ''
      if (w?.status === 'SUCCESS') {
        toast.success(t('sqlScript.writeSucceeded', { name: target }))
        try {
          const d = await api.get(scriptId.value)
          if (detail.value) detail.value = { ...detail.value, datasource: d.datasource }
        } catch { /* the link comes with the next load */ }
      } else if (w) {
        toast.error(t('sqlScript.writeFailed', { name: target }))
      }
      return
    }
  }
}
function writeLine(w: SqlScriptRun): string {
  const when = fmtx.dateTime(w.finished_at ?? w.started_at ?? '')
  const by = w.trigger_type === 'schedule' ? t('sqlScript.bySchedule')
    : w.parent_run_id != null ? t('sqlScript.byGroup')
      : w.launched_by ? t('sqlScript.byPerson', { who: w.launched_by }) : ''
  const what = w.status === 'SUCCESS' ? t('sqlScript.writeOk', { when, rows: fmtx.count(w.rows ?? 0) }, w.rows ?? 0)
    : w.status === 'FAILURE' ? t('sqlScript.writeKo', { when }) : t('sqlScript.writeRunning')
  return by ? `${what} · ${by}` : what
}

// the schedule: RUN on the folder; it writes with the rights of whoever sets it
const scheduleOpen = ref(false)
const savingSchedule = ref(false)
async function saveSchedule(cron: string) {
  if (scriptId.value == null || savingSchedule.value) return
  savingSchedule.value = true
  try {
    const d = await api.setSchedule(scriptId.value, cron)
    // only the schedule changed: unsaved edits stay on screen
    if (detail.value) detail.value = { ...detail.value, run_schedule: d.run_schedule, run_scheduled_by: d.run_scheduled_by, next_run_at: d.next_run_at }
    scheduleOpen.value = false
    toast.success(d.run_schedule ? t('flowGroups.scheduleSaved') : t('flowGroups.scheduleCleared'))
  } catch (e) {
    toast.error(errMessage(e))
  } finally {
    savingSchedule.value = false
  }
}

// ── versions ─────────────────────────────────────────────────────────────────
const versions = ref<SqlScriptVersion[]>([])
const restoring = ref<number | null>(null)
// a version looked at in the editor, read-only; null = the SQL being edited
const viewing = ref<{ version: number; sql: string } | null>(null)

async function loadVersions() {
  if (scriptId.value == null) return
  try { versions.value = await api.versions(scriptId.value) } catch { versions.value = [] }
}

async function viewVersion(v: SqlScriptVersion) {
  if (scriptId.value == null) return
  if (v.current) {
    viewing.value = null
    return
  }
  try {
    viewing.value = { version: v.version, sql: (await api.version(scriptId.value, v.version)).sql }
  } catch (e) {
    toast.error(errMessage(e))
  }
}

async function restore(version: number) {
  if (scriptId.value == null || restoring.value != null) return
  if (dirty.value && !confirm(t('sqlScript.restoreDiscards'))) return
  if (!confirm(t('sqlScript.restoreConfirm', { n: version }))) return
  restoring.value = version
  try {
    apply(await api.restore(scriptId.value, version))
    viewing.value = null
    await loadVersions()
    toast.success(t('sqlScript.restored', { n: version }))
  } catch (e) {
    toast.error(errMessage(e))
  } finally {
    restoring.value = null
  }
}

// what the editor shows: the SQL being edited, or the version looked at
const editorText = computed({
  get: () => (viewing.value ? viewing.value.sql : sql.value),
  set: (v: string) => { if (!viewing.value) sql.value = v },
})

// ── keys and leaving ─────────────────────────────────────────────────────────
function onKey(e: KeyboardEvent) {
  if ((e.ctrlKey || e.metaKey) && !e.altKey && e.key.toLowerCase() === 's') {
    e.preventDefault()
    save()
  }
}
const guard = () => dirty.value && canEdit.value && !(scriptId.value == null && !sql.value.trim())
function onBeforeUnload(e: BeforeUnloadEvent) {
  if (!guard()) return
  e.preventDefault()
  e.returnValue = ''
}
onMounted(() => {
  document.addEventListener('keydown', onKey)
  window.addEventListener('beforeunload', onBeforeUnload)
  folderPaths.load()
  loadCatalog()
  loadCatalogList()
  loadConnections()
  loadProjects()
  load()
})
onUnmounted(() => {
  pageGone = true
  controller?.abort()
  document.removeEventListener('keydown', onKey)
  window.removeEventListener('beforeunload', onBeforeUnload)
})
onBeforeRouteLeave(() => !guard() || confirm(t('sqlScript.leaveConfirm')))
onBeforeRouteUpdate((to) => (Number(to.query.id) || null) === scriptId.value || !guard() || confirm(t('sqlScript.leaveConfirm')))
watch(() => route.query.id, (id) => {
  const next = Number(id) || null
  if (next === scriptId.value) return // a script just created took its address
  scriptId.value = next
  versions.value = []
  writes.value = []
  following = null
  load()
})
</script>

<template>
  <AppShell>
    <div class="qs">
      <div class="page-head">
        <h1>
          <DatabaseSearch :size="18" />
          <input
            v-model="name"
            class="qname"
            type="text"
            maxlength="200"
            :readonly="!canEdit"
            :placeholder="$t('sqlScript.namePlaceholder')"
            :aria-label="$t('sqlScript.nameLabel')"
          />
        </h1>
        <span v-if="folderPath" class="where muted">{{ $t('sqlScript.inFolder', { path: folderPath }) }}</span>
        <div class="head-actions">
          <span v-if="detail" class="tag">v{{ detail.current_version }}</span>
          <span v-if="dirty && canEdit && detail" class="tag unsaved">{{ $t('sqlScript.unsaved') }}</span>
          <Select
            class="source"
            :model-value="connectionId"
            :options="sourceOptions"
            :disabled="!!viewing"
            :aria-label="$t('sqlScript.source')"
            @update:model-value="(v: number | null) => (connectionId = v)"
          />
          <Select
            v-if="connectionId == null"
            class="engine"
            :model-value="engine"
            :options="engineOptions"
            :disabled="!!viewing"
            :aria-label="$t('sqlScript.engine')"
            @update:model-value="(v: string | null) => (engine = v)"
          />
          <button v-if="running" type="button" @click="stop">
            <Square :size="13" /> {{ $t('sqlScript.stop') }}
          </button>
          <button v-else type="button" :disabled="loading || !editorText.trim() || cannotRun" :title="$t('sqlScript.runTitle')" @click="run()">
            <Play :size="14" /> {{ $t('sqlScript.run') }}
          </button>
          <button v-if="canEdit" class="primary" :disabled="saving || loading || !!viewing || (!dirty && scriptId != null)" @click="save">
            <LoaderCircle v-if="saving" :size="14" class="spin" />
            <Save v-else :size="14" />
            {{ scriptId == null ? $t('sqlScript.create') : $t('sqlScript.save') }}
          </button>
        </div>
      </div>

      <p v-if="loadError" class="err" role="alert">{{ loadError }}</p>
      <SkeletonRows v-else-if="loading" :rows="5" />

      <div v-else class="work">
        <section class="main">
          <p v-if="cannotRun" class="note small" role="note">
            {{ foreignConnection?.name ? $t('sqlScript.cannotRun', { name: foreignConnection.name }) : $t('sqlScript.connectionGoneNote') }}
          </p>
          <p v-else-if="!canEdit" class="note small" role="note">{{ $t('sqlScript.readOnly') }}</p>
          <div v-if="viewing" class="viewing" role="status">
            <History :size="14" />
            <span>{{ $t('sqlScript.viewingVersion', { n: viewing.version }) }}</span>
            <button v-if="canEdit" type="button" class="mini" :disabled="restoring != null" @click="restore(viewing.version)">
              <RotateCcw :size="12" /> {{ $t('sqlScript.restore') }}
            </button>
            <button type="button" class="mini" @click="viewing = null"><X :size="12" /> {{ $t('sqlScript.backToCurrent') }}</button>
          </div>
          <div class="card editor">
            <SqlEditor
              ref="editorRef"
              v-model="editorText"
              :tables="editorTables"
              :read-only="!!viewing"
              @run="run"
            />
          </div>
          <p class="muted small keys">{{ $t('sqlScript.keys') }}</p>

          <div class="card result" aria-live="polite">
            <h2><Table2 :size="15" /> {{ $t('sqlScript.result') }}
              <span v-if="resultLine && !running && !runError" class="muted small rline">{{ resultLine }}</span>
              <span v-if="result && !runError" class="exports">
                <button type="button" class="mini" :disabled="running || !!exporting || cannotRun" :title="$t('sqlScript.exportCsvTitle')" @click="exportResult('csv')">
                  <LoaderCircle v-if="exporting === 'csv'" :size="12" class="spin" /><Download v-else :size="12" /> CSV
                </button>
                <button type="button" class="mini" :disabled="running || !!exporting || cannotRun" :title="$t('sqlScript.exportExcelTitle')" @click="exportResult('xlsx')">
                  <LoaderCircle v-if="exporting === 'xlsx'" :size="12" class="spin" /><FileSpreadsheet v-else :size="12" /> Excel
                </button>
              </span>
            </h2>
            <DataGrid
              v-if="running || result || runError"
              :result="result"
              :loading="running"
              :error="runError"
            />
            <p v-else class="muted small">{{ $t('sqlScript.noResult') }}</p>
          </div>
        </section>

        <aside class="side">
          <div class="card">
            <label class="field">
              <span>{{ $t('sqlScript.description') }}</span>
              <textarea v-model="description" rows="2" maxlength="2000" :readonly="!canEdit"
                        :placeholder="$t('sqlScript.descriptionPlaceholder')" />
            </label>
          </div>

          <div v-if="canEdit || savedOutput" class="card dest" :aria-label="$t('sqlScript.datasource')">
            <h2><Database :size="15" /> {{ $t('sqlScript.datasource') }}</h2>
            <p class="muted small help">{{ $t('sqlScript.datasourceHelp') }}</p>
            <label class="field">
              <span>{{ $t('sqlScript.datasourceName') }}</span>
              <input v-model="outName" type="text" maxlength="200" :readonly="!canEdit || !!viewing"
                     :placeholder="$t('sqlScript.datasourceNamePlaceholder')" @input="onOutName" />
            </label>
            <label v-if="outName.trim()" class="field">
              <span>{{ $t('sqlScript.datasourceFolder') }}</span>
              <Select
                :model-value="outProject"
                :options="folderOptions"
                :disabled="!canEdit || !!viewing"
                :aria-label="$t('sqlScript.datasourceFolder')"
                searchable
                @update:model-value="(v: number | null) => (outProject = v)"
              />
            </label>
            <template v-if="savedOutput && scriptId != null">
              <p v-if="detail?.datasource" class="small dsline">
                <NuxtLink :to="`/datasources?q=${encodeURIComponent(detail.datasource.name)}&ds=${detail.datasource.id}`">{{ detail.datasource.name }}</NuxtLink>
                <span v-if="detail.datasource.rows != null" class="muted"> · {{ $t('sqlScript.rows', { n: fmtx.count(detail.datasource.rows), more: '' }, detail.datasource.rows) }}</span>
              </p>
              <p v-else class="muted small">{{ $t('sqlScript.datasourceNotYet') }}</p>
              <div v-if="lastWrite" class="lastwrite small" :class="lastWrite.status.toLowerCase()" role="status">
                <LoaderCircle v-if="writing" :size="13" class="spin" />
                <CircleCheck v-else-if="lastWrite.status === 'SUCCESS'" :size="13" />
                <CircleX v-else :size="13" />
                <span>{{ writeLine(lastWrite) }}</span>
              </div>
              <p v-if="lastWrite?.status === 'FAILURE' && lastWrite.error" class="err small werr">{{ lastWrite.error }}</p>
              <div v-if="detail?.runnable" class="dest-actions">
                <button type="button" :disabled="launching || writing || cannotRun || !!viewing" :title="$t('sqlScript.refreshNowTitle')" @click="refreshNow">
                  <LoaderCircle v-if="launching || writing" :size="13" class="spin" /><RefreshCw v-else :size="13" />
                  {{ $t('sqlScript.refreshNow') }}
                </button>
                <button
                  type="button"
                  :title="detail.run_schedule ? $t('flowGroups.scheduleTitle', { cron: detail.run_schedule }) : $t('flowGroups.schedule')"
                  @click="scheduleOpen = true"
                >
                  <CalendarClock :size="13" />
                  <span v-if="detail.run_schedule" class="cron">{{ detail.run_schedule }}</span>
                  <template v-else>{{ $t('flowGroups.schedule') }}</template>
                </button>
              </div>
              <p v-if="detail?.next_run_at && detail.run_schedule" class="muted small">
                {{ $t('flowGroups.nextRun', { when: fmtx.dateTime(detail.next_run_at) }) }}
              </p>
            </template>
            <p v-else-if="outName.trim()" class="muted small">{{ $t('sqlScript.datasourceSaveFirst') }}</p>
          </div>

          <div v-if="connectionId != null" class="card catalog" :aria-label="$t('sqlScript.tables')">
            <h2>{{ $t('sqlScript.tables') }} <span v-if="dbTables.length" class="muted small">{{ dbTables.length }}</span></h2>
            <p class="muted small help">
              {{ connection ? $t('sqlScript.tablesHelp', { name: connection.name, type: dbLabel(connection.db_type) }) : $t('sqlScript.tablesUnavailable') }}
            </p>
            <template v-if="connection">
              <label class="search">
                <Search :size="13" aria-hidden="true" />
                <input v-model="catalogQuery" type="search" :placeholder="$t('sqlScript.tablesSearch')" :aria-label="$t('sqlScript.tablesSearch')" />
              </label>
              <SkeletonRows v-if="dbTablesState === 'loading'" :rows="4" />
              <p v-else-if="dbTablesState === 'error'" class="err small" role="alert">{{ dbTablesError }}</p>
              <ul v-else class="items">
                <li v-for="tb in shownTables" :key="tb">
                  <div class="item-head">
                    <button
                      type="button"
                      class="chev"
                      :aria-expanded="openTable === tb"
                      :aria-label="$t('sqlScript.columnsOf', { name: tb })"
                      @click="toggleTable(tb)"
                    >
                      <ChevronRight :size="13" />
                    </button>
                    <button type="button" class="dsname" :disabled="!!viewing" :title="$t('sqlScript.insertName')" @click="insertTable(tb)">
                      <span class="nm">{{ tb }}</span>
                    </button>
                  </div>
                  <template v-if="openTable === tb">
                    <p v-if="dbColumns[tb] === 'loading'" class="muted small nodata">{{ $t('sqlScript.columnsLoading') }}</p>
                    <p v-else-if="columnsError(tb)" class="err small nodata">{{ columnsError(tb) }}</p>
                    <ul v-else-if="columnsOf(tb)" class="cols">
                      <li v-for="c in columnsOf(tb)" :key="c.name">
                        <button type="button" :disabled="!!viewing" @click="insertColumn(c.name)">
                          <span class="cn">{{ c.name }}</span><span class="muted ct">{{ c.dtype }}</span>
                        </button>
                      </li>
                    </ul>
                  </template>
                </li>
              </ul>
              <p v-if="dbTablesState === 'idle' && !shownTables.length" class="muted small">{{ $t('sqlScript.tablesEmpty') }}</p>
            </template>
          </div>

          <div v-else class="card catalog" :aria-label="$t('sqlScript.catalog')">
            <h2>{{ $t('sqlScript.catalog') }} <span class="muted small">{{ catalog.length }}</span></h2>
            <p class="muted small help">{{ $t('sqlScript.catalogHelp') }}</p>
            <label class="search">
              <Search :size="13" aria-hidden="true" />
              <input v-model="catalogQuery" type="search" :placeholder="$t('sqlScript.catalogSearch')" :aria-label="$t('sqlScript.catalogSearch')" />
            </label>
            <ul class="items">
              <li v-for="d in shownCatalog" :key="d.id" :class="{ off: !d.has_data }">
                <div class="item-head">
                  <button
                    type="button"
                    class="chev"
                    :aria-expanded="openItem === d.id"
                    :aria-label="$t('sqlScript.columnsOf', { name: d.name })"
                    @click="openItem = openItem === d.id ? null : d.id"
                  >
                    <ChevronRight :size="13" />
                  </button>
                  <button type="button" class="dsname" :disabled="!d.has_data || !!viewing" :title="$t('sqlScript.insertName')" @click="insertName(d)">
                    <span class="nm">{{ d.name }}</span>
                    <span class="muted path">{{ d.path }}</span>
                  </button>
                </div>
                <p v-if="!d.has_data" class="muted small nodata">{{ $t('sqlScript.noData') }}</p>
                <ul v-if="openItem === d.id" class="cols">
                  <li v-for="c in d.columns" :key="c.name">
                    <button type="button" :disabled="!!viewing" @click="insertColumn(c.name)">
                      <span class="cn">{{ c.name }}</span><span class="muted ct">{{ c.type }}</span>
                    </button>
                  </li>
                </ul>
              </li>
            </ul>
            <p v-if="!shownCatalog.length" class="muted small">{{ $t('sqlScript.catalogEmpty') }}</p>
          </div>

          <div v-if="scriptId != null" class="card" :aria-label="$t('sqlScript.versions')">
            <h2><History :size="15" /> {{ $t('sqlScript.versions') }}</h2>
            <ol class="versions">
              <li v-for="v in versions" :key="v.version" :class="{ current: v.current, seen: viewing?.version === v.version }">
                <div class="vtop">
                  <strong>v{{ v.version }}</strong>
                  <span v-if="v.current" class="tag">{{ $t('sqlScript.currentVersion') }}</span>
                  <span class="muted small">{{ versionNote(v.note, t) }}</span>
                </div>
                <div class="muted small">{{ fmtx.dateTime(v.created_at) }}<template v-if="v.created_by"> · {{ v.created_by }}</template></div>
                <div class="vacts">
                  <button v-if="!v.current" type="button" class="mini" @click="viewVersion(v)">{{ $t('sqlScript.viewVersion') }}</button>
                  <button v-if="canEdit && !v.current" type="button" class="mini" :disabled="restoring != null" @click="restore(v.version)">
                    <LoaderCircle v-if="restoring === v.version" :size="12" class="spin" /><RotateCcw v-else :size="12" />
                    {{ $t('sqlScript.restore') }}
                  </button>
                </div>
              </li>
            </ol>
          </div>
        </aside>
      </div>

      <ScheduleDialog
        :open="scheduleOpen"
        :title="name"
        :subtitle="$t('sqlScript.scheduleSubtitle')"
        :current="detail?.run_schedule ?? null"
        :busy="savingSchedule"
        @save="saveSchedule"
        @cancel="scheduleOpen = false"
      />
    </div>
  </AppShell>
</template>

<style scoped>
.qs { display: flex; flex-direction: column; gap: 12px; min-height: 0; }
.page-head { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.page-head h1 { display: inline-flex; align-items: center; gap: 8px; margin: 0; flex: 0 1 380px; min-width: 0; }
.qname { font-size: 16px; font-weight: 600; padding: 4px 9px; min-width: 0; width: 100%; }
.qname[readonly] { border-color: transparent; background: transparent; padding-left: 0; }
.where { font-size: 12.5px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; min-width: 0; flex: 1; }
.head-actions { display: flex; align-items: center; gap: 8px; margin-left: auto; flex-shrink: 0; flex-wrap: wrap; }
.head-actions button { display: inline-flex; align-items: center; gap: 6px; white-space: nowrap; }
.engine { width: 190px; }
.source { width: 210px; }
.tag {
  font-size: 10px; text-transform: uppercase; letter-spacing: 0.04em; padding: 1px 7px;
  border-radius: 8px; background: var(--panel-2); border: 1px solid var(--border); color: var(--muted);
}
.tag.unsaved { color: var(--warning); border-color: color-mix(in srgb, var(--warning) 45%, var(--border)); }
.err { color: var(--danger); }
.work { display: grid; grid-template-columns: minmax(0, 1fr) 300px; gap: 16px; align-items: start; }
.main { display: flex; flex-direction: column; gap: 10px; min-width: 0; }
.side { display: flex; flex-direction: column; gap: 12px; min-width: 0; }
.card { border: 1px solid var(--border-soft); border-radius: 10px; padding: 12px 14px; background: var(--panel); display: flex; flex-direction: column; gap: 10px; min-width: 0; }
.card h2 { margin: 0; font-size: 13.5px; display: flex; align-items: center; gap: 7px; flex-wrap: wrap; }
.editor { padding: 0; height: clamp(220px, 40vh, 520px); resize: vertical; overflow: hidden; }
.keys { margin: -4px 0 0; }
.rline { font-weight: 400; }
.result { min-height: 120px; }
.result :deep(.datagrid) { max-height: 52vh; overflow: auto; }
.note { margin: 0; padding: 8px 10px; border-radius: 8px; background: var(--bg-soft); border: 1px solid var(--border); }
.viewing { display: flex; align-items: center; gap: 8px; padding: 7px 10px; border-radius: 8px; font-size: 12.5px;
  border: 1px solid color-mix(in srgb, var(--accent) 45%, var(--border)); background: var(--bg-soft); }
.viewing span { flex: 1; }
.viewing .mini, .vacts .mini, .exports .mini { display: inline-flex; align-items: center; gap: 4px; }
.exports { margin-left: auto; display: inline-flex; gap: 6px; }
.field { display: flex; flex-direction: column; gap: 5px; font-size: 12px; color: var(--muted); }
.field textarea { resize: vertical; font: inherit; color: var(--text); }
.small { font-size: 12px; }
.help { margin: 0; line-height: 1.45; }
.search { display: flex; align-items: center; gap: 6px; color: var(--muted); }
.search input { flex: 1; min-width: 0; }
.items { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 2px; max-height: 46vh; overflow: auto; }
.item-head { display: flex; align-items: flex-start; gap: 2px; }
.chev, .dsname, .cols button {
  border: 0; background: none; min-height: 0; box-shadow: none; color: var(--text); cursor: pointer; text-align: left;
}
.chev { padding: 4px 2px; color: var(--muted); }
.chev[aria-expanded='true'] svg { transform: rotate(90deg); }
.chev svg { transition: transform 0.15s ease; }
.dsname { flex: 1; min-width: 0; display: flex; flex-direction: column; align-items: flex-start; justify-content: flex-start; padding: 3px 6px; border-radius: 6px; }
.dsname:hover:not(:disabled), .cols button:hover:not(:disabled) { background: var(--bg-soft); }
.dsname:active, .cols button:active { transform: none; }
.nm, .path { max-width: 100%; }
.nm { font-size: 12.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.path { font-size: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.off .nm { color: var(--muted); }
.nodata { margin: 0 0 0 26px; font-size: 11px; }
.cols { list-style: none; margin: 0 0 4px 22px; padding: 0 0 0 8px; border-left: 1px solid var(--border-soft); }
.cols button { width: 100%; display: flex; justify-content: space-between; gap: 8px; padding: 2px 6px; border-radius: 5px; font-size: 12px; }
.cn { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.ct { font-size: 11px; white-space: nowrap; }
.versions { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; max-height: 40vh; overflow: auto; }
.versions li { border: 1px solid var(--border-soft); border-radius: 8px; padding: 7px 9px; display: flex; flex-direction: column; gap: 3px; }
.versions li.current { border-color: var(--accent); }
.versions li.seen { background: var(--bg-soft); }
.vtop { display: flex; align-items: center; gap: 7px; flex-wrap: wrap; }
.vacts { display: flex; gap: 6px; margin-top: 3px; }
.field input { font: inherit; color: var(--text); }
.dsline { margin: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.lastwrite { display: flex; align-items: center; gap: 6px; color: var(--muted); }
.lastwrite.success svg { color: var(--accent-2); }
.lastwrite.failure svg { color: var(--danger); }
.werr { margin: -4px 0 0; overflow-wrap: anywhere; }
.dest-actions { display: flex; gap: 8px; flex-wrap: wrap; }
.dest-actions button { display: inline-flex; align-items: center; gap: 6px; }
.cron { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
@media (max-width: 900px) {
  .work { grid-template-columns: minmax(0, 1fr); }
  .engine, .source { width: 160px; }
  .head-actions { flex: 1 1 100%; flex-shrink: 1; min-width: 0; margin-left: 0; }
}
</style>
