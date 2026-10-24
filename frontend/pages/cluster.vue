<script setup lang="ts">
// Administration → Cluster: what Tabularia has running on Kubernetes, read-only and
// nearly live (gateway services/cluster_view.py), read every three seconds while the page
// is in view. Three readings of the same snapshot, from the glance to the detail:
//   - one health bar: every pod, by state;
//   - the node map: a row per node of the cluster, a tile per pod on it (a square for
//     Tabularia's own, a circle for a script or a container), the pods not on a node yet
//     in a row of their own; a tile leads to its row;
//   - the pods, grouped by component (the replicas of the gateway together) or by what
//     started them, with the last minute of CPU, kept by the page itself, and memory
//     against the limit. A row that changes state lights up for a moment.
// The warning events close the page.
import { computed, nextTick, onMounted, onUnmounted, ref, watchEffect } from 'vue'
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
type Tone = 'ok' | 'wait' | 'bad' | 'done'

const data = ref<Snapshot | null>(null)
const error = ref('')
const paused = ref(false)
const now = ref(Date.now())
let timer: ReturnType<typeof setTimeout> | undefined
let gone = false

// ── what the page remembers between two readings ───────────────────────────
// the last minute of CPU of each pod (20 readings, one every three seconds)
const HISTORY = 20
const cpuHistory = ref<Record<string, number[]>>({})
// the rows that just changed state, or just appeared: lit for a moment
const flashing = ref<Set<string>>(new Set())
let lastTone: Record<string, Tone> | null = null

function remember(snapshot: Snapshot) {
  arrivedAt.value = Date.now()
  const pods = (snapshot.namespaces ?? []).flatMap((s) => s.pods)
  // a namespace that could not be read lists no pods: theirs are not gone, their history
  // and state stay (else every row would light up when it comes back)
  const partial = (snapshot.namespaces ?? []).some((s) => s.error)
  const history: Record<string, number[]> = partial ? { ...cpuHistory.value } : {}
  for (const p of pods) {
    const before = cpuHistory.value[p.name] ?? []
    history[p.name] = p.cpu_millis == null ? before : [...before, p.cpu_millis].slice(-HISTORY)
  }
  cpuHistory.value = history
  const tones: Record<string, Tone> = Object.fromEntries(pods.map((p) => [p.name, tone(p)]))
  if (lastTone) {
    const changed = pods.filter((p) => lastTone![p.name] !== tones[p.name]).map((p) => p.name)
    if (changed.length) light(changed)
  }
  lastTone = partial && lastTone ? { ...lastTone, ...tones } : tones
}
function light(names: string[]) {
  flashing.value = new Set([...flashing.value, ...names])
  setTimeout(() => {
    const left = new Set(flashing.value)
    for (const n of names) left.delete(n)
    flashing.value = left
  }, 1600)
}

