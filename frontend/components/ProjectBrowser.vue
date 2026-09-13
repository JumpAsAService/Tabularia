<script setup lang="ts">
// Explore: navigazione a CARTELLE con una sola lista di risorse.
//
// Prima erano due riquadri: un albero fisso da 320px a sinistra e, a destra,
// fino a sette sezioni impilate di cui QUATTRO erano la stessa identica tabella
// (flussi, datasource, viste, connessioni). Il risultato era affollato e senza
// ritmo: nulla diceva a colpo d'occhio cosa contenesse una cartella.
//
// Ora: breadcrumb per il percorso, le sottocartelle come righe della lista
// insieme a tutto il resto, un selettore per tipo coi conteggi, e una ricerca
// per nome. L'amministrazione della cartella (permessi, sottocartelle,
// eliminazione) esce dallo scorrimento dei dati e vive in un pannello laterale
// a scomparsa: le azioni distruttive non si incontrano più per caso mentre si
// leggono i propri flussi.
//
// La ricerca qui è LOCALE (filtra ciò che è già caricato): istantanea e senza
// rete. Quella che attraversa le cartelle è `/search` (vedi useSearch.ts).
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
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
} from 'lucide-vue-next'
import { errMessage, useApi } from '~/composables/useApi'
import { useFlows, type FlowSummary } from '~/composables/useFlows'
import { useRuns, type RunInfo } from '~/composables/useRuns'
import { useDatasources, type DatasourceInfo, type DbDatasourceDraft } from '~/composables/useDatasources'
import { useConnections, type ConnectionInfo, type ConnectionDraft } from '~/composables/useConnections'
import { useSavedViews, type SavedView } from '~/composables/useSavedViews'
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
const { user } = useAuth()
const toast = useToast()
const { t } = useI18n()
const route = useRoute()

const projects = ref<Project[]>([])
const currentId = ref<number | null>(null)
const error = ref('')

const flows = ref<FlowSummary[]>([])
const dsList = ref<DatasourceInfo[]>([])
const savedViews = ref<SavedView[]>([])
const connections = ref<ConnectionInfo[]>([])
const canConnect = ref(false)
const canManage = ref(false)
const permissions = ref<Permission[]>([])
const groups = ref<GroupOut[]>([])
const users = ref<UserOut[]>([])
const loading = ref(false)

const isSuper = computed(() => !!user.value?.is_superuser)
const current = computed(() => projects.value.find((p) => p.id === currentId.value) ?? null)

// ── Percorso e navigazione ──────────────────────────────────────────────────
const byId = computed(() => new Map(projects.value.map((p) => [p.id, p])))

/** Catena radice → cartella corrente. Sostituisce l'albero: il percorso si
 *  legge in una riga invece che in una colonna larga 320px. */
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
  // il percorso sta nell'URL: ricaricare la pagina o tornare indietro col
  // browser riporta dove si era, e `/search` può linkare una cartella
  navigateTo(id == null ? '/' : `/?folder=${id}`, { replace: true })
  loadFolder(id)
}

// ── Riga unificata: un solo tipo di riga per cinque tipi di risorsa ─────────
type Kind = 'folder' | 'flow' | 'datasource' | 'view' | 'connection'
interface Row {
  kind: Kind
  id: number
  name: string
  meta: string
  /** valorizzato = la riga è un collegamento; altrimenti è solo testo */
  to?: string
  raw: any
}

const KIND_ICON: Record<Kind, any> = {
  folder: Folder,
  flow: Workflow,
  datasource: Database,
  view: Bookmark,
  connection: Plug,
}

function fmtDate(iso: string | null): string {
  if (!iso) return ''
  const d = new Date(iso)
  return (
    d.toLocaleDateString('it-IT', { day: '2-digit', month: '2-digit', year: '2-digit' }) +
    ' ' +
    d.toLocaleTimeString('it-IT', { hour: '2-digit', minute: '2-digit' })
  )
}

