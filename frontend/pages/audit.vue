<script setup lang="ts">
// Audit log (solo admin): chi entra, chi fa login, quali flussi/datasource/
// connessioni vengono creati/modificati/eseguiti, quali dati vengono scaricati,
// chi cambia i permessi. In alto le sessioni attive; sotto il registro filtrabile.
import { ref, computed, watch, onMounted } from 'vue'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { BarChart } from 'echarts/charts'
import { GridComponent, TooltipComponent, LegendComponent } from 'echarts/components'
import VChart from 'vue-echarts'
import {
  ScrollText, Search, RefreshCw, ChevronLeft, ChevronRight, Circle,
  LogIn, LogOut, Workflow, Database, Plug, Download, Shield, CheckCircle2, XCircle, AlertTriangle,
} from 'lucide-vue-next'
import { useApi, type AuditEntry, type ActiveSession, type AccessActivity } from '~/composables/useApi'

use([CanvasRenderer, BarChart, GridComponent, TooltipComponent, LegendComponent])

const { user } = useAuth()
const router = useRouter()
watch(() => user.value, (u) => { if (u && !u.is_superuser) router.replace('/') }, { immediate: true })

const api = useApi()

// ── filtri + paginazione del registro ────────────────────────────────────────
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
    error.value = e?.message ?? 'Errore'
  } finally {
    loading.value = false
  }
}
watch([q, action, outcome], () => { offset.value = 0; load() })
function next() { if (offset.value + pageSize < total.value) { offset.value += pageSize; load() } }
function prev() { if (offset.value > 0) { offset.value = Math.max(0, offset.value - pageSize); load() } }

// ── sessioni attive ──────────────────────────────────────────────────────────
const sessions = ref<ActiveSession[]>([])
async function loadSessions() {
  try { sessions.value = await api.auditSessions() } catch { /* ignore */ }
}
const onlineCount = computed(() => sessions.value.filter((s) => s.online).length)

// ── grafico accessi ultime 24h ───────────────────────────────────────────────
const { theme } = useTheme()
const access = ref<AccessActivity | null>(null)
async function loadAccess() {
  try { access.value = await api.auditAccessActivity(24) } catch { /* ignore */ }
}
function readUi() {
  const fb = { text: '#e8ebf2', muted: '#8b93a7', border: '#262e40', panel: '#141926' }
  if (!import.meta.client) return fb
  const s = getComputedStyle(document.documentElement)
  const g = (n: string, f: string) => s.getPropertyValue(n).trim() || f
  return { text: g('--text', fb.text), muted: g('--muted', fb.muted), border: g('--border', fb.border), panel: g('--panel', fb.panel) }
}
const ui = ref(readUi())
watch(theme, () => { ui.value = readUi() })

const accessOption = computed(() => {
  const a = access.value
  const c = ui.value
  const labels = a?.buckets.map((b) => b.label) ?? []
  return {
    tooltip: {
      trigger: 'axis', backgroundColor: c.panel, borderColor: c.border,
      textStyle: { color: c.text, fontSize: 12 },
    },
    legend: { data: ['Accessi', 'Falliti'], textStyle: { color: c.muted, fontSize: 11 }, top: 0, icon: 'circle' },
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
      { name: 'Accessi', type: 'bar', stack: 'x', data: a?.buckets.map((b) => b.success) ?? [],
        itemStyle: { color: '#6ee7b7', borderRadius: [0, 0, 0, 0] }, barMaxWidth: 18 },
      { name: 'Falliti', type: 'bar', stack: 'x', data: a?.buckets.map((b) => b.failure) ?? [],
        itemStyle: { color: '#ff6b6b', borderRadius: [3, 3, 0, 0] }, barMaxWidth: 18 },
    ],
  }
})

onMounted(async () => {
  try { actions.value = await api.auditActions() } catch { /* ignore */ }
  await Promise.all([load(), loadSessions(), loadAccess()])
})

