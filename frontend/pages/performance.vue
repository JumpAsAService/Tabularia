<script setup lang="ts">
// Prestazioni (solo superuser). Tre domande, tre fonti, NESSUNA tabella nuova:
//  - quali flussi e refresh costano di più, e quanto si aspetta un worker libero
//    → aggregati calcolati al volo dalla tabella dei run;
//  - come vanno le preview e QUALI sono state lente → contatori a dimensione
//    fissa su Valkey (i grafici nel tempo stanno in Monitoring/Grafana);
//  - cosa pesa sul warehouse → il query_log che ClickHouse tiene da sé.
import { computed, onMounted, ref, watch, watchEffect } from 'vue'
import { useI18n } from 'vue-i18n'
import { Gauge, RefreshCw, CircleAlert, Workflow, Database } from 'lucide-vue-next'
import { useApiClient } from '~/composables/useApiClient'
import { errMessage } from '~/composables/useApi'
import Select from '~/components/ui/Select.vue'

const { user } = useAuth()
const router = useRouter()
const { apiFetch } = useApiClient()
const { t } = useI18n()
// guardia UX: la RBAC vera la impone il gateway (require_superuser)
watchEffect(() => { if (user.value && !user.value.is_superuser) router.replace('/') })

interface RunItem { kind: 'flow' | 'ingest'; id: number | null; name: string | null; runs: number; failures: number; median_s: number | null; p95_s: number | null; max_s: number | null; total_s: number; median_wait_s: number | null; rows: number; last_error: string | null }
interface RunsPerf { days: number; truncated: boolean; totals: { runs: number; failures: number; median_wait_s: number | null; p95_wait_s: number | null }; items: RunItem[] }
interface EngineStat { engine: string; ok: number; superseded: number; error: number; avg_ok_ms: number | null; phases: Record<string, number> }
interface SlowPreview { at: number; ms: number; engine: string; dataset: string; ops: number; source: string; cache: boolean; phases: Record<string, number> }
interface PreviewsPerf { engines: EngineStat[]; slowest: SlowPreview[]; window_hours: number }
interface WhQuery { at: string; kind: string; ok: boolean; exception_code: number; duration_ms: number; read_rows: number; read_bytes: number; memory_bytes: number; origin: 'preview' | 'run'; query: string }
interface WarehousePerf { enabled: boolean; queries: WhQuery[]; error?: string }

const days = ref(7)
const minutes = ref(60)
const runs = ref<RunsPerf | null>(null)
const previews = ref<PreviewsPerf | null>(null)
const warehouse = ref<WarehousePerf | null>(null)
const loading = ref(false)
const error = ref('')

async function load() {
  loading.value = true
  error.value = ''
  // le tre fonti sono indipendenti: una che non risponde non spegne le altre
  const [r, p, w] = await Promise.allSettled([
    apiFetch<RunsPerf>(`/admin/performance/runs?days=${days.value}`),
    apiFetch<PreviewsPerf>('/admin/performance/previews?limit=15'),
    apiFetch<WarehousePerf>(`/admin/performance/warehouse?minutes=${minutes.value}&limit=15`),
  ])
  if (r.status === 'fulfilled') runs.value = r.value; else error.value = errMessage(r.reason)
  if (p.status === 'fulfilled') previews.value = p.value; else error.value ||= errMessage(p.reason)
  if (w.status === 'fulfilled') warehouse.value = w.value; else error.value ||= errMessage(w.reason)
  loading.value = false
}
onMounted(load)
watch([days, minutes], load)

const nf = new Intl.NumberFormat('it-IT')
const secs = (s: number | null | undefined) => s == null ? '—' : s < 1 ? `${Math.round(s * 1000)} ms` : s < 90 ? `${s.toFixed(1)} s` : `${Math.floor(s / 60)} min ${Math.round(s % 60)} s`
const ms = (v: number | null | undefined) => (v == null ? '—' : secs(v / 1000))
const bytes = (b: number) => b > 1e9 ? `${(b / 1e9).toFixed(1)} GB` : b > 1e6 ? `${(b / 1e6).toFixed(0)} MB` : `${Math.round(b / 1e3)} KB`
const time = (iso: string | number) => new Date(typeof iso === 'number' ? iso * 1000 : iso).toLocaleString('it-IT', { dateStyle: 'short', timeStyle: 'short' })
const total = (e: EngineStat) => e.ok + e.superseded + e.error
// la fase che ha pesato di più: è ciò che dice DOVE guardare
const dominant = (p: Record<string, number>) => Object.entries(p).sort((a, b) => b[1] - a[1])[0]
const share = (p: Record<string, number>, k: string) => { const s = Object.values(p).reduce((a, b) => a + b, 0); return s ? Math.round((100 * (p[k] ?? 0)) / s) : 0 }
const failureRate = computed(() => runs.value && runs.value.totals.runs ? Math.round((100 * runs.value.totals.failures) / runs.value.totals.runs) : 0)
const dayOptions = computed(() => [1, 7, 30, 90].map((d) => ({ value: d, label: t('performance.lastDays', { n: d }) })))
const minuteOptions = computed(() => [15, 60, 360, 1440].map((m) => ({ value: m, label: m < 60 ? t('performance.lastMinutes', { n: m }) : t('performance.lastHours', { n: m / 60 }) })))
</script>

