<script setup lang="ts">
// Workers (Admin panel): who runs flows, imports and previews, how busy they are,
// and how big. The processes per worker are engine settings: every worker resizes
// its pool within seconds, growing at once and shrinking only by idle processes.
// On Kubernetes the minimum and maximum pods of each kind go to their autoscaler
// (only with adminScaling in the chart); on compose the panel says how to scale.
// The observer sees everything and changes nothing.
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { Cpu, Eye, LoaderCircle, Play, RotateCcw, Server } from 'lucide-vue-next'
import { errMessage } from '~/composables/useApi'
import { useFormat } from '~/composables/useFormat'
import { useEngineSettings } from '~/composables/useEngineSettings'
import {
  useWorkers, type ScalingError, type WorkerGroup, type WorkerKind, type WorkersState, type WorkerState,
} from '~/composables/useWorkers'

const emit = defineEmits<{ (e: 'count', n: number): void }>()

const { t } = useI18n()
const fmt = useFormat()
const api = useWorkers()
const settingsApi = useEngineSettings()
const toast = useToast()

const REFRESH_MS = 5000
const ICONS: Record<WorkerKind, any> = { run: Play, preview: Eye }
const SERVICE: Record<WorkerKind, string> = { run: 'worker', preview: 'preview-worker' }

const state = ref<WorkersState | null>(null)
const error = ref('')
const loading = ref(true)
const stale = ref(false) // the last refresh failed: what is shown is older
const saving = ref('') // "<kind>-processes" | "<kind>-pods" while it saves
// what is typed in the fields, per kind: a string, a half-typed field is not a number
const processes = ref<Record<string, string>>({})
const podsMin = ref<Record<string, string>>({})
const podsMax = ref<Record<string, string>>({})
const refusals = ref<Record<string, ScalingError>>({})
let timer: ReturnType<typeof setInterval> | null = null

async function load(quiet = false) {
  if (!quiet) loading.value = true
  try {
    state.value = await api.get()
    error.value = ''
    stale.value = false
    emit('count', state.value.groups.reduce((n, g) => n + g.workers.length, 0))
  } catch (e) {
    if (quiet && state.value) stale.value = true
    else error.value = errMessage(e)
  } finally {
    loading.value = false
  }
}
onMounted(() => {
  load()
  timer = setInterval(() => load(true), REFRESH_MS)
})
onBeforeUnmount(() => {
  if (timer) clearInterval(timer)
})

// ── formats ─────────────────────────────────────────────────────────────────

function bytes(n: number | null | undefined): string {
  if (n == null) return '—'
  return n >= 1024 ** 3 ? `${fmt.number(n / 1024 ** 3, 1)} GiB` : `${fmt.count(Math.round(n / 1024 ** 2))} MiB`
}
const since = (w: WorkerState) => (w.started_at ? fmt.ago(w.started_at * 1000) : '—')
const pct = (part: number | null, whole: number | null) =>
  part != null && whole ? Math.min(100, Math.round((100 * part) / whole)) : 0

// ── processes per worker ────────────────────────────────────────────────────

const totals = (g: WorkerGroup) => ({
  processes: g.workers.reduce((n, w) => n + (w.processes ?? 0), 0),
  busy: g.workers.reduce((n, w) => n + (w.busy ?? 0), 0),
})
// the size the workers started with (each container has its own environment)
function startSize(g: WorkerGroup): string {
  const sizes = [...new Set(g.workers.map((w) => w.started_with).filter((x): x is number => x != null))].sort((a, b) => a - b)
  if (!sizes.length) return '—'
  return sizes.length === 1 ? String(sizes[0]) : `${sizes[0]}–${sizes[sizes.length - 1]}`
}
const current = (g: WorkerGroup) =>
  g.concurrency.value ?? g.workers.find((w) => w.started_with != null)?.started_with ?? g.concurrency.min