// ── presentazione ─────────────────────────────────────────────────────────────
const ACTION_META: Record<string, { icon: any; color: string; label: string }> = {
  'auth.login': { icon: LogIn, color: '#6ee7b7', label: 'Login' },
  'auth.login_failed': { icon: XCircle, color: '#ff6b6b', label: 'Login fallito' },
  'auth.logout': { icon: LogOut, color: '#8b93a7', label: 'Logout' },
  'flow.create': { icon: Workflow, color: '#4f8cff', label: 'Flusso creato' },
  'flow.update': { icon: Workflow, color: '#4f8cff', label: 'Flusso modificato' },
  'flow.delete': { icon: Workflow, color: '#ff6b6b', label: 'Flusso eliminato' },
  'flow.run': { icon: Workflow, color: '#a78bfa', label: 'Flusso eseguito' },
  'flow.schedule': { icon: Workflow, color: '#4f8cff', label: 'Flusso schedulato' },
  'flow.promote': { icon: Workflow, color: '#4f8cff', label: 'Versione promossa' },
  'datasource.create': { icon: Database, color: '#6ee7b7', label: 'Datasource creata' },
  'datasource.refresh': { icon: Database, color: '#a78bfa', label: 'Datasource aggiornata' },
  'datasource.delete': { icon: Database, color: '#ff6b6b', label: 'Datasource eliminata' },
  'datasource.schedule': { icon: Database, color: '#4f8cff', label: 'Refresh schedulato' },
  'connection.create': { icon: Plug, color: '#6ee7b7', label: 'Connessione creata' },
  'connection.update': { icon: Plug, color: '#4f8cff', label: 'Connessione modificata' },
  'connection.delete': { icon: Plug, color: '#ff6b6b', label: 'Connessione eliminata' },
  'export.download': { icon: Download, color: '#fbbf24', label: 'Download' },
  'permission.grant': { icon: Shield, color: '#6ee7b7', label: 'Permesso concesso' },
  'permission.revoke': { icon: Shield, color: '#ff6b6b', label: 'Permesso revocato' },
}
function meta(a: string) {
  return ACTION_META[a] ?? { icon: Circle, color: '#8b93a7', label: a }
}
function fmtDate(iso: string) {
  try { return new Date(iso).toLocaleString('it-IT', { dateStyle: 'short', timeStyle: 'medium' }) } catch { return iso }
}
function fmtAgo(iso: string | null) {
  if (!iso) return '—'
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000)
  if (s < 60) return 'adesso'
  if (s < 3600) return `${Math.floor(s / 60)} min fa`
  if (s < 86400) return `${Math.floor(s / 3600)} h fa`
  return `${Math.floor(s / 86400)} g fa`
}
// riassunto compatto del detail (JSON completo nel title)
function detailSummary(e: AuditEntry): string {
  const d = e.detail
  if (!d) return ''
  const parts: string[] = []
  if (d.reason) parts.push(String(d.reason))
  if (d.format) parts.push(`${d.format}`)
  if (d.filename && !d.format) parts.push(String(d.filename))
  if (d.cron) parts.push(`cron: ${d.cron}`)
  if (Array.isArray(d.changed) && d.changed.length) parts.push(`campi: ${d.changed.join(', ')}`)
  if (d.capability) parts.push(String(d.capability))
  if (d.db_type) parts.push(String(d.db_type))
  if (d.engine) parts.push(`engine: ${d.engine}`)
  return parts.join(' · ')
}
</script>

