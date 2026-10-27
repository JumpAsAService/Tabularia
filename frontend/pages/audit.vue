<script setup lang="ts">
// Audit log (admin only): who signs in, who logs in, which flows/datasources/connections
// are created/changed/run, which data is downloaded, who changes the permissions. At the
// top the active sessions; below the filterable log.
import { ref, computed, watch, onMounted } from 'vue'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { BarChart } from 'echarts/charts'
import { GridComponent, TooltipComponent, LegendComponent } from 'echarts/components'
import VChart from 'vue-echarts'
import {
  ScrollText, Search, RefreshCw, ChevronLeft, ChevronRight, Circle,
  LogIn, LogOut, Workflow, Database, Plug, Download, Shield, CheckCircle2, XCircle, AlertTriangle, KeyRound, SlidersHorizontal, Radar, Rows3, Server, Folder, Code2, DatabaseSearch, Boxes, DatabaseBackup, Sparkles, Megaphone, FileCheck, Mail, Cpu } from 'lucide-vue-next'
import { useApi, type AuditEntry, type ActiveSession, type AccessActivity } from '~/composables/useApi'
import { useI18n } from 'vue-i18n'
import { useFormat } from '~/composables/useFormat'
import { useChartTheme } from '~/composables/useChartTheme'
import { deviceLabel } from '~/composables/sessionDevice'

use([CanvasRenderer, BarChart, GridComponent, TooltipComponent, LegendComponent])

const { t } = useI18n()
const fmtx = useFormat()

const { user } = useAuth()
const router = useRouter()
watch(() => user.value, (u) => { if (u && !u.is_superuser && !u.is_observer) router.replace('/') }, { immediate: true })

const api = useApi()

// ── the log's filters + pagination ────────────────────────────────────────
const q = ref('')
const action = ref('')
const outcome = ref('')
const pageSize = 50
const offset = ref(0)
const items = ref<AuditEntry[]>([])
const total = ref(0)
const loading = ref(false)
const error = ref('')
const actions = ref<string[]>([])

async function load() {
  loading.value = true
  error.value = ''
  try {
    const page = await api.audit({
      q: q.value || undefined, action: action.value || undefined,
      outcome: outcome.value || undefined, limit: pageSize, offset: offset.value,
    })
    items.value = page.items
    total.value = page.total
  } catch (e: any) {
    error.value = e?.message ?? t('audit.errorGeneric')
  } finally {
    loading.value = false
  }
}
watch([q, action, outcome], () => { offset.value = 0; load() })
function next() { if (offset.value + pageSize < total.value) { offset.value += pageSize; load() } }
function prev() { if (offset.value > 0) { offset.value = Math.max(0, offset.value - pageSize); load() } }

// ── active sessions ──────────────────────────────────────────────────────────
// One per browser or script. The administrator closes one (the observer looks): that
// browser leaves at the next request.
const sessions = ref<ActiveSession[]>([])
const sessionsTotal = ref(0) // all the open ones: the list carries only the newest
async function loadSessions() {
  try {
    const page = await api.auditSessions()
    sessions.value = page.items
    sessionsTotal.value = page.total
  } catch { /* ignore */ }
}
const onlineCount = computed(() => sessions.value.filter((s) => s.online).length)
const canRevoke = computed(() => !!user.value?.is_superuser)
const revoking = ref<string | null>(null)
async function revoke(s: ActiveSession) {
  const who = s.full_name || s.email
  if (!confirm(t('audit.revokeConfirm', { who, device: deviceLabel(s.user_agent) ?? t('sessions.unknownDevice') }))) return
  revoking.value = s.id
  try {
    await api.revokeSession(s.id)
    await Promise.all([loadSessions(), load()])
  } catch (e: any) {
    error.value = e?.message ?? t('audit.errorGeneric')
  } finally {
    revoking.value = null
  }
}

// ── sign-ins chart, last 24h ───────────────────────────────────────────────
const access = ref<AccessActivity | null>(null)
async function loadAccess() {
  try { access.value = await api.auditAccessActivity(24) } catch { /* ignore */ }
}
// chrome from the current theme: reading the tokens lives in the shared composable
const { ui } = useChartTheme()

