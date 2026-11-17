<script setup lang="ts">
// The «Flow runs» widget: the last runs of each flow as bars, oldest on the left. A bar is as
// tall as its run lasted against the flow's longest, and coloured by how it ended (success,
// failure, still going); it says its time, duration and trigger. The colour
// is never alone: each bar has its words for a screen reader and on hover, and the line beside
// the flow's name says the last outcome in text.
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { durationText, median, runBars, type FlowRunBar } from '~/composables/homeLayout'
import { useFormat } from '~/composables/useFormat'
import { useFolderPaths } from '~/composables/useFolderPaths'

interface FlowRuns { id: number; name: string; project_id: number; runs: FlowRunBar[] }
const props = defineProps<{ options: Record<string, any> }>()
const { t } = useI18n()
const fmt = useFormat()
const { apiFetch } = useApiClient()
const { pathOf } = useFolderPaths()

const flows = ref<FlowRuns[] | null>(null)
const failed = ref(false)
async function read() {
  try {
    const ids: number[] = props.options.flow_ids ?? []
    const r = await apiFetch<{ flows: FlowRuns[] }>(`/home/flow-runs?runs=${Number(props.options.runs) || 20}${ids.length ? `&flows=${ids.join(',')}` : ''}`)
    flows.value = r.flows
    failed.value = false
  } catch {
    failed.value = true
    flows.value ??= []
  }
}
let timer: ReturnType<typeof setInterval> | undefined
const whenShown = () => { if (!document.hidden) read() }
onMounted(() => { read(); timer = setInterval(whenShown, 60_000); document.addEventListener('visibilitychange', whenShown) })
onBeforeUnmount(() => { clearInterval(timer); document.removeEventListener('visibilitychange', whenShown) })
watch(() => JSON.stringify(props.options), read)

const rows = computed(() => (flows.value ?? []).map((f) => {
  const done = f.runs.map((r) => r.seconds).filter((x): x is number => x != null)
  const last = f.runs[f.runs.length - 1]
  return { ...f, bars: runBars(f.runs), median: median(done), last, failures: f.runs.filter((r) => r.status === 'FAILURE').length }
}))
const statusText = (s: string) => t(s === 'SUCCESS' ? 'home.flowRuns.ok' : s === 'FAILURE' ? 'home.flowRuns.ko' : 'home.flowRuns.live')
const barLabel = (r: FlowRunBar) => `${statusText(r.status)} · ${fmt.dateTime(r.started_at)} · ${durationText(r.seconds)} · ${t(r.trigger === 'schedule' ? 'home.flowRuns.scheduled' : 'home.flowRuns.manual')}`
</script>

<template>
  <div class="fr" :aria-busy="flows === null">
    <p v-if="flows === null" class="msg"><span class="sk" style="width: 60%" /></p>
    <p v-else-if="failed && !rows.length" class="msg" role="alert">{{ $t('home.flowRuns.failed') }} <button type="button" class="retry" @click="read">{{ $t('home.retry') }}</button></p>
    <p v-else-if="!rows.length" class="msg">{{ $t('home.flowRuns.empty') }}</p>
    <ul v-else class="flows">
      <li v-for="f in rows" :key="f.id" class="flow">
        <div class="who">
          <NuxtLink :to="`/editor?flow=${f.id}`" class="name">{{ f.name }}</NuxtLink>
          <span class="where">{{ pathOf(f.project_id) }}</span>
          <span v-if="f.last" class="last" :class="`t-${f.bars[f.bars.length - 1]!.tone}`">
            <span class="dot" aria-hidden="true" />{{ statusText(f.last.status) }} · {{ fmt.ago(f.last.started_at) }}
          </span>
        </div>
        <ol v-if="f.bars.length" class="bars" :aria-label="$t('home.flowRuns.barsOf', { name: f.name, n: f.bars.length })">
          <li v-for="b in f.bars" :key="b.run.id">
            <span class="bar" :class="`t-${b.tone}`" role="img" :style="{ '--h': `${b.height}%` }" :title="barLabel(b.run)" :aria-label="barLabel(b.run)" />
          </li>
        </ol>
        <p v-else class="msg none">{{ $t('home.flowRuns.never') }}</p>
        <div class="nums">
          <span><b>{{ durationText(f.median) }}</b> {{ $t('home.flowRuns.median') }}</span>
          <span v-if="f.failures" class="t-ko"><b>{{ f.failures }}</b> {{ $t('home.flowRuns.failures', f.failures) }}</span>
        </div>
      </li>
    </ul>
  </div>
</template>

<style scoped>
.fr { height: 100%; overflow: auto; }
.msg { margin: 2px 0; color: var(--muted); font-size: 13px; }
.msg .sk { display: inline-block; height: 12px; }
.msg.none { align-self: center; }
.retry { min-height: 0; padding: 0 2px; border: none; background: none; color: var(--accent); font-size: 13px; text-decoration: underline; }
.flows { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 14px; }
.flow { display: grid; grid-template-columns: minmax(120px, 1fr) minmax(0, 2.2fr) auto; align-items: end; gap: 6px 16px; }
.who { display: flex; flex-direction: column; gap: 2px; min-width: 0; align-self: center; }
.name { color: var(--text); font-size: 13.5px; font-weight: 600; text-decoration: none; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.name:hover, .name:focus-visible { color: var(--accent-hi, var(--accent)); text-decoration: underline; }
.where { color: var(--muted); font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.last { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: var(--muted); }
.dot { width: 7px; height: 7px; border-radius: 50%; background: currentColor; flex-shrink: 0; }
.last.t-ok .dot { color: var(--accent-2); }
.last.t-ko, .nums .t-ko { color: var(--danger-text, var(--danger)); }
.last.t-live .dot { color: var(--accent); }
.bars { list-style: none; margin: 0; padding: 0; display: flex; align-items: flex-end; gap: 3px; height: 56px; border-bottom: 1px solid var(--border); }
.bars li { flex: 1 1 0; min-width: 4px; max-width: 16px; height: 100%; display: flex; align-items: flex-end; }
.bar { display: block; width: 100%; height: var(--h); min-height: 4px; border-radius: 3px 3px 0 0; background: var(--accent-2); transform-origin: bottom; }
.bar.t-ko { background: var(--danger); }
.bar.t-live { background: var(--accent); }
.bar:hover { filter: brightness(1.25); outline: 2px solid var(--text); outline-offset: 1px; }
.nums { display: flex; flex-direction: column; align-items: flex-end; gap: 2px; font-size: 12px; color: var(--muted); white-space: nowrap; align-self: center; }
.nums b { color: var(--text); font-weight: 650; font-variant-numeric: tabular-nums; }
.nums .t-ko b { color: inherit; }
@container (max-width: 520px) {
  .flow { grid-template-columns: 1fr auto; }
  .bars { grid-column: 1 / -1; grid-row: 2; }
}
@media (prefers-reduced-motion: no-preference) {
  .bar { animation: rise 520ms var(--ease-out, cubic-bezier(.16, 1, .3, 1)) both; }
  .bar.t-live { animation: rise 520ms var(--ease-out, cubic-bezier(.16, 1, .3, 1)) both, live 1.6s ease-in-out 520ms infinite; }
}
@keyframes rise { from { transform: scaleY(0.05); opacity: 0.4; } to { transform: none; opacity: 1; } }
@keyframes live { 50% { opacity: 0.45; } }
</style>
