<script setup lang="ts">
// Tutte le connessioni usabili (capability CONNECT): test in-place, modifica,
// eliminazione. La creazione resta nella cartella (Explore), dove la
// connessione vive e prende i permessi.
import { onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { Plug, Search, Trash2, Folder, Pencil, CheckCircle2, XCircle, LoaderCircle } from 'lucide-vue-next'
import { errMessage } from '~/composables/useApi'
import {
  useConnections,
  type ConnectionInfo,
  type ConnectionDraft,
} from '~/composables/useConnections'
import { useProjects } from '~/composables/useProjects'
import { usePagedList } from '~/composables/usePagedList'

const connApi = useConnections()
const projectsApi = useProjects()
const toast = useToast()
const { t } = useI18n()

// ricerca server-side (nome/host/database, su tutto il dataset) + paginazione
const { q, items, total, offset, pageSize, loading, error, load, next, prev } =
  usePagedList<ConnectionInfo>((p) => connApi.listPaged(p))

const folderName = ref<Record<number, string>>({})
onMounted(async () => {
  try {
    const projects = await projectsApi.list()
    folderName.value = Object.fromEntries(projects.map((p) => [p.id, p.name]))
  } catch {
    /* i nomi cartella sono accessori */
  }
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
    await load() // ricarica la pagina (aggiorna totale/finestra)
  } catch (e) {
    toast.error(errMessage(e))
  }
}
</script>

<template>
  <AppShell>
    <div class="page-head">
      <h2><Plug :size="18" /> {{ $t('connections.title') }} <span class="muted count">{{ total }}</span></h2>
      <div class="head-actions">
        <span class="searchbox"><Search :size="14" /><input v-model="q" type="text" :placeholder="$t('connections.searchPlaceholder')" /></span>
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
        <tr v-for="c in items" :key="c.id">
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
            <button class="mini" :title="$t('connections.editConnection')" @click="editing = c"><Pencil :size="13" /></button>
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
