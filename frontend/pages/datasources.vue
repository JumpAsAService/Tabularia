<script setup lang="ts">
// Tutte le datasource nelle cartelle leggibili: ricerca, refresh (kind=database)
// con stato live, eliminazione.
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { Database, Search, Trash2, Folder, RefreshCw, LoaderCircle, CalendarClock, BookText, ChevronRight, Save } from 'lucide-vue-next'
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

// ── Expander per-datasource: descrizioni dei CAMPI modificabili inline ──────
// Come la pagina Flows: click sulla riga → si apre il dettaglio con una riga per
// colonna dello schema (nome, tipo, descrizione libera) e il bottone Salva.
const expanded = ref<number | null>(null)
const drafts = reactive<Record<number, Record<string, string>>>({})
const savingDescriptions = ref<number | null>(null)
const describedCount = (d: DatasourceInfo) => Object.keys(d.column_descriptions ?? {}).length

function toggle(d: DatasourceInfo) {
  if (expanded.value === d.id) {
    expanded.value = null
    return
  }
  expanded.value = d.id
  if (!drafts[d.id]) resetDraft(d)
}
function resetDraft(d: DatasourceInfo) {
  drafts[d.id] = Object.fromEntries(d.columns.map((c) => [c.name, d.column_descriptions?.[c.name] ?? '']))
}
const draftDescribed = (d: DatasourceInfo) => Object.values(drafts[d.id] ?? {}).filter((v) => v.trim()).length
const isDirty = (d: DatasourceInfo) =>
  !!drafts[d.id] && d.columns.some((c) => (drafts[d.id][c.name] ?? '').trim() !== (d.column_descriptions?.[c.name] ?? ''))

async function saveDescriptions(d: DatasourceInfo) {
  const draft = drafts[d.id]
  if (!draft) return
  // conserva le descrizioni di colonne non più nello schema (tornano se ricompaiono)
  const stale = Object.fromEntries(
    Object.entries(d.column_descriptions ?? {}).filter(([k]) => !d.columns.some((c) => c.name === k)),
  )
  savingDescriptions.value = d.id
  try {
    const updated = await dsApi.update(d.id, { column_descriptions: { ...stale, ...draft } })
    items.value = items.value.map((x) => (x.id === updated.id ? { ...x, ...updated } : x))
    resetDraft(updated)
    toast.success(t('datasources.descriptionsSavedToast', { n: describedCount(updated) }))
  } catch (e) {
    toast.error(errMessage(e))
  } finally {
    savingDescriptions.value = null
  }
}

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

// il source_ref di SharePoint e' JSON {path, sheet}: in tooltip si mostra leggibile
function sourceLabel(d: DatasourceInfo): string {
  if (d.source_type !== 'sharepoint') return d.source_ref ?? ''
  try { const r = JSON.parse(d.source_ref ?? '{}'); return `${r.path} · ${r.sheet}` } catch { return d.source_ref ?? '' }
}
const isTerminal = (r: RunInfo) => r.status === 'SUCCESS' || r.status === 'FAILURE'
const isImporting = (id: number) => !!ingestRuns.value[id] && !isTerminal(ingestRuns.value[id])

// `restored`: poll riagganciato dal flag del server, non da un click. Se l'ultimo
// run e' gia' terminale non si ricarica la pagina: la ricarica farebbe ripartire
// il watch → altro poll → altra ricarica, senza fine.
async function pollIngest(dsId: number, token: number, restored = false) {
  try {
    const runs = await dsApi.listRuns(dsId)
    if (token !== pollToken) return
    const last = runs[0]
    if (!last) { forgetPlaceholder(dsId); return }
    ingestRuns.value = { ...ingestRuns.value, [dsId]: last }
    if (!isTerminal(last)) {
      setTimeout(() => { if (token === pollToken) pollIngest(dsId, token) }, 2500)
    } else if (last.status === 'SUCCESS' && !restored) {
      await load() // snapshot aggiornato: ricarica la pagina
    }
  } catch {
    // il segnaposto messo dal watch non deve restare appeso: altrimenti spinner
    // per sempre e bottone di refresh disabilitato
    forgetPlaceholder(dsId)
  }
}
function forgetPlaceholder(dsId: number) {
  if (ingestRuns.value[dsId]?.id == null) {
    const { [dsId]: _gone, ...rest } = ingestRuns.value
    ingestRuns.value = rest
  }
}