const rows = computed<Row[]>(() => {
  const out: Row[] = []
  for (const p of childrenOf(currentId.value)) {
    out.push({ kind: 'folder', id: p.id, name: p.name, meta: p.description || '', raw: p })
  }
  for (const f of flows.value) {
    out.push({ kind: 'flow', id: f.id, name: f.name, meta: fmtDate(f.updated_at), to: `/editor?flow=${f.id}`, raw: f })
  }
  for (const d of dsList.value) {
    const stato = ingestRuns.value[d.id]
    const meta = stato && !isTerminal(stato)
      ? t('projectBrowser.importingLabel')
      : stato?.status === 'FAILURE'
        ? t('projectBrowser.importFailedLabel')
        : [d.rows != null ? t('projectBrowser.rowsCount', { n: d.rows }) : '', fmtDate(d.refreshed_at ?? d.updated_at)]
            .filter(Boolean)
            .join(' · ')
    out.push({ kind: 'datasource', id: d.id, name: d.name, meta, raw: d })
  }
  for (const v of savedViews.value) {
    out.push({ kind: 'view', id: v.id, name: v.name, meta: v.datasource_name ?? '', to: `/viewer?view=${v.id}`, raw: v })
  }
  for (const c of connections.value) {
    out.push({
      kind: 'connection', id: c.id, name: c.name,
      meta: `${c.db_type}${c.database ? ' · ' + c.database : ''}`, raw: c,
    })
  }
  return out
})

// ── Ricerca locale + filtro per tipo ────────────────────────────────────────
const q = ref('')
const kindFilter = ref<Kind | null>(null)

const matching = computed(() => {
  const ago = q.value.trim().toLowerCase()
  return ago ? rows.value.filter((r) => r.name.toLowerCase().includes(ago)) : rows.value
})
/** I conteggi seguono la RICERCA ma non il filtro per tipo: cliccando un
 *  segmento i numeri degli altri non devono sparire. */
const counts = computed(() => {
  const c: Record<string, number> = {}
  for (const r of matching.value) c[r.kind] = (c[r.kind] ?? 0) + 1
  return c
})
const visibleRows = computed(() =>
  kindFilter.value ? matching.value.filter((r) => r.kind === kindFilter.value) : matching.value,
)
const segments = computed(() =>
  (['folder', 'flow', 'datasource', 'view', 'connection'] as Kind[])
    .filter((k) => counts.value[k])
    .map((k) => ({ kind: k, n: counts.value[k] })),
)

// ── Caricamento ─────────────────────────────────────────────────────────────
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
  dsList.value = []
  savedViews.value = []
  connections.value = []
  permissions.value = []
  canManage.value = false
  canConnect.value = false
  expandedFlowId.value = null
  ingestToken++ // ferma i poll della cartella precedente
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
        if (d.kind === 'database' && d.rows == null) pollIngest(d.id, ingestToken)
      }
    } catch { dsList.value = [] }
    try { savedViews.value = await viewsApi.listByProject(id) } catch { savedViews.value = [] }
    // le connessioni richiedono CONNECT: il rifiuto è informazione, non errore
    try {
      connections.value = await connApi.listByProject(id)
      canConnect.value = true
    } catch { connections.value = []; canConnect.value = false }
    // se i permessi si leggono, abbiamo MANAGE
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
  try { engines.value = await coreApi.engines() } catch { /* resta Polars */ }
})

// creando o modificando in un'altra scheda, tornando qui si ricarica
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

// ── Cronologia run (espandibile, con auto-aggiornamento) ────────────────────
const expandedFlowId = ref<number | null>(null)
const flowRuns = ref<RunInfo[]>([])
const runsLoading = ref(false)
const isTerminal = (r: RunInfo) => r.status === 'SUCCESS' || r.status === 'FAILURE'

