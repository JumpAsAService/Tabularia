<script setup lang="ts">
// The «Runs by day» widget: one column per day of the span, as tall as its runs against the
// busiest day, split into what succeeded and what failed (GET /runs/activity, the Runs page's
// calendar: the runs this person may see). The totals are said in words above the chart, and each
// column says its day and its numbers.
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { dayColumns } from '~/composables/homeLayout'
import { useFormat } from '~/composables/useFormat'
import { useRuns, type RunActivity } from '~/composables/useRuns'

const props = defineProps<{ options: Record<string, any> }>()
const { t } = useI18n()
const fmt = useFormat()
const runsApi = useRuns()
const days = computed(() => Number(props.options.days) || 30)
const activity = ref<RunActivity | null>(null)
const failed = ref(false)
async function read() {
  try { activity.value = await runsApi.activity({ days: days.value }); failed.value = false } catch { failed.value = true }
}
let timer: ReturnType<typeof setInterval> | undefined
const whenShown = () => { if (!document.hidden) read() }
onMounted(() => { read(); timer = setInterval(whenShown, 300_000); document.addEventListener('visibilitychange', whenShown) })
onBeforeUnmount(() => { clearInterval(timer); document.removeEventListener('visibilitychange', whenShown) })
watch(days, read)

const cols = computed(() => dayColumns(activity.value?.buckets ?? [], days.value, new Date()))
const total = computed(() => cols.value.reduce((n, c) => n + c.total, 0))
const ok = computed(() => cols.value.reduce((n, c) => n + c.ok, 0))
const ko = computed(() => cols.value.reduce((n, c) => n + c.ko, 0))
const rate = computed(() => (ok.value + ko.value ? Math.round((ok.value / (ok.value + ko.value)) * 100) : null))
const dayText = (key: string) => fmt.date(`${key}T12:00:00`, { weekday: 'short', day: 'numeric', month: 'short' })
const colLabel = (c: { key: string; total: number; ok: number; ko: number }) => t('home.byDay.col', { day: dayText(c.key), n: c.total, ok: c.ok, ko: c.ko })
</script>

<template>
  <div class="bd" :aria-busy="activity === null && !failed">
    <p v-if="failed && !activity" class="msg" role="alert">{{ $t('home.byDay.failed') }} <button type="button" class="retry" @click="read">{{ $t('home.retry') }}</button></p>
    <template v-else>
      <p class="sum">
        <span class="big">{{ fmt.count(total) }}</span> {{ $t('home.byDay.runs', { n: days }) }}
        <span v-if="rate !== null" class="rate"><b>{{ rate }}%</b> {{ $t('home.byDay.ok') }}</span>
        <span v-if="ko" class="ko"><b>{{ fmt.count(ko) }}</b> {{ $t('home.byDay.ko', ko) }}</span>
      </p>
      <ol class="cols" :aria-label="$t('home.byDay.chart', { n: days })">
        <li v-for="c in cols" :key="c.key" :title="colLabel(c)">
          <span class="sr-only">{{ colLabel(c) }}</span>
          <span v-if="c.otherH" class="seg other" :style="{ height: `${c.otherH}%` }" aria-hidden="true" />
          <span v-if="c.koH" class="seg ko" :style="{ height: `${c.koH}%` }" aria-hidden="true" />
          <span v-if="c.okH" class="seg ok" :style="{ height: `${c.okH}%` }" aria-hidden="true" />
        </li>
      </ol>
      <p class="axis" aria-hidden="true"><span>{{ dayText(cols[0]?.key ?? '') }}</span><span>{{ $t('home.byDay.today') }}</span></p>
    </template>
  </div>
</template>

<style scoped>
.bd { display: flex; flex-direction: column; height: 100%; min-height: 0; }
.msg { margin: 2px 0; color: var(--muted); font-size: 13px; }
.retry { min-height: 0; padding: 0 2px; border: none; background: none; color: var(--accent); font-size: 13px; text-decoration: underline; }
.sum { display: flex; align-items: baseline; flex-wrap: wrap; gap: 4px 12px; margin: 0 0 10px; color: var(--muted); font-size: 12.5px; }
.big { color: var(--text); font-size: 26px; font-weight: 700; line-height: 1; font-variant-numeric: tabular-nums; letter-spacing: -0.01em; }
.sum b { color: var(--text); font-weight: 650; font-variant-numeric: tabular-nums; }
.sum .ko, .sum .ko b { color: var(--danger-text, var(--danger)); }
.cols { flex: 1; min-height: 40px; list-style: none; margin: 0; padding: 0; display: flex; align-items: stretch; gap: 2px; border-bottom: 1px solid var(--border); }
.cols li { flex: 1 1 0; min-width: 0; display: flex; flex-direction: column; justify-content: flex-end; border-radius: 2px 2px 0 0; }
.cols li:hover { background: var(--row-hover, var(--panel-2)); }
.seg { display: block; min-height: 2px; }
.seg.ok { background: var(--accent-2); }
.seg.ko { background: var(--danger); }
.seg.other { background: var(--accent); }
.cols li .seg:first-of-type { border-radius: 2px 2px 0 0; }
.axis { display: flex; justify-content: space-between; margin: 4px 0 0; color: var(--muted); font-size: 11.5px; }
@media (prefers-reduced-motion: no-preference) {
  .cols li { animation: grow 480ms var(--ease-out, cubic-bezier(.16, 1, .3, 1)) both; transform-origin: bottom; }
}
@keyframes grow { from { transform: scaleY(0.05); } to { transform: none; } }
</style>
