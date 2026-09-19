<script setup lang="ts">
// Il grafico che l'assistente chiede insieme a una query: una specifica fissa,
// senza controlli. Il pannello del Viewer è un'altra cosa — lì il grafico lo
// costruisce l'utente scegliendo colonne e aggregazioni; qui la scelta l'ha già
// fatta il modello e l'utente la guarda. Un pannello di controlli sotto ogni
// risposta sarebbe rumore in una conversazione.
//
// Le righe sono quelle già mostrate nella tabella sopra: nessuna query in più,
// e il grafico non può discordare dai numeri che gli stanno accanto.
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { BarChart, LineChart, PieChart, ScatterChart } from 'echarts/charts'
import { GridComponent, TooltipComponent, LegendComponent } from 'echarts/components'
import VChart from 'vue-echarts'
import { Download } from 'lucide-vue-next'
import type { AiChartSpec } from '~/composables/useAi'
import { useChartTheme } from '~/composables/useChartTheme'

use([CanvasRenderer, BarChart, LineChart, PieChart, ScatterChart, GridComponent, TooltipComponent, LegendComponent])

const props = defineProps<{ spec: AiChartSpec; rows: Record<string, any>[]; name?: string }>()
const { t } = useI18n()
const { ui } = useChartTheme()
const chart = ref<any>(null)

const PALETTE = ['#3987e5', '#199e70', '#c98500', '#9085e9', '#d55181']
const num = (v: unknown) => (typeof v === 'number' ? v : Number(v))
const fmt = new Intl.NumberFormat()

// testo → HTML sicuro: i formatter di ECharts finiscono in innerHTML
const esc = (v: unknown) =>
  String(v).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c] as string)

/** Le righe raggruppate per serie, quando la specifica ne chiede una. */
const serie = computed(() => {
  const { x, y, series } = props.spec
  if (!series) return [{ name: y, rows: props.rows }]
  const per = new Map<string, Record<string, any>[]>()
  for (const r of props.rows) {
    const k = String(r[series] ?? '—')
    if (!per.has(k)) per.set(k, [])
    per.get(k)!.push(r)
  }
  return [...per.entries()].map(([name, rows]) => ({ name, rows }))
    .sort((a, b) => a.name.localeCompare(b.name))
    .slice(0, PALETTE.length)
})

const option = computed(() => {
  const c = ui.value
  const { type, x, y } = props.spec
  const asse = { axisLine: { lineStyle: { color: c.border } }, axisLabel: { color: c.muted, fontSize: 11 } }
  const base = {
    backgroundColor: 'transparent',
    color: PALETTE,
    tooltip: {
      backgroundColor: c.panel, borderColor: c.border, textStyle: { color: c.text, fontSize: 12 },
    },
    ...(serie.value.length > 1 ? { legend: { textStyle: { color: c.muted, fontSize: 11 }, top: 0 } } : {}),
  }

  if (type === 'pie' || type === 'donut') {
    return {
      ...base,
      tooltip: { ...base.tooltip, trigger: 'item', formatter: (p: any) => `${esc(p.name)}: <b>${esc(fmt.format(p.value))}</b> (${p.percent}%)` },
      series: [{
        type: 'pie',
        radius: type === 'donut' ? ['46%', '72%'] : '72%',
        label: { color: c.text, fontSize: 11 },
        data: props.rows.map((r) => ({ name: String(r[x] ?? '—'), value: num(r[y]) })),
      }],
    }
  }

  if (type === 'scatter') {
    return {
      ...base,
      tooltip: { ...base.tooltip, trigger: 'item', formatter: (p: any) => `${esc(p.data[2] ?? '')}<br/>${esc(x)}: <b>${esc(fmt.format(p.data[0]))}</b><br/>${esc(y)}: <b>${esc(fmt.format(p.data[1]))}</b>` },
      grid: { left: 8, right: 16, top: 16, bottom: 8, containLabel: true },
      xAxis: { type: 'value', ...asse, splitLine: { lineStyle: { color: c.borderSoft } } },
      yAxis: { type: 'value', ...asse, splitLine: { lineStyle: { color: c.borderSoft } } },
      series: [{ type: 'scatter', symbolSize: 9, itemStyle: { opacity: 0.75 }, data: props.rows.map((r) => [num(r[x]), num(r[y]), r[x]]) }],
    }
  }

  // barre orizzontali: le categorie stanno sull'asse Y, ed è il motivo per cui
  // esistono — le etichette lunghe si leggono
  const orizzontale = type === 'hbar'
  const categorie = [...new Set(props.rows.map((r) => String(r[x] ?? '—')))]
  const valori = { type: 'value' as const, ...asse, splitLine: { lineStyle: { color: c.borderSoft } } }
  const cat = { type: 'category' as const, data: categorie, ...asse }
  return {
    ...base,
    tooltip: { ...base.tooltip, trigger: 'axis', axisPointer: { type: 'shadow' } },
    grid: { left: 8, right: 16, top: serie.value.length > 1 ? 26 : 12, bottom: 8, containLabel: true },
    xAxis: orizzontale ? valori : cat,
    yAxis: orizzontale ? cat : valori,
    series: serie.value.map((s) => ({
      name: s.name,
      type: type === 'line' || type === 'area' ? 'line' : 'bar',
      smooth: false,
      ...(type === 'area' ? { areaStyle: { opacity: 0.18 } } : {}),
      data: categorie.map((k) => {
        const r = s.rows.find((row) => String(row[x] ?? '—') === k)
        return r ? num(r[y]) : null
      }),
    })),
  }
})

/** Il PNG è quello che si vede, al doppio della densità: un grafico scaricato
 *  finisce in una presentazione, dove 1× si vede sgranato. */
function downloadPng() {
  const inst = chart.value?.chart ?? chart.value
  if (!inst?.getDataURL) return
  const url = inst.getDataURL({ type: 'png', pixelRatio: 2, backgroundColor: ui.value.panel })
  const a = document.createElement('a')
  a.href = url
  a.download = `${(props.name || 'grafico').replace(/[^a-z0-9._-]+/gi, '_')}.png`.toLowerCase()
  a.click()
}

// il tema cambia sotto i piedi: il grafico non legge le variabili CSS, le riceve
watch(ui, () => chart.value?.setOption?.(option.value, true))

defineExpose({ downloadPng })
</script>

<template>
  <figure class="mini">
    <figcaption v-if="spec.title">{{ spec.title }}</figcaption>
    <VChart ref="chart" class="canvas" :option="option" autoresize />
    <button type="button" class="png" :title="t('chat.downloadPng')" :aria-label="t('chat.downloadPng')" @click="downloadPng">
      <Download :size="12" />
    </button>
  </figure>
</template>

<style scoped>
.mini { position: relative; margin: 8px 0 0; padding: 0; }
.mini figcaption { font-size: 12px; color: var(--muted); margin-bottom: 4px; }
.canvas { height: 240px; width: 100%; }
/* il bottone sta sul grafico ma non sopra i dati: angolo alto, appare al passaggio */
.png {
  position: absolute; top: 0; right: 0; padding: 3px 6px; min-height: 22px;
  opacity: 0; transition: opacity 0.15s ease;
}
.mini:hover .png, .png:focus-visible { opacity: 1; }
@media (pointer: coarse) { .png { opacity: 1; } }
</style>
