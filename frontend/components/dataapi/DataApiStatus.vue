<script setup lang="ts">
// How a data API's pods are (docs/specs/data-apis.md, «The object»): what the cluster says (the
// pods wanted and ready, the disk each has, what it refused), each pod as the cluster sees it and
// as it reports itself (ready, preparing the data, in error; the data version each resource
// serves), whether the latest data of each datasource has reached the pods, and the traffic of
// the last hour, day or week. Asked again every few seconds while the page is in view.
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { Activity, CircleAlert, Server } from 'lucide-vue-next'
import { errMessage } from '~/composables/useApi'
import { useFormat } from '~/composables/useFormat'
import { dataApiOrigin, useDataApis, type DataApiPod, type DataApiState, type DataApiStatus } from '~/composables/useDataApis'

const props = defineProps<{ apiId: number; state: DataApiState }>()
const { t } = useI18n()
const fmtx = useFormat()
const api = useDataApis()

const status = ref<DataApiStatus | null>(null)
const failed = ref('')
const hours = ref(24)
let timer: ReturnType<typeof setTimeout> | null = null
let alive = true

async function load() {
  try {
    status.value = await api.status(props.apiId, hours.value)
    failed.value = ''
  } catch (e) {
    failed.value = errMessage(e)
  }
}

// often while it runs (the pods change in seconds), seldom otherwise
function schedule() {
  if (timer) clearTimeout(timer)
  if (!alive) return
  const every = props.state === 'active' ? 5000 : 30000
  timer = setTimeout(async () => {
    if (document.visibilityState === 'visible') await load()
    schedule()
  }, every)
}
function onVisible() {
  if (document.visibilityState === 'visible') load()
}
onMounted(() => {
  load()
  schedule()
  document.addEventListener('visibilitychange', onVisible)
})
onUnmounted(() => {
  alive = false
  if (timer) clearTimeout(timer)
  document.removeEventListener('visibilitychange', onVisible)
})
watch(() => [props.apiId, props.state], () => { load(); schedule() })
watch(hours, load)

const pods = computed<DataApiPod[]>(() => status.value?.pods ?? [])
const summary = computed(() => {
  const s = status.value
  if (!s) return ''
  if (props.state === 'draft') return t('dataApi.stDraft')
  if (props.state === 'paused') return s.wanted || pods.value.length ? t('dataApi.stPausing') : t('dataApi.stPaused')
  if (!s.checked_at) return t('dataApi.stWaiting')
  return t('dataApi.stReady', { ready: s.ready, wanted: s.wanted })
})
const tone = computed(() => {
  const s = status.value
  if (!s || props.state !== 'active') return 'muted'
  if (s.error || pods.value.some((p) => p.condition === 'error')) return 'bad'
  return s.checked_at && s.wanted > 0 && s.ready >= s.wanted ? 'good' : 'wait'
})

/** The address the other systems call: the public address, the published host, or the page's own. */
const address = computed(() => {
  const s = status.value
  if (!s) return ''
  return dataApiOrigin(s.base_url ?? '', s.host, location) + s.path
})

const short = (name: string) => name.replace(/^.*-data-api-\d+-/, '…')
const condLabel = (p: DataApiPod) => t(`dataApi.cond_${p.condition}`)
/** What is wrong with a pod, in the cluster's words or its own. */
function trouble(p: DataApiPod): string {
  const cluster = [p.reason, p.message].filter(Boolean).join(': ')
  return [cluster, p.error].filter(Boolean).join(' · ')
}

const spans = [1, 24, 168]
const points = computed(() => status.value?.traffic.points ?? [])
const peak = computed(() => Math.max(1, ...points.value.map((p) => p.requests)))
/** One bar per bucket of the span, the empty ones too, oldest first. */
const bars = computed(() => {
  const s = status.value
  if (!s) return []
  const step = s.traffic.bucket_minutes * 60_000
  const n = Math.round((s.traffic.hours * 60) / s.traffic.bucket_minutes)
  const end = Math.floor(Date.now() / step) * step
  const by = new Map(points.value.map((p) => [Math.floor(new Date(p.at).getTime() / step) * step, p]))
  return Array.from({ length: n }, (_, i) => {
    const at = end - (n - 1 - i) * step
    const p = by.get(at)
    return { at, requests: p?.requests ?? 0, errors: (p?.errors ?? 0) + (p?.throttled ?? 0) }
  })
})
const barWidth = computed(() => 100 / Math.max(bars.value.length, 1))
</script>