// Lo stato "sta importando" non può vivere solo qui: un refresh da cinque minuti
// sopravvive a un cambio di pagina, e quelli schedulati non partono da un click.
// Il server segna `refreshing`; a ogni caricamento si riaggancia il polling.
watch(items, (list) => {
  for (const d of list) {
    if (d.refreshing && !isImporting(d.id)) {
      ingestRuns.value = { ...ingestRuns.value, [d.id]: { status: 'STARTED' } as RunInfo }
      pollIngest(d.id, pollToken, true)
    }
  }
}, { immediate: true })

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
      <h1><Database :size="18" /> {{ $t('datasources.title') }} <span class="muted count">{{ total }}</span></h1>
      <div class="head-actions">
        <span class="searchbox"><Search :size="14" /><input v-model="q" type="text" :placeholder="$t('datasources.searchPlaceholder')" /></span>
      </div>
    </div>

    <p v-if="error" class="err">{{ error }}</p>
    <SkeletonRows v-else-if="loading" :rows="4" />
    <p v-else-if="!items.length" class="muted">
      {{ q ? $t('datasources.noSearchResults') : $t('datasources.emptyState') }}
    </p>

    <div v-else class="dsl">
      <div v-for="d in items" :key="d.id" class="ds" :class="{ open: expanded === d.id }">
        <div class="ds-row">
          <button class="ds-head" @click="toggle(d)">
            <ChevronRight :size="14" class="chev" :class="{ rot: expanded === d.id }" />
            <span class="dname" :title="sourceLabel(d)">
              <Database :size="14" /> {{ d.name }}
              <span v-if="d.kind === 'database'" class="tag">{{ $t('datasources.tagDb') }}</span>
              <span v-else-if="d.kind === 'flow'" class="tag">{{ $t('datasources.tagFlow') }}</span>
              <span v-if="describedCount(d)" class="desc-badge" :title="$t('columnDescriptions.heading')">
                <BookText :size="11" /> {{ $t('columnDescriptions.counter', { n: describedCount(d), total: d.columns.length }) }}
              </span>
            </span>
            <span class="folder muted"><Folder :size="12" /> {{ folderName[d.project_id] ?? `#${d.project_id}` }}</span>
            <span class="rows muted">
              <template v-if="isImporting(d.id)">
                <span class="okline"><LoaderCircle :size="12" class="spin" /> {{ $t('datasources.importingStatus') }}</span>
              </template>
              <template v-else-if="ingestRuns[d.id]?.status === 'FAILURE'">
                <span class="koline" :title="ingestRuns[d.id].error ?? ''">{{ $t('datasources.importFailedStatus') }}</span>
              </template>
              <template v-else>{{ d.rows != null ? d.rows.toLocaleString('it-IT') : '—' }}</template>
            </span>
            <span class="when muted">{{ fmtDate(d.refreshed_at ?? d.updated_at) }}</span>
          </button>
          <div class="ds-actions">
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
          </div>
        </div>

        <div v-if="expanded === d.id" class="ds-detail">
          <div v-if="d.description" class="muted desc">{{ d.description }}</div>
          <div v-if="d.refresh_schedule" class="muted sched">
            <CalendarClock :size="11" /> <code>{{ d.refresh_schedule }}</code>
            <span v-if="d.next_refresh_at"> {{ $t('datasources.nextRefresh', { date: fmtDate(d.next_refresh_at) }) }}</span>
          </div>

          <div class="section-title"><BookText :size="12" /> {{ $t('columnDescriptions.heading') }}</div>
          <p class="muted hint">{{ $t('columnDescriptions.intro') }} <strong>{{ d.name }}</strong>. {{ $t('columnDescriptions.introTail') }}</p>
          <p v-if="!d.columns.length" class="muted">{{ $t('columnDescriptions.noColumns') }}</p>
          <template v-else>
            <div class="cd-table">
              <div class="cd-row cd-headrow">
                <span>{{ $t('columnDescriptions.colName') }}</span>
                <span>{{ $t('columnDescriptions.colType') }}</span>
                <span>{{ $t('columnDescriptions.colDescription') }}</span>
              </div>
              <div v-for="c in d.columns" :key="c.name" class="cd-row">
                <code class="cd-name" :title="c.name">{{ c.name }}</code>
                <span class="muted cd-type">{{ c.dtype }}</span>
                <input
                  v-model="drafts[d.id][c.name]"
                  type="text"
                  maxlength="2000"
                  :placeholder="$t('columnDescriptions.placeholder')"
                  @keyup.enter="saveDescriptions(d)"
                />
              </div>
            </div>
            <div class="cd-actions">
              <span class="muted">{{ $t('columnDescriptions.counter', { n: draftDescribed(d), total: d.columns.length }) }}</span>
              <span class="spacer" />
              <button v-if="isDirty(d)" :disabled="savingDescriptions === d.id" @click="resetDraft(d)">{{ $t('columnDescriptions.cancel') }}</button>
              <button class="primary" :disabled="!isDirty(d) || savingDescriptions === d.id" @click="saveDescriptions(d)">
                <LoaderCircle v-if="savingDescriptions === d.id" :size="13" class="spin" /><Save v-else :size="13" /> {{ $t('columnDescriptions.save') }}
              </button>
            </div>
          </template>
        </div>
      </div>
    </div>

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
.dsl { display: flex; flex-direction: column; gap: 6px; margin-top: 14px; }
.ds { border: 1px solid var(--border-soft); border-radius: 8px; background: var(--panel); overflow: hidden; }
.ds.open { border-color: var(--border); }
.ds-row { display: flex; align-items: center; }
.ds-head { flex: 1; display: grid; grid-template-columns: 18px minmax(200px, 1.6fr) minmax(90px, 1fr) 110px 120px; align-items: center; gap: 10px; padding: 10px 12px; background: transparent; border: none; text-align: left; cursor: pointer; color: inherit; font: inherit; }
.ds-head:hover { background: var(--panel-2); }
.chev { color: var(--muted); transition: transform 0.15s; flex: none; }
.chev.rot { transform: rotate(90deg); }
.dname { display: inline-flex; align-items: center; gap: 7px; font-weight: 550; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.desc-badge { display: inline-flex; align-items: center; gap: 4px; font-size: 11px; color: var(--accent-2); font-weight: 500; }
.folder { display: inline-flex; align-items: center; gap: 5px; font-size: 12.5px; white-space: nowrap; }
.rows { text-align: right; font-variant-numeric: tabular-nums; font-size: 12.5px; white-space: nowrap; }
.when { text-align: right; font-size: 12.5px; white-space: nowrap; }
.ds-actions { display: flex; align-items: center; gap: 4px; padding-right: 10px; }
.ds-detail { border-top: 1px solid var(--border-soft); padding: 12px; background: var(--panel-2); display: flex; flex-direction: column; gap: 6px; }
.desc { font-size: 12.5px; }
.section-title { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); margin: 8px 0 0; }
.hint { font-size: 12px; margin: 0; }
.cd-table { border: 1px solid var(--border); border-radius: 8px; background: var(--panel); max-height: 420px; overflow-y: auto; }
.cd-row { display: grid; grid-template-columns: minmax(140px, 220px) 100px 1fr; gap: 10px; align-items: center; padding: 6px 10px; border-top: 1px solid var(--border-soft); }
.cd-row:first-child { border-top: 0; }
.cd-headrow { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; position: sticky; top: 0; background: var(--panel); }
.cd-name { font-family: ui-monospace, monospace; font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.cd-type { font-size: 11px; }
.cd-row input { width: 100%; font-size: 12.5px; }
.cd-actions { display: flex; align-items: center; gap: 8px; font-size: 12px; }
.cd-actions .primary { display: inline-flex; align-items: center; gap: 5px; }
.spacer { flex: 1; }
@media (max-width: 720px) {
  .ds-head { grid-template-columns: 18px 1fr; }
  .folder, .rows, .when { display: none; }
  .cd-row { grid-template-columns: 1fr; gap: 4px; }
  .cd-headrow { display: none; }
}
.sched { font-size: 11px; display: flex; align-items: center; gap: 4px; margin-top: 2px; }
.sched code { font-family: ui-monospace, monospace; }
.mini.active { color: var(--accent-2); border-color: var(--accent-2); }
</style>
