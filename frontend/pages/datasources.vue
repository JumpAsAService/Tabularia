<script setup lang="ts">
// All the datasources in the readable folders: search, refresh (kind=database) with live
// state, deletion.
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useFormat } from '~/composables/useFormat'
import { Database, Search, Trash2, Folder, RefreshCw, LoaderCircle, CalendarClock, BookText, ChevronRight, Save, Sparkles, ScrollText, ListOrdered, TriangleAlert } from 'lucide-vue-next'
import { errMessage } from '~/composables/useApi'
import { knownError } from '~/composables/knownErrors'
import { useDatasources, type DatasourceInfo } from '~/composables/useDatasources'
import { useProjects } from '~/composables/useProjects'
import { usePagedList } from '~/composables/usePagedList'
import type { RunInfo } from '~/composables/useRuns'
import RunProgressText from '~/components/ui/RunProgressText.vue'
import MissingValues from '~/components/ui/MissingValues.vue'
import { runNotes, notesText, type RunNotes } from '~/composables/usePlaceholders'

const dsApi = useDatasources()
const projectsApi = useProjects()
const toast = useToast()
// the data contract of the datasource open in the dialog (null = closed)
const contractFor = ref<DatasourceInfo | null>(null)
const { t, locale } = useI18n()
const fmtx = useFormat()

// server-side search (name/description, over the whole dataset) + pagination
const { q, items, total, offset, pageSize, loading, error, load, next, prev } =
  usePagedList<DatasourceInfo>((p) => dsApi.listPaged(p), { initialQuery: String(useRoute().query.q ?? '') })