<template>
  <div class="card st" :aria-label="$t('dataApi.stTitle')">
    <h2><Server :size="15" /> {{ $t('dataApi.stTitle') }}
      <span v-if="status" class="sum" :class="tone" role="status">{{ summary }}</span>
    </h2>
    <p v-if="failed" class="err small" role="alert">{{ failed }}</p>
    <template v-if="status">
      <p v-if="status.error && state !== 'draft'" class="note warn small" role="alert">
        <CircleAlert :size="14" /> <span>{{ $t('dataApi.stClusterSays') }} <span class="mono">{{ status.error }}</span></span>
      </p>
      <dl v-if="state !== 'draft'" class="facts small">
        <div><dt>{{ $t('dataApi.stAddress') }}</dt><dd class="mono">{{ address }}</dd></div>
        <div v-if="status.disk"><dt>{{ $t('dataApi.stDisk') }}</dt><dd>{{ status.disk }}</dd></div>
        <div v-if="status.checked_at"><dt>{{ $t('dataApi.stChecked') }}</dt><dd>{{ fmtx.dateTime(status.checked_at, { seconds: true }) }}</dd></div>
      </dl>
      <ul v-if="pods.length" class="pods">
        <li v-for="p in pods" :key="p.name" :class="p.condition">
          <div class="ptop">
            <span class="dot" :class="p.condition" aria-hidden="true" />
            <code :title="p.name">{{ short(p.name) }}</code>
            <span class="tag" :class="`c-${p.condition}`">{{ condLabel(p) }}</span>
            <span v-if="p.node" class="muted small">{{ $t('dataApi.stOnNode', { node: p.node }) }}</span>
            <span v-if="p.restarts" class="muted small">{{ $t('dataApi.stRestarts', { n: p.restarts }, p.restarts) }}</span>
          </div>
          <ul v-if="p.resources.length" class="served small">
            <li v-for="r in p.resources" :key="r.name">
              <code>{{ r.name }}</code>
              <span v-if="r.version">{{ $t('dataApi.stServes', { version: fmtx.dateTime(r.version, { seconds: true }) }) }}</span>
              <span v-else class="muted">{{ $t('dataApi.stNothingYet') }}</span>
              <span v-if="r.preparing" class="muted">· {{ $t('dataApi.stPreparing', { version: fmtx.dateTime(r.preparing, { seconds: true }) }) }}</span>
              <span v-if="r.error" class="err">· {{ r.error }}</span>
            </li>
          </ul>
          <p v-if="trouble(p)" class="small trouble mono">{{ trouble(p) }}</p>
        </li>
      </ul>
      <ul v-if="state === 'active' && status.resources.length" class="latest small">
        <li v-for="r in status.resources" :key="r.name">
          <code>{{ r.name }}</code>
          <span v-if="r.latest">
            {{ $t('dataApi.stLatest', { version: fmtx.dateTime(r.latest, { seconds: true }), serving: r.serving, pods: status.wanted }) }}
          </span>
          <span v-else class="muted">{{ $t('dataApi.stNoData') }}</span>
        </li>
      </ul>
      <p v-if="state === 'active'" class="muted small help">{{ $t('dataApi.stPoll', { n: status.poll_seconds }) }}</p>

      <div class="traffic" :aria-label="$t('dataApi.stTraffic')">
        <div class="thead">
          <h3><Activity :size="14" /> {{ $t('dataApi.stTraffic') }}</h3>
          <div v-roving class="spans" role="radiogroup" :aria-label="$t('dataApi.stTraffic')">
            <button v-for="h in spans" :key="h" type="button" class="mini" role="radio" :class="{ on: hours === h }" :aria-checked="hours === h"
                    @click="hours = h">{{ $t(`dataApi.stSpan_${h}`) }}</button>
          </div>
        </div>
        <dl class="totals">
          <div><dt>{{ $t('dataApi.stRequests') }}</dt><dd>{{ fmtx.count(status.traffic.totals.requests) }}</dd></div>
          <div><dt>{{ $t('dataApi.stRows') }}</dt><dd>{{ fmtx.count(status.traffic.totals.rows) }}</dd></div>
          <div><dt>{{ $t('dataApi.stErrors') }}</dt><dd :class="{ err: status.traffic.totals.errors }">{{ fmtx.count(status.traffic.totals.errors) }}</dd></div>
          <div><dt>{{ $t('dataApi.stThrottled') }}</dt><dd :class="{ warn: status.traffic.totals.throttled }">{{ fmtx.count(status.traffic.totals.throttled) }}</dd></div>
        </dl>
        <svg v-if="points.length" class="bars" viewBox="0 0 100 40" preserveAspectRatio="none" role="img"
             :aria-label="$t('dataApi.stBarsLabel', { n: fmtx.count(status.traffic.totals.requests) })">
          <g v-for="(b, i) in bars" :key="b.at">
            <title>{{ $t('dataApi.stBar', { at: fmtx.dateTime(b.at), n: fmtx.count(b.requests), errors: fmtx.count(b.errors) }) }}</title>
            <rect :x="i * barWidth + barWidth * 0.1" :width="barWidth * 0.8" :y="40 - (b.requests / peak) * 38" :height="(b.requests / peak) * 38" class="ok" />
            <rect v-if="b.errors" :x="i * barWidth + barWidth * 0.1" :width="barWidth * 0.8" :y="40 - (Math.min(b.errors, b.requests) / peak) * 38"
                  :height="(Math.min(b.errors, b.requests) / peak) * 38" class="ko" />
          </g>
        </svg>
        <p v-else class="muted small">{{ $t('dataApi.stNoTraffic') }}</p>
      </div>
    </template>
  </div>
