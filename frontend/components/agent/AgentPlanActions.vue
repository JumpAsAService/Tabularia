<script setup lang="ts">
// The actions of an agent's plan (docs/specs/mcp-agent.md), in the user's language: what
// each one does — flows, datasources from existing connections, SQL scripts, flow groups,
// notebooks — where, when and to whom, the differences of what changes, the first rows
// it would write and, once applied, how each one went with a link to what it made. The
// plan's page (/agent-plan) and the chat's plan card show the same.
import { computed, type Component } from 'vue'
import { useI18n } from 'vue-i18n'
import cronstrue from 'cronstrue/i18n'
import {
  CalendarClock, CircleCheck, CircleSlash, CircleX, Database, DatabaseSearch, ExternalLink, NotebookPen, Play, RefreshCw,
  Rows3, Workflow,
} from 'lucide-vue-next'
import type { AgentPlan, AgentPlanAction, AgentPlanOutput, AgentPlanResult } from '~/composables/useAgentPlans'

const props = defineProps<{ plan: AgentPlan; compact?: boolean }>()
const { t, locale } = useI18n()

// the headings: under the page's h2, or under the chat card's h3
const actionHeading = computed(() => (props.compact ? 'h4' : 'h3'))
const blockHeading = computed(() => (props.compact ? 'h5' : 'h4'))

const CRON_LOCALES: Record<string, string> = { en: 'en', it: 'it', fr: 'fr', de: 'de', es: 'es' }

// the kinds of action that turn a schedule on or off, and those that work in a folder
const SCHEDULES = new Set(['schedule_flow', 'schedule_datasource', 'schedule_sql_script', 'schedule_flow_group'])
const IN_FOLDER = new Set(['save_flow', 'create_datasource', 'refresh_datasource', 'save_sql_script', 'save_flow_group',
  'save_notebook'])

function actionTitle(a: AgentPlanAction): string {
  const p = { name: a.name ?? '' }
  const off = 'agentPlan.kind.schedule_flow_off'
  switch (a.kind) {
    case 'save_flow': return t(a.flow_id ? 'agentPlan.kind.save_flow_change' : 'agentPlan.kind.save_flow_new', p)
    case 'run_flow': return t('agentPlan.kind.run_flow', p)
    case 'schedule_flow': return t(a.cron ? 'agentPlan.kind.schedule_flow' : off, p)
    case 'create_datasource': return t('agentPlan.kind.create_datasource', p)
    case 'refresh_datasource': return t('agentPlan.kind.refresh_datasource', p)
    case 'schedule_datasource': return t(a.cron ? 'agentPlan.kind.schedule_datasource' : 'agentPlan.kind.schedule_datasource_off', p)
    case 'save_sql_script': return t(a.id ? 'agentPlan.kind.save_sql_script_change' : 'agentPlan.kind.save_sql_script_new', p)
    case 'run_sql_script': return t('agentPlan.kind.run_sql_script', p)
    case 'schedule_sql_script': return t(a.cron ? 'agentPlan.kind.schedule_sql_script' : off, p)
    case 'save_flow_group': return t(a.id ? 'agentPlan.kind.save_flow_group_change' : 'agentPlan.kind.save_flow_group_new', p)
    case 'run_flow_group': return t('agentPlan.kind.run_flow_group', p)
    case 'schedule_flow_group': return t(a.cron ? 'agentPlan.kind.schedule_flow_group' : off, p)
    case 'save_notebook': return t(a.id ? 'agentPlan.kind.save_notebook_change' : 'agentPlan.kind.save_notebook_new', p)
    default: return a.words
  }
}

const KIND_ICONS: Record<string, Component> = {
  run_flow: Play, run_sql_script: Play, run_flow_group: Play,
  schedule_flow: CalendarClock, schedule_datasource: CalendarClock, schedule_sql_script: CalendarClock,
  schedule_flow_group: CalendarClock,
  create_datasource: Database, refresh_datasource: RefreshCw, save_sql_script: DatabaseSearch,
  save_flow_group: Rows3, save_notebook: NotebookPen,
}

function actionIcon(a: AgentPlanAction): Component {
  return KIND_ICONS[a.kind] ?? Workflow
}

// what a new datasource imports, in words (a SQL query and an API call are shown as code instead)
function sourceText(a: AgentPlanAction): string {
  const text = a.source_text ?? ''
  switch (a.source_type) {
    case 'table': return t('agentPlan.source.table', { text })
    case 'sql': return t('agentPlan.source.sql')
    default: return t('agentPlan.source.files', { text })
  }
}
// a source shown whole, as code: the query, or the API call with its headers and body
const AS_CODE = new Set(['sql', 'api'])
function sourceLabel(a: AgentPlanAction): string {
  return a.source_type === 'api' ? t('agentPlan.source.apiCall') : t('agentPlan.sqlCode')
}