const draft = (g: WorkerGroup) => processes.value[g.kind] ?? String(current(g))
function typedProcesses(g: WorkerGroup): number | null {
  const s = (processes.value[g.kind] ?? '').trim()
  return /^\d+$/.test(s) ? Number(s) : null
}
function processesProblem(g: WorkerGroup): string {
  if (!(g.kind in processes.value)) return ''
  const n = typedProcesses(g)
  return n === null || n < g.concurrency.min || n > g.concurrency.max
    ? t('workers.processes.invalid', { min: g.concurrency.min, max: g.concurrency.max })
    : ''
}
const processesChanged = (g: WorkerGroup) => {
  const n = typedProcesses(g)
  return n !== null && !processesProblem(g) && n !== current(g)
}
// a worker still moving to its size: it shrinks as its busy processes finish
const adjusting = (g: WorkerGroup) => g.workers.some((w) => w.target != null && w.processes != null && w.target !== w.processes)

// the memory each process would get with `n` of them, in the tightest container
function sharePerProcess(g: WorkerGroup, n: number): { share: number, floor: number } | null {
  const shares = g.workers
    .filter((w) => w.memory_limit && w.memory_share)
    .map((w) => ({ share: (w.memory_limit! * w.memory_share!) / n, floor: w.memory_floor ?? 0 }))
  if (!shares.length) return null
  return shares.reduce((a, b) => (b.share < a.share ? b : a))
}
function memoryNote(g: WorkerGroup): { text: string, low: boolean } | null {
  const n = typedProcesses(g) ?? current(g)
  const s = sharePerProcess(g, Math.max(1, n))
  if (!s) return null
  if (s.floor && s.share < s.floor) {
    return { text: t('workers.processes.lowMemory', { n, share: bytes(s.share), floor: bytes(s.floor) }), low: true }
  }
  return { text: t('workers.processes.share', { share: bytes(s.share) }), low: false }
}

async function saveProcesses(g: WorkerGroup, value: number | null) {
  saving.value = `${g.kind}-processes`
  try {
    await settingsApi.save({ [g.concurrency.key]: value })
    const { [g.kind]: _gone, ...rest } = processes.value
    processes.value = rest
    toast.success(t('workers.processes.saved', { seconds: state.value?.tick_seconds ?? 10 }))
    await load(true)
  } catch (e) {
    toast.error(errMessage(e))
  } finally {
    saving.value = ''
  }
}

// ── pods (Kubernetes) ───────────────────────────────────────────────────────

const minDraft = (g: WorkerGroup) => podsMin.value[g.kind] ?? String(g.scaling.min ?? '')
const maxDraft = (g: WorkerGroup) => podsMax.value[g.kind] ?? String(g.scaling.max ?? '')
const asInt = (s: string) => (/^\d+$/.test(s.trim()) ? Number(s.trim()) : null)

function podsProblem(g: WorkerGroup): string {
  const s = g.scaling
  if (!(g.kind in podsMin.value) && !(g.kind in podsMax.value)) {
    const r = refusals.value[g.kind]
    return r ? scalingError(r) : ''
  }
  const low = asInt(minDraft(g))
  const hi = asInt(maxDraft(g))
  if (low === null || hi === null) return t('workers.pods.invalid')
  if (low < (s.floor ?? 1)) return scalingError({ code: 'min', limit: s.floor ?? 1 })
  if (hi < Math.max(low, 1)) return scalingError({ code: 'order', limit: Math.max(low, 1) })
  if (s.ceiling != null && hi > s.ceiling) return scalingError({ code: 'ceiling', limit: s.ceiling })
  return ''
}
const scalingError = (e: ScalingError) => t(`workers.pods.errors.${e.code}`, { limit: e.limit ?? '' })
const podsChanged = (g: WorkerGroup) => {
  const low = asInt(minDraft(g))
  const hi = asInt(maxDraft(g))
  return low !== null && hi !== null && (low !== g.scaling.min || hi !== g.scaling.max)
}

function forgetPods(kind: WorkerKind) {
  const { [kind]: _a, ...mins } = podsMin.value
  const { [kind]: _b, ...maxs } = podsMax.value
  const { [kind]: _c, ...refs } = refusals.value
  podsMin.value = mins
  podsMax.value = maxs
  refusals.value = refs
}