<template>
  <AppShell>
    <template v-if="user?.is_superuser">
      <div class="page-head">
        <h1><Gauge :size="18" /> {{ $t('performance.pageTitle') }}</h1>
        <div class="head-actions">
          <button class="mini" :title="$t('performance.refresh')" :aria-label="$t('performance.refresh')" :disabled="loading" @click="load">
            <RefreshCw :size="14" :class="{ spin: loading }" />
          </button>
        </div>
      </div>
      <p class="muted lead">{{ $t('performance.lead') }}</p>
      <p v-if="error" class="err"><CircleAlert :size="14" /> {{ error }}</p>

      <!-- ── Run ─────────────────────────────────────────────────────────── -->
      <div class="sec-head">
        <h2>{{ $t('performance.runsTitle') }}</h2>
        <Select v-model="days" :options="dayOptions" class="win" :aria-label="$t('performance.window')" />
      </div>
      <div v-if="runs" class="stats">
        <div class="stat"><span class="n">{{ nf.format(runs.totals.runs) }}</span><span class="l">{{ $t('performance.runsCount') }}</span></div>
        <div class="stat"><span class="n" :class="{ bad: failureRate >= 10 }">{{ failureRate }}%</span><span class="l">{{ $t('performance.failureRate') }}</span></div>
        <div class="stat"><span class="n">{{ secs(runs.totals.median_wait_s) }}</span><span class="l">{{ $t('performance.medianWait') }}</span></div>
        <div class="stat"><span class="n" :class="{ bad: (runs.totals.p95_wait_s ?? 0) > 30 }">{{ secs(runs.totals.p95_wait_s) }}</span><span class="l">{{ $t('performance.p95Wait') }}</span></div>
      </div>
      <p class="muted hint">{{ $t('performance.waitHint') }}</p>
      <div class="scroll">
        <table v-if="runs?.items.length" class="data">
          <thead><tr>
            <th>{{ $t('performance.colWhat') }}</th><th class="r">{{ $t('performance.colRuns') }}</th><th class="r">{{ $t('performance.colFailures') }}</th>
            <th class="r">{{ $t('performance.colMedian') }}</th><th class="r">p95</th><th class="r">{{ $t('performance.colMax') }}</th>
            <th class="r">{{ $t('performance.colTotal') }}</th><th class="r">{{ $t('performance.colWait') }}</th>
          </tr></thead>
          <tbody>
            <tr v-for="i in runs.items" :key="i.kind + i.id">
              <td class="what" :title="i.last_error ?? ''">
                <Database v-if="i.kind === 'ingest'" :size="12" /><Workflow v-else :size="12" /> <span :class="{ muted: !i.name }">{{ i.name ?? $t('performance.deleted') }}</span>
                <span v-if="i.last_error" class="lasterr">{{ i.last_error }}</span>
              </td>
              <td class="r">{{ nf.format(i.runs) }}</td>
              <td class="r" :class="{ bad: i.failures > 0 }">{{ i.failures || '' }}</td>
              <td class="r">{{ secs(i.median_s) }}</td><td class="r">{{ secs(i.p95_s) }}</td><td class="r">{{ secs(i.max_s) }}</td>
              <td class="r strong">{{ secs(i.total_s) }}</td><td class="r">{{ secs(i.median_wait_s) }}</td>
            </tr>
          </tbody>
        </table>
        <p v-else-if="runs" class="muted">{{ $t('performance.noRuns') }}</p>
      </div>
      <p v-if="runs?.truncated" class="muted hint">{{ $t('performance.truncated') }}</p>

      <!-- ── Preview ─────────────────────────────────────────────────────── -->
      <div class="sec-head"><h2>{{ $t('performance.previewsTitle') }}</h2></div>
      <p class="muted hint">{{ $t('performance.previewsHint') }}</p>
      <div class="scroll">
        <table v-if="previews?.engines.length" class="data">
          <thead><tr>
            <th>{{ $t('performance.colEngine') }}</th><th class="r">{{ $t('performance.colPreviews') }}</th><th class="r">{{ $t('performance.colAvg') }}</th>
            <th class="r">{{ $t('performance.colSuperseded') }}</th><th class="r">{{ $t('performance.colErrors') }}</th><th>{{ $t('performance.colWhere') }}</th>
          </tr></thead>
          <tbody>
            <tr v-for="e in previews.engines" :key="e.engine">
              <td>{{ e.engine }}</td><td class="r">{{ nf.format(total(e)) }}</td><td class="r">{{ ms(e.avg_ok_ms) }}</td>
              <td class="r">{{ nf.format(e.superseded) }}</td><td class="r" :class="{ bad: e.error > 0 }">{{ e.error || '' }}</td>
              <td><template v-if="dominant(e.phases)">{{ $t('performance.phase.' + dominant(e.phases)[0]) }} · {{ share(e.phases, dominant(e.phases)[0]) }}%</template></td>
            </tr>
          </tbody>
        </table>
        <p v-else-if="previews" class="muted">{{ $t('performance.noPreviews') }}</p>
      </div>
      <h3 v-if="previews?.slowest.length">{{ $t('performance.slowestTitle', { h: previews.window_hours }) }}</h3>
      <div class="scroll">
        <table v-if="previews?.slowest.length" class="data">
          <thead><tr><th>{{ $t('performance.colWhen') }}</th><th class="r">{{ $t('performance.colDuration') }}</th><th>{{ $t('performance.colEngine') }}</th><th>{{ $t('performance.colDataset') }}</th><th class="r">{{ $t('performance.colOps') }}</th><th>{{ $t('performance.colWhere') }}</th></tr></thead>
          <tbody>
            <tr v-for="(s, k) in previews.slowest" :key="k">
              <td>{{ time(s.at) }}</td><td class="r strong">{{ ms(s.ms) }}</td><td>{{ s.engine }}</td>
              <td class="mono" :title="s.dataset">{{ s.dataset }}</td><td class="r">{{ s.ops }}</td>
              <td><template v-if="dominant(s.phases)">{{ $t('performance.phase.' + dominant(s.phases)[0]) }} · {{ share(s.phases, dominant(s.phases)[0]) }}%</template><span v-if="s.cache" class="tag">cache</span></td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- ── Warehouse ───────────────────────────────────────────────────── -->
      <div class="sec-head">
        <h2>{{ $t('performance.warehouseTitle') }}</h2>
        <Select v-if="warehouse?.enabled" v-model="minutes" :options="minuteOptions" class="win" :aria-label="$t('performance.window')" />
      </div>
      <p v-if="warehouse && !warehouse.enabled" class="muted">{{ $t('performance.warehouseOff') }}</p>
      <p v-else-if="warehouse?.error" class="err"><CircleAlert :size="14" /> {{ warehouse.error }}</p>
      <div v-else class="scroll">
        <table v-if="warehouse?.queries.length" class="data">
          <thead><tr><th>{{ $t('performance.colWhen') }}</th><th class="r">{{ $t('performance.colDuration') }}</th><th>{{ $t('performance.colOrigin') }}</th><th class="r">{{ $t('performance.colRowsRead') }}</th><th class="r">{{ $t('performance.colBytesRead') }}</th><th class="r">{{ $t('performance.colMemory') }}</th><th>Query</th></tr></thead>
          <tbody>
            <tr v-for="(q, k) in warehouse.queries" :key="k">
              <td>{{ time(q.at) }}</td><td class="r strong" :class="{ bad: !q.ok }">{{ ms(q.duration_ms) }}</td>
              <td>{{ $t('performance.origin.' + q.origin) }}<span v-if="!q.ok" class="tag bad">{{ q.exception_code }}</span></td>
              <td class="r">{{ nf.format(q.read_rows) }}</td><td class="r">{{ bytes(q.read_bytes) }}</td><td class="r">{{ bytes(q.memory_bytes) }}</td>
              <td class="mono q" :title="q.query">{{ q.query }}</td>
            </tr>
          </tbody>
        </table>
        <p v-else-if="warehouse" class="muted">{{ $t('performance.noQueries') }}</p>
      </div>
    </template>
  </AppShell>
