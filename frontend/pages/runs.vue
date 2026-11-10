<script setup lang="ts">
// Runs: the global search of the runs in the readable projects, to understand why the
// flows fail. Filter by state + text search on the reason (server-side, over the whole
// dataset) + pagination; click on a row for the whole traceback, and from there the way to
// the flow, datasource or script to repair. Failures that follow one another for the same
// reason on the same object are one row (composables/runGroups.ts), on the page already
// loaded: nothing more is asked of the server.
import { ref, watch, computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { knownError } from '~/composables/knownErrors'
import { useFormat } from '~/composables/useFormat'
import { History, Search, RefreshCw, ChevronRight, Workflow, Database, DatabaseSearch, CalendarClock, User, Rows3, TriangleAlert, FileCode2, Container, Mail, ArrowUpRight } from 'lucide-vue-next'
import { useRuns, type RunInfo } from '~/composables/useRuns'
import { runNotes, notesText } from '~/composables/usePlaceholders'
import MissingValues from '~/components/ui/MissingValues.vue'
import ScriptRunDetail from '~/components/scripting/ScriptRunDetail.vue'
import ContainerRunDetail from '~/components/containers/ContainerRunDetail.vue'
import { usePagedList } from '~/composables/usePagedList'
import { groupRuns, runOpens } from '~/composables/runGroups'

const { t, te } = useI18n()
const fmtx = useFormat()
const runsApi = useRuns()
const status = ref<'FAILURE' | 'SUCCESS' | ''>('FAILURE') // the errors by default
const expanded = ref<number | null>(null)

const { q, items, total, offset, pageSize, loading, error, load, next, prev } = usePagedList<RunInfo>(
  (p) => runsApi.search({ ...p, status: status.value || undefined }),
)
// the state is an extra filter: on change it goes back to the first page and reloads
watch(status, () => {
  offset.value = 0
  load()
})

// the run whose detail is open; a group is open while it is one of its runs
const groups = computed(() => groupRuns(items.value))
const shown = (g: { runs: RunInfo[] }) => g.runs.find((r) => r.id === expanded.value) ?? null

function toggle(g: { head: RunInfo; runs: RunInfo[] }) {
  expanded.value = shown(g) ? null : g.head.id
}

function fmtDate(s: string | null): string {
  return fmtx.dateTime(s)
}

// who started the run: when scheduled we show "schedule", otherwise the name
// the Script or Container node's name, from what its run recorded
function scriptLabel(r: RunInfo): string {
  if (r.kind === 'pushmail') {
    try {
      return JSON.parse(r.email ?? '{}').cell || t('pushmail.kind')
    } catch {
      return t('pushmail.kind')
    }
  }
  const fallback = r.kind === 'container' ? t('containers.node.title') : t('scripting.node.title')
  try {
    return JSON.parse(r.script ?? '{}').label || fallback
  } catch {
    return fallback
  }
}

function isScheduled(r: RunInfo): boolean {
  return r.trigger_type === 'schedule'
}
function triggeredBy(r: RunInfo): string {
  return isScheduled(r) ? t('runs.triggerSchedule') : (r.launched_by_name || '—')
}

const STATUSES = computed<{ value: 'FAILURE' | 'SUCCESS' | ''; label: string }[]>(() => [
  { value: 'FAILURE', label: t('runs.statusFailed') },
  { value: 'SUCCESS', label: t('runs.statusSuccess') },
  { value: '', label: t('runs.statusAll') },
])
</script>

<template>
  <AppShell>
    <div class="page-head">
      <h1><History :size="18" /> {{ $t('runs.title') }} <span class="muted count">{{ total }}</span></h1>
      <div class="head-actions">
        <div class="segmented">
          <button
            v-for="s in STATUSES"
            :key="s.value || 'all'"
            :class="{ on: status === s.value }"
            :aria-pressed="status === s.value"
            @click="status = s.value"
          >{{ s.label }}</button>
        </div>
        <span class="searchbox">
          <Search :size="14" />
          <input v-model="q" type="text" :placeholder="$t('runs.searchPlaceholder')" :aria-label="$t('runs.searchPlaceholder')" />
        </span>
        <button class="mini" :title="$t('runs.refresh')" :aria-label="$t('runs.refresh')" @click="load()"><RefreshCw :size="14" /></button>
      </div>
    </div>

    <p v-if="error" class="err">{{ error }}</p>
    <SkeletonRows v-else-if="loading" :rows="5" />
    <p v-else-if="!items.length" class="muted">
      {{ status === 'FAILURE'
        ? (q ? $t('runs.noRunsFailedSearch') : $t('runs.noRunsFailed'))
        : (q ? $t('runs.noRunsSearch') : $t('runs.noRunsAny')) }}
    </p>

    <div v-else class="runs">
      <div v-for="g in groups" :key="g.head.id" class="run" :class="{ open: !!shown(g) }">
        <button class="run-row" :aria-expanded="!!shown(g)" @click="toggle(g)">
          <ChevronRight :size="14" class="chev" :class="{ rot: !!shown(g) }" />
          <span class="stpill" :class="g.head.status.toLowerCase()">{{ te(`runs.pill_${g.head.status}`) ? t(`runs.pill_${g.head.status}`) : g.head.status }}</span>
          <span class="who">
            <component :is="g.head.kind === 'ingest' ? Database : g.head.kind === 'sql' ? DatabaseSearch : g.head.kind === 'script' ? FileCode2 : g.head.kind === 'container' ? Container : g.head.kind === 'pushmail' ? Mail : g.head.flow_group_id ? Rows3 : Workflow" :size="13" />
            {{ g.head.flow_name || g.head.sql_script_name || g.head.source_name || g.head.group_name || (g.head.kind === 'ingest' ? $t('runs.refreshDatasource') : $t('runs.runNumber', { id: g.head.id })) }}
            <span v-if="g.head.kind === 'script' || g.head.kind === 'container' || g.head.kind === 'pushmail'" class="muted">· {{ scriptLabel(g.head) }}</span>
          </span>
          <span v-if="g.head.status === 'SUCCESS' && runNotes(g.head.notes)" class="msg run-note">
            <TriangleAlert :size="12" /> {{ notesText(runNotes(g.head.notes)!, t) }}
          </span>
          <span v-else class="msg muted">
            <!-- the same failure, several times in a row: once, with how many -->
            <span v-if="g.runs.length > 1" class="times" :title="$t('runs.repeatedTitle', { n: g.runs.length })">{{ $t('runs.repeated', { n: g.runs.length }) }}<span class="sr-only"> {{ $t('runs.repeatedTitle', { n: g.runs.length }) }}</span></span>
            {{ knownError(g.head.error, t) || (g.head.status === 'SUCCESS' ? '—' : '') }}
          </span>
          <span class="by muted" :class="{ sched: isScheduled(g.head) }" :title="isScheduled(g.head) ? $t('runs.triggeredByScheduler') : $t('runs.triggeredByName', { name: triggeredBy(g.head) })">
            <component :is="isScheduled(g.head) ? CalendarClock : User" :size="12" />
            {{ triggeredBy(g.head) }}
          </span>
          <span class="when muted">{{ g.runs.length > 1 ? $t('runs.last', { when: fmtDate(g.head.started_at) }) : fmtDate(g.head.started_at) }}</span>
        </button>
        <Transition name="unfold">
        <div v-if="shown(g)" class="detail-fold">
        <div v-for="r in [shown(g)!]" :key="g.head.id" class="detail">
          <!-- the runs of the group: which one the detail below is of -->
          <div v-if="g.runs.length > 1" class="same" role="group" :aria-label="$t('runs.sameFailure', { n: g.runs.length })">
            <span class="muted">{{ $t('runs.sameFailure', { n: g.runs.length }) }}</span>
            <button
              v-for="m in g.runs" :key="m.id" type="button" class="same-run" :class="{ on: m.id === r.id }"
              :aria-pressed="m.id === r.id" :title="$t('runs.runNumber', { id: m.id })" @click="expanded = m.id"
            >{{ fmtDate(m.started_at) }}</button>
          </div>
          <div class="detail-head">
            <div v-if="r.error" class="detail-summary">{{ knownError(r.error, t) }}</div>
            <!-- the object to repair, when the row names one that still exists -->
            <NuxtLink v-if="runOpens(r)" :to="runOpens(r)!.to" class="open-it">
              {{ $t(`runs.open_${runOpens(r)!.what}`) }} <ArrowUpRight :size="13" aria-hidden="true" />
            </NuxtLink>
          </div>
          <p v-if="r.status === 'SUCCESS' && runNotes(r.notes)?.duplicate_keys" class="run-note small">
            {{ $t('placeholders.repeatedKeys', { n: runNotes(r.notes)!.duplicate_keys }) }}
          </p>
          <MissingValues
            v-if="r.status === 'SUCCESS' && runNotes(r.notes)?.missing_count"
            class="run-notes"
            :missing="runNotes(r.notes)!.missing"
            :count="runNotes(r.notes)!.missing_count"
            :expansions="runNotes(r.notes)!.expansions"
            :max="50"
          />
          <ScriptRunDetail v-if="r.kind === 'script' && r.script" :script="r.script" />
          <ContainerRunDetail v-else-if="r.kind === 'container' && r.script" :info="r.script" />
          <pre v-else-if="r.error_detail" class="trace">{{ r.error_detail }}</pre>
          <p v-else-if="!(r.status === 'SUCCESS' && runNotes(r.notes))" class="muted small">{{ $t('runs.noDetail') }}</p>
        </div>
        </div>
        </Transition>
      </div>
    </div>

    <Pager :offset="offset" :page-size="pageSize" :total="total" :loading="loading" @prev="prev" @next="next" />
  </AppShell>
</template>

<style scoped>
.page-head { display: flex; align-items: center; justify-content: space-between; gap: 16px; flex-wrap: wrap; }
/* explicit font-size/margin: the title was an <h2> and took the browser's defaults
   (1.5em, margin 0.83em). Promoted to <h1> for the navigation by headings, it fixes them
   here to render exactly as before. */
.page-head h1 { display: inline-flex; align-items: center; gap: 8px; font-size: 1.5em; margin-block: 0.83em; }
.count { font-weight: 400; font-size: 14px; }
.head-actions { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.searchbox { display: inline-flex; align-items: center; gap: 6px; border: 1px solid var(--border); border-radius: 7px; padding: 5px 9px; background: var(--panel-2); }
.searchbox input { border: none; background: transparent; outline: none; color: var(--text); width: 220px; }
.segmented { display: inline-flex; border: 1px solid var(--border); border-radius: 7px; overflow: hidden; }
.segmented button { padding: 5px 11px; background: var(--panel-2); color: var(--muted); border: none; border-right: 1px solid var(--border); font-size: 13px; }
.segmented button:last-child { border-right: none; }
/* The filter in force: the accent's tint under `accent-hi`, as every other pressed segment of
   the app (Audit, the share dialog) — 5:1 at least on the five themes. It was --on-accent on
   the plain accent: white on #4f8cff, 3.22:1 at 13px on the dark theme. */
.segmented button.on { background-image: linear-gradient(var(--tint-accent), var(--tint-accent)); color: var(--accent-hi); box-shadow: inset 0 0 0 1px var(--accent); }

.runs { display: flex; flex-direction: column; gap: 6px; margin-top: 14px; }
.run { border: 1px solid var(--border-soft); border-radius: 8px; background: var(--panel); overflow: hidden; }
.run.open { border-color: var(--border); }
.run-row { width: 100%; display: grid; grid-template-columns: 18px 78px minmax(150px, 1fr) minmax(0, 2fr) 120px 130px; align-items: center; gap: 10px; padding: 9px 12px; background: transparent; border: none; text-align: left; cursor: pointer; }
.run-row:hover { background: var(--panel-2); }
.chev { color: var(--muted); transition: transform var(--dur-2); flex: none; }
.chev.rot { transform: rotate(90deg); }
.stpill { font-size: 11px; font-weight: 600; padding: 2px 7px; border-radius: 20px; text-align: center; }
.stpill.failure { background: color-mix(in srgb, var(--danger) 15%, transparent); color: color-mix(in srgb, var(--danger) 70%, var(--text)); }
.stpill.success { background: color-mix(in srgb, var(--accent-2) 15%, transparent); color: color-mix(in srgb, var(--accent-2) 70%, var(--text)); }
.stpill.pending, .stpill.started { background: rgba(148, 163, 184, 0.15); color: var(--muted); }
.who { display: inline-flex; align-items: center; gap: 6px; font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.msg { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13px; }
/* how many times in a row: tabular, before the reason it counts */
.times { display: inline-block; margin-right: 6px; padding: 0 6px; border: 1px solid var(--border); border-radius: 20px; color: var(--text); font-size: 12px; font-weight: 600; font-variant-numeric: tabular-nums; }
.run-note { color: var(--warning); display: inline-flex; align-items: center; gap: 5px; }
.run-note svg { flex: none; }
.by { display: inline-flex; align-items: center; gap: 5px; font-size: 12.5px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.by.sched { color: var(--accent-2); }
.when { text-align: right; font-size: 12.5px; white-space: nowrap; }

.detail { border-top: 1px solid var(--border-soft); padding: 10px 12px 12px; background: var(--panel-2); }
.detail-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 8px 16px; flex-wrap: wrap; margin-bottom: 8px; }
.detail-head:empty { display: none; }
.detail-summary { flex: 1 1 320px; min-width: 0; font-size: 13px; color: var(--text); }
.open-it { flex: none; margin-left: auto; display: inline-flex; align-items: center; gap: 4px; font-size: 13px; font-weight: 500; color: var(--accent-hi); text-decoration: none; }
.open-it:hover { text-decoration: underline; }
.open-it:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 4px; }
.same { display: flex; align-items: center; flex-wrap: wrap; gap: 6px; margin-bottom: 10px; font-size: 12.5px; }
.same-run { padding: 2px 8px; border: 1px solid var(--border); border-radius: 20px; background: var(--panel); color: var(--muted); font-size: 12px; font-variant-numeric: tabular-nums; box-shadow: none; }
.same-run:hover { color: var(--text); border-color: var(--control-border); box-shadow: none; }
.same-run.on { border-color: var(--accent); background: var(--tint-accent); color: var(--accent-hi); }
.trace { margin: 0; padding: 10px; background: var(--panel); border: 1px solid var(--border-soft); border-radius: 6px; font-family: ui-monospace, monospace; font-size: 12px; line-height: 1.5; white-space: pre-wrap; word-break: break-word; max-height: 340px; overflow: auto; }
.small { font-size: 12.5px; }
.mini { padding: 6px 8px; }
/* a phone: the name beside the status, the message, who and when under them (the six
   columns needed ~550 px and the page scrolled sideways) */
@media (max-width: 720px) {
  .run-row { grid-template-columns: 18px auto minmax(0, 1fr); row-gap: 4px; }
  .run-row .msg, .run-row .by, .run-row .when { grid-column: 2 / -1; }
  .searchbox input { width: 140px; }
}
</style>
