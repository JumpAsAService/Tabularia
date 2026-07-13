<script setup lang="ts">
// Tutte le datasource nelle cartelle leggibili: ricerca, refresh (kind=database)
// con stato live, eliminazione.
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { Database, Search, Trash2, Folder, RefreshCw, LoaderCircle, CalendarClock } from 'lucide-vue-next'
import { errMessage } from '~/composables/useApi'
import { skeletonPad } from '~/composables/useSkeleton'
import { useDatasources, type DatasourceInfo } from '~/composables/useDatasources'
import { useProjects } from '~/composables/useProjects'
import type { RunInfo } from '~/composables/useRuns'

const dsApi = useDatasources()
const projectsApi = useProjects()
const toast = useToast()

const list = ref<DatasourceInfo[]>([])
const folderName = ref<Record<number, string>>({})
const q = ref('')
const loading = ref(true)
const error = ref('')

onMounted(async () => {
  const t0 = performance.now()
  try {
    const [ds, projects] = await Promise.all([dsApi.list(), projectsApi.list()])
    list.value = ds
    folderName.value = Object.fromEntries(projects.map((p) => [p.id, p.name]))
  } catch (e) {
    error.value = errMessage(e)
  } finally {
    await skeletonPad(t0) // skeleton visibile almeno il minimo: niente flash
    loading.value = false
  }
})

const filtered = computed(() => {
  const needle = q.value.trim().toLowerCase()
  if (!needle) return list.value
  return list.value.filter(
    (d) =>
      d.name.toLowerCase().includes(needle) ||
      (folderName.value[d.project_id] ?? '').toLowerCase().includes(needle),
  )
})

function fmtDate(iso: string | null): string {
  if (!iso) return ''
  const d = new Date(iso)
  return d.toLocaleDateString('it-IT', { day: '2-digit', month: '2-digit', year: '2-digit' }) +
    ' ' + d.toLocaleTimeString('it-IT', { hour: '2-digit', minute: '2-digit' })
}

// ── Refresh con stato live (stesso pattern del ProjectBrowser) ──────────────
const ingestRuns = ref<Record<number, RunInfo>>({})
let pollToken = 0
onUnmounted(() => { pollToken++ })

const isTerminal = (r: RunInfo) => r.status === 'SUCCESS' || r.status === 'FAILURE'
const isImporting = (id: number) => !!ingestRuns.value[id] && !isTerminal(ingestRuns.value[id])

async function pollIngest(dsId: number, token: number) {
  try {
    const runs = await dsApi.listRuns(dsId)
    if (token !== pollToken) return
    const last = runs[0]
    if (!last) return
    ingestRuns.value = { ...ingestRuns.value, [dsId]: last }
    if (!isTerminal(last)) {
      setTimeout(() => { if (token === pollToken) pollIngest(dsId, token) }, 2500)
    } else if (last.status === 'SUCCESS') {
      list.value = await dsApi.list()
    }
  } catch { /* riproverà al prossimo refresh manuale */ }
}

async function refresh(d: DatasourceInfo) {
  try {
    const run = await dsApi.refresh(d.id)
    ingestRuns.value = { ...ingestRuns.value, [d.id]: run }
    pollIngest(d.id, pollToken)
  } catch (e) {
    toast.error(errMessage(e))
  }
}

async function remove(d: DatasourceInfo) {
  if (!confirm(`Delete datasource "${d.name}"? The parquet snapshot is removed too.`)) return
  try {
    await dsApi.remove(d.id)
    list.value = list.value.filter((x) => x.id !== d.id)
    toast.success(`Datasource "${d.name}" deleted`)
  } catch (e) {
    toast.error(errMessage(e))
  }
}

// ── Refresh schedulato (cron) — dialog condiviso ScheduleDialog ──────────────
const scheduleFor = ref<DatasourceInfo | null>(null)
const savingSchedule = ref(false)

function openSchedule(d: DatasourceInfo) {
  scheduleFor.value = d
}

