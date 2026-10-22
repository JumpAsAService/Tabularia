<script setup lang="ts">
// Administration → Cluster: what Tabularia has running on Kubernetes, read-only and
// nearly live (gateway services/cluster_view.py). The pods of the release and of the
// users' code — state, restarts, age, node, CPU and memory against their limits — and
// the warning events. A Job of a script or a container links to what started it.
// Read every three seconds while the page is in view.
import { computed, onMounted, onUnmounted, ref, watchEffect } from 'vue'
import { useI18n } from 'vue-i18n'
import { Activity, CircleAlert, Pause, Play, Server, TriangleAlert } from 'lucide-vue-next'
import { useApiClient } from '~/composables/useApiClient'
import { errMessage } from '~/composables/useApi'
import { useFormat } from '~/composables/useFormat'

const { user } = useAuth()
const router = useRouter()
const { apiFetch } = useApiClient()
const { t } = useI18n()
const fmtx = useFormat()
// the gateway enforces it (require_observer); this only spares a page that would fail
watchEffect(() => { if (user.value && !user.value.is_superuser && !user.value.is_observer) router.replace('/') })

interface StartedBy { kind: 'flow' | 'script' | 'group'; id: number; name: string }
interface Pod {
  name: string
  component: string
  state: string
  phase: string | null
  ready: string
  restarts: number
  started_at: string | null
  node: string | null
  cpu_millis: number | null
  memory_bytes: number | null
  cpu_limit_millis: number | null
  memory_limit_bytes: number | null
  run_id: number | null
  started_by: StartedBy | null
  environment: string | null
}
interface Space { name: string; role: string; pods: Pod[]; error: string | null }
interface ClusterEvent { namespace: string; object: string; reason: string; message: string; count: number; last_seen: string | null }
interface Snapshot {
  disabled_reason: 'kubernetes' | 'off' | null
  error?: string
  generated_at?: string
  metrics?: boolean
  namespaces?: Space[]
  events?: ClusterEvent[]
}

const data = ref<Snapshot | null>(null)
const error = ref('')
const paused = ref(false)
const now = ref(Date.now())
let timer: ReturnType<typeof setTimeout> | undefined
let gone = false

async function load() {
  try {
    data.value = await apiFetch<Snapshot>('/admin/cluster')
    error.value = data.value.error ?? ''
  } catch (e) {
    error.value = errMessage(e)
  }
}
// every three seconds while the page is in view; nothing while the tab is hidden
async function tick() {
  if (gone) return
  if (!paused.value && document.visibilityState === 'visible') await load()
  now.value = Date.now()
  timer = setTimeout(tick, 3000)
}
onMounted(tick)
onUnmounted(() => { gone = true; clearTimeout(timer) })

const spaces = computed(() => data.value?.namespaces ?? [])
const allPods = computed(() => spaces.value.flatMap((s) => s.pods))
const codeRoles = new Set(['scripts', 'script-trials', 'script-builds', 'containers', 'container-trials'])

const OK = new Set(['Running', 'Succeeded'])
function tone(p: Pod): 'ok' | 'done' | 'wait' | 'bad' {
  if (p.state === 'Succeeded') return 'done'
  if (p.state === 'Running') return p.ready.startsWith('0/') ? 'wait' : 'ok'
  if (p.state === 'Pending' || p.state === 'ContainerCreating') return 'wait'
  return 'bad'
}
const stats = computed(() => ({
  running: allPods.value.filter((p) => p.state === 'Running').length,
  trouble: allPods.value.filter((p) => tone(p) === 'bad' || (tone(p) === 'wait' && p.state !== 'Pending')).length,
  pending: allPods.value.filter((p) => p.state === 'Pending').length,
  jobs: spaces.value.filter((s) => codeRoles.has(s.role)).flatMap((s) => s.pods).filter((p) => !OK.has(p.state) || p.state === 'Running').length,
}))