async function load() {
  try {
    const snapshot = await apiFetch<Snapshot>('/admin/cluster')
    if (!snapshot.disabled_reason) remember(snapshot)
    data.value = snapshot
    error.value = snapshot.error ?? ''
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

// ── reading a pod ───────────────────────────────────────────────────────────
const codeRoles = new Set(['scripts', 'script-trials', 'script-builds', 'containers', 'container-trials'])
function tone(p: Pod): Tone {
  if (p.state === 'Succeeded') return 'done'
  if (p.state === 'Running') return p.ready.startsWith('0/') ? 'wait' : 'ok'
  if (p.state === 'Pending' || p.state === 'ContainerCreating' || p.state === 'PodInitializing') return 'wait'
  return 'bad'
}
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
// a replica's own part of its name: the hash of its ReplicaSet and its own suffix
function shortName(p: Pod): string {
  const parts = p.name.split('-')
  return parts.length > 2 ? parts.slice(-2).join('-') : p.name
}
function linkOf(b: StartedBy): string {
  return b.kind === 'flow' ? `/editor?flow=${b.id}` : b.kind === 'script' ? `/script?id=${b.id}` : `/flow-group?id=${b.id}`
}
const roleLabel = (role: string) => t(`cluster.role.${role.replace(/-([a-z])/g, (_, c) => c.toUpperCase())}`)
const rowId = (p: Pod) => `pod-${p.name}`

// the last minute of CPU as a line, scaled to its own peak (or to the limit when there is one)
function spark(p: Pod): { line: string; area: string } | null {
  const h = cpuHistory.value[p.name] ?? []
  if (h.length < 2) return null
  const W = 72, H = 18
  const top = Math.max(p.cpu_limit_millis ?? 0, ...h, 1)
  const step = W / (HISTORY - 1)
  const x0 = W - step * (h.length - 1)
  const pts = h.map((v, i) => [x0 + i * step, H - 1 - (v / top) * (H - 3)])
  const line = pts.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(' ')
  return { line, area: `${x0.toFixed(1)},${H} ${line} ${W},${H}` }
}

// ── the snapshot, read three ways ───────────────────────────────────────────
const spaces = computed(() => data.value?.namespaces ?? [])
const allPods = computed(() => spaces.value.flatMap((s) => s.pods.map((p) => ({ pod: p, code: codeRoles.has(s.role) }))))

const health = computed(() => {
  const n = { ok: 0, wait: 0, bad: 0, done: 0 }
  for (const { pod } of allPods.value) n[tone(pod)]++
  const at_work = allPods.value.filter(({ pod, code }) => code && tone(pod) !== 'done').length
  return { ...n, total: allPods.value.length, at_work }
})
const healthLabel = computed(() => t('cluster.healthLabel', { ok: health.value.ok, wait: health.value.wait, bad: health.value.bad, done: health.value.done }))

const lanes = computed(() => {
  const byNode = new Map<string, { pod: Pod; code: boolean }[]>()
  for (const item of allPods.value) {
    const key = item.pod.node ?? ''
    if (!byNode.has(key)) byNode.set(key, [])
    byNode.get(key)!.push(item)
  }
  return [...byNode.entries()]
    .sort(([a], [b]) => (a === '' ? 1 : b === '' ? -1 : a.localeCompare(b)))
    .map(([node, pods]) => ({ node, pods: [...pods].sort((a, b) => Number(a.code) - Number(b.code) || a.pod.name.localeCompare(b.pod.name)) }))
})

interface Group { key: string; label: string | null; ready: string | null; pods: Pod[] }
const tables = computed(() => spaces.value
  .filter((s) => s.pods.length || s.error || !codeRoles.has(s.role))
  .map((s) => {
    const code = codeRoles.has(s.role)
    let groups: Group[]
    if (code) {
      groups = [{ key: s.name, label: null, ready: null, pods: [...s.pods].sort((a, b) => a.name.localeCompare(b.name)) }]
    } else {
      const byComponent = new Map<string, Pod[]>()
      for (const p of s.pods) {
        const c = p.component || p.name
        if (!byComponent.has(c)) byComponent.set(c, [])
        byComponent.get(c)!.push(p)
      }
      groups = [...byComponent.entries()].sort(([a], [b]) => a.localeCompare(b)).map(([c, pods]) => ({
        key: c, label: c, pods: pods.sort((a, b) => a.name.localeCompare(b.name)),
        ready: t('cluster.replicasReady', { ready: pods.filter((p) => tone(p) === 'ok').length, n: pods.length }),
      }))
    }
    return { space: s, code, groups }
  }))
const idleCode = computed(() => spaces.value.filter((s) => codeRoles.has(s.role) && !s.pods.length && !s.error))

async function goTo(p: Pod) {
  await nextTick()
  const row = document.getElementById(rowId(p))
  if (!row) return
  const still = window.matchMedia('(prefers-reduced-motion: reduce)').matches
  row.scrollIntoView({ behavior: still ? 'auto' : 'smooth', block: 'center' })
  light([p.name])
}
// from when the answer arrived, on this browser's clock: the server's own clock may differ
const arrivedAt = ref(0)
const updated = computed(() => {
  if (!data.value?.generated_at || !arrivedAt.value) return ''
  return t('cluster.updated', { s: Math.max(0, Math.round((now.value - arrivedAt.value) / 1000)) })
})
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

      <!-- every pod, by state -->
      <section class="health" :aria-label="healthLabel">
        <div class="hbar" role="img" :aria-label="healthLabel">
          <span v-for="k in (['ok', 'wait', 'bad', 'done'] as const)" v-show="health[k]" :key="k" class="seg" :class="k"
                :style="{ flexGrow: health[k] }" />
          <span v-if="!health.total" class="seg empty" />
        </div>
        <ul class="legend">
          <li v-for="k in (['ok', 'wait', 'bad', 'done'] as const)" :key="k" :class="[k, { none: !health[k] }]">
            <span class="key" /><strong>{{ health[k] }}</strong> {{ $t(`cluster.legend.${k}`) }}
          </li>
          <li class="atwork"><strong>{{ health.at_work }}</strong> {{ $t('cluster.legend.atWork') }}</li>
        </ul>
      </section>
      <p v-if="data.metrics === false" class="muted hint">{{ $t('cluster.noMetrics') }}</p>

      <!-- where they run -->
      <section v-if="lanes.length" class="map">
        <h2>{{ $t('cluster.mapTitle') }}</h2>
        <p class="muted hint">{{ $t('cluster.mapLead') }}</p>
        <div class="lanes">
          <div v-for="l in lanes" :key="l.node || '-'" class="lane" :class="{ unscheduled: !l.node }">
            <div class="lname">
              <code v-if="l.node">{{ l.node }}</code>
              <span v-else>{{ $t('cluster.unscheduled') }}</span>
              <span class="muted">{{ l.pods.length }}</span>
            </div>
            <div class="tiles">
              <button v-for="{ pod, code } in l.pods" :key="pod.name" type="button" class="tile"
                      :class="[tone(pod), { code, lit: flashing.has(pod.name) }]"
                      :title="`${pod.name} · ${pod.state} · CPU ${cpu(pod.cpu_millis)} · ${mem(pod.memory_bytes)}`"
                      :aria-label="$t('cluster.tileLabel', { name: pod.name, state: pod.state, cpu: cpu(pod.cpu_millis), mem: mem(pod.memory_bytes) })"
                      @click="goTo(pod)" />
            </div>
          </div>
        </div>
      </section>

      <!-- the pods -->
      <section v-for="tb in tables" :key="tb.space.name" class="space">
        <div class="sec-head">
          <h2>{{ roleLabel(tb.space.role) }} <code class="ns">{{ tb.space.name }}</code></h2>
          <span class="muted small">{{ $t('cluster.pods', { n: tb.space.pods.length }) }}</span>
        </div>
        <p v-if="tb.space.error" class="err small"><CircleAlert :size="13" /> {{ tb.space.error }}</p>
        <p v-else-if="!tb.space.pods.length" class="muted small">{{ $t('cluster.noPods') }}</p>
        <div v-else class="scroll">
          <table class="pods" :class="{ code: tb.code }">
            <thead>
              <tr>
                <th>{{ $t('cluster.col.state') }}</th>
                <th>{{ tb.code ? $t('cluster.col.startedBy') : $t('cluster.col.pod') }}</th>
                <th class="hide-s">{{ $t('cluster.col.node') }}</th>
                <th class="num">{{ $t('cluster.col.age') }}</th>
                <th class="num">{{ $t('cluster.col.restarts') }}</th>
                <th>{{ $t('cluster.col.cpu') }}</th>
                <th>{{ $t('cluster.memory') }}</th>
              </tr>
            </thead>
            <tbody v-for="g in tb.groups" :key="g.key">
              <tr v-if="g.label" class="grp">
                <th colspan="7" scope="rowgroup"><span>{{ g.label }}</span> <span class="muted">{{ g.ready }}</span></th>
              </tr>
              <tr v-for="p in g.pods" :id="rowId(p)" :key="p.name" class="pod" :class="[tone(p), { lit: flashing.has(p.name) }]" :title="p.name">
                <td class="state"><div class="cell"><span class="dot" /><span class="stxt">{{ p.state }}</span> <span class="ready muted">{{ p.ready }}</span></div></td>
                <td class="who">
                  <template v-if="tb.code">
                    <span v-if="p.started_by" class="by">{{ $t(`cluster.by.${p.started_by.kind}`) }}
                      <NuxtLink :to="linkOf(p.started_by)">{{ p.started_by.name }}</NuxtLink></span>
                    <span v-else class="by">{{ p.component || p.name }}</span>
                    <span class="sub muted">{{ [p.environment, p.run_id ? `run ${p.run_id}` : ''].filter(Boolean).join(' · ') }}</span>
                  </template>
                  <code v-else class="pname">{{ shortName(p) }}</code>
                </td>
                <td class="hide-s"><code class="nodename">{{ p.node || '—' }}</code></td>
                <td class="num age"><span class="lbl-s">{{ $t('cluster.col.age') }}</span> {{ age(p.started_at) }}</td>
                <td class="num rst" :class="{ restarts: p.restarts > 0 }"><span class="lbl-s">{{ $t('cluster.col.restarts') }}</span> {{ p.restarts }}</td>
                <td class="cpu"><div class="cell">
                  <svg v-if="spark(p)" class="spark" viewBox="0 0 72 18" width="72" height="18" aria-hidden="true">
                    <polygon :points="spark(p)!.area" class="area" />
                    <polyline :points="spark(p)!.line" class="line" />
                  </svg>
                  <span class="val">{{ cpu(p.cpu_millis) }}</span>
                </div></td>
                <td class="mem"><div class="cell">
                  <span v-if="p.memory_limit_bytes" class="track" :title="`${pct(p.memory_bytes, p.memory_limit_bytes) ?? 0}%`">
                    <span class="fill" :class="{ warm: (pct(p.memory_bytes, p.memory_limit_bytes) ?? 0) >= 70, hot: (pct(p.memory_bytes, p.memory_limit_bytes) ?? 0) >= 85 }"
                          :style="{ width: `${pct(p.memory_bytes, p.memory_limit_bytes) ?? 0}%` }" />
                  </span>
                  <span class="val">{{ mem(p.memory_bytes) }}<span v-if="p.memory_limit_bytes" class="muted"> / {{ mem(p.memory_limit_bytes) }}</span></span>
                </div></td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>
      <p v-if="idleCode.length" class="idle muted small">
        {{ $t('cluster.idleCode', { list: idleCode.map((s) => roleLabel(s.role)).join(', ') }) }}
      </p>

      <section class="space">
        <div class="sec-head"><h2>{{ $t('cluster.events') }}</h2></div>
        <p v-if="!data.events?.length" class="muted small">{{ $t('cluster.noEvents') }}</p>
        <div v-else class="scroll">
          <table class="data events">
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
.hint { margin: 4px 0 0; font-size: 12px; }
.small { font-size: 12px; }
.live { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: var(--accent-2); font-variant-numeric: tabular-nums; }
.live svg { animation: pulse 1.6s ease-in-out infinite; }
.live.paused { color: var(--muted); }
.live.paused svg { animation: none; }
@keyframes pulse { 50% { opacity: .35; } }
.off { display: flex; gap: 8px; align-items: flex-start; margin-top: 16px; color: var(--muted); max-width: 80ch; }
.err { display: flex; align-items: center; gap: 6px; color: var(--danger); font-size: 13px; }

/* the four states, one vocabulary for the bar, the tiles and the rows */
.ok { --tone: var(--accent-2); }
.wait { --tone: var(--warning); }
.bad { --tone: var(--danger); }
.done { --tone: var(--muted); }

/* ── every pod, by state ── */
.health { margin: 18px 0 0; display: flex; flex-direction: column; gap: 8px; }
.hbar { display: flex; gap: 2px; height: 8px; border-radius: 999px; overflow: hidden; background: var(--panel-2); }
.seg { flex: 1 1 0; min-width: 6px; background: var(--tone); transition: flex-grow .4s cubic-bezier(.16, 1, .3, 1); }
.seg.done { background: color-mix(in srgb, var(--muted) 45%, transparent); }
.seg.empty { background: transparent; }
.legend { display: flex; flex-wrap: wrap; gap: 4px 18px; margin: 0; padding: 0; list-style: none; font-size: 12.5px; color: var(--muted); }
.legend li { display: inline-flex; align-items: center; gap: 6px; }
.legend strong { color: var(--text); font-weight: 600; font-variant-numeric: tabular-nums; }
.legend li.bad:not(.none) strong { color: var(--danger); }
.legend li.wait:not(.none) strong { color: var(--warning); }
.legend .none { opacity: .7; }
.key { width: 8px; height: 8px; border-radius: 2px; background: var(--tone); }
.legend .done .key { background: color-mix(in srgb, var(--muted) 45%, transparent); }
.legend .atwork { margin-left: auto; }

/* ── where they run ── */
.map { margin-top: 26px; }
.map h2, .sec-head h2 { margin: 0; font-size: 15px; font-weight: 600; }
.lanes { margin-top: 10px; border: 1px solid var(--border); border-radius: 10px; background: var(--panel); overflow: hidden; }
.lane { display: grid; grid-template-columns: minmax(150px, 220px) minmax(0, 1fr); align-items: center; gap: 14px; padding: 9px 14px; }
.lane + .lane { border-top: 1px solid var(--border-soft); }
.lane.unscheduled { background: color-mix(in srgb, var(--warning) 7%, transparent); }
.lname { display: flex; align-items: baseline; justify-content: space-between; gap: 10px; min-width: 0; font-size: 12.5px; }
.lname code { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12px; }
.lname .muted { font-variant-numeric: tabular-nums; }
.tiles { display: flex; flex-wrap: wrap; gap: 4px; }
.tile {
  width: 16px; height: 16px; min-height: 0; padding: 0; border-radius: 3px; cursor: pointer;
  border: 1px solid color-mix(in srgb, var(--tone) 70%, var(--panel));
  background: color-mix(in srgb, var(--tone) 34%, var(--panel));
  transition: background-color .2s ease, border-color .2s ease, transform .15s ease;
}
.tile.code { border-radius: 50%; }
.tile.done { background: transparent; }
.tile.bad { background: var(--danger); }
.tile:hover { transform: scale(1.18); box-shadow: var(--shadow-1); border-color: var(--tone); }
.tile:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.tile.lit { animation: tile-lit 1.6s cubic-bezier(.16, 1, .3, 1); }
@keyframes tile-lit { from { box-shadow: 0 0 0 4px color-mix(in srgb, var(--tone) 45%, transparent); } }

/* ── the pods ── */
.space { margin-top: 26px; }
.sec-head { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; margin-bottom: 8px; }
.sec-head h2 { display: flex; align-items: baseline; gap: 8px; }
.ns { font-size: 11.5px; color: var(--muted); font-weight: 400; }
.scroll { overflow-x: auto; }
table.pods { width: 100%; border-collapse: separate; border-spacing: 0; border: 1px solid var(--border); border-radius: 10px; background: var(--panel); font-size: 12.5px; overflow: hidden; }
.pods thead th {
  background: var(--panel-2); color: var(--muted); font-size: 11px; font-weight: 600; text-transform: uppercase;
  letter-spacing: 0.05em; text-align: left; padding: 7px 12px; white-space: nowrap; border-bottom: 1px solid var(--border);
}
.pods td { padding: 7px 12px; border-top: 1px solid var(--border-soft); vertical-align: middle; white-space: nowrap; }
.pods tbody:first-of-type tr:first-child td { border-top: 0; }
.pods tr.grp th {
  text-align: left; padding: 10px 12px 4px; font-size: 12.5px; font-weight: 600; color: var(--text);
  border-top: 1px solid var(--border-soft); background: var(--panel);
}
.pods tbody:first-of-type tr.grp th { border-top: 0; }
.pods tr.grp + tr td { border-top: 0; }
.pods tr.grp .muted { font-weight: 400; margin-left: 6px; }
.pods tr.pod { transition: background-color .2s ease; }
.pods tr.pod:hover td { background: var(--panel-2); }
.pods tr.pod.lit td { animation: row-lit 1.6s cubic-bezier(.16, 1, .3, 1); }
@keyframes row-lit { from { background: color-mix(in srgb, var(--tone) 22%, var(--panel)); } }
.pods tr.done { color: var(--muted); }
.num { text-align: right; font-variant-numeric: tabular-nums; }
th.num { text-align: right; }
.cell { display: flex; align-items: center; gap: 8px; }
.state .cell { gap: 7px; }
.lbl-s { display: none; }
.dot { width: 8px; height: 8px; border-radius: 50%; background: var(--tone); flex: none; }
.done .dot { background: transparent; box-shadow: inset 0 0 0 1.5px var(--muted); }
.stxt { font-weight: 500; }
.bad .stxt { color: var(--danger); }
.wait .stxt { color: var(--warning); }
.ready { font-size: 11.5px; font-variant-numeric: tabular-nums; }
.pname, .nodename { font-size: 11.5px; }
.who { max-width: 360px; overflow: hidden; text-overflow: ellipsis; }
.who .by { font-weight: 500; }
.who .sub { margin-left: 8px; font-size: 11.5px; }
.restarts { color: var(--warning); font-weight: 600; }
.cpu, .mem { min-width: 150px; }
.spark { flex: none; display: block; }
.spark .line { fill: none; stroke: var(--accent); stroke-width: 1.4; stroke-linejoin: round; stroke-linecap: round; }
.spark .area { fill: color-mix(in srgb, var(--accent) 16%, transparent); stroke: none; }
.val { font-variant-numeric: tabular-nums; }
.track { flex: none; width: 72px; height: 6px; border-radius: 999px; background: var(--panel-2); overflow: hidden; }
.fill { display: block; height: 100%; background: var(--accent); border-radius: 999px; transition: width .6s cubic-bezier(.16, 1, .3, 1); }
.fill.warm { background: var(--warning); }
.fill.hot { background: var(--danger); }
.idle { margin: 14px 0 0; }

table.data { width: 100%; }
.nowrap { white-space: nowrap; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11.5px; }
.reason { font-weight: 600; white-space: nowrap; }
.msg { font-size: 12px; max-width: 70ch; }

@media (max-width: 760px) {
  .lane { grid-template-columns: 1fr; gap: 6px; }
  .hide-s { display: none; }
  .legend .atwork { margin-left: 0; }
  /* a pod per block: state and name, then age and restarts, then CPU and memory */
  .scroll { overflow-x: visible; }
  table.pods, .pods tbody { display: block; }
  .pods thead { display: none; }
  .pods tr.grp { display: block; }
  .pods tr.grp th { display: block; }
  .pods tr.pod {
    display: grid; grid-template-columns: minmax(0, 1fr) auto auto; column-gap: 14px; row-gap: 4px;
    padding: 9px 12px; border-top: 1px solid var(--border-soft);
  }
  .pods tr.grp + tr.pod { border-top: 0; }
  .pods td { display: block; padding: 0; border: 0; min-width: 0; }
  .pods td.hide-s { display: none; }
  .pods td.state { grid-column: 1 / -1; }
  .pods td.who { grid-column: 1; grid-row: 2; max-width: none; }
  .pods td.age { grid-column: 2; grid-row: 2; }
  .pods td.rst { grid-column: 3; grid-row: 2; }
  .pods td.cpu, .pods td.mem { grid-column: 1 / -1; min-width: 0; }
  .lbl-s { display: inline; color: var(--muted); font-size: 11px; margin-right: 3px; }
  .pods tr.pod.lit td { animation: none; }
  .pods tr.pod.lit { animation: row-lit 1.6s cubic-bezier(.16, 1, .3, 1); }
  /* an event per block too: when and why, then where, then what it says (a message of a
     few hundred characters made the whole page as wide as itself) */
  table.events, .events tbody { display: block; }
  .events thead { display: none; }
  .events tr {
    display: grid; grid-template-columns: auto minmax(0, 1fr); column-gap: 12px; row-gap: 3px;
    padding: 9px 0; border-top: 1px solid var(--border-soft);
  }
  /* the app's tables keep a cell on one line (white-space: nowrap): here a cell wraps */
  .events td, .events td code { display: block; padding: 0; border: 0; min-width: 0; white-space: normal; overflow-wrap: anywhere; }
  .events td code { display: inline; }
  .events td.msg { grid-column: 1 / -1; max-width: none; }
}
@media (prefers-reduced-motion: reduce) {
  .live svg, .tile.lit, .pods tr.pod.lit td { animation: none; }
  .seg, .tile, .fill { transition: none; }
}
</style>