</template>

<style scoped>
.card { border: 1px solid var(--border-soft); border-radius: 10px; padding: 12px 14px; background: var(--panel); display: flex; flex-direction: column; gap: 10px; min-width: 0; }
.card h2 { margin: 0; font-size: 13.5px; display: flex; align-items: center; gap: 7px; flex-wrap: wrap; }
.sum { font-size: 12px; font-weight: 500; margin-left: auto; }
.sum.good { color: var(--accent-2); }
.sum.wait { color: var(--warning); }
.sum.bad { color: var(--danger); }
.sum.muted { color: var(--muted); }
.small { font-size: 12px; }
.help { margin: 0; line-height: 1.45; }
.err { color: var(--danger); }
.warn { color: var(--warning); }
.mono, code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.note { margin: 0; padding: 8px 10px; border-radius: 8px; background: var(--bg-soft); border: 1px solid var(--border); display: flex; gap: 8px; align-items: flex-start; overflow-wrap: anywhere; }
.note.warn { border-color: color-mix(in srgb, var(--warning) 45%, var(--border)); color: var(--text); }
.facts { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 6px 14px; margin: 0; }
.facts dt { color: var(--muted); font-size: 11.5px; }
.facts dd { margin: 0; overflow-wrap: anywhere; }
.pods, .served, .latest { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
.pods > li { border: 1px solid var(--border-soft); border-radius: 8px; padding: 7px 9px; display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.pods > li.error { border-color: color-mix(in srgb, var(--danger) 45%, var(--border)); }
.ptop { display: flex; align-items: center; gap: 7px; flex-wrap: wrap; }
.dot { width: 8px; height: 8px; border-radius: 50%; background: var(--muted); flex-shrink: 0; }
.dot.ready { background: var(--accent-2); }
.dot.preparing, .dot.starting { background: var(--warning); }
.dot.error { background: var(--danger); }
.served { gap: 2px; padding-left: 15px; }
.served li, .latest li { display: flex; flex-wrap: wrap; gap: 0 6px; align-items: baseline; }
.trouble { margin: 0; color: var(--danger); overflow-wrap: anywhere; }
.tag { font-size: 10px; text-transform: uppercase; letter-spacing: 0.04em; padding: 1px 7px; border-radius: 8px; background: var(--panel-2); border: 1px solid var(--border); color: var(--muted); }
.tag.c-ready { color: var(--accent-2); border-color: color-mix(in srgb, var(--accent-2) 45%, var(--border)); }
.tag.c-preparing, .tag.c-starting { color: var(--warning); border-color: color-mix(in srgb, var(--warning) 45%, var(--border)); }
.tag.c-error { color: var(--danger); border-color: color-mix(in srgb, var(--danger) 45%, var(--border)); }
.traffic { display: flex; flex-direction: column; gap: 8px; border-top: 1px solid var(--border-soft); padding-top: 10px; }
.thead { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.thead h3 { margin: 0; font-size: 12.5px; display: inline-flex; align-items: center; gap: 6px; }
.spans { display: inline-flex; gap: 4px; margin-left: auto; }
.mini { min-height: 0; padding: 1px 8px; font-size: 11.5px; }
.mini.on { background: var(--tint-accent); border-color: color-mix(in srgb, var(--accent) 55%, var(--border)); color: var(--text); }
.totals { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; margin: 0; }
.totals dt { color: var(--muted); font-size: 11px; }
.totals dd { margin: 0; font-size: 15px; font-weight: 600; font-variant-numeric: tabular-nums; }
.bars { width: 100%; height: 64px; display: block; }
.bars rect.ok { fill: color-mix(in srgb, var(--accent) 70%, transparent); }
.bars rect.ko { fill: var(--danger); }
@media (max-width: 600px) {
  .totals { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
</style>