const accessOption = computed(() => {
  const a = access.value
  const c = ui.value
  const labels = a?.buckets.map((b) => b.label) ?? []
  return {
    tooltip: {
      trigger: 'axis', backgroundColor: c.panel, borderColor: c.border,
      textStyle: { color: c.text, fontSize: 12 },
    },
    legend: { data: [t('audit.chartLegendAccess'), t('audit.chartLegendFailed')], textStyle: { color: c.muted, fontSize: 11 }, top: 0, icon: 'circle' },
    grid: { left: 30, right: 10, top: 28, bottom: 22 },
    xAxis: {
      type: 'category', data: labels,
      axisLabel: { color: c.muted, fontSize: 10, interval: 2 },
      axisLine: { lineStyle: { color: c.border } }, axisTick: { show: false },
    },
    yAxis: {
      type: 'value', minInterval: 1,
      axisLabel: { color: c.muted, fontSize: 10 },
      splitLine: { lineStyle: { color: c.border, opacity: 0.4 } },
    },
    series: [
      { name: t('audit.chartLegendAccess'), type: 'bar', stack: 'x', data: a?.buckets.map((b) => b.success) ?? [],
        itemStyle: { color: c.success, borderRadius: [0, 0, 0, 0] }, barMaxWidth: 18 },
      { name: t('audit.chartLegendFailed'), type: 'bar', stack: 'x', data: a?.buckets.map((b) => b.failure) ?? [],
        itemStyle: { color: c.danger, borderRadius: [3, 3, 0, 0] }, barMaxWidth: 18 },
    ],
  }
})

onMounted(async () => {
  try { actions.value = await api.auditActions() } catch { /* ignore */ }
  await Promise.all([load(), loadSessions(), loadAccess()])
})