<template>
  <AppShell fluid>
    <div class="audit">
      <div class="page-head">
        <h2><ScrollText :size="18" /> Audit log</h2>
        <span class="muted sub">Chi entra, chi fa cosa, chi scarica cosa</span>
        <span class="spacer" />
        <button class="mini" title="Aggiorna" @click="load(); loadSessions(); loadAccess()"><RefreshCw :size="14" /></button>
      </div>

      <!-- accessi ultime 24h -->
      <section class="acc">
        <div class="acc-head">
          <span class="acc-title">Accessi · ultime 24 ore</span>
          <span class="acc-stats muted" v-if="access">
            <span class="ok">{{ access.total_success }} accessi</span>
            <span v-if="access.total_failure" class="ko">· {{ access.total_failure }} falliti</span>
            <span>· fuso {{ access.timezone }}</span>
          </span>
        </div>
        <VChart class="acc-chart" :option="accessOption" autoresize />
      </section>

      <!-- sessioni attive -->
      <section class="sess">
        <div class="sess-head">
          <span class="sess-title">Sessioni attive</span>
          <span class="sess-count"><Circle :size="8" class="dot on" /> {{ onlineCount }} online · {{ sessions.length }} viste di recente</span>
        </div>
        <div class="sess-grid">
          <div v-for="s in sessions" :key="s.user_id" class="sess-card" :class="{ off: !s.online }">
            <Circle :size="8" class="dot" :class="{ on: s.online }" />
            <div class="sess-who">
              <span class="sess-name">{{ s.full_name || s.email }}<span v-if="s.is_superuser" class="adm">admin</span></span>
              <span class="sess-meta muted">{{ s.last_seen_ip || '—' }} · {{ fmtAgo(s.last_seen_at) }}</span>
            </div>
          </div>
          <p v-if="!sessions.length" class="muted empty">Nessuna sessione recente.</p>
        </div>
      </section>

      <!-- filtri -->
      <div class="filters">
        <span class="searchbox"><Search :size="14" /><input v-model="q" type="text" placeholder="Cerca attore, bersaglio, IP…" /></span>
        <select v-model="action" class="fsel">
          <option value="">Tutte le azioni</option>
          <option v-for="a in actions" :key="a" :value="a">{{ meta(a).label }}</option>
        </select>
        <select v-model="outcome" class="fsel">
          <option value="">Ogni esito</option>
          <option value="success">Successo</option>
          <option value="failure">Fallito</option>
        </select>
        <span class="spacer" />
        <span class="muted count">{{ total }} eventi</span>
      </div>

      <!-- registro -->
      <div class="tbl-wrap">
        <p v-if="error" class="msg err">{{ error }}</p>
        <table v-else class="tbl">
          <thead>
            <tr>
              <th>Quando</th><th>Chi</th><th>Azione</th><th>Bersaglio</th><th>Dettaglio</th><th>IP</th><th></th>
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
              <td colspan="7" class="msg muted">Nessun evento.</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="pager">
        <button class="mini" :disabled="offset === 0" @click="prev"><ChevronLeft :size="14" /> Precedenti</button>
        <span class="muted">{{ total ? offset + 1 : 0 }}–{{ Math.min(offset + pageSize, total) }} di {{ total }}</span>
        <button class="mini" :disabled="offset + pageSize >= total" @click="next">Successivi <ChevronRight :size="14" /></button>
      </div>
    </div>
  </AppShell>
</template>

<style scoped>
.audit { display: flex; flex-direction: column; gap: 14px; }
.page-head { display: flex; align-items: center; gap: 12px; }
.page-head h2 { display: inline-flex; align-items: center; gap: 8px; margin: 0; }
.sub { font-size: 12.5px; }
.spacer { flex: 1; }
.mini { padding: 5px 9px; font-size: 12px; }

/* accessi 24h */
.acc { background: var(--panel); border: 1px solid var(--border); border-radius: 11px; padding: 12px 14px; }
.acc-head { display: flex; align-items: baseline; gap: 12px; }
.acc-title { font-size: 13px; font-weight: 600; }
.acc-stats { font-size: 12px; display: inline-flex; gap: 6px; }
.acc-stats .ok { color: var(--accent-2); }
.acc-stats .ko { color: var(--danger); }
.acc-chart { width: 100%; height: 160px; }

/* sessioni */
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
.empty { font-size: 12.5px; }

/* filtri */
.filters { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.searchbox { display: inline-flex; align-items: center; gap: 6px; border: 1px solid var(--border); border-radius: 7px; padding: 5px 9px; background: var(--panel-2); }
.searchbox input { border: none; background: transparent; padding: 0; min-width: 220px; }
.searchbox input:focus { box-shadow: none; }
.fsel { width: auto; padding: 6px 9px; font-size: 12.5px; }
.count { font-size: 12.5px; }

/* tabella */
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
