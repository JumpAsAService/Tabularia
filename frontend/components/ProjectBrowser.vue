<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import {
  ChevronRight,
  ChevronDown,
  Folder,
  FolderOpen,
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
} from 'lucide-vue-next'
import { errMessage, useApi } from '~/composables/useApi'
import { useFlows, type FlowSummary } from '~/composables/useFlows'
import { useRuns, type RunInfo } from '~/composables/useRuns'
import {
  useDatasources,
  type DatasourceInfo,
  type DbDatasourceDraft,
} from '~/composables/useDatasources'
import {
  useConnections,
  type ConnectionInfo,
  type ConnectionDraft,
} from '~/composables/useConnections'
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
const { user } = useAuth()
const toast = useToast()

const projects = ref<Project[]>([])
const expanded = ref<Set<number>>(new Set())
const selectedId = ref<number | null>(null)
const error = ref('')

// flussi contenuti nel progetto selezionato
const flows = ref<FlowSummary[]>([])
const flowsError = ref('')

// motori disponibili per il dropdown "Nuovo flusso" (il flusso è pinnato al
// motore scelto). Stesso comportamento della tab Flows.
interface EngineOpt { id: string; label: string; available: boolean; description: string }
const engines = ref<EngineOpt[]>([{ id: 'polars', label: 'Polars', available: true, description: '' }])
const newMenu = ref(false)
function createFlowWith(engineId: string) {
  newMenu.value = false
  if (selectedId.value == null) return
  navigateTo(`/editor?project=${selectedId.value}&engine=${engineId}`)
}
onMounted(async () => {
  try {
    engines.value = await coreApi.engines()
  } catch {
    /* fallback al solo Polars */
  }
})

// permessi del progetto selezionato (null = pannello nascosto / non gestibile)
const canManage = ref(false)
const permissions = ref<Permission[]>([])
const groups = ref<GroupOut[]>([])
const users = ref<UserOut[]>([])

const isSuper = computed(() => !!user.value?.is_superuser)
const selected = computed(() => projects.value.find((p) => p.id === selectedId.value) ?? null)

// ── Albero: DFS rispettando gli espansi ─────────────────────────────────────
const idSet = computed(() => new Set(projects.value.map((p) => p.id)))
function childrenOf(parentId: number | null): Project[] {
  return projects.value
    .filter((p) => p.parent_id === parentId)
    .sort((a, b) => a.name.localeCompare(b.name))
}
const roots = computed(() =>
  projects.value
    .filter((p) => p.parent_id === null || !idSet.value.has(p.parent_id))
    .sort((a, b) => a.name.localeCompare(b.name)),
)

interface Row {
  project: Project
  depth: number
  hasChildren: boolean
}
const rows = computed<Row[]>(() => {
  const out: Row[] = []
  const walk = (p: Project, depth: number) => {
    const kids = childrenOf(p.id)
    out.push({ project: p, depth, hasChildren: kids.length > 0 })
    if (expanded.value.has(p.id)) kids.forEach((k) => walk(k, depth + 1))
  }
  roots.value.forEach((r) => walk(r, 0))
  return out
})

function toggle(id: number) {
  const s = new Set(expanded.value)
  s.has(id) ? s.delete(id) : s.add(id)
  expanded.value = s
}

// ── Caricamento ─────────────────────────────────────────────────────────────
async function loadProjects() {
  error.value = ''
  try {
    projects.value = await api.list()
  } catch (e) {
    error.value = errMessage(e)
  }
}

