<script setup lang="ts">
// Gantt delle ESECUZIONI di un flusso: ogni run una barra start→fine sull'asse
// tempo, colorata per esito. È la timeline delle attività del flusso.
import { computed } from 'vue'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { CustomChart } from 'echarts/charts'
import { GridComponent, TooltipComponent, DataZoomComponent } from 'echarts/components'
import VChart from 'vue-echarts'
import type { RunInfo } from '~/composables/useRuns'

use([CanvasRenderer, CustomChart, GridComponent, TooltipComponent, DataZoomComponent])

const props = defineProps<{ runs: RunInfo[] }>()

const COLORS: Record<string, string> = {
  SUCCESS: '#34d399',
  FAILURE: '#ef4444',
  STARTED: '#facc15',
  PENDING: '#94a3b8',
}

function fmt(s: string): string {
  return new Date(s).toLocaleString('it-IT', { dateStyle: 'short', timeStyle: 'short' })
}

// solo gli ultimi 20 run (più recenti in alto): oltre, il grafico diventa
// illeggibile
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
      itemStyle: { color: COLORS[r.status] ?? '#94a3b8' },
    }
  })

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
        `<b>${p.data.name}</b> · ${p.data.status}<br/>${p.data.startLabel}<br/>durata: ${p.data.durationSec.toFixed(1)}s`,
    },
    xAxis: {
      type: 'time',
      name: 'quando è avvenuta l\'esecuzione',
      nameLocation: 'middle',
      nameGap: 26,
      nameTextStyle: { fontSize: 10, color: '#8b97ad' },
      axisLabel: { fontSize: 10, hideOverlap: true },
      splitLine: { show: true, lineStyle: { opacity: 0.15 } },
    },
    yAxis: {
      type: 'category',
      inverse: true,
      data: rows.value.map((r) => `#${r.id}`),
      axisTick: { show: false },
      axisLabel: { fontSize: 10 },
    },
    dataZoom: [{ type: 'inside', xAxisIndex: 0 }],
    series: [{ type: 'custom', renderItem, encode: { x: [1, 2], y: 0 }, data }],
  }
})
</script>

<template>
  <div>
    <div v-if="rows.length" class="gantt"><VChart :option="option" autoresize /></div>
    <p v-else class="muted small">Nessuna esecuzione da mostrare.</p>
    <p v-if="rows.length" class="legend muted">
      Ogni barra è un'esecuzione (asse Y: <code>#id</code> del run) — orizzontale: <b>quando</b> è
      partita, larghezza: <b>durata</b>, colore l'esito
      <span class="dot ok" /> riuscita <span class="dot ko" /> fallita.
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
.dot.ok { background: #34d399; }
.dot.ko { background: #ef4444; }
</style>
