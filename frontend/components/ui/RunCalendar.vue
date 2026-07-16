<script setup lang="ts">
// Calendar plot dell'ATTIVITÀ delle esecuzioni (stile GitHub): un heatmap per
// giorno con intensità = numero di run, drill-down ORARIO al click su un giorno.
// I conteggi distinguono esito (successi/falliti) e origine (manuali/schedulati).
import { computed, onMounted, ref, shallowRef } from 'vue'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { HeatmapChart, BarChart } from 'echarts/charts'
import {
  CalendarComponent, VisualMapComponent, TooltipComponent, GridComponent,
} from 'echarts/components'
import VChart from 'vue-echarts'
import { ChevronLeft } from 'lucide-vue-next'
import { useRuns, type ActivityBucket, type RunActivity } from '~/composables/useRuns'
import { errMessage } from '~/composables/useApi'
import { skeletonPad } from '~/composables/useSkeleton'

use([
  CanvasRenderer, HeatmapChart, BarChart,
  CalendarComponent, VisualMapComponent, TooltipComponent, GridComponent,
])

const runsApi = useRuns()

const RANGES = [
  { days: 90, label: '3 mesi' },
  { days: 180, label: '6 mesi' },
  { days: 365, label: '1 anno' },
]
const rangeDays = ref(180)

const view = ref<'calendar' | 'day'>('calendar')
const selectedDay = ref<string | null>(null)
const loading = ref(true)
const error = ref('')
const daily = shallowRef<RunActivity | null>(null)
const hourly = shallowRef<RunActivity | null>(null)

// mappe key→bucket per i tooltip (senza rovistare nell'array nei formatter)
const dailyByKey = computed(
  () => new Map<string, ActivityBucket>((daily.value?.buckets ?? []).map((b) => [b.key, b])),
)

async function fetchDaily() {
  loading.value = true
  const t0 = performance.now()
  try {
    daily.value = await runsApi.activity({ days: rangeDays.value })
    error.value = ''
  } catch (e) {
    error.value = errMessage(e)
  } finally {
    await skeletonPad(t0)
    loading.value = false
  }
}

async function openDay(date: string) {
  selectedDay.value = date
  view.value = 'day'
  hourly.value = null
  const t0 = performance.now()
  try {
    hourly.value = await runsApi.activity({ day: date })
  } catch (e) {
    error.value = errMessage(e)
  } finally {
    await skeletonPad(t0)
  }
}

function backToCalendar() {
  view.value = 'calendar'
  selectedDay.value = null
}

function setRange(days: number) {
  if (days === rangeDays.value) return
  rangeDays.value = days
  fetchDaily()
}

onMounted(fetchDaily)

// ── calendario ───────────────────────────────────────────────────────────────
const weeks = computed(() => Math.ceil(rangeDays.value / 7) + 1)
const calWidth = computed(() => Math.max(weeks.value * 16 + 60, 320)) // px; scroll-x se serve

const maxTotal = computed(() =>
  (daily.value?.buckets ?? []).reduce((m, b) => Math.max(m, b.total), 0),
)

function fmtDayIT(iso: string): string {
  return new Date(iso + 'T00:00:00').toLocaleDateString('it-IT', {
    weekday: 'long', day: 'numeric', month: 'long', year: 'numeric',
  })
}

const calendarOption = computed(() => {
  const d = daily.value
  if (!d) return {}
  const data = (d.buckets ?? []).map((b) => [b.key, b.total])
  const byKey = dailyByKey.value
  return {
    tooltip: {
      // appesa al body: il wrapper ha overflow-y hidden (per lo scroll-x) e
      // altrimenti taglierebbe la tooltip in alto
      appendToBody: true,
      formatter: (p: any) => {
        const b = byKey.get(p.data[0])
        if (!b) return `${p.data[0]}<br/>nessuna esecuzione`
        return (
          `<b>${fmtDayIT(b.key)}</b><br/>` +
          `esecuzioni: <b>${b.total}</b><br/>` +
          `✓ ${b.success} riuscite · ✗ ${b.failure} fallite<br/>` +
          `⏱ ${b.scheduled} schedulate · 👤 ${b.manual} manuali<br/>` +
          `<span style="opacity:.6">click per il dettaglio orario</span>`
        )
      },
    },
    visualMap: {
      min: 0,
      max: Math.max(maxTotal.value, 1),
      calculable: false,
      orient: 'horizontal',
      left: 'center',
      bottom: 0,
      itemWidth: 11,
      itemHeight: 11,
      textStyle: { fontSize: 10, color: '#8b97ad' },
      inRange: { color: ['#bbf7d0', '#4ade80', '#15803d'] },
      text: ['più', 'meno'],
    },
    calendar: {
      top: 18,
      left: 40,
      right: 8,
      cellSize: [14, 14],
      range: [d.from_key, d.to_key],
      splitLine: { show: false },
      itemStyle: { color: 'transparent', borderColor: 'rgba(148,163,184,0.18)', borderWidth: 1 },
      yearLabel: { show: false },
      monthLabel: {
        fontSize: 10,
        color: '#8b97ad',
        nameMap: ['gen', 'feb', 'mar', 'apr', 'mag', 'giu', 'lug', 'ago', 'set', 'ott', 'nov', 'dic'],
      },
      // nameMap indicizzato da domenica(0); firstDay:1 avvia la settimana da lunedì
      dayLabel: {
        fontSize: 9,
        color: '#8b97ad',
        firstDay: 1,
        nameMap: ['D', 'L', 'M', 'M', 'G', 'V', 'S'],
      },
    },
    series: [{ type: 'heatmap', coordinateSystem: 'calendar', data }],
  }
})