async function selectProject(id: number) {
  selectedId.value = id
  canManage.value = false
  permissions.value = []
  flows.value = []
  flowsError.value = ''
  dsList.value = []
  expandedFlowId.value = null
  ingestToken++ // ferma i poll di ingest della cartella precedente
  ingestRuns.value = {}
  // flussi contenuti nella cartella (basta VIEW)
  try {
    flows.value = await flowsApi.listByProject(id)
  } catch (e) {
    flowsError.value = errMessage(e)
  }
  // datasource della cartella
  try {
    dsList.value = await dsApi.listByProject(id)
    // datasource DB senza snapshot: il primo import potrebbe essere in corso
    for (const d of dsList.value) {
      if (d.kind === 'database' && d.rows == null) pollIngest(d.id, ingestToken)
    }
  } catch {
    dsList.value = []
  }
  // connessioni: visibili solo con la capability CONNECT sulla cartella
  try {
    connections.value = await connApi.listByProject(id)
    canConnect.value = true
  } catch {
    connections.value = []
    canConnect.value = false
  }
  // se riusciamo a leggere i permessi → abbiamo MANAGE sul progetto
  try {
    permissions.value = await api.permissions(id)
    canManage.value = true
    if (!groups.value.length) groups.value = await api.groups()
    if (isSuper.value && !users.value.length) users.value = await api.users()
  } catch {
    canManage.value = false
  }
}

// ── Flussi: sposta / elimina ────────────────────────────────────────────────
function fmtDate(iso: string | null): string {
  if (!iso) return ''
  const d = new Date(iso)
  return d.toLocaleDateString('it-IT', { day: '2-digit', month: '2-digit', year: '2-digit' }) +
    ' ' + d.toLocaleTimeString('it-IT', { hour: '2-digit', minute: '2-digit' })
}

const movingFlowId = ref<number | null>(null) // riga con il selettore "sposta" aperto

async function moveFlow(flow: FlowSummary, target: number | null) {
  if (!target || target === flow.project_id) {
    movingFlowId.value = null
    return
  }
  try {
    await flowsApi.update(flow.id, { project_id: target })
    movingFlowId.value = null
    flows.value = flows.value.filter((f) => f.id !== flow.id)
    toast.success(`Flow "${flow.name}" moved`)
  } catch (e) {
    toast.error(errMessage(e))
  }
}

async function deleteFlow(flow: FlowSummary) {
  if (!confirm(`Eliminare il flusso "${flow.name}"?`)) return
  try {
    await flowsApi.remove(flow.id)
    flows.value = flows.value.filter((f) => f.id !== flow.id)
    toast.success(`Flow "${flow.name}" deleted`)
  } catch (e) {
    toast.error(errMessage(e))
  }
}

// ── Cronologia run (espandibile per flusso) ────────────────────────────────
const expandedFlowId = ref<number | null>(null)
const flowRuns = ref<RunInfo[]>([])
const runsLoading = ref(false)

const isTerminal = (r: RunInfo) => r.status === 'SUCCESS' || r.status === 'FAILURE'

async function loadRuns(flowId: number) {
  try {
    const rows = await runsApi.listByFlow(flowId) // il GET riconcilia gli stati
    if (expandedFlowId.value !== flowId) return // risposta stantia: riga cambiata/chiusa
    flowRuns.value = rows
    // run in corso → auto-refresh finché la riga resta espansa
    if (rows.some((r) => !isTerminal(r))) {
      setTimeout(() => {
        if (expandedFlowId.value === flowId) loadRuns(flowId)
      }, 2500)
    }
  } catch (e) {
    if (expandedFlowId.value === flowId) error.value = errMessage(e)
  } finally {
    runsLoading.value = false
  }
}

function toggleRuns(flow: FlowSummary) {
  if (expandedFlowId.value === flow.id) {
    expandedFlowId.value = null
    return
  }
  expandedFlowId.value = flow.id
  flowRuns.value = []
  runsLoading.value = true
  loadRuns(flow.id)
}

onUnmounted(() => {
  expandedFlowId.value = null // ferma l'auto-refresh
  ingestToken++ // ferma i poll degli ingest
})