function runsOn(a: AgentPlanAction): string {
  return a.connection
    ? t('agentPlan.runsOnDatabase', { connection: a.connection })
    : t('agentPlan.runsOnCatalog', { engine: a.engine || t('agentPlan.defaultEngine') })
}

// a notebook's app runs a new version, with the rights of whoever saves it, when its source changes
function newNotebookVersion(a: AgentPlanAction): boolean {
  return !a.id || (a.diff_lines ?? []).some((line) => /^[+-]/.test(line))
}

function codeLabel(a: AgentPlanAction): string {
  return a.kind === 'save_notebook' ? t('agentPlan.notebookSource') : t('agentPlan.sqlCode')
}

function cronText(cron: string): string {
  try {
    return cronstrue.toString(cron, { locale: CRON_LOCALES[locale.value] ?? 'en', verbose: false, use24HourTimeFormat: true })
  } catch {
    return cron
  }
}

function outputText(o: AgentPlanOutput): string {
  const p = { target: o.target, folder: o.folder ?? '', connection: o.connection ?? '', format: o.format ?? '', subject: o.subject ?? '' }
  switch (o.kind) {
    case 'datasource': return t(o.overwrite ? 'agentPlan.output.datasourceOverwrite' : 'agentPlan.output.datasource', p)
    case 'database': return t('agentPlan.output.database', { ...p, mode: t(o.mode === 'replace' ? 'agentPlan.modeReplace' : 'agentPlan.modeAppend') })
    case 's3': return t('agentPlan.output.s3', p)
    case 'sftp': return t('agentPlan.output.sftp', p)
    case 'email': return t('agentPlan.output.email', p)
    default: return o.words
  }
}

// a line of a difference: its sign, said in words too (not by colour alone)
function lineKind(line: string): 'added' | 'removed' | 'changed' | '' {
  return line.startsWith('+') ? 'added' : line.startsWith('-') ? 'removed' : line.startsWith('~') ? 'changed' : ''
}

function resultOf(a: AgentPlanAction): AgentPlanResult | null {
  return props.plan.result?.[a.n - 1] ?? null
}

function cell(v: unknown): string {
  if (v === null || v === undefined) return '—'
  return typeof v === 'object' ? JSON.stringify(v) : String(v)
}
</script>

