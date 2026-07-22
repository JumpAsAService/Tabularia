<script setup lang="ts">
// Tutte le datasource nelle cartelle leggibili: ricerca, refresh (kind=database)
// con stato live, eliminazione.
import { onMounted, onUnmounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { Database, Search, Trash2, Folder, RefreshCw, LoaderCircle, CalendarClock } from 'lucide-vue-next'
import { errMessage } from '~/composables/useApi'
import { useDatasources, type DatasourceInfo } from '~/composables/useDatasources'
import { useProjects } from '~/composables/useProjects'
import { usePagedList } from '~/composables/usePagedList'
import type { RunInfo } from '~/composables/useRuns'

const dsApi = useDatasources()
const projectsApi = useProjects()
const toast = useToast()
const { t } = useI18n()

// ricerca server-side (nome/descrizione, su tutto il dataset) + paginazione
const { q, items, total, offset, pageSize, loading, error, load, next, prev } =
  usePagedList<DatasourceInfo>((p) => dsApi.listPaged(p))

const folderName = ref<Record<number, string>>({})
onMounted(async () => {
  try {
    const projects = await projectsApi.list()
    folderName.value = Object.fromEntries(projects.map((p) => [p.id, p.name]))
  } catch {
    /* i nomi cartella sono accessori */
  }
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
      await load() // snapshot aggiornato: ricarica la pagina
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
  if (!confirm(t('datasources.confirmDelete', { name: d.name }))) return
  try {
    await dsApi.remove(d.id)
    toast.success(t('datasources.deletedToast', { name: d.name }))
    await load() // ricarica la pagina (aggiorna totale/finestra)
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
    items.value = items.value.map((x) => (x.id === updated.id ? updated : x))
    toast.success(cron.trim() ? t('datasources.scheduleSetToast', { cron: updated.refresh_schedule }) : t('datasources.scheduleDisabledToast'))
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
      <h2><Database :size="18" /> {{ $t('datasources.title') }} <span class="muted count">{{ total }}</span></h2>
      <div class="head-actions">
        <span class="searchbox"><Search :size="14" /><input v-model="q" type="text" :placeholder="$t('datasources.searchPlaceholder')" /></span>
      </div>
    </div>

    <p v-if="error" class="err">{{ error }}</p>
    <SkeletonRows v-else-if="loading" :rows="4" />
    <p v-else-if="!items.length" class="muted">
      {{ q ? $t('datasources.noSearchResults') : $t('datasources.emptyState') }}
    </p>

    <table v-else class="list">
      <thead>
        <tr><th>{{ $t('datasources.colName') }}</th><th>{{ $t('datasources.colFolder') }}</th><th>{{ $t('datasources.colRows') }}</th><th>{{ $t('datasources.colUpdated') }}</th><th /></tr>
      </thead>
      <tbody>
        <tr v-for="d in items" :key="d.id">
          <td>
            <span class="rowlink" :title="d.source_ref ?? ''">
              <Database :size="14" /> {{ d.name }}
              <span v-if="d.kind === 'database'" class="tag">{{ $t('datasources.tagDb') }}</span>
              <span v-else-if="d.kind === 'flow'" class="tag">{{ $t('datasources.tagFlow') }}</span>
            </span>
            <div v-if="d.description" class="muted desc">{{ d.description }}</div>
            <div v-if="d.refresh_schedule" class="muted sched">
              <CalendarClock :size="11" /> <code>{{ d.refresh_schedule }}</code>
              <span v-if="d.next_refresh_at"> {{ $t('datasources.nextRefresh', { date: fmtDate(d.next_refresh_at) }) }}</span>
            </div>
          </td>
          <td class="muted"><Folder :size="13" /> {{ folderName[d.project_id] ?? `#${d.project_id}` }}</td>
          <td class="muted nowrap">
            <template v-if="isImporting(d.id)">
              <span class="okline"><LoaderCircle :size="12" class="spin" /> {{ $t('datasources.importingStatus') }}</span>
            </template>
            <template v-else-if="ingestRuns[d.id]?.status === 'FAILURE'">
              <span class="koline" :title="ingestRuns[d.id].error ?? ''">{{ $t('datasources.importFailedStatus') }}</span>
            </template>
            <template v-else>{{ d.rows != null ? d.rows.toLocaleString('it-IT') : '—' }}</template>
          </td>
          <td class="muted nowrap">{{ fmtDate(d.refreshed_at ?? d.updated_at) }}</td>
          <td class="right">
            <button
              v-if="d.kind === 'database'"
              class="mini"
              :class="{ active: !!d.refresh_schedule }"
              :title="$t('datasources.scheduleRefreshTitle')"
              @click="openSchedule(d)"
            ><CalendarClock :size="13" /></button>
            <button
              v-if="d.kind === 'database'"
              class="mini"
              :title="$t('datasources.refreshNowTitle')"
              :disabled="isImporting(d.id)"
              @click="refresh(d)"
            ><RefreshCw :size="13" /></button>
            <button class="mini danger" :title="$t('datasources.deleteTitle')" @click="remove(d)"><Trash2 :size="13" /></button>
          </td>
        </tr>
      </tbody>
    </table>

    <Pager :offset="offset" :page-size="pageSize" :total="total" :loading="loading" @prev="prev" @next="next" />

    <ScheduleDialog
      :open="!!scheduleFor"
      :title="scheduleFor?.name ?? ''"
      :subtitle="$t('datasources.scheduleSubtitle')"
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