function fmtDuration(run: RunInfo): string {
  if (!run.started_at || !run.finished_at) return '—'
  const ms = new Date(run.finished_at).getTime() - new Date(run.started_at).getTime()
  if (ms < 1000) return '<1s'
  const s = Math.round(ms / 1000)
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`
}

// ── Datasources della cartella ─────────────────────────────────────────────
const dsList = ref<DatasourceInfo[]>([])

async function deleteDatasource(ds: DatasourceInfo) {
  if (!confirm(`Eliminare la datasource "${ds.name}"? Il parquet verrà rimosso dallo storage.`)) return
  try {
    await dsApi.remove(ds.id)
    dsList.value = dsList.value.filter((d) => d.id !== ds.id)
    toast.success(`Datasource "${ds.name}" deleted`)
  } catch (e) {
    toast.error(errMessage(e))
  }
}

// ── Ingest (import/refresh) delle datasource database ──────────────────────
// stato dell'ultimo ingest per datasource; il token invalida i poll orfani
const ingestRuns = ref<Record<number, RunInfo>>({})
let ingestToken = 0

async function pollIngest(dsId: number, token: number) {
  try {
    const runs = await dsApi.listRuns(dsId) // il GET riconcilia gli stati
    if (token !== ingestToken) return
    const last = runs[0]
    if (!last) return
    ingestRuns.value = { ...ingestRuns.value, [dsId]: last }
    if (!isTerminal(last)) {
      setTimeout(() => {
        if (token === ingestToken) pollIngest(dsId, token)
      }, 2500)
    } else if (last.status === 'SUCCESS' && selectedId.value) {
      dsList.value = await dsApi.listByProject(selectedId.value) // righe/refreshed_at
    }
  } catch {
    // errore transitorio: il prossimo refresh manuale riproverà
  }
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

// ── Datasource da database (dialog) ─────────────────────────────────────────
const showDbDsDialog = ref(false)
const dbDsBusy = ref(false)
const dbDsError = ref('')
const usableConnections = ref<ConnectionInfo[]>([])

async function openDbDsDialog() {
  dbDsError.value = ''
  try {
    // tutte quelle con CONNECT; le S3 sono solo destinazioni, non sorgenti
    usableConnections.value = (await connApi.list()).filter((c) => c.db_type !== 's3')
  } catch {
    usableConnections.value = []
  }
  showDbDsDialog.value = true
}

async function createDbDatasource(draft: DbDatasourceDraft) {
  if (!selectedId.value) return
  dbDsBusy.value = true
  dbDsError.value = ''
  try {
    const ds = await dsApi.createDb(selectedId.value, draft)
    showDbDsDialog.value = false
    toast.success(`Datasource "${ds.name}" created — import started`)
    dsList.value = await dsApi.listByProject(selectedId.value)
    pollIngest(ds.id, ingestToken)
  } catch (e) {
    dbDsError.value = errMessage(e) // resta nel dialog, input preservato
  } finally {
    dbDsBusy.value = false
  }
}

// ── Connessioni della cartella (capability CONNECT) ─────────────────────────
const connections = ref<ConnectionInfo[]>([])
const canConnect = ref(false)
const showConnDialog = ref(false)
const editingConn = ref<ConnectionInfo | null>(null)
const connBusy = ref(false)
const connDialogError = ref('')

function openConnDialog(conn: ConnectionInfo | null) {
  editingConn.value = conn
  connDialogError.value = ''
  showConnDialog.value = true
}

async function saveConnection(draft: ConnectionDraft) {
  if (!selectedId.value) return
  connBusy.value = true
  connDialogError.value = ''
  try {
    if (editingConn.value) {
      const body: any = { ...draft }
      if (!body.password) delete body.password // vuota = invariata
      await connApi.update(editingConn.value.id, body)
      toast.success(`Connection "${draft.name}" updated`)
    } else {
      await connApi.create(selectedId.value, draft)
      toast.success(`Connection "${draft.name}" created`)
    }
    showConnDialog.value = false
    connections.value = await connApi.listByProject(selectedId.value)
  } catch (e) {
    connDialogError.value = errMessage(e)
  } finally {
    connBusy.value = false
  }
}

async function deleteConnection(c: ConnectionInfo) {
  if (!confirm(`Delete connection "${c.name}"?`)) return
  try {
    await connApi.remove(c.id)
    connections.value = connections.value.filter((x) => x.id !== c.id)
    toast.success(`Connection "${c.name}" deleted`)
  } catch (e) {
    toast.error(errMessage(e))
  }
}

onMounted(async () => {
  await loadProjects()
})

// se crei/modifichi una connessione (o datasource/flusso) in un'altra scheda,
// tornando qui riaggiorniamo l'albero e la cartella aperta: niente vista stantia
function onVisible() {
  if (document.visibilityState !== 'visible') return
  loadProjects()
  if (selectedId.value != null) selectProject(selectedId.value)
}
onMounted(() => document.addEventListener('visibilitychange', onVisible))
onUnmounted(() => document.removeEventListener('visibilitychange', onVisible))

// ── Azioni progetti ─────────────────────────────────────────────────────────
const newRootName = ref('')
const newChildName = ref('')

async function createRoot() {
  if (!newRootName.value.trim()) return
  try {
    const p = await api.create({ name: newRootName.value.trim(), parent_id: null })
    newRootName.value = ''
    await loadProjects()
    await selectProject(p.id)
  } catch (e) {
    error.value = errMessage(e)
  }
}

async function createChild() {
  if (!selectedId.value || !newChildName.value.trim()) return
  try {
    const p = await api.create({ name: newChildName.value.trim(), parent_id: selectedId.value })
    newChildName.value = ''
    expanded.value = new Set(expanded.value).add(selectedId.value)
    await loadProjects()
    await selectProject(p.id)
  } catch (e) {
    error.value = errMessage(e)
  }
}

async function removeProject(p: Project) {
  if (!confirm(`Eliminare la cartella "${p.name}"?`)) return
  try {
    await api.remove(p.id)
    if (selectedId.value === p.id) selectedId.value = null
    await loadProjects()
    toast.success(`Folder "${p.name}" deleted`)
  } catch (e) {
    toast.error(errMessage(e))
  }
}

// ── Permessi ────────────────────────────────────────────────────────────────
const grantSubjectType = ref<'group' | 'user'>('group')
const grantGroupId = ref<number | null>(null)
const grantUserId = ref<number | null>(null)
const grantCapability = ref<string>('view')

function groupName(id: number | null) {
  return groups.value.find((g) => g.id === id)?.name ?? (id != null ? `gruppo #${id}` : '')
}
function userLabel(id: number | null) {
  return users.value.find((u) => u.id === id)?.email ?? (id != null ? `utente #${id}` : '')
}