// Coming from the global search (?ds=<id>): the row opens and comes into view, once.
const target = Number(useRoute().query.ds) || null
watch(items, (list) => {
  const d = target != null ? list.find((x) => x.id === target) : undefined
  if (!d || expanded.value === d.id) return
  toggle(d)
  nextTick(() => document.getElementById(`ds-${d.id}`)?.scrollIntoView({ block: 'center' }))
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

// ── Per-datasource expander: the FIELDS' descriptions editable inline ──────
// As the Flows page: click on the row → the detail opens with one row per schema column
// (name, type, free description) and the Save button.
const expanded = ref<number | null>(null)
const drafts = reactive<Record<number, Record<string, string>>>({})
// the DATASOURCE's description (what it holds, one line = what, period): curated here
// together with the fields', and saved with the same button
const aboutDrafts = reactive<Record<number, string>>({})
const MAX_ABOUT = 4000
const savingDescriptions = ref<number | null>(null)
const describedCount = (d: DatasourceInfo) => Object.keys(d.column_descriptions ?? {}).length

// Sort keys, saved with the same button. Database imports only: a flow sets them
// on its Output node (every publish rewrites them), SharePoint keeps file order.
// Picked from the known columns, in the order ticked, so a typo cannot break every
// refresh; typed by hand only while no import has succeeded yet.
const keyDrafts = reactive<Record<number, string[]>>({})
const keyText = reactive<Record<number, string>>({})
const canEditKeys = (d: DatasourceInfo) => d.kind === 'database' && !['sharepoint', 'sftp', 's3', 'api'].includes(d.source_type ?? '')
const draftKeys = (d: DatasourceInfo): string[] =>
  d.columns.length
    ? keyDrafts[d.id] ?? []
    : [...new Set((keyText[d.id] ?? '').split(',').map((k) => k.trim()).filter(Boolean))]
const keysDirty = (d: DatasourceInfo) =>
  canEditKeys(d) && JSON.stringify(draftKeys(d)) !== JSON.stringify(d.sort_keys ?? [])
// the columns, plus any key that is no longer one (renamed at the source): it has
// to stay visible to be unticked, or a broken refresh could never be repaired
function keyOptions(d: DatasourceInfo): { name: string; missing: boolean }[] {
  const names = d.columns.map((c) => c.name)
  const stale = [...new Set([...(d.sort_keys ?? []), ...(keyDrafts[d.id] ?? [])])].filter((k) => !names.includes(k))
  return [...names.map((name) => ({ name, missing: false })), ...stale.map((name) => ({ name, missing: true }))]
}
function toggleKey(d: DatasourceInfo, name: string) {
  const keys = keyDrafts[d.id] ?? []
  keyDrafts[d.id] = keys.includes(name) ? keys.filter((k) => k !== name) : [...keys, name]
}

function toggle(d: DatasourceInfo) {
  if (expanded.value === d.id) {
    expanded.value = null
    return
  }
  expanded.value = d.id
  if (!drafts[d.id]) resetDraft(d)
  if (d.kind === 'database') loadLastImport(d.id)
}

// The last finished import of an open datasource: a refresh that skipped values of
// a range (their file was not there) says which, here, until the next one.
const lastImport = ref<Record<number, RunInfo | null>>({})
async function loadLastImport(dsId: number) {
  try {
    const runs = await dsApi.listRuns(dsId)
    lastImport.value = { ...lastImport.value, [dsId]: runs.find(isTerminal) ?? null }
  } catch { /* accessory: the row works without it */ }
}
function notesOf(d: DatasourceInfo): RunNotes | null {
  const live = ingestRuns.value[d.id]
  const run = live && isTerminal(live) ? live : lastImport.value[d.id]
  return run?.status === 'SUCCESS' ? runNotes(run.notes) : null
}
function resetDraft(d: DatasourceInfo) {
  drafts[d.id] = Object.fromEntries(d.columns.map((c) => [c.name, d.column_descriptions?.[c.name] ?? '']))
  aboutDrafts[d.id] = d.description ?? ''
  keyDrafts[d.id] = [...(d.sort_keys ?? [])]
  keyText[d.id] = (d.sort_keys ?? []).join(', ')
}
const draftDescribed = (d: DatasourceInfo) => Object.values(drafts[d.id] ?? {}).filter((v) => v.trim()).length
const aboutDirty = (d: DatasourceInfo) => (aboutDrafts[d.id] ?? '').trim() !== (d.description ?? '').trim()
const isDirty = (d: DatasourceInfo) =>
  aboutDirty(d) ||
  keysDirty(d) ||
  (!!drafts[d.id] && d.columns.some((c) => (drafts[d.id][c.name] ?? '').trim() !== (d.column_descriptions?.[c.name] ?? '')))

async function saveDescriptions(d: DatasourceInfo) {
  const draft = drafts[d.id]
  if (!draft) return
  // keeps the descriptions of columns no longer in the schema (they come back if they reappear)
  const stale = Object.fromEntries(
    Object.entries(d.column_descriptions ?? {}).filter(([k]) => !d.columns.some((c) => c.name === k)),
  )
  const keysChanged = keysDirty(d)
  savingDescriptions.value = d.id
  try {
    const updated = await dsApi.update(d.id, {
      description: (aboutDrafts[d.id] ?? '').trim(),
      column_descriptions: { ...stale, ...draft },
      ...(keysChanged ? { sort_keys: draftKeys(d) } : {}),
    })
    items.value = items.value.map((x) => (x.id === updated.id ? { ...x, ...updated } : x))
    resetDraft(updated)
    toast.success(t('datasources.docsSavedToast', { n: describedCount(updated) }))
    if (keysChanged) toast.success(t('sortKeys.savedToast'))
  } catch (e) {
    toast.error(errMessage(e))
  } finally {
    savingDescriptions.value = null
  }
}

function fmtDate(iso: string | null): string {
  return iso ? fmtx.dateTime(iso) : ''
}

// ── Refresh with live state (the same pattern as ProjectBrowser) ──────────────
const ingestRuns = ref<Record<number, RunInfo>>({})
// Announcements for the screen readers, once per import: start (from the click) and end.
// The ticking progress is not announced: it would be one voice per second.
const announcement = ref('')
const watched = new Set<number>() // imports seen in progress: their end is announced
function announceEnd(dsId: number, run: RunInfo) {
  if (!watched.delete(dsId)) return
  const name = items.value.find((x) => x.id === dsId)?.name ?? `#${dsId}`
  announcement.value = run.status === 'SUCCESS'
    ? t('datasources.announceDone', { name, n: fmtx.count(run.rows_written ?? 0) })
    : t('datasources.announceFailed', { name })
  // the same words on screen: the end of an import watched here gets a message
  if (run.status === 'SUCCESS') {
    toast.success(announcement.value)
    const notes = runNotes(run.notes)
    if (notes) toast.warning(t('placeholders.notesToast', { name, notes: notesText(notes, t) }))
  } else toast.error(run.error ? `${announcement.value}: ${knownError(run.error, t)}` : announcement.value)
}
let pollToken = 0
onUnmounted(() => { pollToken++ })

// SharePoint's source_ref is JSON {path, sheet}, SFTP's {folder, pattern, …}: in the
// tooltip it is shown readable
function sourceLabel(d: DatasourceInfo): string {
  if (d.source_type === 'api') {
    try { const r = JSON.parse(d.source_ref ?? '{}'); return `${r.method ?? 'GET'} ${r.path ?? ''}${r.mode === 'append' ? ' · append' : ''}` } catch { return d.source_ref ?? '' }
  }
  if (d.source_type === 'sftp') {
    try { const r = JSON.parse(d.source_ref ?? '{}'); return [r.folder, r.pattern].filter(Boolean).join('/') } catch { return d.source_ref ?? '' }
  }
  if (d.source_type === 's3') {
    try { const r = JSON.parse(d.source_ref ?? '{}'); return `s3://${[r.bucket || '…', r.folder, r.pattern].filter(Boolean).join('/')}` } catch { return d.source_ref ?? '' }
  }
  if (d.source_type !== 'sharepoint') return d.source_ref ?? ''
  try { const r = JSON.parse(d.source_ref ?? '{}'); return `${r.path} · ${r.sheet}` } catch { return d.source_ref ?? '' }
}
const isTerminal = (r: RunInfo) => r.status === 'SUCCESS' || r.status === 'FAILURE'
const isImporting = (id: number) => !!ingestRuns.value[id] && !isTerminal(ingestRuns.value[id])

// `restored`: poll reattached from the server's flag, not from a click. When the last run
// is terminal already the page does not reload: the reload would restart the watch →
// another poll → another reload, without end.
async function pollIngest(dsId: number, token: number, restored = false) {
  try {
    const runs = await dsApi.listRuns(dsId)
    if (token !== pollToken) return
    const last = runs[0]
    if (!last) { forgetPlaceholder(dsId); return }
    ingestRuns.value = { ...ingestRuns.value, [dsId]: last }
    if (!isTerminal(last)) {
      watched.add(dsId)
      setTimeout(() => { if (token === pollToken) pollIngest(dsId, token) }, 2500)
    } else {
      announceEnd(dsId, last)
      // new snapshot: rows, columns and dates update in place, the open row stays open
      if (last.status === 'SUCCESS' && !restored) await load({ quiet: true })
    }
  } catch {
    // the placeholder the watch put must not stay hanging: otherwise a spinner forever and
    // the refresh button disabled
    forgetPlaceholder(dsId)
  }
}
function forgetPlaceholder(dsId: number) {
  if (ingestRuns.value[dsId]?.id == null) {
    const { [dsId]: _gone, ...rest } = ingestRuns.value
    ingestRuns.value = rest
  }
}

// The "importing" state cannot live only here: a five-minute refresh survives a page
// change, and the scheduled ones do not start from a click. The server marks
// `refreshing`; at every load the polling is reattached.
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
    watched.add(d.id)
    announcement.value = t('datasources.announceStarted', { name: d.name })
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
    await load() // reloads the page (updates total/window)
  } catch (e) {
    toast.error(errMessage(e))
  }
}