async function savePods(g: WorkerGroup) {
  const low = asInt(minDraft(g))
  const hi = asInt(maxDraft(g))
  if (low === null || hi === null || podsProblem(g)) return
  saving.value = `${g.kind}-pods`
  try {
    state.value = await api.scale(g.kind, low, hi)
    forgetPods(g.kind)
    toast.success(t('workers.pods.saved'))
  } catch (e: any) {
    const errors: ScalingError[] | undefined = e?.data?.detail?.errors
    if (errors?.length) refusals.value = { ...refusals.value, [g.kind]: errors[0] }
    toast.error(errors?.length ? scalingError(errors[0]) : errMessage(e))
  } finally {
    saving.value = ''
  }
}

async function releasePods(g: WorkerGroup) {
  saving.value = `${g.kind}-pods`
  try {
    state.value = await api.release(g.kind)
    forgetPods(g.kind)
    toast.success(t('workers.pods.released'))
  } catch (e) {
    toast.error(errMessage(e))
  } finally {
    saving.value = ''
  }
}

const autoscalerLabel = (g: WorkerGroup) =>
  t(g.scaling.autoscaler === 'scaledobject' ? 'workers.pods.keda' : 'workers.pods.hpa', { queue: g.queue })
const groups = computed(() => state.value?.groups ?? [])
</script>