async function grant() {
  if (!selectedId.value) return
  const body: any = { capability: grantCapability.value }
  if (grantSubjectType.value === 'group') {
    if (!grantGroupId.value) return
    body.group_id = grantGroupId.value
  } else {
    if (!grantUserId.value) return
    body.user_id = grantUserId.value
  }
  try {
    await api.grant(selectedId.value, body)
    permissions.value = await api.permissions(selectedId.value)
  } catch (e) {
    error.value = errMessage(e)
  }
}

async function revoke(perm: Permission) {
  try {
    await api.revoke(perm.id)
    permissions.value = permissions.value.filter((p) => p.id !== perm.id)
  } catch (e) {
    error.value = errMessage(e)
  }
}
</script>

<template>
  <div class="browser">
    <!-- Albero cartelle -->
    <div class="tree-pane">
      <div class="pane-head">
        <span>Progetti</span>
        <button class="mini" title="Ricarica" @click="loadProjects"><RefreshCw :size="13" /></button>
      </div>

      <div v-if="!rows.length" class="muted empty">
        Nessun progetto visibile.
        <template v-if="isSuper"> Crea il primo qui sotto.</template>
      </div>

      <ul class="tree">
        <li
          v-for="row in rows"
          :key="row.project.id"
          :class="{ sel: row.project.id === selectedId }"
          :style="{ paddingLeft: 8 + row.depth * 16 + 'px' }"
          @click="selectProject(row.project.id)"
        >
          <span
            class="caret"
            :class="{ hidden: !row.hasChildren }"
            @click.stop="toggle(row.project.id)"
          >
            <ChevronDown v-if="expanded.has(row.project.id)" :size="14" />
            <ChevronRight v-else :size="14" />
          </span>
          <span class="folder">
            <FolderOpen v-if="expanded.has(row.project.id) && row.hasChildren" :size="15" />
            <Folder v-else :size="15" />
          </span>
          <span class="name">{{ row.project.name }}</span>
        </li>
      </ul>

      <div v-if="isSuper" class="new-root">
        <input v-model="newRootName" type="text" placeholder="Nuovo progetto root…" @keyup.enter="createRoot" />
        <button class="primary" @click="createRoot"><Plus :size="14" /> Root</button>
      </div>
    </div>

    <!-- Dettaglio progetto selezionato -->
    <div class="detail-pane">
      <div v-if="!selected" class="muted empty">Seleziona un progetto.</div>

      <template v-else>
        <div class="pane-head">
          <span class="detail-title"><Folder :size="16" /> {{ selected.name }}</span>
        </div>

        <!-- flussi contenuti nella cartella -->
        <div class="section">
          <div class="section-head">
            <label>Flussi</label>
            <div class="newflow">
              <button class="btn-link primary small" @click="newMenu = !newMenu">
                <Plus :size="13" /> Nuovo flusso
              </button>
              <div v-if="newMenu" class="menu-backdrop" @click="newMenu = false" />
              <div v-if="newMenu" class="menu-pop">
                <div class="menu-label">Motore di esecuzione</div>
                <button
                  v-for="e in engines"
                  :key="e.id"
                  class="menu-item"
                  :disabled="!e.available"
                  @click="createFlowWith(e.id)"
                >
                  <span class="mi-top">{{ e.label }}<span v-if="e.id === preferredEngine && e.available" class="pref">preferita</span><span v-if="!e.available" class="soon">in arrivo</span></span>
                  <span v-if="e.description" class="mi-desc">{{ e.description }}</span>
                </button>
              </div>
            </div>
          </div>

          <p v-if="flowsError" class="muted">{{ flowsError }}</p>
          <p v-else-if="!flows.length" class="muted">Nessun flusso in questa cartella.</p>

          <table v-else class="flows">
            <tbody>
              <template v-for="f in flows" :key="f.id">
              <tr>
                <td class="fname">
                  <NuxtLink :to="`/editor?flow=${f.id}`" class="flowlink">
                    <Workflow :size="14" /> {{ f.name }}
                  </NuxtLink>
                </td>
                <td class="fdate muted">{{ fmtDate(f.updated_at) }}</td>
                <td class="factions">
                  <button
                    class="mini"
                    :class="{ activebtn: expandedFlowId === f.id }"
                    title="Cronologia dei run"
                    @click="toggleRuns(f)"
                  ><History :size="13" /></button>
                  <Select
                    v-if="movingFlowId === f.id"
                    class="movesel"
                    :model-value="null"
                    :options="projects.filter((p) => p.id !== f.project_id).map((p) => ({ value: p.id, label: p.name }))"
                    placeholder="sposta in…"
                    @update:model-value="(v: any) => moveFlow(f, v)"
                    @close="movingFlowId = null"
                  />
                  <button
                    v-else
                    class="mini"
                    title="Sposta in un'altra cartella"
                    @click="movingFlowId = f.id"
                  ><FolderInput :size="13" /></button>
                  <button class="mini danger" title="Elimina flusso" @click="deleteFlow(f)">
                    <Trash2 :size="13" />
                  </button>
                </td>
              </tr>

              <!-- cronologia dei run del flusso (espansa) -->
              <tr v-if="expandedFlowId === f.id" class="runsrow">
                <td colspan="3">
                  <p v-if="runsLoading" class="muted runmeta">
                    <LoaderCircle :size="13" class="spin" /> Carico la cronologia…
                  </p>
                  <p v-else-if="!flowRuns.length" class="muted runmeta">Nessun run per questo flusso.</p>
                  <ul v-else class="runlist">
                    <li v-for="r in flowRuns" :key="r.id" class="runitem">
                      <CheckCircle2 v-if="r.status === 'SUCCESS'" :size="14" class="rok" />
                      <XCircle v-else-if="r.status === 'FAILURE'" :size="14" class="rko" />
                      <LoaderCircle v-else :size="14" class="spin rwip" />
                      <span class="rwhen">{{ fmtDate(r.started_at) }}</span>
                      <span class="muted">{{ fmtDuration(r) }}</span>
                      <span v-if="r.rows_written != null" class="muted">{{ r.rows_written }} righe</span>
                      <span v-if="r.publish_name" class="rpub">
                        <Database :size="12" /> {{ r.publish_name }}
                      </span>
                      <span v-if="r.error" class="rerr" :title="r.error">{{ r.error.slice(0, 80) }}</span>
                    </li>
                  </ul>
                </td>
              </tr>
              </template>
            </tbody>
          </table>
        </div>

        <!-- datasource della cartella -->
        <div class="section">
          <div class="section-head">
            <label>Datasources</label>
            <button class="mini" @click="openDbDsDialog"><Plus :size="13" /> From database</button>
          </div>
          <p v-if="!dsList.length" class="muted">Nessuna datasource in questa cartella.</p>
          <table v-else class="flows">
            <tbody>
              <tr v-for="d in dsList" :key="d.id">
                <td class="fname">
                  <span class="flowlink dsname" :title="d.source_ref ?? ''">
                    <Database :size="14" /> {{ d.name }}
                    <span v-if="d.kind === 'database'" class="dbtag">db</span>
                  </span>
                </td>
                <td class="fdate muted">
                  <template v-if="ingestRuns[d.id] && !isTerminal(ingestRuns[d.id])">
                    <LoaderCircle :size="12" class="spin rwip" /> importing…
                  </template>
                  <template v-else-if="ingestRuns[d.id]?.status === 'FAILURE'">
                    <span class="rerr" :title="ingestRuns[d.id].error ?? ''">import failed</span>
                  </template>
                  <template v-else>
                    {{ d.rows != null ? `${d.rows} righe` : '' }} · {{ fmtDate(d.refreshed_at ?? d.updated_at) }}
                  </template>
                </td>
                <td class="factions">
                  <button
                    v-if="d.kind === 'database'"
                    class="mini"
                    title="Refresh snapshot (re-run the source)"
                    :disabled="!!ingestRuns[d.id] && !isTerminal(ingestRuns[d.id])"
                    @click="refreshDatasource(d)"
                  ><RefreshCw :size="13" /></button>
                  <button class="mini danger" title="Elimina datasource (anche il parquet)" @click="deleteDatasource(d)">
                    <Trash2 :size="13" />
                  </button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>

        <!-- connessioni della cartella (solo con capability CONNECT) -->
        <div v-if="canConnect" class="section">
          <div class="section-head">
            <label>Connections</label>
            <button class="mini" @click="openConnDialog(null)"><Plus :size="13" /> New connection</button>
          </div>
          <p v-if="!connections.length" class="muted">No connections in this folder.</p>
          <table v-else class="flows">
            <tbody>
              <tr v-for="c in connections" :key="c.id">
                <td class="fname">
                  <span class="flowlink dsname"><Plug :size="14" /> {{ c.name }}</span>
                </td>
                <td class="fdate muted">
                  {{ c.db_type }} · {{ c.host }}{{ c.database ? '/' + c.database : '' }}
                </td>
                <td class="factions">
                  <button class="mini" title="Edit connection" @click="openConnDialog(c)">
                    <Pencil :size="13" />
                  </button>
                  <button class="mini danger" title="Delete connection" @click="deleteConnection(c)">
                    <Trash2 :size="13" />
                  </button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>

        <template v-if="canManage">
          <!-- crea sottocartella -->
          <div class="section">
            <label>Nuova sottocartella</label>
            <div class="row">
              <input v-model="newChildName" type="text" placeholder="Nome…" @keyup.enter="createChild" />
              <button @click="createChild"><Plus :size="14" /> Cartella</button>
            </div>
          </div>

          <!-- permessi -->
          <div class="section">
            <label>Permessi</label>
            <table class="perm">
              <tbody>
                <tr v-for="p in permissions" :key="p.id">
                  <td class="subject">
                    <UsersIcon v-if="p.group_id != null" :size="14" />
                    <UserIcon v-else :size="14" />
                    {{ p.group_id != null ? groupName(p.group_id) : userLabel(p.user_id) }}
                  </td>
                  <td><span class="cap">{{ p.capability }}</span></td>
                  <td class="right"><button class="mini danger" @click="revoke(p)"><X :size="13" /></button></td>
                </tr>
                <tr v-if="!permissions.length">
                  <td colspan="3" class="muted">Nessun permesso esplicito.</td>
                </tr>
              </tbody>
            </table>

            <div class="grant">
              <Select
                v-model="grantSubjectType"
                :options="isSuper ? [{ value: 'group', label: 'Gruppo' }, { value: 'user', label: 'Utente' }] : [{ value: 'group', label: 'Gruppo' }]"
              />
              <Select
                v-if="grantSubjectType === 'group'"
                v-model="grantGroupId"
                :options="groups.map((g) => ({ value: g.id, label: g.name }))"
                placeholder="gruppo…"
              />
              <Select
                v-else
                v-model="grantUserId"
                :options="users.map((u) => ({ value: u.id, label: u.email }))"
                placeholder="utente…"
              />
              <Select v-model="grantCapability" :options="CAPABILITIES" />
              <button class="primary" @click="grant">Concedi</button>
            </div>
          </div>

          <div class="section">
            <button class="danger" @click="removeProject(selected)"><Trash2 :size="14" /> Elimina cartella</button>
          </div>
        </template>

        <div v-else class="muted section">
          Hai accesso in sola lettura a questa cartella.
        </div>
      </template>
    </div>

    <p v-if="error" class="err">{{ error }}</p>

    <ConnectionDialog
      :open="showConnDialog"
      :project-id="selectedId ?? 0"
      :existing="editingConn"
      :error="connDialogError"
      :busy="connBusy"
      @confirm="saveConnection"
      @cancel="showConnDialog = false"
    />
    <DbDatasourceDialog
      :open="showDbDsDialog"
      :connections="usableConnections"
      :error="dbDsError"
      :busy="dbDsBusy"
      @confirm="createDbDatasource"
      @cancel="showDbDsDialog = false"
    />
  </div>