// ── Scheduled refresh (cron) — the shared ScheduleDialog ─────────────────────
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
    toast.error(errMessage(e)) // 422 invalid cron, 403 permissions
  } finally {
    savingSchedule.value = false
  }
}
</script>

<template>
  <AppShell>
    <p class="sr-only" role="status">{{ announcement }}</p>
    <div class="page-head">
      <h1><Database :size="18" /> {{ $t('datasources.title') }} <span class="muted count">{{ total }}</span></h1>
      <div class="head-actions">
        <span class="searchbox"><Search :size="14" /><input v-model="q" type="text" :placeholder="$t('datasources.searchPlaceholder')" :aria-label="$t('datasources.searchPlaceholder')" /></span>
      </div>
    </div>

    <p v-if="error" class="err">{{ error }}</p>
    <SkeletonRows v-else-if="loading" :rows="4" />
    <p v-else-if="!items.length" class="muted">
      {{ q ? $t('datasources.noSearchResults') : $t('datasources.emptyState') }}
    </p>

    <div v-else class="dsl">
      <div v-for="d in items" :id="`ds-${d.id}`" :key="d.id" class="ds" :class="{ open: expanded === d.id }">
        <div class="ds-row">
          <button class="ds-head" @click="toggle(d)">
            <ChevronRight :size="14" class="chev" :class="{ rot: expanded === d.id }" />
            <span class="dcell">
            <span class="dname" :title="sourceLabel(d)">
              <Database :size="14" /> {{ d.name }}
              <span v-if="d.kind === 'database'" class="tag">{{ $t('datasources.tagDb') }}</span>
              <span v-else-if="d.kind === 'flow'" class="tag">{{ $t('datasources.tagFlow') }}</span>
              <ContractBadge :contract="d.contract" :size="15" />
              <!-- documented in full: the assistant can query it without guessing. The same symbol as
                   the Assistant item, so the link reads without explanations. -->
              <span v-if="d.ai_ready" class="ready-badge" :title="$t('datasources.aiReadyHint')">
                <Sparkles :size="11" /> {{ $t('datasources.aiReady') }}
              </span>
              <span v-else-if="describedCount(d)" class="desc-badge" :title="$t('columnDescriptions.heading')">
                <BookText :size="11" /> {{ $t('columnDescriptions.counter', { n: describedCount(d), total: d.columns.length }) }}
              </span>
            </span>
            <!-- where the import stands: under the name, where there is room in every language and
                 which stays visible on the phone too -->
            <span v-if="isImporting(d.id)" class="dprog">
              <RunProgressText :run="ingestRuns[d.id]" :sr-label="$t('datasources.importingStatus')" />
            </span>
            </span>
            <span class="folder muted"><Folder :size="12" /> {{ folderName[d.project_id] ?? `#${d.project_id}` }}</span>
            <span class="rows muted">
              <template v-if="isImporting(d.id)">
                <span class="okline" :title="$t('datasources.importingStatus')"><LoaderCircle :size="12" class="spin" /></span>
              </template>
              <template v-else-if="ingestRuns[d.id]?.status === 'FAILURE'">
                <span class="koline" :title="knownError(ingestRuns[d.id].error, t)">{{ $t('datasources.importFailedStatus') }}</span>
              </template>
              <template v-else>
                <span
                  v-if="notesOf(d)"
                  class="noteline"
                  :title="notesText(notesOf(d)!, t)"
                  :aria-label="notesText(notesOf(d)!, t)"
                ><TriangleAlert :size="12" /></span>
                {{ fmtx.count(d.rows) }}
              </template>
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
            <button class="mini" :title="$t('contracts.open')" :aria-label="$t('contracts.open')" @click="contractFor = d"><ScrollText :size="13" /></button>
            <button class="mini danger" :title="$t('datasources.deleteTitle')" @click="remove(d)"><Trash2 :size="13" /></button>
          </div>
        </div>

        <div v-if="expanded === d.id" class="ds-detail">
          <div v-if="d.refresh_schedule" class="muted sched">
            <CalendarClock :size="11" /> <code>{{ d.refresh_schedule }}</code>
            <span v-if="d.next_refresh_at"> {{ $t('datasources.nextRefresh', { date: fmtDate(d.next_refresh_at) }) }}</span>
          </div>

          <div v-if="notesOf(d)" class="ds-notes" role="note">
            <p class="muted hint">{{ $t('placeholders.lastImportNotes') }}</p>
            <p v-if="notesOf(d)!.duplicate_keys" class="hint">{{ $t('placeholders.repeatedKeys', { n: notesOf(d)!.duplicate_keys }) }}</p>
            <MissingValues v-if="notesOf(d)!.missing_count" :missing="notesOf(d)!.missing" :count="notesOf(d)!.missing_count" :expansions="notesOf(d)!.expansions" />
          </div>

          <div class="section-title"><BookText :size="12" /> {{ $t('datasourceAbout.heading') }}</div>
          <p class="muted hint">{{ $t('datasourceAbout.intro') }}</p>
          <textarea
            v-model="aboutDrafts[d.id]"
            class="about"
            rows="3"
            :maxlength="MAX_ABOUT"
            :placeholder="$t('datasourceAbout.placeholder')"
            :aria-label="$t('datasourceAbout.heading')"
          />

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
                  :aria-label="`${$t('columnDescriptions.colDescription')}: ${c.name}`"
                  @keyup.enter="saveDescriptions(d)"
                />
              </div>
            </div>
          </template>
          <template v-if="canEditKeys(d)">
            <div class="section-title"><ListOrdered :size="12" /> {{ $t('sortKeys.heading') }}</div>
            <p class="muted hint">{{ d.columns.length ? $t('sortKeys.intro') : $t('sortKeys.introNoColumns') }}</p>
            <div v-if="d.columns.length" class="sk-checks">
              <label v-for="o in keyOptions(d)" :key="o.name" class="sk-chk" :class="{ 'sk-missing': o.missing }">
                <input type="checkbox" :checked="draftKeys(d).includes(o.name)" @change="toggleKey(d, o.name)" />
                <code :title="o.name">{{ o.name }}</code>
                <span v-if="draftKeys(d).includes(o.name)" class="sk-pos">{{ draftKeys(d).indexOf(o.name) + 1 }}</span>
                <span v-if="o.missing" class="sk-miss">{{ $t('sortKeys.missing') }}</span>
              </label>
            </div>
            <input
              v-else
              v-model="keyText[d.id]"
              type="text"
              class="sk-text"
              :placeholder="$t('sortKeys.placeholder')"
              :aria-label="$t('sortKeys.heading')"
            />
            <p class="muted hint sk-now">
              {{ draftKeys(d).length ? $t('sortKeys.current', { keys: draftKeys(d).join(', ') }) : $t('sortKeys.none') }}
            </p>
          </template>
          <template v-else-if="d.kind === 'flow' && d.sort_keys?.length">
            <div class="section-title"><ListOrdered :size="12" /> {{ $t('sortKeys.heading') }}</div>
            <p class="muted hint">{{ $t('sortKeys.fromFlow', { keys: d.sort_keys.join(', ') }) }}</p>
          </template>

          <div class="cd-actions">
            <span v-if="d.columns.length" class="muted">{{ $t('columnDescriptions.counter', { n: draftDescribed(d), total: d.columns.length }) }}</span>
            <span class="spacer" />
            <button v-if="isDirty(d)" :disabled="savingDescriptions === d.id" @click="resetDraft(d)">{{ $t('columnDescriptions.cancel') }}</button>
            <button class="primary" :disabled="!isDirty(d) || savingDescriptions === d.id" @click="saveDescriptions(d)">
              <LoaderCircle v-if="savingDescriptions === d.id" :size="13" class="spin" /><Save v-else :size="13" /> {{ $t('columnDescriptions.save') }}
            </button>
          </div>
        </div>
      </div>
    </div>

    <Pager :offset="offset" :page-size="pageSize" :total="total" :loading="loading" @prev="prev" @next="next" />

    <ContractDialog :open="contractFor !== null" :datasource="contractFor" @close="contractFor = null" @changed="load()" />
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
.dcell { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.dprog { font-size: 12px; color: var(--accent-2); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; font-variant-numeric: tabular-nums; }
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
.noteline { color: var(--warning); display: inline-flex; align-items: center; vertical-align: -2px; margin-right: 3px; }
.ds-notes { display: flex; flex-direction: column; gap: 4px; margin: 4px 0 8px; padding: 8px 10px; border: 1px solid color-mix(in srgb, var(--warning) 40%, transparent); border-radius: 8px; }
.cd-table { border: 1px solid var(--border); border-radius: 8px; background: var(--panel); max-height: 420px; overflow-y: auto; }
.cd-row { display: grid; grid-template-columns: minmax(140px, 220px) 100px 1fr; gap: 10px; align-items: center; padding: 6px 10px; border-top: 1px solid var(--border-soft); }
.cd-row:first-child { border-top: 0; }
.cd-headrow { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; position: sticky; top: 0; background: var(--panel); }
.ready-badge {
  display: inline-flex; align-items: center; gap: 4px; margin-left: 8px;
  font-size: 10px; text-transform: uppercase; letter-spacing: 0.04em;
  padding: 1px 7px; border-radius: 8px;
  background: var(--tint-accent); border: 1px solid var(--accent); color: var(--accent-hi);
}
.cd-name { font-family: ui-monospace, monospace; font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.cd-type { font-size: 11px; }
.cd-row input { width: 100%; font-size: 12.5px; }
.cd-actions { display: flex; align-items: center; gap: 8px; font-size: 12px; }
.about { width: 100%; min-height: 64px; max-height: 240px; padding: 8px 10px; font: inherit; font-size: 13px; line-height: 1.45; resize: vertical; }
.cd-actions .primary { display: inline-flex; align-items: center; gap: 5px; }
.spacer { flex: 1; }
@media (max-width: 720px) {
  .ds-head { grid-template-columns: 18px 1fr; }
  .folder, .rows, .when { display: none; }
  .cd-row { grid-template-columns: 1fr; gap: 4px; }
  .cd-headrow { display: none; }
}
.sched { font-size: 11px; display: flex; align-items: center; gap: 4px; margin-top: 2px; }
.sk-checks {
  display: flex; flex-wrap: wrap; gap: 4px 14px; max-height: 180px; overflow-y: auto;
  padding: 8px 10px; border: 1px solid var(--border); border-radius: 8px; background: var(--panel);
}
.sk-chk { display: inline-flex; align-items: center; gap: 6px; font-size: 12.5px; max-width: 100%; cursor: pointer; }
.sk-chk code { font-family: ui-monospace, monospace; font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sk-pos {
  min-width: 16px; padding: 0 4px; border-radius: 8px; font-size: 10.5px; text-align: center;
  font-variant-numeric: tabular-nums; background: var(--tint-accent); color: var(--accent-hi);
}
.sk-missing code { text-decoration: line-through; }
.sk-miss { font-size: 11px; color: var(--danger); }
.sk-text { width: 100%; font-family: ui-monospace, monospace; font-size: 12.5px; }
.sk-now { font-variant-numeric: tabular-nums; }
.sched code { font-family: ui-monospace, monospace; }
.mini.active { color: var(--accent-2); border-color: var(--accent-2); }
</style>