// ── drill-down orario ────────────────────────────────────────────────────────
const hourlyOption = computed(() => {
  const h = hourly.value
  if (!h) return {}
  const byHour = new Map(h.buckets.map((b) => [b.key, b]))
  const hours = Array.from({ length: 24 }, (_, i) => String(i).padStart(2, '0'))
  const success = hours.map((k) => byHour.get(k)?.success ?? 0)
  const failure = hours.map((k) => byHour.get(k)?.failure ?? 0)
  const pending = hours.map((k) => {
    const b = byHour.get(k)
    return b ? b.total - b.success - b.failure : 0 // in corso o non terminati
  })
  return {
    grid: { left: 34, right: 12, top: 20, bottom: 40 },
    tooltip: {
      trigger: 'axis',
      appendToBody: true,
      formatter: (ps: any[]) => {
        const k = ps[0].axisValue
        const b = byHour.get(k)
        if (!b || !b.total) return `ore ${k}:00<br/>nessuna esecuzione`
        return (
          `<b>ore ${k}:00</b> · ${b.total} esecuzioni<br/>` +
          `✓ ${b.success} · ✗ ${b.failure}<br/>` +
          `⏱ ${b.scheduled} schedulate · 👤 ${b.manual} manuali`
        )
      },
    },
    legend: { bottom: 0, textStyle: { fontSize: 10, color: '#8b97ad' }, itemHeight: 8, itemWidth: 12 },
    xAxis: {
      type: 'category',
      data: hours,
      name: 'ora',
      nameLocation: 'middle',
      nameGap: 26,
      nameTextStyle: { fontSize: 10, color: '#8b97ad' },
      axisLabel: { fontSize: 9, interval: 1 },
    },
    yAxis: { type: 'value', minInterval: 1, axisLabel: { fontSize: 9 }, splitLine: { lineStyle: { opacity: 0.12 } } },
    series: [
      { name: 'riuscite', type: 'bar', stack: 'x', data: success, itemStyle: { color: '#34d399' } },
      { name: 'fallite', type: 'bar', stack: 'x', data: failure, itemStyle: { color: '#ef4444' } },
      { name: 'in corso', type: 'bar', stack: 'x', data: pending, itemStyle: { color: '#94a3b8' } },
    ],
  }
})

const dailyTotal = computed(() =>
  (daily.value?.buckets ?? []).reduce((s, b) => s + b.total, 0),
)
</script>

<template>
  <div class="cal-card">
    <div class="cal-head">
      <div class="cal-title">
        <template v-if="view === 'calendar'">
          Attività esecuzioni <span class="muted">· {{ dailyTotal }} run</span>
        </template>
        <template v-else>
          <button class="back" @click="backToCalendar"><ChevronLeft :size="14" /> calendario</button>
          <span class="day-title">{{ selectedDay ? fmtDayIT(selectedDay) : '' }}</span>
        </template>
      </div>
      <div v-if="view === 'calendar'" class="segmented">
        <button
          v-for="r in RANGES"
          :key="r.days"
          :class="{ on: rangeDays === r.days }"
          @click="setRange(r.days)"
        >{{ r.label }}</button>
      </div>
    </div>

    <p v-if="error" class="err">{{ error }}</p>
    <div v-else-if="loading" class="cal-skel" />

    <template v-else>
      <div v-show="view === 'calendar'" class="cal-scroll">
        <VChart
          :option="calendarOption"
          :style="{ width: calWidth + 'px', height: '150px' }"
          autoresize
          @click="(p: any) => p?.data && openDay(p.data[0])"
        />
      </div>

      <div v-if="view === 'day'" class="hour-wrap">
        <div v-if="!hourly" class="cal-skel small" />
        <VChart v-else class="hour-chart" :option="hourlyOption" autoresize />
      </div>
    </template>
  </div>
</template>

<style scoped>
.cal-card { border: 1px solid var(--border-soft); border-radius: 10px; background: var(--panel); padding: 12px 14px; margin-bottom: 16px; }
.cal-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; margin-bottom: 6px; }
.cal-title { font-size: 13px; font-weight: 600; display: inline-flex; align-items: center; gap: 8px; }
.cal-title .muted { font-weight: 400; }
.day-title { text-transform: capitalize; }
.back { display: inline-flex; align-items: center; gap: 3px; padding: 3px 8px; font-size: 12px; border: 1px solid var(--border); border-radius: 6px; background: var(--panel-2); color: var(--text); cursor: pointer; }
.back:hover { background: var(--panel); }
.segmented { display: inline-flex; border: 1px solid var(--border); border-radius: 7px; overflow: hidden; }
.segmented button { padding: 4px 10px; background: var(--panel-2); color: var(--muted); border: none; border-right: 1px solid var(--border); font-size: 12px; cursor: pointer; }
.segmented button:last-child { border-right: none; }
.segmented button.on { background: var(--accent); color: #fff; }

.cal-scroll { overflow-x: auto; overflow-y: hidden; }
.hour-wrap { margin-top: 4px; }
.hour-chart { height: 220px; width: 100%; }
.cal-skel { height: 150px; border-radius: 8px; background: linear-gradient(90deg, var(--panel-2), var(--panel), var(--panel-2)); background-size: 200% 100%; animation: sh 1.2s ease-in-out infinite; }
.cal-skel.small { height: 220px; }
@keyframes sh { 0% { background-position: 200% 0; } 100% { background-position: -200% 0; } }
</style>