</template>

<style scoped>
.lead { margin: 4px 0 0; max-width: 78ch; }
.hint { margin: 6px 0 10px; font-size: 12px; max-width: 86ch; }
.sec-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin: 30px 0 8px; }
.sec-head h2 { margin: 0; font-size: 16px; font-weight: 600; }
h3 { margin: 20px 0 8px; font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); }
.win { width: 170px; }
.stats { display: flex; gap: 12px; flex-wrap: wrap; margin: 4px 0; }
.stat { display: flex; flex-direction: column; gap: 2px; padding: 12px 16px; border: 1px solid var(--border-soft); border-radius: 10px; background: var(--panel); min-width: 140px; }
.stat .n { font-size: 22px; font-weight: 700; font-variant-numeric: tabular-nums; }
.stat .l { font-size: 11.5px; text-transform: uppercase; letter-spacing: 0.04em; color: var(--muted); }
.scroll { overflow-x: auto; }
table.data td, table.data th { font-variant-numeric: tabular-nums; }
.r { text-align: right; white-space: nowrap; }
.strong { font-weight: 600; }
.bad { color: var(--danger); }
.what { display: flex; align-items: center; gap: 6px; min-width: 220px; }
.what svg { flex: none; color: var(--muted); }
.lasterr { color: var(--muted); font-size: 11.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 36ch; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11.5px; max-width: 46ch; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.q { max-width: 60ch; }
.tag { margin-left: 6px; padding: 1px 6px; border-radius: 999px; border: 1px solid var(--border); font-size: 10.5px; color: var(--muted); }
.tag.bad { color: var(--danger); border-color: var(--danger); }
.err { display: flex; align-items: center; gap: 6px; color: var(--danger); font-size: 13px; }
</style>