<template>
  <ol class="actions" :class="{ compact }">
    <li v-for="a in plan.actions" :key="a.n" class="action">
      <div class="action-head">
        <span class="n" aria-hidden="true">{{ a.n }}</span>
        <component :is="actionIcon(a)" :size="15" class="kind-icon" aria-hidden="true" />
        <component :is="actionHeading" class="action-title">{{ actionTitle(a) }}</component>
      </div>

      <div class="action-body">
        <p v-if="a.folder && IN_FOLDER.has(a.kind)" class="meta">
          {{ $t('agentPlan.inFolder', { folder: a.folder }) }}<span v-if="a.engine && a.kind === 'save_flow'"> · {{ $t('agentPlan.engine', { engine: a.engine }) }}</span><span v-if="a.environment && a.kind === 'save_notebook'"> · {{ $t('agentPlan.environment', { environment: a.environment }) }}</span>
        </p>
        <p v-if="a.kind === 'run_flow'" class="meta">
          {{ a.mode === 'production' ? $t('agentPlan.modeProduction') : $t('agentPlan.modeDevelopment') }}
        </p>
        <template v-if="a.kind === 'create_datasource'">
          <p class="meta">{{ $t('agentPlan.fromConnection', { connection: a.connection ?? '', type: a.connection_type ?? '' }) }}</p>
          <div class="block">
            <component :is="blockHeading">{{ $t('agentPlan.imports') }}</component>
            <pre v-if="AS_CODE.has(a.source_type ?? '')" class="code-block" tabindex="0" :aria-label="sourceLabel(a)">{{ a.source_text }}</pre>
            <p v-else class="meta tight">{{ sourceText(a) }}</p>
          </div>
          <p class="meta muted">{{ $t('agentPlan.firstImport') }}</p>
        </template>
        <p v-if="a.kind === 'refresh_datasource' && a.connection" class="meta">
          {{ $t('agentPlan.throughConnection', { connection: a.connection }) }}
        </p>
        <p v-if="a.kind === 'save_sql_script'" class="meta">{{ runsOn(a) }}</p>
        <p v-if="a.output && (a.kind === 'save_sql_script' || a.kind === 'run_sql_script')" class="meta">
          {{ $t('agentPlan.writesDatasource', { name: a.output.name, folder: a.output.folder }) }}
        </p>
        <p v-if="a.kind === 'save_notebook' && newNotebookVersion(a)" class="meta muted">{{ $t('agentPlan.notebookRights') }}</p>
        <template v-if="SCHEDULES.has(a.kind) && a.cron">
          <p class="meta">{{ $t('agentPlan.cron', { text: cronText(a.cron) }) }}</p>
          <p class="meta muted"><code>{{ $t('agentPlan.cronCode', { cron: a.cron, tz: a.timezone ?? 'UTC' }) }}</code></p>
        </template>
        <p v-if="a.kind === 'schedule_flow' && a.notify_emails" class="meta">{{ $t('agentPlan.notify', { emails: a.notify_emails }) }}</p>

        <div v-if="a.outputs?.length" class="block">
          <component :is="blockHeading">{{ $t('agentPlan.writes') }}</component>
          <ul class="plain">
            <li v-for="o in a.outputs" :key="o.node">
              {{ outputText(o) }}
              <template v-if="o.kind === 'email' && o.cc">. {{ $t('agentPlan.output.cc', { cc: o.cc }) }}</template>
              <template v-if="o.post_sql">
                <span class="sub">{{ $t('agentPlan.output.postSql') }}</span>
                <pre class="code-block" tabindex="0" :aria-label="$t('agentPlan.output.postSql')">{{ o.post_sql }}</pre>
              </template>
              <template v-if="o.body">
                <span class="sub">{{ $t('agentPlan.output.body') }}</span>
                <pre class="code-block wrap" tabindex="0" :aria-label="$t('agentPlan.output.body')">{{ o.body }}</pre>
              </template>
            </li>
          </ul>
        </div>

        <div v-if="a.diff_lines?.length" class="block">
          <component :is="blockHeading">{{ $t('agentPlan.changes') }} <span class="muted small">{{ $t('agentPlan.changesHelp') }}</span></component>
          <ul class="code">
            <li v-for="(line, i) in a.diff_lines" :key="i" :class="lineKind(line)">
              <span class="sr-only">{{ lineKind(line) ? $t(`agentPlan.line.${lineKind(line)}`) : '' }}</span>{{ line }}
            </li>
          </ul>
        </div>
        <div v-else-if="a.node_lines?.length" class="block">
          <component :is="blockHeading">{{ $t('agentPlan.steps') }}</component>
          <ul class="code">
            <li v-for="(line, i) in a.node_lines" :key="i">{{ line }}</li>
          </ul>
        </div>

        <div v-if="a.code" class="block">
          <component :is="blockHeading">{{ codeLabel(a) }}</component>
          <pre class="code-block" tabindex="0" :aria-label="codeLabel(a)">{{ a.code }}</pre>
        </div>

        <div v-if="a.preview" class="block">
          <component :is="blockHeading">{{ $t('agentPlan.firstRows') }}</component>
          <p v-if="a.preview.error" class="muted small">{{ $t('agentPlan.previewError', { error: a.preview.error }) }}</p>
          <div v-else-if="a.preview.columns?.length" class="table-wrap" tabindex="0" :aria-label="$t('agentPlan.firstRows')">
            <table>
              <caption class="sr-only">{{ $t('agentPlan.firstRows') }}</caption>
              <thead>
                <tr><th v-for="c in a.preview.columns" :key="c.name" scope="col">{{ c.name }}</th></tr>
              </thead>
              <tbody>
                <tr v-for="(row, i) in a.preview.rows ?? []" :key="i">
                  <td v-for="c in a.preview.columns" :key="c.name">{{ cell(row[c.name]) }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>

        <p v-if="resultOf(a)" class="result" :class="resultOf(a)!.ok ? 'ok' : resultOf(a)!.skipped ? 'skipped' : 'error'">
          <CircleCheck v-if="resultOf(a)!.ok" :size="14" aria-hidden="true" />
          <CircleSlash v-else-if="resultOf(a)!.skipped" :size="14" aria-hidden="true" />
          <CircleX v-else :size="14" aria-hidden="true" />
          <strong>{{ resultOf(a)!.ok ? $t('agentPlan.result.ok') : resultOf(a)!.skipped ? $t('agentPlan.result.skipped') : $t('agentPlan.result.error') }}</strong>
          <span v-if="!resultOf(a)!.ok && !resultOf(a)!.skipped" class="why">{{ resultOf(a)!.error }}</span>
          <NuxtLink v-if="resultOf(a)!.ok && resultOf(a)!.link" :to="resultOf(a)!.link!" class="open">
            <ExternalLink :size="13" aria-hidden="true" /> {{ $t('agentPlan.result.open') }}
          </NuxtLink>
        </p>
      </div>
    </li>
  </ol>
</template>

<style scoped>
.actions { list-style: none; padding: 0; margin: 0; display: flex; flex-direction: column; gap: 10px; }
.action { border: 1px solid var(--border-soft); border-radius: var(--radius); background: var(--panel); padding: 12px 14px; min-width: 0; }
.action-head { display: flex; align-items: center; gap: 8px; min-width: 0; }
.action-head .action-title { margin: 0; font-size: 14.5px; font-weight: 600; overflow-wrap: anywhere; }
.n { flex: none; width: 22px; height: 22px; border-radius: 50%; display: inline-flex; align-items: center; justify-content: center; font-size: 12px; font-weight: 600; background: var(--panel-2); border: 1px solid var(--border); }
.kind-icon { flex: none; color: var(--accent); }
.action-body { margin-left: 30px; min-width: 0; }
.meta { margin: 6px 0 0; font-size: 13.5px; overflow-wrap: anywhere; }
.meta code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12.5px; }
.block { margin-top: 10px; min-width: 0; }
.block > h4, .block > h5 { margin: 0 0 4px; font-size: 12.5px; font-weight: 600; color: var(--muted); text-transform: none; }
.plain .sub { display: block; margin: 6px 0 3px; font-size: 12.5px; color: var(--muted); }
.plain { margin: 0; padding-left: 18px; font-size: 13.5px; }
.plain li { overflow-wrap: anywhere; }
.code { list-style: none; margin: 0; padding: 8px 10px; background: var(--bg-soft); border: 1px solid var(--border-soft); border-radius: 6px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; line-height: 1.55; }
.code li { white-space: pre-wrap; overflow-wrap: anywhere; }
/* a SQL text or a notebook's source: its lines as written, scrolling inside its own box */
.code-block { margin: 0; padding: 8px 10px; background: var(--bg-soft); border: 1px solid var(--border-soft); border-radius: 6px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; line-height: 1.55;
  white-space: pre; overflow: auto; max-height: 360px; max-width: 100%; }
