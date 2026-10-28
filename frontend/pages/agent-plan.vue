<script setup lang="ts">
// A plan of the agent (docs/specs/mcp-agent.md), for its owner to review and confirm in
// Tabularia: what an agent proposes — over MCP, or in the chat — in the user's language,
// with the differences of a changed flow and the first rows it would write. Nothing has
// changed until «Confirm»; the gateway applies the actions in order, checking the
// permissions again, and stops at the first error. Afterwards the page links what was made.
//
// /agent-plan?id=N
import { computed, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import cronstrue from 'cronstrue/i18n'
import { Bot, CalendarClock, CircleCheck, CircleSlash, CircleX, ExternalLink, LoaderCircle, Play, Workflow } from 'lucide-vue-next'
import { errMessage } from '~/composables/useApi'
import { useFormat } from '~/composables/useFormat'
import { useToast } from '~/composables/useToast'
import { useAgentPlans, type AgentPlan, type AgentPlanAction, type AgentPlanOutput, type AgentPlanResult } from '~/composables/useAgentPlans'

const { t, locale } = useI18n()
const route = useRoute()
const api = useAgentPlans()
const fmt = useFormat()
const toast = useToast()

const planId = computed(() => Number(route.query.id) || null)
const plan = ref<AgentPlan | null>(null)
const loading = ref(true)
const loadError = ref('')
const busy = ref<'confirm' | 'cancel' | null>(null)
// read out by screen readers when the plan's state changes
const announcement = ref('')

useHead({ title: computed(() => (planId.value ? t('agentPlan.title', { id: planId.value }) : 'Tabularia')) })

const CRON_LOCALES: Record<string, string> = { en: 'en', it: 'it', fr: 'fr', de: 'de', es: 'es' }

async function load() {
  const id = planId.value
  loadError.value = ''
  if (id == null) {
    loading.value = false
    loadError.value = t('agentPlan.noId')
    return
  }
  loading.value = true
  try {
    plan.value = await api.get(id)
  } catch (e: any) {
    plan.value = null
    loadError.value = (e?.response?.status ?? e?.statusCode) === 404 ? t('agentPlan.notFound') : errMessage(e)
  } finally {
    loading.value = false
  }
}

onMounted(load)
watch(planId, load)

const statusLabel = computed(() => (plan.value ? t(`agentPlan.status.${plan.value.status}`) : ''))
const statusHelp = computed(() => {
  const s = plan.value?.status
  return s && s !== 'pending' && s !== 'applying' ? t(`agentPlan.statusHelp.${s}`) : ''
})
const origin = computed(() => {
  const p = plan.value
  if (!p) return ''
  if (p.origin === 'chat') return t('agentPlan.fromChat')
  return p.token_name ? t('agentPlan.fromMcp', { token: p.token_name }) : t('agentPlan.fromMcpNoToken')
})

async function act(kind: 'confirm' | 'cancel') {
  const id = planId.value
  if (id == null || busy.value) return
  busy.value = kind
  try {
    plan.value = kind === 'confirm' ? await api.confirm(id) : await api.cancel(id)
    const s = plan.value.status
    if (s === 'applied') toast.success(t('agentPlan.confirmedToast'))
    else if (s === 'failed') toast.error(t('agentPlan.failedToast'))
    else if (s === 'cancelled') toast.info(t('agentPlan.cancelledToast'))
    announcement.value = `${statusLabel.value}. ${statusHelp.value}`
  } catch (e) {
    toast.error(errMessage(e))
    await load()
  } finally {
    busy.value = null
  }
}

function actionTitle(a: AgentPlanAction): string {
  const name = a.name ?? ''
  switch (a.kind) {
    case 'save_flow': return t(a.flow_id ? 'agentPlan.kind.save_flow_change' : 'agentPlan.kind.save_flow_new', { name })
    case 'run_flow': return t('agentPlan.kind.run_flow', { name })
    case 'schedule_flow': return t(a.cron ? 'agentPlan.kind.schedule_flow' : 'agentPlan.kind.schedule_flow_off', { name })
    default: return a.words
  }
}

function actionIcon(a: AgentPlanAction) {
  return a.kind === 'run_flow' ? Play : a.kind === 'schedule_flow' ? CalendarClock : Workflow
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

// a line of a flow's difference: its sign, said in words too (not by colour alone)
function lineKind(line: string): 'added' | 'removed' | 'changed' | '' {
  return line.startsWith('+') ? 'added' : line.startsWith('-') ? 'removed' : line.startsWith('~') ? 'changed' : ''
}

function resultOf(a: AgentPlanAction): AgentPlanResult | null {
  return plan.value?.result?.[a.n - 1] ?? null
}

function cell(v: unknown): string {
  if (v === null || v === undefined) return '—'
  return typeof v === 'object' ? JSON.stringify(v) : String(v)
}
</script>

<template>
  <AppShell>
    <div class="plan-page">
      <div class="page-head">
        <h1><Bot :size="18" aria-hidden="true" /> {{ planId ? $t('agentPlan.title', { id: planId }) : $t('agentPlan.titleBare') }}</h1>
        <span v-if="plan" class="state" :class="plan.status">{{ statusLabel }}</span>
      </div>
      <p class="sr-only" role="status" aria-live="polite">{{ announcement }}</p>

      <SkeletonRows v-if="loading && !plan" :rows="4" />
      <p v-else-if="loadError" class="err" role="alert">{{ loadError }}</p>

      <template v-else-if="plan">
        <p class="origin">{{ origin }}</p>
        <p v-if="plan.status === 'pending'" class="lead">
          {{ $t('agentPlan.nothingYet') }}
          <span class="muted">{{ $t('agentPlan.expiresAt', { time: fmt.dateTime(plan.expires_at) }) }}</span>
        </p>
        <p v-else-if="statusHelp" class="lead" :class="{ bad: plan.status === 'failed' }">{{ statusHelp }}</p>

        <h2 class="section">{{ $t('agentPlan.actionsTitle') }}</h2>
        <ol class="actions">
          <li v-for="a in plan.actions" :key="a.n" class="action">
            <div class="action-head">
              <span class="n" aria-hidden="true">{{ a.n }}</span>
              <component :is="actionIcon(a)" :size="15" class="kind-icon" aria-hidden="true" />
              <h3>{{ actionTitle(a) }}</h3>
            </div>

            <div class="action-body">
              <p v-if="a.folder && a.kind === 'save_flow'" class="meta">
                {{ $t('agentPlan.inFolder', { folder: a.folder }) }}<span v-if="a.engine"> · {{ $t('agentPlan.engine', { engine: a.engine }) }}</span>
              </p>
              <p v-if="a.kind === 'run_flow'" class="meta">
                {{ a.mode === 'production' ? $t('agentPlan.modeProduction') : $t('agentPlan.modeDevelopment') }}
              </p>
              <template v-if="a.kind === 'schedule_flow' && a.cron">
                <p class="meta">{{ $t('agentPlan.cron', { text: cronText(a.cron) }) }}</p>
                <p class="meta muted"><code>{{ $t('agentPlan.cronCode', { cron: a.cron, tz: a.timezone ?? 'UTC' }) }}</code></p>
              </template>
              <p v-if="a.kind === 'schedule_flow' && a.notify_emails" class="meta">{{ $t('agentPlan.notify', { emails: a.notify_emails }) }}</p>

              <div v-if="a.outputs?.length" class="block">
                <h4>{{ $t('agentPlan.writes') }}</h4>
                <ul class="plain">
                  <li v-for="o in a.outputs" :key="o.node">{{ outputText(o) }}</li>
                </ul>
              </div>

              <div v-if="a.diff_lines?.length" class="block">
                <h4>{{ $t('agentPlan.changes') }} <span class="muted small">{{ $t('agentPlan.changesHelp') }}</span></h4>
                <ul class="code">
                  <li v-for="(line, i) in a.diff_lines" :key="i" :class="lineKind(line)">
                    <span class="sr-only">{{ lineKind(line) ? $t(`agentPlan.line.${lineKind(line)}`) : '' }}</span>{{ line }}
                  </li>
                </ul>
              </div>
              <div v-else-if="a.node_lines?.length" class="block">
                <h4>{{ $t('agentPlan.steps') }}</h4>
                <ul class="code">
                  <li v-for="(line, i) in a.node_lines" :key="i">{{ line }}</li>
                </ul>
              </div>

              <div v-if="a.preview" class="block">
                <h4>{{ $t('agentPlan.firstRows') }}</h4>
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

        <div v-if="plan.can_confirm" class="buttons">
          <button class="primary" :disabled="!!busy" :aria-busy="busy === 'confirm'" @click="act('confirm')">
            <LoaderCircle v-if="busy === 'confirm'" :size="14" class="spin" aria-hidden="true" />
            {{ busy === 'confirm' ? $t('agentPlan.confirming') : $t('agentPlan.confirm') }}
          </button>
          <button :disabled="!!busy" :aria-busy="busy === 'cancel'" @click="act('cancel')">
            {{ busy === 'cancel' ? $t('agentPlan.cancelling') : $t('agentPlan.cancel') }}
          </button>
        </div>
      </template>
    </div>
  </AppShell>
</template>

<style scoped>
.plan-page { max-width: 880px; min-width: 0; }
.page-head { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.page-head h1 { display: inline-flex; align-items: center; gap: 8px; font-size: 1.5em; margin-block: 0.83em; }
.state { font-size: 12px; font-weight: 600; padding: 3px 9px; border-radius: 20px; background: var(--panel-2); color: var(--muted); border: 1px solid var(--border-soft); }
.state.pending, .state.applying { background: var(--tint-accent); color: color-mix(in srgb, var(--accent) 70%, var(--text)); }
.state.applied { background: color-mix(in srgb, var(--accent-2) 15%, transparent); color: color-mix(in srgb, var(--accent-2) 70%, var(--text)); }
.state.failed { background: color-mix(in srgb, var(--danger) 15%, transparent); color: color-mix(in srgb, var(--danger) 70%, var(--text)); }
.origin { margin: 0 0 6px; color: var(--text); }
.lead { margin: 0 0 4px; color: var(--muted); line-height: 1.5; }
.lead.bad { color: color-mix(in srgb, var(--danger) 70%, var(--text)); }
.lead .muted { display: block; margin-top: 2px; font-size: 13px; }
.err { color: color-mix(in srgb, var(--danger) 70%, var(--text)); }
.section { font-size: 15px; margin: 22px 0 10px; }

.actions { list-style: none; padding: 0; margin: 0; display: flex; flex-direction: column; gap: 10px; }
.action { border: 1px solid var(--border-soft); border-radius: var(--radius); background: var(--panel); padding: 12px 14px; min-width: 0; }
.action-head { display: flex; align-items: center; gap: 8px; min-width: 0; }
.action-head h3 { margin: 0; font-size: 14.5px; font-weight: 600; overflow-wrap: anywhere; }
.n { flex: none; width: 22px; height: 22px; border-radius: 50%; display: inline-flex; align-items: center; justify-content: center; font-size: 12px; font-weight: 600; background: var(--panel-2); border: 1px solid var(--border); }
.kind-icon { flex: none; color: var(--accent); }
.action-body { margin-left: 30px; min-width: 0; }
.meta { margin: 6px 0 0; font-size: 13.5px; overflow-wrap: anywhere; }
.meta code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12.5px; }
.block { margin-top: 10px; min-width: 0; }
.block h4 { margin: 0 0 4px; font-size: 12.5px; font-weight: 600; color: var(--muted); text-transform: none; }
.plain { margin: 0; padding-left: 18px; font-size: 13.5px; }
.plain li { overflow-wrap: anywhere; }
.code { list-style: none; margin: 0; padding: 8px 10px; background: var(--bg-soft); border: 1px solid var(--border-soft); border-radius: 6px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; line-height: 1.55; }
.code li { white-space: pre-wrap; overflow-wrap: anywhere; }
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

.buttons { display: flex; gap: 10px; flex-wrap: wrap; margin: 18px 0 8px; }

@media (max-width: 600px) {
  .action-body { margin-left: 0; }
  .buttons button { flex: 1 1 auto; }
}
</style>
