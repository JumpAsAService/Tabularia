<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
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
} from 'lucide-vue-next'
import { errMessage } from '~/composables/useApi'
import { useFlows, type FlowSummary } from '~/composables/useFlows'
import {
  useProjects,
  CAPABILITIES,
  type Project,
  type Permission,
  type GroupOut,
  type UserOut,
} from '~/composables/useProjects'

const api = useProjects()
const flowsApi = useFlows()
const { user } = useAuth()

const projects = ref<Project[]>([])
const expanded = ref<Set<number>>(new Set())
const selectedId = ref<number | null>(null)
const error = ref('')

// flussi contenuti nel progetto selezionato
const flows = ref<FlowSummary[]>([])
const flowsError = ref('')

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
  // flussi contenuti nella cartella (basta VIEW)
  try {
    flows.value = await flowsApi.listByProject(id)
  } catch (e) {
    flowsError.value = errMessage(e)
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
  } catch (e) {
    error.value = errMessage(e)
  }
}

async function deleteFlow(flow: FlowSummary) {
  if (!confirm(`Eliminare il flusso "${flow.name}"?`)) return
  try {
    await flowsApi.remove(flow.id)
    flows.value = flows.value.filter((f) => f.id !== flow.id)
  } catch (e) {
    error.value = errMessage(e)
  }
}

onMounted(async () => {
  await loadProjects()
})

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
  } catch (e) {
    error.value = errMessage(e)
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
            <NuxtLink :to="`/editor?project=${selected.id}`" class="btn-link primary small">
              <Plus :size="13" /> Nuovo flusso
            </NuxtLink>
          </div>

          <p v-if="flowsError" class="muted">{{ flowsError }}</p>
          <p v-else-if="!flows.length" class="muted">Nessun flusso in questa cartella.</p>

          <table v-else class="flows">
            <tbody>
              <tr v-for="f in flows" :key="f.id">
                <td class="fname">
                  <NuxtLink :to="`/editor?flow=${f.id}`" class="flowlink">
                    <Workflow :size="14" /> {{ f.name }}
                  </NuxtLink>
                </td>
                <td class="fdate muted">{{ fmtDate(f.updated_at) }}</td>
                <td class="factions">
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
</style>