function pct(used: number | null, limit: number | null): number | null {
  if (used == null || !limit) return null
  return Math.min(100, Math.round((used / limit) * 100))
}
function cpu(m: number | null): string {
  if (m == null) return '—'
  return m >= 1000 ? `${(m / 1000).toFixed(m >= 10000 ? 0 : 1)} CPU` : `${Math.round(m)}m`
}
function mem(b: number | null): string {
  if (b == null) return '—'
  return b >= 2 ** 30 ? `${(b / 2 ** 30).toFixed(1)} Gi` : `${Math.round(b / 2 ** 20)} Mi`
}
function age(iso: string | null): string {
  if (!iso) return '—'
  const s = Math.max(0, Math.round((now.value - Date.parse(iso)) / 1000))
  if (s < 60) return `${s}s`
  if (s < 3600) return `${Math.floor(s / 60)}m`
  if (s < 86400) return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`
  return `${Math.floor(s / 86400)}d`
}
const updated = computed(() => {
  if (!data.value?.generated_at) return ''
  return t('cluster.updated', { s: Math.max(0, Math.round((now.value - Date.parse(data.value.generated_at)) / 1000)) })
})
function linkOf(b: StartedBy): string {
  return b.kind === 'flow' ? `/editor?flow=${b.id}` : b.kind === 'script' ? `/script?id=${b.id}` : `/flow-group?id=${b.id}`
}
const roleLabel = (role: string) => t(`cluster.role.${role.replace(/-([a-z])/g, (_, c) => c.toUpperCase())}`)
</script>

<template>
  <AppShell>
    <div class="page-head">
      <h1><Server :size="18" /> {{ $t('cluster.title') }}</h1>
      <div class="head-actions">
        <span v-if="data && !data.disabled_reason" class="live" :class="{ paused }">
          <Activity :size="13" /> {{ paused ? $t('cluster.paused') : updated }}
        </span>
        <button v-if="data && !data.disabled_reason" class="mini" :title="paused ? $t('cluster.resume') : $t('cluster.pause')"
                :aria-label="paused ? $t('cluster.resume') : $t('cluster.pause')" @click="paused = !paused">
          <Play v-if="paused" :size="14" /><Pause v-else :size="14" />
        </button>
      </div>
    </div>
    <p class="muted lead">{{ $t('cluster.lead') }}</p>

    <SkeletonRows v-if="!data && !error" :rows="6" />
    <p v-else-if="data?.disabled_reason" class="off" role="note">
      <TriangleAlert :size="14" /> {{ data.disabled_reason === 'kubernetes' ? $t('cluster.offCompose') : $t('cluster.offChart') }}
    </p>
    <template v-else-if="data">
      <p v-if="error" class="err" role="alert"><CircleAlert :size="14" /> {{ error }}</p>
      <div class="stats">
        <div class="stat"><span class="n">{{ stats.running }}</span><span class="l">{{ $t('cluster.running') }}</span></div>
        <div class="stat"><span class="n" :class="{ warn: stats.pending }">{{ stats.pending }}</span><span class="l">{{ $t('cluster.pending') }}</span></div>
        <div class="stat"><span class="n" :class="{ bad: stats.trouble }">{{ stats.trouble }}</span><span class="l">{{ $t('cluster.trouble') }}</span></div>
        <div class="stat"><span class="n">{{ stats.jobs }}</span><span class="l">{{ $t('cluster.codeJobs') }}</span></div>
      </div>
      <p v-if="data.metrics === false" class="muted hint">{{ $t('cluster.noMetrics') }}</p>

      <section v-for="s in spaces" :key="s.name" class="space">
        <div class="sec-head">
          <h2>{{ roleLabel(s.role) }} <code class="ns">{{ s.name }}</code></h2>
          <span class="muted small">{{ $t('cluster.pods', { n: s.pods.length }) }}</span>
        </div>
        <p v-if="s.error" class="err small"><CircleAlert :size="13" /> {{ s.error }}</p>
        <p v-else-if="!s.pods.length" class="muted small">{{ codeRoles.has(s.role) ? $t('cluster.nothingRunning') : $t('cluster.noPods') }}</p>
        <div v-else class="pods">
          <article v-for="p in s.pods" :key="p.name" class="pod" :class="tone(p)" :title="p.name">
            <div class="ptop">
              <span class="dot" />
              <strong class="comp">{{ p.started_by?.name || p.environment || p.component || p.name }}</strong>
              <span class="state">{{ p.state }}</span>
            </div>
            <div class="pname muted">{{ p.name }}</div>
            <div class="facts small">
              <span>{{ $t('cluster.ready') }} {{ p.ready }}</span>
              <span :class="{ restarts: p.restarts > 0 }">{{ $t('cluster.restarts', { n: p.restarts }) }}</span>
              <span>{{ age(p.started_at) }}</span>
              <span v-if="p.node" class="muted">{{ p.node }}</span>
            </div>
            <div v-if="p.cpu_millis != null || p.memory_bytes != null" class="bars">
              <div class="bar">
                <span class="bl">CPU</span>
                <span class="track"><span class="fill" :style="{ width: `${pct(p.cpu_millis, p.cpu_limit_millis) ?? 0}%` }" /></span>
                <span class="bv">{{ cpu(p.cpu_millis) }}<template v-if="p.cpu_limit_millis"> / {{ cpu(p.cpu_limit_millis) }}</template></span>
              </div>
              <div class="bar">
                <span class="bl">{{ $t('cluster.memory') }}</span>
                <span class="track"><span class="fill" :class="{ hot: (pct(p.memory_bytes, p.memory_limit_bytes) ?? 0) >= 85 }"
                                          :style="{ width: `${pct(p.memory_bytes, p.memory_limit_bytes) ?? 0}%` }" /></span>
                <span class="bv">{{ mem(p.memory_bytes) }}<template v-if="p.memory_limit_bytes"> / {{ mem(p.memory_limit_bytes) }}</template></span>
              </div>
            </div>
            <div v-if="p.started_by" class="small by">
              {{ $t(`cluster.by.${p.started_by.kind}`) }} <NuxtLink :to="linkOf(p.started_by)">{{ p.started_by.name }}</NuxtLink>
              <span v-if="p.run_id" class="muted"> · run {{ p.run_id }}</span>
            </div>
          </article>
        </div>
      </section>

      <section class="space">
        <div class="sec-head"><h2>{{ $t('cluster.events') }}</h2></div>
        <p v-if="!data.events?.length" class="muted small">{{ $t('cluster.noEvents') }}</p>
        <div v-else class="scroll">
          <table class="data">
            <thead><tr><th>{{ $t('cluster.when') }}</th><th>{{ $t('cluster.namespace') }}</th><th>{{ $t('cluster.object') }}</th><th>{{ $t('cluster.reason') }}</th><th>{{ $t('cluster.message') }}</th></tr></thead>
            <tbody>
              <tr v-for="(e, i) in data.events" :key="i">
                <td class="nowrap">{{ e.last_seen ? fmtx.ago(e.last_seen) : '—' }}<span v-if="e.count > 1" class="muted"> ×{{ e.count }}</span></td>
                <td><code>{{ e.namespace }}</code></td>
                <td class="mono">{{ e.object }}</td>
                <td class="reason">{{ e.reason }}</td>
                <td class="msg">{{ e.message }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>
    </template>
    <p v-else-if="error" class="err" role="alert"><CircleAlert :size="14" /> {{ error }}</p>
  </AppShell>
</template>

<style scoped>
.lead { margin: 4px 0 0; max-width: 80ch; }
.hint { margin: 6px 0 0; font-size: 12px; }
.small { font-size: 12px; }
.live { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: var(--accent-2); }
.live svg { animation: pulse 1.6s ease-in-out infinite; }
.live.paused { color: var(--muted); }
.live.paused svg { animation: none; }
@keyframes pulse { 50% { opacity: .35; } }
@media (prefers-reduced-motion: reduce) { .live svg { animation: none; } }
.off { display: flex; gap: 8px; align-items: flex-start; margin-top: 16px; color: var(--muted); max-width: 80ch; }
.err { display: flex; align-items: center; gap: 6px; color: var(--danger); font-size: 13px; }
.stats { display: flex; gap: 12px; flex-wrap: wrap; margin: 14px 0 4px; }
.stat { display: flex; flex-direction: column; gap: 2px; padding: 12px 16px; border: 1px solid var(--border-soft); border-radius: 10px; background: var(--panel); min-width: 140px; }
.stat .n { font-size: 22px; font-weight: 700; font-variant-numeric: tabular-nums; }
.stat .l { font-size: 11.5px; text-transform: uppercase; letter-spacing: 0.04em; color: var(--muted); }
.n.bad { color: var(--danger); }
.n.warn { color: var(--warning); }
.space { margin-top: 26px; }
.sec-head { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; margin-bottom: 8px; }
.sec-head h2 { margin: 0; font-size: 15px; font-weight: 600; display: flex; align-items: baseline; gap: 8px; }
.ns { font-size: 11.5px; color: var(--muted); font-weight: 400; }
.pods { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 10px; }
.pod { border: 1px solid var(--border-soft); border-left: 3px solid var(--border); border-radius: 9px; padding: 9px 11px; background: var(--panel); display: flex; flex-direction: column; gap: 5px; min-width: 0; }
.pod.ok { border-left-color: var(--accent-2); }
.pod.done { border-left-color: var(--border); opacity: .75; }
.pod.wait { border-left-color: var(--warning); }
.pod.bad { border-left-color: var(--danger); }
.ptop { display: flex; align-items: center; gap: 7px; min-width: 0; }
.dot { width: 8px; height: 8px; border-radius: 50%; background: var(--border); flex: none; }
.ok .dot { background: var(--accent-2); }
.wait .dot { background: var(--warning); }
.bad .dot { background: var(--danger); }
.comp { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13px; }
.state { margin-left: auto; font-size: 11px; padding: 1px 7px; border-radius: 999px; border: 1px solid var(--border); color: var(--muted); white-space: nowrap; }
.bad .state { color: var(--danger); border-color: var(--danger); }
.wait .state { color: var(--warning); border-color: var(--warning); }
.pname { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.facts { display: flex; flex-wrap: wrap; gap: 3px 10px; }
.restarts { color: var(--danger); }
.bars { display: flex; flex-direction: column; gap: 4px; }
.bar { display: grid; grid-template-columns: 52px minmax(0, 1fr) auto; gap: 8px; align-items: center; font-size: 11.5px; }
.bl { color: var(--muted); }
.track { height: 6px; border-radius: 999px; background: var(--panel-2); overflow: hidden; }
.fill { display: block; height: 100%; background: var(--accent); border-radius: 999px; transition: width .6s ease; }
.fill.hot { background: var(--danger); }
.bv { font-variant-numeric: tabular-nums; white-space: nowrap; }
.by { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.scroll { overflow-x: auto; }
table.data { width: 100%; }
.nowrap { white-space: nowrap; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11.5px; }
.reason { font-weight: 600; white-space: nowrap; }
.msg { font-size: 12px; max-width: 70ch; }
</style>