<template>
  <div class="card">
    <h4><Cpu :size="14" /> {{ $t('workers.title') }}</h4>
    <p class="hint muted small">{{ $t('workers.lead', { seconds: state?.tick_seconds ?? 10 }) }}</p>
    <p v-if="state && !state.panel_enabled" class="note">{{ $t('workers.lockedByEnv') }}</p>
    <p v-else-if="state && !state.concurrency_editable" class="note">{{ $t('workers.readOnly') }}</p>
    <p v-if="state && !state.workers_known" class="note" role="status">{{ $t('workers.unknown') }}</p>
    <p v-if="stale" class="note" role="status">{{ $t('workers.stale') }}</p>
  </div>

  <p v-if="loading" class="muted small"><LoaderCircle :size="13" class="spin" /> {{ $t('workers.loading') }}</p>
  <p v-else-if="error" class="card page-error" role="alert">{{ error }}</p>

  <template v-else-if="state">
    <section v-for="g in groups" :key="g.kind" class="card" :aria-labelledby="`workers-${g.kind}`">
      <div class="head">
        <h4 :id="`workers-${g.kind}`"><component :is="ICONS[g.kind]" :size="14" /> {{ $t(`workers.groups.${g.kind}.title`) }}</h4>
        <div class="figures">
          <span class="figure"><strong>{{ g.queued ?? '—' }}</strong> {{ $t('workers.queued', g.queued ?? 0) }}</span>
          <span class="figure"><strong>{{ g.workers.length }}</strong> {{ $t('workers.live', g.workers.length) }}</span>
          <span class="figure">
            <strong>{{ totals(g).busy }}/{{ totals(g).processes }}</strong> {{ $t('workers.busy', totals(g).processes) }}
          </span>
        </div>
      </div>
      <p class="hint muted small">{{ $t(`workers.groups.${g.kind}.lead`, { queue: g.queue }) }}</p>

      <!-- processes per worker -->
      <div class="item">
        <div class="description">
          <label :for="`processes-${g.kind}`" class="name">{{ $t('workers.processes.label') }}</label>
          <p class="help">{{ $t('workers.processes.help') }}</p>
          <p class="meta">
            {{ $t('workers.processes.start', { value: startSize(g) }) }} <code>CELERY__WORKER_CONCURRENCY</code>
            <template v-if="g.concurrency.value !== null && g.concurrency.updated_by">
              · {{ $t('workers.setBy', { who: g.concurrency.updated_by, when: fmt.ago(g.concurrency.updated_at) }) }}
            </template>
          </p>
          <p v-if="memoryNote(g)" class="meta" :class="{ warning: memoryNote(g)!.low }" :role="memoryNote(g)!.low ? 'alert' : undefined">
            {{ memoryNote(g)!.text }}
          </p>
        </div>
        <div class="value">
          <div class="controls">
            <div class="field" :class="{ changed: processesChanged(g), invalid: !!processesProblem(g) }">
              <input
                :id="`processes-${g.kind}`"
                type="text"
                inputmode="numeric"
                autocomplete="off"
                :value="draft(g)"
                :disabled="!state.concurrency_editable || !!saving"
                :aria-invalid="!!processesProblem(g)"
                :aria-describedby="`processes-${g.kind}-intervallo`"
                @input="processes = { ...processes, [g.kind]: ($event.target as HTMLInputElement).value }"
                @keydown.enter="processesChanged(g) && saveProcesses(g, typedProcesses(g))"
              />
              <span class="unit">{{ $t('workers.processes.unit') }}</span>
            </div>
            <button
              v-if="state.concurrency_editable"
              type="button"
              class="primary"
              :disabled="!processesChanged(g) || !!saving"
              @click="saveProcesses(g, typedProcesses(g))"
            >
              <LoaderCircle v-if="saving === `${g.kind}-processes`" :size="13" class="spin" /> {{ $t('workers.apply') }}
            </button>
          </div>
          <p :id="`processes-${g.kind}-intervallo`" class="range">
            {{ $t('workers.range', { min: g.concurrency.min, max: g.concurrency.max }) }}
          </p>
          <p v-if="processesProblem(g)" class="error" role="alert">{{ processesProblem(g) }}</p>
          <div class="status">
            <span v-if="g.concurrency.value !== null" class="chip from-panel">{{ $t('workers.fromPanel') }}</span>
            <span v-if="adjusting(g)" class="chip adjusting" :title="$t('workers.processes.adjustingHint')">{{ $t('workers.processes.adjusting') }}</span>
            <button
              v-if="state.concurrency_editable && g.concurrency.value !== null"
              type="button"
              class="back"
              :disabled="!!saving"
              @click="saveProcesses(g, null)"
            >
              <RotateCcw :size="12" /> {{ $t('workers.processes.reset') }}
            </button>
          </div>
        </div>
      </div>

      <!-- pods -->
      <div class="item">
        <div class="description">
          <span class="name"><Server :size="13" class="icon" /> {{ $t('workers.pods.title') }}</span>
          <template v-if="g.scaling.mode === 'none'">
            <p class="help">{{ $t('workers.pods.compose') }}</p>
            <p class="meta"><code>docker compose up -d --scale {{ SERVICE[g.kind] }}=N</code></p>
          </template>
          <template v-else>
            <p v-if="g.scaling.error" class="help warning" role="alert">{{ g.scaling.error }}</p>
            <p v-else-if="!g.scaling.autoscaler" class="help">{{ $t('workers.pods.noAutoscaler', { replicas: g.scaling.replicas ?? '—' }) }}</p>
            <template v-else>
              <p class="help">{{ autoscalerLabel(g) }} · {{ $t('workers.pods.help') }}</p>
              <p class="meta">
                {{ $t('workers.pods.status', { ready: g.scaling.ready ?? 0, current: g.scaling.current ?? 0 }) }}
                <template v-if="g.scaling.desired != null"> · {{ $t('workers.pods.desired', { n: g.scaling.desired }) }}</template>
                <template v-if="g.scaling.saved && g.scaling.updated_by">
                  · {{ $t('workers.setBy', { who: g.scaling.updated_by, when: fmt.ago(g.scaling.updated_at) }) }}
                </template>
              </p>
            </template>
          </template>
        </div>
        <div v-if="g.scaling.mode === 'kubernetes' && g.scaling.autoscaler && !g.scaling.error" class="value">
          <div class="controls">
            <div class="field narrow" :class="{ invalid: !!podsProblem(g) }">
              <input
                :id="`pod-min-${g.kind}`"
                type="text"
                inputmode="numeric"
                autocomplete="off"
                :value="minDraft(g)"
                :disabled="!state.scaling_editable || !!saving"
                :aria-label="$t('workers.pods.min')"
                :aria-invalid="!!podsProblem(g)"
                @input="podsMin = { ...podsMin, [g.kind]: ($event.target as HTMLInputElement).value }"
              />
              <span class="unit">{{ $t('workers.pods.minShort') }}</span>
            </div>
            <div class="field narrow" :class="{ invalid: !!podsProblem(g) }">
              <input
                :id="`pod-max-${g.kind}`"
                type="text"
                inputmode="numeric"
                autocomplete="off"
                :value="maxDraft(g)"
                :disabled="!state.scaling_editable || !!saving"
                :aria-label="$t('workers.pods.max')"
                :aria-invalid="!!podsProblem(g)"
                @input="podsMax = { ...podsMax, [g.kind]: ($event.target as HTMLInputElement).value }"
                @keydown.enter="podsChanged(g) && savePods(g)"
              />
              <span class="unit">{{ $t('workers.pods.maxShort') }}</span>
            </div>
            <button
              v-if="state.scaling_editable"
              type="button"
              class="primary"
              :disabled="!podsChanged(g) || !!podsProblem(g) || !!saving"
              @click="savePods(g)"
            >
              <LoaderCircle v-if="saving === `${g.kind}-pods`" :size="13" class="spin" /> {{ $t('workers.apply') }}
            </button>
          </div>
          <p class="range">{{ $t('workers.pods.range', { floor: g.scaling.floor ?? 1, ceiling: g.scaling.ceiling ?? '—' }) }}</p>
          <p v-if="podsProblem(g)" class="error" role="alert">{{ podsProblem(g) }}</p>
          <div class="status">
            <span v-if="g.scaling.saved" class="chip from-panel" :title="$t('workers.pods.keptHint')">{{ $t('workers.fromPanel') }}</span>
            <button
              v-if="state.scaling_editable && g.scaling.saved"
              type="button"
              class="back"
              :disabled="!!saving"
              :title="$t('workers.pods.releaseHint')"
              @click="releasePods(g)"
            >
              <RotateCcw :size="12" /> {{ $t('workers.pods.release') }}
            </button>
          </div>
        </div>
      </div>

      <!-- the workers alive -->
      <div class="tablewrap">
        <table class="rows">
          <thead>
            <tr>
              <th scope="col">{{ $t('workers.table.name') }}</th>
              <th scope="col">{{ $t('workers.table.processes') }}</th>
              <th scope="col" class="num">{{ $t('workers.table.waiting') }}</th>
              <th scope="col">{{ $t('workers.table.memory') }}</th>
              <th scope="col">{{ $t('workers.table.since') }}</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="w in g.workers" :key="w.name">
              <td class="mono">{{ w.name }}</td>
              <td>
                <div class="measure">
                  <span class="meter" aria-hidden="true"><span :style="{ width: `${pct(w.busy, w.processes)}%` }" /></span>
                  {{ $t('workers.table.busyOf', { busy: w.busy ?? 0, total: w.processes ?? 0 }) }}
                  <span v-if="w.target != null && w.target !== w.processes" class="muted">→ {{ w.target }}</span>
                </div>
              </td>
              <td class="num">{{ w.waiting ?? 0 }}</td>
              <td>
                <div class="measure">
                  <span class="meter" :class="{ high: pct(w.memory_used, w.memory_limit) >= 85 }" aria-hidden="true">
                    <span :style="{ width: `${pct(w.memory_used, w.memory_limit)}%` }" />
                  </span>
                  {{ w.memory_limit ? $t('workers.table.memoryOf', { used: bytes(w.memory_used), limit: bytes(w.memory_limit) }) : bytes(w.memory_used) }}
                </div>
              </td>
              <td class="muted">{{ since(w) }}</td>
            </tr>
            <tr v-if="!g.workers.length">
              <td colspan="5" class="muted empty">{{ $t('workers.table.empty') }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  </template>
</template>

<style scoped>
.card { display: flex; flex-direction: column; gap: 10px; padding: 16px; background: var(--panel); border: 1px solid var(--border); border-radius: var(--radius); }
.card h4 { margin: 0; display: inline-flex; align-items: center; gap: 7px; }
.hint { margin: 0; line-height: 1.5; }
.small { font-size: 12px; }
.note { margin: 0; font-size: 12.5px; padding: 8px 10px; border-radius: 8px; background: var(--bg-soft); border: 1px solid var(--border); }
.page-error { color: var(--danger); font-size: 13px; }

.head { display: flex; flex-wrap: wrap; align-items: baseline; justify-content: space-between; gap: 8px 16px; }
.figures { display: flex; flex-wrap: wrap; gap: 6px 16px; font-size: 12.5px; color: var(--muted); }
.figure strong { color: var(--text); font-variant-numeric: tabular-nums; font-weight: 600; }

.item {
  display: grid; grid-template-columns: minmax(0, 1fr) minmax(240px, 320px); gap: 8px 24px;
  padding: 14px 0 4px; border-top: 1px solid var(--border-soft);
}
.description { min-width: 0; }
.name { display: inline-flex; align-items: center; gap: 6px; font-size: 13.5px; font-weight: 600; color: var(--text); }
.help { margin: 4px 0 0; font-size: 12.5px; line-height: 1.55; color: var(--muted); max-width: 72ch; }
.meta { margin: 6px 0 0; font-size: 11.5px; color: var(--muted); }
.meta code { font-size: 11px; padding: 1px 5px; border-radius: 5px; background: var(--bg-soft); border: 1px solid var(--border-soft); }
.warning { color: var(--danger); }

.value { display: flex; flex-direction: column; gap: 4px; }
.controls { display: flex; gap: 8px; align-items: stretch; }
.controls .primary { white-space: nowrap; }
.field {
  flex: 1; display: flex; align-items: stretch; border: 1px solid var(--control-border); border-radius: 8px;
  background: var(--bg-soft); overflow: hidden; min-width: 0;
}
.field:focus-within { border-color: var(--accent); box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 22%, transparent); }
.field.changed { border-color: color-mix(in srgb, var(--accent) 60%, var(--control-border)); }
.field.invalid { border-color: var(--danger); }
.field input {
  flex: 1; min-width: 0; border: 0; background: transparent; box-shadow: none; padding: 7px 9px;
  font-variant-numeric: tabular-nums; text-align: right;
}
.field input:focus { outline: none; box-shadow: none; }
.unit { display: inline-flex; align-items: center; padding: 0 10px; font-size: 12px; color: var(--muted); border-left: 1px solid var(--border-soft); white-space: nowrap; }
.range { margin: 0; font-size: 11.5px; color: var(--muted); }
.error { margin: 0; font-size: 12px; color: var(--danger); }
.status { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; min-height: 22px; }
.chip { font-size: 10.5px; letter-spacing: 0.02em; border-radius: 999px; padding: 1px 8px; border: 1px solid var(--border); background: var(--panel-2); color: var(--muted); }
.chip.from-panel { color: var(--accent-hi); border-color: color-mix(in srgb, var(--accent) 40%, var(--border)); background: var(--tint-accent); }
.chip.adjusting { color: var(--text); border-style: dashed; }
.back { display: inline-flex; align-items: center; gap: 4px; padding: 0 2px; min-height: 22px; border: 0; background: none; font-size: 12px; color: var(--muted); cursor: pointer; }
.back:hover:not(:disabled) { color: var(--accent); box-shadow: none; }

