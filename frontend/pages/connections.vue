<script setup lang="ts">
// All the usable connections (CONNECT capability): in-place test, edit, deletion. The
// creation stays in the folder (Explore), where the connection lives and takes its
// permissions.
import { nextTick, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { Plug, Search, Trash2, Folder, Pencil, CheckCircle2, XCircle, LoaderCircle } from 'lucide-vue-next'
import { errMessage } from '~/composables/useApi'
import {
  useConnections,
  type ConnectionInfo,
  type ConnectionDraft,
} from '~/composables/useConnections'
import { connectionChanges } from '~/composables/connectionChanges'
import { useProjects } from '~/composables/useProjects'
import { usePagedList } from '~/composables/usePagedList'

const connApi = useConnections()
const projectsApi = useProjects()
const toast = useToast()
const { t } = useI18n()

// server-side search (name/host/database, over the whole dataset) + pagination
const { q, items, total, offset, pageSize, loading, error, load, next, prev } =
  usePagedList<ConnectionInfo>((p) => connApi.listPaged(p), { initialQuery: String(useRoute().query.q ?? '') })
// coming from the global search (?conn=<id>): the row is highlighted and in view
const target = Number(useRoute().query.conn) || null
watch(items, (list) => {
  if (target != null && list.some((c) => c.id === target)) {
    nextTick(() => document.getElementById(`conn-${target}`)?.scrollIntoView({ block: 'center' }))
  }
})

const folderName = ref<Record<number, string>>({})
onMounted(async () => {
  try {
    const projects = await projectsApi.list()
    folderName.value = Object.fromEntries(projects.map((p) => [p.id, p.name]))
  } catch {
    /* the folder names are an extra */
  }
})

// ── Test in-place ────────────────────────────────────────────────────────────
const testState = ref<Record<number, 'busy' | 'ok' | string>>({})

async function test(c: ConnectionInfo) {
  testState.value = { ...testState.value, [c.id]: 'busy' }
  try {
    const res = await connApi.test(c.id)
    // SFTP: a key that is not accepted is only shown, nothing was sent; accepting it
    // (after comparing it) happens in the connection's dialog
    testState.value = { ...testState.value, [c.id]: res.needs_acceptance ? t('sftp.needsAcceptanceRow') : 'ok' }
  } catch (e) {
    testState.value = { ...testState.value, [c.id]: errMessage(e) }
  }
}

// ── Edit (reuses the folder's dialog) ───────────────────────────────
const editing = ref<ConnectionInfo | null>(null)
const dialogBusy = ref(false)
const dialogError = ref('')

async function saveEdit(draft: ConnectionDraft) {
  if (!editing.value) return
  dialogBusy.value = true
  dialogError.value = ''
  try {
    // only what the edit changed (an empty password keeps the stored one)
    await connApi.update(editing.value.id, connectionChanges(editing.value, draft))
    editing.value = null
    toast.success(t('connections.updatedToast', { name: draft.name }))
    await load()
  } catch (e) {
    dialogError.value = errMessage(e)
  } finally {
    dialogBusy.value = false
  }
}

async function remove(c: ConnectionInfo) {
  if (!confirm(t('connections.deleteConfirm', { name: c.name }))) return
  try {
    await connApi.remove(c.id)
    toast.success(t('connections.deletedToast', { name: c.name }))
    await load() // reloads the page (updates total/window)
  } catch (e) {
    toast.error(errMessage(e))
  }
}
</script>

<template>
  <AppShell>
    <div class="page-head">
      <h1><Plug :size="18" /> {{ $t('connections.title') }} <span class="muted count">{{ total }}</span></h1>
      <div class="head-actions">
        <span class="searchbox"><Search :size="14" /><input v-model="q" type="text" :placeholder="$t('connections.searchPlaceholder')" :aria-label="$t('connections.searchPlaceholder')" /></span>
      </div>
    </div>

    <p v-if="error" class="err">{{ error }}</p>
    <SkeletonRows v-else-if="loading" :rows="4" />
    <p v-else-if="!items.length" class="muted">
      {{ q ? $t('connections.noSearchResults') : $t('connections.noConnections') }}
    </p>

    <table v-else class="list">
      <thead>
        <tr><th>{{ $t('connections.colName') }}</th><th>{{ $t('connections.colType') }}</th><th>{{ $t('connections.colHostDatabase') }}</th><th>{{ $t('connections.colFolder') }}</th><th /></tr>
      </thead>
      <tbody>
        <tr v-for="c in items" :id="`conn-${c.id}`" :key="c.id" :class="{ target: c.id === target }">
          <td>
            <span class="rowlink"><Plug :size="14" /> {{ c.name }}</span>
            <div v-if="c.description" class="muted desc">{{ c.description }}</div>
            <div v-if="testState[c.id] === 'ok'" class="okline"><CheckCircle2 :size="12" /> {{ $t('connections.connectionWorks') }}</div>
            <div v-else-if="testState[c.id] && testState[c.id] !== 'busy'" class="koline" :title="testState[c.id]">
              <XCircle :size="12" /> {{ String(testState[c.id]).slice(0, 90) }}
            </div>
          </td>
          <td class="muted">{{ c.db_type }}</td>
          <td class="muted nowrap">{{ c.host }}{{ c.database ? '/' + c.database : '' }}</td>
          <td class="muted"><Folder :size="13" /> {{ folderName[c.project_id] ?? `#${c.project_id}` }}</td>
          <td class="right">
            <button class="mini" :title="$t('connections.testConnection')" :disabled="testState[c.id] === 'busy'" @click="test(c)">
              <LoaderCircle v-if="testState[c.id] === 'busy'" :size="13" class="spin" />
              <Plug v-else :size="13" />
            </button>
            <button class="mini" :title="$t('connections.editConnection')" :aria-label="$t('connections.editConnection')" @click="editing = c"><Pencil :size="13" /></button>
            <button class="mini danger" :title="$t('connections.deleteConnection')" @click="remove(c)"><Trash2 :size="13" /></button>
          </td>
        </tr>
      </tbody>
    </table>

    <Pager :offset="offset" :page-size="pageSize" :total="total" :loading="loading" @prev="prev" @next="next" />

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

<style scoped>
/* the connection opened from the global search: it is found at a glance */
tr.target td { background: var(--tint-accent); }
/* a phone: the table fits (752 px wide, the page scrolled sideways) */
@media (max-width: 720px) {
  td.nowrap { white-space: normal; overflow-wrap: anywhere; }
  th:nth-child(2), td:nth-child(2), th:nth-child(4), td:nth-child(4) { display: none; }
}
</style>