</template>

<style scoped>
.browser {
  display: grid;
  grid-template-columns: 320px 1fr;
  gap: 1px;
  background: var(--border);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  overflow: hidden;
  min-height: 60vh;
}
.tree-pane,
.detail-pane {
  background: var(--bg);
  padding: 10px 12px;
}
.pane-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-weight: 600;
  margin-bottom: 8px;
}
.detail-title { font-size: 15px; display: inline-flex; align-items: center; gap: 6px; }
.empty { padding: 16px 4px; }

ul.tree { list-style: none; margin: 0; padding: 0; }
ul.tree li {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 4px 8px;
  border-radius: 6px;
  cursor: pointer;
  white-space: nowrap;
}
ul.tree li:hover { background: var(--panel-2); }
ul.tree li.sel { background: rgba(79, 140, 255, 0.16); }
.caret { width: 14px; color: var(--muted); display: inline-flex; align-items: center; }
.caret.hidden { visibility: hidden; }
.folder { display: inline-flex; align-items: center; color: var(--accent-2); }
.name { overflow: hidden; text-overflow: ellipsis; }

.new-root { display: flex; gap: 6px; margin-top: 12px; }
.section-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 6px;
}
.section-head > label { margin-bottom: 0; }
table.flows { width: 100%; border-collapse: collapse; font-size: 13px; }
table.flows td { padding: 5px 6px; border-bottom: 1px solid var(--border); }
td.fname { width: 55%; }
td.fdate { white-space: nowrap; font-size: 12px; }
td.factions { text-align: right; white-space: nowrap; display: flex; gap: 4px; justify-content: flex-end; }
.flowlink {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--text);
  text-decoration: none;
}
.flowlink:hover { color: var(--accent); }
.movesel { width: 130px; font-size: 12px; padding: 3px 6px; }
.activebtn { border-color: var(--accent); }
.runsrow td { background: var(--bg-soft); }
.runmeta { display: flex; align-items: center; gap: 6px; font-size: 12px; margin: 4px 2px; }
.runlist { list-style: none; margin: 2px 0; padding: 0; display: flex; flex-direction: column; gap: 3px; }
.runitem { display: flex; align-items: center; gap: 10px; font-size: 12px; padding: 2px 4px; }
.rok { color: var(--accent-2); }
.rko { color: var(--danger); }
.rwip { color: var(--accent); }
.rwhen { min-width: 110px; }
.rpub { display: inline-flex; align-items: center; gap: 4px; color: var(--accent-hi); }
.rerr { color: var(--danger); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.dsname { cursor: default; }
.dbtag {
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  padding: 0 6px;
  border-radius: 8px;
  background: var(--panel-2);
  border: 1px solid var(--border);
  color: var(--muted);
}
.btn-link.small { font-size: 12px; padding: 4px 10px; }
.btn-link {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 6px 12px;
  border-radius: 6px;
  text-decoration: none;
  background: var(--accent);
  border: 1px solid var(--accent);
  color: #fff;
}
.section { margin-top: 16px; }
.section > label { display: block; font-size: 12px; color: var(--muted); margin-bottom: 6px; }
.row { display: flex; gap: 6px; }

table.perm { width: 100%; border-collapse: collapse; font-size: 13px; }
table.perm td { padding: 5px 6px; border-bottom: 1px solid var(--border); }
table.perm td.subject { display: flex; align-items: center; gap: 6px; }
table.perm td.right { text-align: right; width: 32px; }
.cap {
  font-size: 11px;
  padding: 1px 8px;
  border-radius: 10px;
  background: var(--panel-2);
  border: 1px solid var(--border);
}
.grant { display: flex; gap: 6px; margin-top: 8px; flex-wrap: wrap; }
.grant select { width: auto; flex: 1; min-width: 90px; }

button.mini { padding: 2px 8px; }
button.danger, .mini.danger { border-color: var(--danger); color: var(--danger); }
button.danger:hover { background: var(--danger); color: #fff; }
.err { color: var(--danger); margin-top: 12px; }
/* dropdown "Nuovo flusso" con scelta del motore (come nella tab Flows) */
.newflow { position: relative; }
.menu-backdrop { position: fixed; inset: 0; z-index: 40; }
.menu-pop { position: absolute; right: 0; top: calc(100% + 6px); z-index: 41; min-width: 260px; background: var(--panel); border: 1px solid var(--border); border-radius: 9px; box-shadow: 0 12px 32px rgba(0, 0, 0, 0.28); padding: 6px; }
.menu-label { font-size: 11px; text-transform: uppercase; letter-spacing: 0.04em; color: var(--muted); padding: 6px 8px 4px; }
.menu-item { display: flex; flex-direction: column; align-items: flex-start; gap: 2px; width: 100%; text-align: left; padding: 8px 9px; border: none; background: transparent; border-radius: 7px; cursor: pointer; }
.menu-item:hover:not(:disabled) { background: var(--panel-2); }
.menu-item:disabled { opacity: 0.55; cursor: not-allowed; }
.mi-top { display: inline-flex; align-items: center; gap: 7px; font-weight: 600; font-size: 13px; color: var(--text); }
.mi-desc { font-size: 11.5px; color: var(--muted); line-height: 1.35; }
.soon { font-size: 10px; text-transform: uppercase; letter-spacing: 0.04em; color: var(--accent-2); border: 1px solid var(--accent-2); border-radius: 20px; padding: 1px 6px; }
.pref { font-size: 10px; text-transform: uppercase; letter-spacing: 0.04em; color: var(--accent); background: var(--tint-accent); border-radius: 20px; padding: 1px 6px; }
</style>