.tablewrap { max-height: 320px; overflow-y: auto; border-top: 1px solid var(--border-soft); padding-top: 6px; }
table.rows { width: 100%; border-collapse: collapse; font-size: 12.5px; }
table.rows th { text-align: left; font-weight: 600; font-size: 11.5px; color: var(--muted); padding: 6px 4px; border-bottom: 1px solid var(--border); position: sticky; top: 0; background: var(--panel); }
table.rows td { padding: 6px 4px; border-bottom: 1px solid var(--border-soft); vertical-align: middle; }
table.rows tr:last-child td { border-bottom: none; }
.num, table.rows th.num { text-align: right; font-variant-numeric: tabular-nums; }
.mono { font-family: var(--font-mono, ui-monospace, monospace); font-size: 12px; }
.empty { text-align: center; padding: 14px 4px; }
.measure { display: flex; align-items: center; gap: 8px; font-variant-numeric: tabular-nums; white-space: nowrap; }
.meter { position: relative; flex: none; width: 64px; height: 6px; border-radius: 999px; background: var(--bg-soft); border: 1px solid var(--border-soft); overflow: hidden; }
.meter > span { position: absolute; inset: 0 auto 0 0; background: var(--accent); border-radius: 999px; }
.meter.high > span { background: var(--danger); }

@media (max-width: 720px) {
  .item { grid-template-columns: 1fr; }
  .head { flex-direction: column; }
}
</style>