// ── presentation ─────────────────────────────────────────────────────────────
// The colours here were 21 hex values written by hand that reproduced EXACTLY the dark
// theme's tokens (#6ee7b7 = --accent-2, #ff6b6b = --danger, #4f8cff = --accent, #a78bfa
// = --violet, #8b93a7 = --muted). They were not data encoding: they were the app's
// palette copied over, and on the other three themes they came out wrong — on the light
// one the mint green on a white background is almost invisible. Being a computed, it
// reads the live tokens and follows the theme change.
const ACTION_META = computed<Record<string, { icon: any; color: string; label: string }>>(() => {
  const u = ui.value
  return {
    'auth.login': { icon: LogIn, color: u.success, label: t('audit.loginAction') },
    'auth.login_failed': { icon: XCircle, color: u.danger, label: t('audit.loginFailedAction') },
    'auth.logout': { icon: LogOut, color: u.muted, label: t('audit.logoutAction') },
    'auth.session_revoke': { icon: LogOut, color: u.danger, label: t('audit.sessionRevokeAction') },
    'auth.password_change': { icon: KeyRound, color: u.accent, label: t('audit.passwordChangeAction') },
    'token.create': { icon: KeyRound, color: u.accent, label: t('audit.tokenCreateAction') },
    'token.revoke': { icon: KeyRound, color: u.danger, label: t('audit.tokenRevokeAction') },
    'service_account.create': { icon: Shield, color: u.accent, label: t('audit.serviceAccountCreateAction') },
    'service_account.delete': { icon: Shield, color: u.danger, label: t('audit.serviceAccountDeleteAction') },
    'webhook.create': { icon: Plug, color: u.accent, label: t('audit.webhookCreateAction') },
    'webhook.update': { icon: Plug, color: u.accent, label: t('audit.webhookUpdateAction') },
    'webhook.delete': { icon: Plug, color: u.danger, label: t('audit.webhookDeleteAction') },
    'engine_settings.update': { icon: SlidersHorizontal, color: u.accent, label: t('audit.engineSettingsUpdateAction') },
    'siem.update': { icon: Radar, color: u.accent, label: t('audit.siemUpdateAction') },
    'siem.test': { icon: Radar, color: u.muted, label: t('audit.siemTestAction') },
    'backup.update': { icon: DatabaseBackup, color: u.accent, label: t('audit.backupUpdateAction') },
    'backup.request': { icon: DatabaseBackup, color: u.muted, label: t('audit.backupRequestAction') },
    'backup.run': { icon: DatabaseBackup, color: u.muted, label: t('audit.backupRunAction') },
    'backup.download': { icon: Download, color: u.danger, label: t('audit.backupDownloadAction') },
    'workers.scaling_update': { icon: Server, color: u.accent, label: t('audit.workersScalingUpdateAction') },
    'workers.scaling_reset': { icon: Server, color: u.muted, label: t('audit.workersScalingResetAction') },
    'scripting.storage_update': { icon: Code2, color: u.accent, label: t('audit.scriptingStorageUpdateAction') },
    'scripting.storage_test': { icon: Code2, color: u.muted, label: t('audit.scriptingStorageTestAction') },
    'scripting.environment_create': { icon: Code2, color: u.accent, label: t('audit.scriptingEnvCreateAction') },
    'scripting.environment_update': { icon: Code2, color: u.accent, label: t('audit.scriptingEnvUpdateAction') },
    'scripting.environment_delete': { icon: Code2, color: u.danger, label: t('audit.scriptingEnvDeleteAction') },
    'scripting.version_current': { icon: Code2, color: u.accent, label: t('audit.scriptingVersionCurrentAction') },
    'scripting.variable_set': { icon: Code2, color: u.accent, label: t('audit.scriptingVariableSetAction') },
    'scripting.variable_delete': { icon: Code2, color: u.muted, label: t('audit.scriptingVariableDeleteAction') },
    'registry.create': { icon: Boxes, color: u.accent, label: t('audit.registryCreateAction') },
    'registry.update': { icon: Boxes, color: u.accent, label: t('audit.registryUpdateAction') },
    'registry.delete': { icon: Boxes, color: u.danger, label: t('audit.registryDeleteAction') },
    'registry.test': { icon: Boxes, color: u.muted, label: t('audit.registryTestAction') },
    'registry.cluster_test': { icon: Boxes, color: u.muted, label: t('audit.registryClusterTestAction') },
    'registry.variable_set': { icon: Boxes, color: u.accent, label: t('audit.registryVariableSetAction') },
    'registry.variable_delete': { icon: Boxes, color: u.muted, label: t('audit.registryVariableDeleteAction') },
    'container.trial': { icon: Boxes, color: u.accent, label: t('audit.containerTrialAction') },
    'script.create': { icon: Code2, color: u.accent, label: t('audit.scriptCreateAction') },
    'script.update': { icon: Code2, color: u.accent, label: t('audit.scriptUpdateAction') },
    'script.delete': { icon: Code2, color: u.danger, label: t('audit.scriptDeleteAction') },
    'script.restore': { icon: Code2, color: u.accent, label: t('audit.scriptRestoreAction') },
    'sql_script.create': { icon: DatabaseSearch, color: u.accent, label: t('audit.sqlScriptCreateAction') },
    'sql_script.update': { icon: DatabaseSearch, color: u.accent, label: t('audit.sqlScriptUpdateAction') },
    'sql_script.delete': { icon: DatabaseSearch, color: u.danger, label: t('audit.sqlScriptDeleteAction') },
    'sql_script.restore': { icon: DatabaseSearch, color: u.accent, label: t('audit.sqlScriptRestoreAction') },
    'sql_script.run': { icon: DatabaseSearch, color: u.violet, label: t('audit.sqlScriptRunAction') },
    'sql_script.export': { icon: DatabaseSearch, color: u.violet, label: t('audit.sqlScriptExportAction') },
    'auth.sso_login': { icon: KeyRound, color: u.success, label: t('audit.ssoLoginAction') },
    'auth.sso_login_failed': { icon: KeyRound, color: u.danger, label: t('audit.ssoLoginFailedAction') },
    'auth.first_run_setup': { icon: KeyRound, color: u.success, label: t('audit.firstRunSetupAction') },
    'auth.bootstrap_login': { icon: KeyRound, color: u.muted, label: t('audit.bootstrapLoginAction') },
    'flow.create': { icon: Workflow, color: u.accent, label: t('audit.flowCreateAction') },
    'flow.update': { icon: Workflow, color: u.accent, label: t('audit.flowUpdateAction') },
    'flow.delete': { icon: Workflow, color: u.danger, label: t('audit.flowDeleteAction') },
    'flow.run': { icon: Workflow, color: u.violet, label: t('audit.flowRunAction') },
    'flow.schedule': { icon: Workflow, color: u.accent, label: t('audit.flowScheduleAction') },
    'flow.promote': { icon: Workflow, color: u.accent, label: t('audit.flowPromoteAction') },
    'folder.create': { icon: Folder, color: u.success, label: t('audit.folderCreateAction') },
    'folder.update': { icon: Folder, color: u.accent, label: t('audit.folderUpdateAction') },
    'folder.move': { icon: Folder, color: u.accent, label: t('audit.folderMoveAction') },
    'folder.delete': { icon: Folder, color: u.danger, label: t('audit.folderDeleteAction') },
    'datasource.update': { icon: Database, color: u.accent, label: t('audit.datasourceUpdateAction') },
    'user.delete': { icon: Shield, color: u.danger, label: t('audit.userDeleteAction') },
    'group.create': { icon: Shield, color: u.success, label: t('audit.groupCreateAction') },
    'queue.job_stop': { icon: Server, color: u.danger, label: t('audit.queueJobStopAction') },
    'flow_group.create': { icon: Rows3, color: u.accent, label: t('audit.flowGroupCreateAction') },
    'flow_group.update': { icon: Rows3, color: u.accent, label: t('audit.flowGroupUpdateAction') },
    'flow_group.delete': { icon: Rows3, color: u.danger, label: t('audit.flowGroupDeleteAction') },
    'flow_group.run': { icon: Rows3, color: u.violet, label: t('audit.flowGroupRunAction') },
    'flow_group.schedule': { icon: Rows3, color: u.accent, label: t('audit.flowGroupScheduleAction') },
    'datasource.create': { icon: Database, color: u.success, label: t('audit.datasourceCreateAction') },
    'datasource.refresh': { icon: Database, color: u.violet, label: t('audit.datasourceRefreshAction') },
    'datasource.delete': { icon: Database, color: u.danger, label: t('audit.datasourceDeleteAction') },
    'datasource.schedule': { icon: Database, color: u.accent, label: t('audit.datasourceScheduleAction') },
    'connection.create': { icon: Plug, color: u.success, label: t('audit.connectionCreateAction') },
    'connection.update': { icon: Plug, color: u.accent, label: t('audit.connectionUpdateAction') },
    'connection.delete': { icon: Plug, color: u.danger, label: t('audit.connectionDeleteAction') },
    'connection.host_key_accept': { icon: Plug, color: u.accent, label: t('sftp.auditHostKeyAccept') },
    // amber: the only one that matches no token, it stays fixed
    'export.download': { icon: Download, color: '#fbbf24', label: t('audit.downloadAction') },
    'permission.grant': { icon: Shield, color: u.success, label: t('audit.permissionGrantAction') },
    'permission.revoke': { icon: Shield, color: u.danger, label: t('audit.permissionRevokeAction') },
    'ai.model_enable': { icon: Sparkles, color: u.accent, label: t('audit.aiModelEnableAction') },
    'ai.model_disable': { icon: Sparkles, color: u.muted, label: t('audit.aiModelDisableAction') },
    'ai.query': { icon: Sparkles, color: u.muted, label: t('audit.aiQueryAction') },
    'ai.prompt_update': { icon: Sparkles, color: u.accent, label: t('audit.aiPromptUpdateAction') },
    'ai.prompt_reset': { icon: Sparkles, color: u.muted, label: t('audit.aiPromptResetAction') },
    'banner.create': { icon: Megaphone, color: u.accent, label: t('audit.bannerCreateAction') },
    'banner.delete': { icon: Megaphone, color: u.danger, label: t('audit.bannerDeleteAction') },
    'contract.save': { icon: FileCheck, color: u.accent, label: t('audit.contractSaveAction') },
    'contract.delete': { icon: FileCheck, color: u.danger, label: t('audit.contractDeleteAction') },
    'email.send': { icon: Mail, color: u.violet, label: t('audit.emailSendAction') },
    'engine.allow': { icon: Cpu, color: u.success, label: t('audit.engineAllowAction') },
    'engine.disallow': { icon: Cpu, color: u.danger, label: t('audit.engineDisallowAction') },
    'user.promote': { icon: Shield, color: u.danger, label: t('audit.userPromoteAction') },
    'user.demote': { icon: Shield, color: u.muted, label: t('audit.userDemoteAction') },
    'group.promote': { icon: Shield, color: u.danger, label: t('audit.groupPromoteAction') },
    'group.demote': { icon: Shield, color: u.muted, label: t('audit.groupDemoteAction') },
    'group.admin_join': { icon: Shield, color: u.danger, label: t('audit.groupAdminJoinAction') },
    'group.admin_leave': { icon: Shield, color: u.muted, label: t('audit.groupAdminLeaveAction') },
    'user.observer_grant': { icon: Shield, color: u.accent, label: t('audit.userObserverGrantAction') },
    'user.observer_revoke': { icon: Shield, color: u.muted, label: t('audit.userObserverRevokeAction') },
    'group.observer_grant': { icon: Shield, color: u.accent, label: t('audit.groupObserverGrantAction') },
    'group.observer_revoke': { icon: Shield, color: u.muted, label: t('audit.groupObserverRevokeAction') },
    'privacy.update': { icon: Shield, color: u.accent, label: t('audit.privacyUpdateAction') },
    'saved_view.create': { icon: Database, color: u.success, label: t('audit.savedViewCreateAction') },
    'saved_view.update': { icon: Database, color: u.accent, label: t('audit.savedViewUpdateAction') },
    'saved_view.delete': { icon: Database, color: u.danger, label: t('audit.savedViewDeleteAction') },
    'script.run': { icon: Code2, color: u.violet, label: t('audit.scriptRunAction') },
    'script.schedule': { icon: Code2, color: u.accent, label: t('audit.scriptScheduleAction') },
    'scripting.trial': { icon: Code2, color: u.accent, label: t('audit.scriptingTrialAction') },
    'settings.update': { icon: SlidersHorizontal, color: u.accent, label: t('audit.settingsUpdateAction') },
    'settings.rollout': { icon: SlidersHorizontal, color: u.violet, label: t('audit.settingsRolloutAction') },
    'settings.rollout_cancel': { icon: SlidersHorizontal, color: u.muted, label: t('audit.settingsRolloutCancelAction') },
    'transform.run': { icon: Workflow, color: u.violet, label: t('audit.transformRunAction') },
  }
})
function meta(a: string) {
  return ACTION_META.value[a] ?? { icon: Circle, color: ui.value.muted, label: a }
}
function fmtDate(iso: string) {
  return fmtx.dateTime(iso, { seconds: true }) // in the log the order to the second counts
}
function fmtAgo(iso: string | null) {
  if (!iso) return '—'
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000)
  if (s < 60) return t('audit.timeJustNow')
  if (s < 3600) return t('audit.timeMinAgo', { n: Math.floor(s / 60) })
  if (s < 86400) return t('audit.timeHoursAgo', { n: Math.floor(s / 3600) })
  return t('audit.timeDaysAgo', { n: Math.floor(s / 86400) })
}
// The reasons the gateway writes, in the language of the interface. Some were
// written in Italian (sign-in) and stay so in older lines; unknown ones as they are.
const REASONS: Record<string, string> = {
  'troppi tentativi': 'audit.reasonThrottled',
  throttled: 'audit.reasonThrottled',
  'credenziali errate': 'audit.reasonWrongCredentials',
  'wrong credentials': 'audit.reasonWrongCredentials',
  'utente disattivato': 'audit.reasonDeactivated',
  deactivated: 'audit.reasonDeactivated',
  "password cambiata durante l'accesso": 'audit.reasonPasswordChangedMeanwhile',
  password: 'audit.reasonPassword',
  observer: 'audit.reasonObserver',
  'wrong current password': 'audit.reasonWrongCurrent',
}
function reasonLabel(r: string): string {
  return REASONS[r] ? t(REASONS[r]) : r
}
// compact summary of the detail (whole JSON in the title)
function detailSummary(e: AuditEntry): string {
  const d = e.detail
  if (!d) return ''
  const parts: string[] = []
  if (d.reason) parts.push(reasonLabel(String(d.reason)))
  if (d.format) parts.push(`${d.format}`)
  if (d.filename && !d.format) parts.push(String(d.filename))
  if (d.cron) parts.push(t('audit.cronLabel', { cron: d.cron }))
  if (Array.isArray(d.changed) && d.changed.length) parts.push(t('audit.fieldsLabel', { fields: d.changed.join(', ') }))
  if (d.capability) parts.push(String(d.capability))
  if (d.db_type) parts.push(String(d.db_type))
  if (d.engine) parts.push(t('audit.engineLabel', { engine: d.engine }))
  return parts.join(' · ')
}
</script>