async function saveSchedule(cron: string) {
  if (!scheduleFor.value) return
  savingSchedule.value = true
  try {
    const updated = await dsApi.setSchedule(scheduleFor.value.id, cron.trim())
    list.value = list.value.map((x) => (x.id === updated.id ? updated : x))
    toast.success(cron.trim() ? `Refresh schedulato: ${updated.refresh_schedule}` : 'Schedulazione disattivata')
    scheduleFor.value = null
  } catch (e) {
    toast.error(errMessage(e)) // 422 cron invalido, 403 permessi
  } finally {
    savingSchedule.value = false
  }
}
</script>

<template>
  <AppShell>
    <div class="page-head">
      <h2><Database :size="18" /> Datasources <span class="muted count">{{ list.length }}</span></h2>
      <div class="head-actions">
        <span class="searchbox"><Search :size="14" /><input v-model="q" type="text" placeholder="Search datasources…" /></span>
      </div>
    </div>

    <p v-if="error" class="err">{{ error }}</p>
    <SkeletonRows v-else-if="loading" :rows="4" />
    <p v-else-if="!filtered.length" class="muted">
      {{ list.length ? 'No datasource matches the search.' : 'No datasources yet: publish a flow output or import from a database.' }}
    </p>

    <table v-else class="list">
      <thead>
        <tr><th>Name</th><th>Folder</th><th>Rows</th><th>Updated</th><th /></tr>
      </thead>
      <tbody>
        <tr v-for="d in filtered" :key="d.id">
          <td>
            <span class="rowlink" :title="d.source_ref ?? ''">
              <Database :size="14" /> {{ d.name }}
              <span v-if="d.kind === 'database'" class="tag">db</span>
              <span v-else-if="d.kind === 'flow'" class="tag">flow</span>
            </span>
            <div v-if="d.description" class="muted desc">{{ d.description }}</div>
            <div v-if="d.refresh_schedule" class="muted sched">
              <CalendarClock :size="11" /> <code>{{ d.refresh_schedule }}</code>
              <span v-if="d.next_refresh_at"> · prossimo {{ fmtDate(d.next_refresh_at) }}</span>
            </div>
          </td>
          <td class="muted"><Folder :size="13" /> {{ folderName[d.project_id] ?? `#${d.project_id}` }}</td>
          <td class="muted nowrap">
            <template v-if="isImporting(d.id)">
              <span class="okline"><LoaderCircle :size="12" class="spin" /> importing…</span>
            </template>
            <template v-else-if="ingestRuns[d.id]?.status === 'FAILURE'">
              <span class="koline" :title="ingestRuns[d.id].error ?? ''">import failed</span>
            </template>
            <template v-else>{{ d.rows != null ? d.rows.toLocaleString('it-IT') : '—' }}</template>
          </td>
          <td class="muted nowrap">{{ fmtDate(d.refreshed_at ?? d.updated_at) }}</td>
          <td class="right">
            <button
              v-if="d.kind === 'database'"
              class="mini"
              :class="{ active: !!d.refresh_schedule }"
              title="Schedule automatic refresh (cron)"
              @click="openSchedule(d)"
            ><CalendarClock :size="13" /></button>
            <button
              v-if="d.kind === 'database'"
              class="mini"
              title="Refresh snapshot now (re-run the source)"
              :disabled="isImporting(d.id)"
              @click="refresh(d)"
            ><RefreshCw :size="13" /></button>
            <button class="mini danger" title="Delete datasource" @click="remove(d)"><Trash2 :size="13" /></button>
          </td>
        </tr>
      </tbody>
    </table>

    <ScheduleDialog
      :open="!!scheduleFor"
      :title="scheduleFor?.name ?? ''"
      subtitle="Aggiorna automaticamente lo snapshot di"
      :current="scheduleFor?.refresh_schedule ?? null"
      :busy="savingSchedule"
      @save="saveSchedule"
      @cancel="scheduleFor = null"
    />
  </AppShell>
</template>

<style scoped src="~/assets/listpage.css" />
<style scoped>
.sched { font-size: 11px; display: flex; align-items: center; gap: 4px; margin-top: 2px; }
.sched code { font-family: ui-monospace, monospace; }
.mini.active { color: var(--accent-2); border-color: var(--accent-2); }
</style>