.code-block:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.meta.tight { margin-top: 0; }
/* a message: its lines wrap, as the email shows them */
.code-block.wrap { white-space: pre-wrap; overflow-wrap: anywhere; }
.code li.added { color: color-mix(in srgb, var(--accent-2) 70%, var(--text)); }
.code li.removed { color: color-mix(in srgb, var(--danger) 70%, var(--text)); }
.code li.changed { color: color-mix(in srgb, var(--warning) 75%, var(--text)); }
.small { font-size: 12px; font-weight: 400; }
/* the table scrolls inside its own box: the page never scrolls sideways */
.table-wrap { max-width: 100%; overflow-x: auto; border: 1px solid var(--border-soft); border-radius: 6px; }
.table-wrap:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
table { border-collapse: collapse; font-size: 12.5px; width: max-content; min-width: 100%; }
th, td { padding: 4px 8px; text-align: left; border-bottom: 1px solid var(--border-soft); white-space: nowrap; max-width: 260px; overflow: hidden; text-overflow: ellipsis; }
th { background: var(--panel-2); font-weight: 600; }
tbody tr:nth-child(even) { background: var(--row-even); }

.result { display: flex; align-items: center; flex-wrap: wrap; gap: 6px; margin: 10px 0 0; font-size: 13.5px; }
.result.ok { color: color-mix(in srgb, var(--accent-2) 70%, var(--text)); }
.result.error { color: color-mix(in srgb, var(--danger) 70%, var(--text)); }
.result.skipped { color: var(--muted); }
.result .why { color: var(--text); overflow-wrap: anywhere; min-width: 0; }
.result .open { display: inline-flex; align-items: center; gap: 4px; color: var(--accent); }

/* in the chat's card: denser, the actions one under the other without a box each */
.compact { gap: 8px; }
.compact .action { padding: 9px 11px; background: var(--bg-soft); }
.compact .action-head .action-title { font-size: 13.5px; }
.compact .meta, .compact .plain, .compact .result { font-size: 12.5px; }
.compact .code { font-size: 11.5px; max-height: 220px; overflow-y: auto; }
.compact .code-block { font-size: 11.5px; max-height: 220px; }
.compact th, .compact td { font-size: 12px; }

@media (max-width: 600px) {
  .action-body { margin-left: 0; }
}
</style>