<template>
  <AppShell fluid>
    <div class="audit">
      <div class="page-head">
        <h1><ScrollText :size="18" /> {{ $t('audit.heading') }}</h1>
        <span class="muted sub">{{ $t('audit.subtitle') }}</span>
        <span class="spacer" />
        <button class="mini" :title="$t('audit.refreshTitle')" :aria-label="$t('audit.refreshTitle')" @click="load(); loadSessions(); loadAccess()"><RefreshCw :size="14" /></button>
      </div>

      <!-- sign-ins, last 24h -->
      <section class="acc">
        <div class="acc-head">
          <span class="acc-title">{{ $t('audit.accessTitle') }}</span>
          <span class="acc-stats muted" v-if="access">
            <span class="ok">{{ $t('audit.accessSuccessCount', { n: access.total_success }) }}</span>
            <span v-if="access.total_failure" class="ko">{{ $t('audit.accessFailureCount', { n: access.total_failure }) }}</span>
            <span>{{ $t('audit.accessTimezone', { tz: access.timezone }) }}</span>
          </span>
        </div>
        <VChart class="acc-chart" :option="accessOption" autoresize />
      </section>

      <!-- active sessions -->
      <section class="sess">
        <div class="sess-head">
          <span class="sess-title">{{ $t('audit.sessionsTitle') }}</span>
          <span class="sess-count"><Circle :size="8" class="dot on" /> {{ $t('audit.sessionsCount', { online: onlineCount, total: sessionsTotal }) }}<template v-if="sessions.length < sessionsTotal"> · {{ $t('audit.sessionsShown', { n: sessions.length }) }}</template></span>
        </div>
        <div class="sess-grid">
          <div v-for="s in sessions" :key="s.id" class="sess-card" :class="{ off: !s.online }">
            <Circle :size="8" class="dot" :class="{ on: s.online }" />
            <div class="sess-who">
              <span class="sess-name">
                {{ s.full_name || s.email }}<span v-if="s.is_superuser" class="adm">{{ $t('audit.adminBadge') }}</span><span v-if="s.current" class="here">{{ $t('sessions.thisDevice') }}</span>
              </span>
              <span class="sess-meta muted">
                <template v-if="deviceLabel(s.user_agent)">{{ deviceLabel(s.user_agent) }} · </template>{{ s.ip || '—' }} · {{ fmtAgo(s.last_seen_at) }}<template v-if="s.method === 'sso'"> · SSO</template>
              </span>
            </div>
            <button
              v-if="canRevoke && !s.current"
              class="sess-revoke"
              :disabled="revoking === s.id"
              :title="$t('audit.revokeTitle')"
              :aria-label="$t('audit.revokeAria', { who: s.full_name || s.email })"
              @click="revoke(s)"
            >
              <LogOut :size="13" />
            </button>
          </div>
          <p v-if="!sessions.length" class="muted empty">{{ $t('audit.noSessions') }}</p>
        </div>
      </section>

      <!-- filters -->
      <div class="filters">
        <span class="searchbox"><Search :size="14" /><input v-model="q" type="text" :placeholder="$t('audit.searchPlaceholder')" :aria-label="$t('audit.searchPlaceholder')" /></span>
        <select v-model="action" class="fsel">
          <option value="">{{ $t('audit.allActions') }}</option>
          <option v-for="a in actions" :key="a" :value="a">{{ meta(a).label }}</option>
        </select>
        <select v-model="outcome" class="fsel">
          <option value="">{{ $t('audit.allOutcomes') }}</option>
          <option value="success">{{ $t('audit.outcomeSuccess') }}</option>
          <option value="failure">{{ $t('audit.outcomeFailure') }}</option>
        </select>
        <span class="spacer" />
        <span class="muted count">{{ $t('audit.eventCount', { n: total }) }}</span>
      </div>

      <!-- log -->
      <div class="tbl-wrap">
        <p v-if="error" class="msg err">{{ error }}</p>
        <table v-else class="tbl">
          <thead>
            <tr>
              <th>{{ $t('audit.colWhen') }}</th><th>{{ $t('audit.colWho') }}</th><th>{{ $t('audit.colAction') }}</th><th>{{ $t('audit.colTarget') }}</th><th>{{ $t('audit.colDetail') }}</th><th>IP</th><th></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="e in items" :key="e.id" :class="{ fail: e.outcome === 'failure' }">
              <td class="when">{{ fmtDate(e.ts) }}</td>
              <td class="who">{{ e.actor_label }}</td>
              <td class="act">
                <component :is="meta(e.action).icon" :size="13" :style="{ color: meta(e.action).color }" />
                {{ meta(e.action).label }}
              </td>
              <td class="tgt">
                <template v-if="e.target_type">
                  <span class="tgt-type">{{ e.target_type }}</span> {{ e.target_label }}
                </template>
                <span v-else class="muted">—</span>
              </td>
              <td class="det muted" :title="e.detail ? JSON.stringify(e.detail, null, 2) : ''">{{ detailSummary(e) }}</td>
              <td class="ip muted">{{ e.ip || '—' }}</td>
              <td class="oc">
                <CheckCircle2 v-if="e.outcome === 'success'" :size="14" class="ok" />
                <AlertTriangle v-else :size="14" class="ko" />
              </td>
            </tr>
            <tr v-if="!items.length && !loading">
              <td colspan="7" class="msg muted">{{ $t('audit.noEvents') }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="pager">
        <button class="mini" :disabled="offset === 0" @click="prev"><ChevronLeft :size="14" /> {{ $t('audit.prevPage') }}</button>
        <span class="muted">{{ $t('audit.pagerRange', { from: total ? offset + 1 : 0, to: Math.min(offset + pageSize, total), total }) }}</span>
        <button class="mini" :disabled="offset + pageSize >= total" @click="next">{{ $t('audit.nextPage') }} <ChevronRight :size="14" /></button>
      </div>
    </div>
  </AppShell>
</template>

<style scoped>
.audit { display: flex; flex-direction: column; gap: 14px; }
.page-head { display: flex; align-items: center; gap: 12px; }
.page-head h1 { display: inline-flex; align-items: center; gap: 8px; margin: 0; font-size: 1.5em; }
.sub { font-size: 12.5px; }
.spacer { flex: 1; }
.mini { padding: 5px 9px; font-size: 12px; }

/* sign-ins 24h */
.acc { background: var(--panel); border: 1px solid var(--border); border-radius: 11px; padding: 12px 14px; }
.acc-head { display: flex; align-items: baseline; gap: 12px; }
.acc-title { font-size: 13px; font-weight: 600; }
.acc-stats { font-size: 12px; display: inline-flex; gap: 6px; }
.acc-stats .ok { color: var(--accent-2); }
.acc-stats .ko { color: var(--danger); }
.acc-chart { width: 100%; height: 160px; }

/* sessions */
.sess { background: var(--panel); border: 1px solid var(--border); border-radius: 11px; padding: 12px 14px; }
.sess-head { display: flex; align-items: baseline; gap: 12px; margin-bottom: 10px; }
.sess-title { font-size: 13px; font-weight: 600; }
.sess-count { font-size: 12px; color: var(--muted); display: inline-flex; align-items: center; gap: 6px; }
.dot { color: var(--muted); }
.dot.on { color: var(--accent-2); fill: var(--accent-2); }
.sess-grid { display: flex; flex-wrap: wrap; gap: 8px; }
.sess-card { display: flex; align-items: center; gap: 8px; padding: 7px 11px; border: 1px solid var(--border); border-radius: 9px; background: var(--panel-2); min-width: 190px; }
.sess-card.off { opacity: 0.6; }
.sess-who { display: flex; flex-direction: column; }
.sess-name { font-size: 13px; font-weight: 550; display: inline-flex; align-items: center; gap: 6px; }
.adm { font-size: 9.5px; text-transform: uppercase; letter-spacing: 0.03em; color: var(--accent); background: var(--tint-accent); border-radius: 4px; padding: 0 5px; }
.sess-meta { font-size: 11px; }
.here { font-size: 9.5px; text-transform: uppercase; letter-spacing: 0.03em; color: var(--accent-hi); background: var(--tint-accent); border-radius: 4px; padding: 0 5px; }
.sess-revoke { margin-left: auto; padding: 4px 6px; color: var(--muted); background: transparent; border-color: transparent; }
.sess-revoke:hover { color: var(--danger); border-color: var(--danger); box-shadow: none; }
.empty { font-size: 12.5px; }

/* filters */
.filters { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.searchbox { display: inline-flex; align-items: center; gap: 6px; border: 1px solid var(--border); border-radius: 7px; padding: 5px 9px; background: var(--panel-2); }
.searchbox input { border: none; background: transparent; padding: 0; min-width: 220px; }
.searchbox input:focus { box-shadow: none; }
.fsel { width: auto; padding: 6px 9px; font-size: 12.5px; }
.count { font-size: 12.5px; }

/* table */
.tbl-wrap { border: 1px solid var(--border); border-radius: 11px; overflow: hidden; background: var(--panel); }
.tbl { width: 100%; border-collapse: collapse; font-size: 12.5px; }
.tbl th { text-align: left; font-size: 11px; text-transform: uppercase; letter-spacing: 0.03em; color: var(--muted); padding: 9px 12px; border-bottom: 1px solid var(--border); background: var(--panel-2); white-space: nowrap; }
.tbl td { padding: 8px 12px; border-bottom: 1px solid var(--border-soft); vertical-align: top; }
.tbl tbody tr:last-child td { border-bottom: none; }
.tbl tbody tr:hover { background: var(--panel-2); }
.tbl tr.fail { background: rgba(255, 107, 107, 0.05); }
.when { white-space: nowrap; font-variant-numeric: tabular-nums; color: var(--muted); }
.who { white-space: nowrap; font-weight: 550; }
.act { white-space: nowrap; display: flex; align-items: center; gap: 6px; }
.tgt-type { font-size: 10px; text-transform: uppercase; letter-spacing: 0.03em; color: var(--muted); background: var(--panel-2); border-radius: 4px; padding: 1px 5px; }
.det { max-width: 320px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ip { white-space: nowrap; font-variant-numeric: tabular-nums; }
.oc .ok { color: var(--accent-2); }
.oc .ko { color: var(--danger); }
.msg { padding: 22px; text-align: center; }
.msg.err { color: var(--danger); }

.pager { display: flex; align-items: center; justify-content: center; gap: 16px; }
</style>
