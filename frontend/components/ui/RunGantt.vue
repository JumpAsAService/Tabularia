<script setup lang="ts">
// Gantt of a flow's RUNS: every run a start→end bar on the time axis, coloured by
// outcome. It is the timeline of the flow's activity.
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { useFormat } from '~/composables/useFormat'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { CustomChart } from 'echarts/charts'
import { GridComponent, TooltipComponent, DataZoomComponent } from 'echarts/components'
import VChart from 'vue-echarts'
import type { RunInfo } from '~/composables/useRuns'
import { useChartTheme } from '~/composables/useChartTheme'

use([CanvasRenderer, CustomChart, GridComponent, TooltipComponent, DataZoomComponent])

const { t } = useI18n()
const fmtx = useFormat()

const props = defineProps<{ runs: RunInfo[] }>()

const { ui } = useChartTheme()

// positive and negative outcomes use the app's semantic tokens, the same as
// .okline/.koline: so the legend below and the rest of the interface say the same colour
// in all four themes. "running" stays a fixed yellow because there is no token for that
// state.
const COLORS = computed<Record<string, string>>(() => ({
  SUCCESS: ui.value.success,
  FAILURE: ui.value.danger,
  STARTED: '#facc15',
  PENDING: ui.value.muted,
}))

function fmt(s: string): string {
  return fmtx.dateTime(s)
}

// only the last 20 runs (newest at the top): beyond, the chart becomes unreadable
const rows = computed(() =>
  props.runs.filter((r) => r.started_at).slice(0, 20),
)

const option = computed(() => {
  const data = rows.value.map((r, i) => {
    const start = new Date(r.started_at as string).getTime()
    const end = r.finished_at ? new Date(r.finished_at).getTime() : Date.now()
    return {
      name: `#${r.id}`,
      value: [i, start, end],
      status: r.status,
      startLabel: fmt(r.started_at as string),
      durationSec: Math.max(0, (end - start) / 1000),
      itemStyle: { color: COLORS.value[r.status] ?? ui.value.muted },
    }
  })

  // Adaptive time axis. When the runs in view fit in ONE day: hourly ticks 00…23 anchored
  // to local midnight (initial label 00, not the first run's hour). When they cover SEVERAL
  // days: I leave ECharts wider ticks with a day+hour label, otherwise 24 hours × N days
  // becomes unreadable.
  const HOUR = 3600 * 1000
  const times = rows.value.flatMap((r) => [
    new Date(r.started_at as string).getTime(),
    r.finished_at ? new Date(r.finished_at).getTime() : Date.now(),
  ])
  const earliest = new Date(Math.min(...times))
  // local midnight of the oldest day (getTime() is UTC, the constructor is local)
  const minT = new Date(earliest.getFullYear(), earliest.getMonth(), earliest.getDate()).getTime()
  // max snapped to the hour relative to minT → the ticks stay aligned to midnight
  let maxT = minT + Math.ceil((Math.max(...times) - minT) / HOUR) * HOUR
  if (maxT <= minT) maxT = minT + HOUR
  const singleDay = maxT - minT <= 24 * HOUR

  function renderItem(params: any, api: any) {
    const row = api.value(0)
    const s = api.coord([api.value(1), row])
    const e = api.coord([api.value(2), row])
    const band = api.size([0, 1])[1]
    const h = Math.max(band * 0.55, 6)
    const coord = params.coordSys
    let x = s[0]
    let x2 = Math.min(e[0], coord.x + coord.width)
    x = Math.max(x, coord.x)
    const w = Math.max(x2 - x, 3)
    if (w <= 0) return
    return {
      type: 'rect',
      shape: { x, y: s[1] - h / 2, width: w, height: h, r: 2 },
      style: api.style(),
    }
  }

  return {
    grid: { left: 54, right: 14, top: 8, bottom: 40 },
    tooltip: {
      formatter: (p: any) =>
        `<b>${p.data.name}</b> · ${p.data.status}<br/>${p.data.startLabel}<br/>${t('runGantt.tooltipDuration', { s: p.data.durationSec.toFixed(1) })}`,
    },
    xAxis: {
      type: 'time',
      min: minT,
      max: maxT,
      // a single day → I force the hourly tick (00…23); several days → I let ECharts choose so
      // as not to crowd the axis
      ...(singleDay ? { minInterval: HOUR, maxInterval: HOUR } : {}),
      name: singleDay ? t('runGantt.axisHourOfDay') : t('runGantt.axisDayAndHour'),
      nameLocation: 'middle',
      nameGap: 28,
      nameTextStyle: { fontSize: 10, color: ui.value.muted },
      axisLabel: {
        fontSize: 10,
        hideOverlap: true,
        // without an explicit color ECharts uses its default dark grey, which on the four dark
        // themes is almost unreadable
        color: ui.value.muted,
        // single day: only the hour (00, 01…); multi-day: date + hour
        formatter: singleDay ? '{HH}' : '{dd}/{MM} {HH}:{mm}',
      },
      axisLine: { lineStyle: { color: ui.value.border } },
      splitLine: { show: true, lineStyle: { opacity: 0.12 } },
    },
    yAxis: {
      type: 'category',
      inverse: true,
      data: rows.value.map((r) => `#${r.id}`),
      axisTick: { show: false },
      axisLabel: { fontSize: 10, color: ui.value.muted },
      axisLine: { lineStyle: { color: ui.value.border } },
    },
    dataZoom: [{ type: 'inside', xAxisIndex: 0 }],
    series: [{ type: 'custom', renderItem, encode: { x: [1, 2], y: 0 }, data }],
  }
})
</script>

<template>
  <div>
    <div v-if="rows.length" class="gantt"><VChart :option="option" autoresize /></div>
    <p v-else class="muted small">{{ $t('runGantt.noRuns') }}</p>
    <p v-if="rows.length" class="legend muted">
      {{ $t('runGantt.legendEach') }}<code>#id</code>{{ $t('runGantt.legendOfRun') }}<b>{{ $t('runGantt.legendTimeLabel') }}</b>
      {{ $t('runGantt.legendTimeDetail') }}
      <b>{{ $t('runGantt.legendDurationLabel') }}</b>{{ $t('runGantt.legendColorOutcome') }}
      <span class="dot ok" />{{ $t('runGantt.legendSuccessLabel') }}<span class="dot ko" />{{ $t('runGantt.legendFailureLabel') }}
    </p>
  </div>
</template>

<style scoped>
.gantt { height: 240px; width: 100%; }
.gantt > :deep(div) { height: 100%; }
.small { font-size: 12.5px; padding: 8px 0; }
.legend { font-size: 11.5px; margin: 6px 0 0; line-height: 1.5; }
.legend code { font-family: ui-monospace, monospace; font-size: 11px; }
.dot { display: inline-block; width: 9px; height: 9px; border-radius: 2px; vertical-align: -1px; margin: 0 2px 0 6px; }
.dot.ok { background: var(--accent-2); }
.dot.ko { background: var(--danger); }
</style>