async function loadRuns(flowId: number) {
  try {
    const rows = await runsApi.listByFlow(flowId) // il GET riconcilia gli stati
    if (expandedFlowId.value !== flowId) return // risposta stantia
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

// ── Import/refresh delle datasource database ────────────────────────────────
const ingestRuns = ref<Record<number, RunInfo>>({})
let ingestToken = 0

async function pollIngest(dsId: number, token: number) {
  try {
    const runs = await dsApi.listRuns(dsId)
    if (token !== ingestToken) return
    const last = runs[0]
    if (!last) return
    ingestRuns.value = { ...ingestRuns.value, [dsId]: last }
    if (!isTerminal(last)) {
      setTimeout(() => { if (token === ingestToken) pollIngest(dsId, token) }, 2500)
    } else if (last.status === 'SUCCESS' && currentId.value) {
      dsList.value = await dsApi.listByProject(currentId.value)
    }
  } catch { /* transitorio: riproverà al prossimo refresh */ }
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

// ── Eliminazioni e spostamento ──────────────────────────────────────────────
const movingFlowId = ref<number | null>(null)

async function moveFlow(flow: FlowSummary, target: number | null) {
  if (!target || target === flow.project_id) { movingFlowId.value = null; return }
  try {
    await flowsApi.update(flow.id, { project_id: target })
    movingFlowId.value = null
    flows.value = flows.value.filter((f) => f.id !== flow.id)
    toast.success(t('projectBrowser.flowMoved', { name: flow.name }))
  } catch (e) { toast.error(errMessage(e)) }
}

async function removeRow(row: Row) {
  const chiedi = (k: string) => confirm(t(k, { name: row.name }))
  try {
    if (row.kind === 'flow') {
      if (!chiedi('projectBrowser.confirmDeleteFlow')) return
      await flowsApi.remove(row.id)
      flows.value = flows.value.filter((f) => f.id !== row.id)
      toast.success(t('projectBrowser.flowDeleted', { name: row.name }))
    } else if (row.kind === 'datasource') {
      if (!chiedi('projectBrowser.confirmDeleteDatasource')) return
      await dsApi.remove(row.id)
      dsList.value = dsList.value.filter((d) => d.id !== row.id)
      toast.success(t('projectBrowser.datasourceDeleted', { name: row.name }))
    } else if (row.kind === 'view') {
      if (!chiedi('projectBrowser.confirmDeleteSavedView')) return
      await viewsApi.remove(row.id)
      savedViews.value = savedViews.value.filter((v) => v.id !== row.id)
      toast.success(t('projectBrowser.savedViewDeleted', { name: row.name }))
    } else if (row.kind === 'connection') {
      if (!chiedi('projectBrowser.confirmDeleteConnection')) return
      await connApi.remove(row.id)
      connections.value = connections.value.filter((c) => c.id !== row.id)
      toast.success(t('projectBrowser.connectionDeleted', { name: row.name }))
    } else if (row.kind === 'folder') {
      if (!chiedi('projectBrowser.confirmDeleteFolder')) return
      await api.remove(row.id)
      await loadProjects()
      toast.success(t('projectBrowser.folderDeleted', { name: row.name }))
    }
  } catch (e) { toast.error(errMessage(e)) }
}

// ── Creazione: un solo punto d'ingresso ─────────────────────────────────────
interface EngineOpt { id: string; label: string; available: boolean; description: string; optional?: boolean; disabled_by_admin?: boolean }
const engines = ref<EngineOpt[]>([{ id: 'polars', label: 'Polars', available: true, description: '' }])
const newMenu = ref(false)
const engineStep = ref(false) // secondo livello del menù: scelta del motore
const newFolderName = ref('')

function closeNew() { newMenu.value = false; engineStep.value = false; newFolderName.value = '' }

function createFlowWith(engineId: string) {
  closeNew()
  const dove = currentId.value != null ? `&project=${currentId.value}` : ''
  navigateTo(`/editor?engine=${engineId}${dove}`)
}

async function createFolder() {
  const nome = newFolderName.value.trim()
  if (!nome) return
  try {
    const p = await api.create({ name: nome, parent_id: currentId.value })
    closeNew()
    await loadProjects()
    openFolder(p.id)
  } catch (e) { toast.error(errMessage(e)) }
}

// ── Pannello laterale: amministrazione della cartella ───────────────────────
const settingsOpen = ref(false)
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
    const su = p.parent_id ?? null
    await loadProjects()
    openFolder(su)
    toast.success(t('projectBrowser.folderDeleted', { name: p.name }))
  } catch (e) { toast.error(errMessage(e)) }
}

// ── Dialoghi ────────────────────────────────────────────────────────────────
const showConnDialog = ref(false)
const editingConn = ref<ConnectionInfo | null>(null)
const connBusy = ref(false)
const connDialogError = ref('')
const showDbDsDialog = ref(false)
const dbDsBusy = ref(false)
const dbDsError = ref('')
const usableConnections = ref<ConnectionInfo[]>([])

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
      const body: any = { ...draft }
      if (!body.password) delete body.password // vuota = invariata
      await connApi.update(editingConn.value.id, body)
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
    // le S3 sono solo destinazioni, non sorgenti
    usableConnections.value = (await connApi.list()).filter((c) => c.db_type !== 's3')
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

// cambiando cartella si azzerano ricerca e filtro: sono domande sul contenuto
// corrente, non preferenze durature
watch(currentId, () => { q.value = ''; kindFilter.value = null; settingsOpen.value = false })
</script>

<template>
  <div class="explore">
    <!-- percorso + ricerca + unica azione primaria -->
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
        <input v-model="q" type="text" :placeholder="$t('projectBrowser.searchPlaceholder')" />
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
        <div v-if="newMenu" class="menu-pop">
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
              <span class="mi-desc">{{ engineDescription(e.id, e.description) }}</span>
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

    <!-- filtro per tipo: i numeri dicono cosa c'è dentro, prima di guardare -->
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
          <tr :class="{ folderrow: r.kind === 'folder' }">
            <td class="rname">
              <button v-if="r.kind === 'folder'" class="asname" @click="openFolder(r.id)">
                <component :is="KIND_ICON[r.kind]" :size="15" class="kicon" /> {{ r.name }}
              </button>
              <NuxtLink v-else-if="r.to" :to="r.to" class="asname">
                <component :is="KIND_ICON[r.kind]" :size="15" class="kicon" /> {{ r.name }}
              </NuxtLink>
              <span v-else class="asname plain">
                <component :is="KIND_ICON[r.kind]" :size="15" class="kicon" /> {{ r.name }}
              </span>
              <span v-if="r.kind === 'datasource' && r.raw.kind === 'database'" class="tag">db</span>
            </td>
            <td class="rmeta muted">{{ r.meta }}</td>
            <td class="racts">
              <button
                v-if="r.kind === 'flow'"
                class="mini"
                :class="{ activebtn: expandedFlowId === r.id }"
                :title="$t('projectBrowser.runHistoryTitle')"
                :aria-label="$t('projectBrowser.runHistoryTitle')"
                @click="toggleRuns(r.raw)"
              ><History :size="13" /></button>

              <Select
                v-if="r.kind === 'flow' && movingFlowId === r.id"
                class="movesel"
                :model-value="null"
                :options="projects.filter((p) => p.id !== r.raw.project_id).map((p) => ({ value: p.id, label: p.name }))"
                :placeholder="$t('projectBrowser.moveToPlaceholder')"
                @update:model-value="(v: any) => moveFlow(r.raw, v)"
                @close="movingFlowId = null"
              />
              <button
                v-else-if="r.kind === 'flow'"
                class="mini"
                :title="$t('projectBrowser.moveToFolderTitle')"
                :aria-label="$t('projectBrowser.moveToFolderTitle')"
                @click="movingFlowId = r.id"
              ><FolderInput :size="13" /></button>

              <button
                v-if="r.kind === 'datasource' && r.raw.kind === 'database'"
                class="mini"
                :title="$t('projectBrowser.refreshSnapshotTitle')"
                :aria-label="$t('projectBrowser.refreshSnapshotTitle')"
                :disabled="!!ingestRuns[r.id] && !isTerminal(ingestRuns[r.id])"
                @click="refreshDatasource(r.raw)"
              ><RefreshCw :size="13" /></button>

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
                  <span v-if="run.rows_written != null" class="muted">{{ $t('projectBrowser.rowsCount', { n: run.rows_written }) }}</span>
                  <span v-if="run.publish_name" class="rpub"><Database :size="12" /> {{ run.publish_name }}</span>
                  <span v-if="run.error" class="rerr" :title="run.error">{{ run.error.slice(0, 80) }}</span>
                </li>
              </ul>
            </td>
          </tr>
        </template>
      </tbody>
    </table>

    <!-- Amministrazione della cartella: fuori dallo scorrimento dei dati -->
    <template v-if="settingsOpen && canManage && current">
      <div class="drawer-backdrop" @click="settingsOpen = false" />
      <aside class="drawer" role="dialog" :aria-label="$t('projectBrowser.folderSettingsTitle')">
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
          <Select v-model="grantCapability" :options="CAPABILITIES" />
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
.explore { display: flex; flex-direction: column; min-height: 0; }

/* ── barra: percorso, ricerca, azione ─────────────────────────────────────── */
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
.newwrap .btn-link { white-space: nowrap; }
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

/* ── filtro per tipo ──────────────────────────────────────────────────────── */
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

/* ── lista unica ──────────────────────────────────────────────────────────── */
/* niente linea sotto OGNI riga: separa l'hover, non un reticolo di hairline */
table.rows { width: 100%; border-collapse: collapse; font-size: 13px; }
table.rows td { padding: 7px 8px; }
table.rows tr:hover td { background: var(--row-hover); }
tr.folderrow .kicon { color: var(--accent-2); }
.kicon { color: var(--muted); flex-shrink: 0; }
.rname { width: 55%; }
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
.rmeta { font-size: 12px; white-space: nowrap; }
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

/* ── pannello laterale ────────────────────────────────────────────────────── */
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
/* l'eliminazione sta in fondo al pannello, staccata: non si incontra per caso */
.dangerzone { margin-top: auto; padding-top: 14px; border-top: 1px solid var(--border-soft); }
button.mini { padding: 2px 8px; min-height: 24px; }
button.danger, .mini.danger { border-color: var(--danger); color: var(--danger); }
button.danger:hover { background: var(--danger); color: #fff; }

@media (max-width: 720px) {
  .bar { flex-wrap: wrap; }
  .searchbox input { width: 120px; }
  .rmeta { display: none; }
}
</style>
