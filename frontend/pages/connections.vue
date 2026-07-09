<script setup lang="ts">
// Tutte le connessioni usabili (capability CONNECT): test in-place, modifica,
// eliminazione. La creazione resta nella cartella (Explore), dove la
// connessione vive e prende i permessi.
import { computed, onMounted, ref } from 'vue'
import { Plug, Search, Trash2, Folder, Pencil, CheckCircle2, XCircle, LoaderCircle } from 'lucide-vue-next'
import { errMessage } from '~/composables/useApi'
import {
  useConnections,
  type ConnectionInfo,
  type ConnectionDraft,
} from '~/composables/useConnections'
import { useProjects } from '~/composables/useProjects'

const connApi = useConnections()
const projectsApi = useProjects()
const toast = useToast()

const list = ref<ConnectionInfo[]>([])
const folderName = ref<Record<number, string>>({})
const q = ref('')
const loading = ref(true)
const error = ref('')

async function load() {
  const [conns, projects] = await Promise.all([connApi.list(), projectsApi.list()])
  list.value = conns
  folderName.value = Object.fromEntries(projects.map((p) => [p.id, p.name]))
}

onMounted(async () => {
  try {
    await load()
  } catch (e) {
    error.value = errMessage(e)
  } finally {
    loading.value = false
  }
})

const filtered = computed(() => {
  const needle = q.value.trim().toLowerCase()
  if (!needle) return list.value
  return list.value.filter(
    (c) =>
      c.name.toLowerCase().includes(needle) ||
      c.host.toLowerCase().includes(needle) ||
      c.db_type.toLowerCase().includes(needle) ||
      (folderName.value[c.project_id] ?? '').toLowerCase().includes(needle),
  )
})

// ── Test in-place ────────────────────────────────────────────────────────────
const testState = ref<Record<number, 'busy' | 'ok' | string>>({})

async function test(c: ConnectionInfo) {
  testState.value = { ...testState.value, [c.id]: 'busy' }
  try {
    await connApi.test(c.id)
    testState.value = { ...testState.value, [c.id]: 'ok' }
  } catch (e) {
    testState.value = { ...testState.value, [c.id]: errMessage(e) }
  }
}

// ── Modifica (riusa il dialog della cartella) ───────────────────────────────
const editing = ref<ConnectionInfo | null>(null)
const dialogBusy = ref(false)
const dialogError = ref('')

async function saveEdit(draft: ConnectionDraft) {
  if (!editing.value) return
  dialogBusy.value = true
  dialogError.value = ''
  try {
    const body: any = { ...draft }
    if (!body.password) delete body.password // vuota = invariata
    await connApi.update(editing.value.id, body)
    editing.value = null
    toast.success(`Connection "${draft.name}" updated`)
    await load()
  } catch (e) {
    dialogError.value = errMessage(e)
  } finally {
    dialogBusy.value = false
  }
}

async function remove(c: ConnectionInfo) {
  if (!confirm(`Delete connection "${c.name}"?`)) return
  try {
    await connApi.remove(c.id)
    list.value = list.value.filter((x) => x.id !== c.id)
    toast.success(`Connection "${c.name}" deleted`)
  } catch (e) {
    toast.error(errMessage(e))
  }
}
</script>

<template>
  <AppShell>
    <div class="page-head">
      <h2><Plug :size="18" /> Connections <span class="muted count">{{ list.length }}</span></h2>
      <div class="head-actions">
        <span class="searchbox"><Search :size="14" /><input v-model="q" type="text" placeholder="Search connections…" /></span>
      </div>
    </div>

    <p v-if="error" class="err">{{ error }}</p>
    <p v-else-if="loading" class="muted">Loading…</p>
    <p v-else-if="!filtered.length" class="muted">
      {{ list.length ? 'No connection matches the search.' : 'No usable connections: create one from a folder where you have the CONNECT permission.' }}
    </p>

    <table v-else class="list">
      <thead>
        <tr><th>Name</th><th>Type</th><th>Host / database</th><th>Folder</th><th /></tr>
      </thead>
      <tbody>
        <tr v-for="c in filtered" :key="c.id">
          <td>
            <span class="rowlink"><Plug :size="14" /> {{ c.name }}</span>
            <div v-if="c.description" class="muted desc">{{ c.description }}</div>
            <div v-if="testState[c.id] === 'ok'" class="okline"><CheckCircle2 :size="12" /> connection works</div>
            <div v-else-if="testState[c.id] && testState[c.id] !== 'busy'" class="koline" :title="testState[c.id]">
              <XCircle :size="12" /> {{ String(testState[c.id]).slice(0, 90) }}
            </div>
          </td>
          <td class="muted">{{ c.db_type }}</td>
          <td class="muted nowrap">{{ c.host }}{{ c.database ? '/' + c.database : '' }}</td>
          <td class="muted"><Folder :size="13" /> {{ folderName[c.project_id] ?? `#${c.project_id}` }}</td>
          <td class="right">
            <button class="mini" title="Test connection" :disabled="testState[c.id] === 'busy'" @click="test(c)">
              <LoaderCircle v-if="testState[c.id] === 'busy'" :size="13" class="spin" />
              <Plug v-else :size="13" />
            </button>
            <button class="mini" title="Edit connection" @click="editing = c"><Pencil :size="13" /></button>
            <button class="mini danger" title="Delete connection" @click="remove(c)"><Trash2 :size="13" /></button>
          </td>
        </tr>
      </tbody>
    </table>

    <ConnectionDialog
      :open="!!editing"
      :project-id="editing?.project_id ?? 0"
      :existing="editing"
      :error="dialogError"
      :busy="dialogBusy"
      @confirm="saveEdit"
      @cancel="editing = null"
    />
  </AppShell>
</template>

<style scoped src="~/assets/listpage.css" />
