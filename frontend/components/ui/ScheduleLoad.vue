<script setup lang="ts">
// Carico PREVISIONALE degli schedule: proietta i cron attivi (flussi + refresh
// datasource) sui prossimi N giorni e mostra dove si accavallano. Heatmap
// giorno×ora del carico + elenco delle fasce critiche (collisioni nello stesso
// minuto oltre la capacità dei worker → job in coda). Complementare al calendar
// plot dei run PASSATI: qui si guarda al FUTURO, per scegliere slot liberi.
import { ref, computed, watch, onMounted } from 'vue'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { HeatmapChart } from 'echarts/charts'
import { VisualMapComponent, TooltipComponent, GridComponent } from 'echarts/components'
import VChart from 'vue-echarts'
import { CalendarClock, TriangleAlert, ChevronDown, ChevronRight } from 'lucide-vue-next'
import { useApi, errMessage, type ScheduleLoad } from '~/composables/useApi'

use([CanvasRenderer, HeatmapChart, VisualMapComponent, TooltipComponent, GridComponent])

const api = useApi()
const { theme } = useTheme()

const WD = ['Lun', 'Mar', 'Mer', 'Gio', 'Ven', 'Sab', 'Dom']
const HORIZONS = [
  { days: 7, label: '7 giorni' },
  { days: 14, label: '14 giorni' },
  { days: 30, label: '30 giorni' },
]

const days = ref(7)
const data = ref<ScheduleLoad | null>(null)
const loading = ref(true)
const error = ref('')
const open = ref(true)

async function load() {
  loading.value = true
  error.value = ''
  try {
    data.value = await api.scheduleLoad(days.value)
  } catch (e) {
    error.value = errMessage(e)
    data.value = null
  } finally {
    loading.value = false
  }
}
watch(days, load)
onMounted(load)

// colori dal tema corrente (heatmap leggibile su chiaro e scuro)
function readUi() {
  const fb = { text: '#e8ebf2', muted: '#8b93a7', border: '#262e40', panel: '#141926' }
  if (!import.meta.client) return fb
  const s = getComputedStyle(document.documentElement)
  const g = (n: string, f: string) => s.getPropertyValue(n).trim() || f
  return { text: g('--text', fb.text), muted: g('--muted', fb.muted), border: g('--border', fb.border), panel: g('--panel', fb.panel) }
}
const ui = ref(readUi())
watch(theme, () => { ui.value = readUi() })

const maxCount = computed(() => Math.max(1, ...(data.value?.cells.map((c) => c.count) ?? [1])))
// lookup per il tooltip (peak per cella)
const peakByCell = computed(() => {
  const m = new Map<string, number>()
  for (const c of data.value?.cells ?? []) m.set(`${c.weekday}-${c.hour}`, c.peak_concurrent)
  return m
})

const option = computed(() => {
  const d = data.value
  const c = ui.value
  const cap = d?.worker_capacity ?? 2
  const points = (d?.cells ?? []).map((cell) => ({
    value: [cell.hour, cell.weekday, cell.count],
    itemStyle: cell.critical ? { borderColor: '#ff6b6b', borderWidth: 2 } : undefined,
  }))
  return {
    tooltip: {
      backgroundColor: c.panel, borderColor: c.border, textStyle: { color: c.text, fontSize: 12 },
      formatter: (p: any) => {
        const [h, wd, n] = p.data.value
        const peak = peakByCell.value.get(`${wd}-${h}`) ?? 1
        const warn = peak > cap ? `<br/><span style="color:#ff6b6b">⚠ picco ${peak} simultanei &gt; capacità ${cap}</span>` : ''
        return `<b>${WD[wd]} ${String(h).padStart(2, '0')}:00</b><br/>${n} esecuzion${n === 1 ? 'e' : 'i'} schedulat${n === 1 ? 'a' : 'e'}${warn}`
      },
    },
    grid: { left: 40, right: 12, top: 8, bottom: 46 },
    xAxis: {
      type: 'category', data: Array.from({ length: 24 }, (_, i) => String(i).padStart(2, '0')),
      splitArea: { show: true }, axisLabel: { color: c.muted, fontSize: 10, interval: 1 },
      axisLine: { lineStyle: { color: c.border } }, axisTick: { show: false },
    },
    yAxis: {
      type: 'category', data: WD, inverse: true,
      splitArea: { show: true }, axisLabel: { color: c.muted, fontSize: 11 },
      axisLine: { lineStyle: { color: c.border } }, axisTick: { show: false },
    },
    visualMap: {
      min: 0, max: maxCount.value, calculable: true, orient: 'horizontal', left: 'center', bottom: 0,
      itemWidth: 12, itemHeight: 90, textStyle: { color: c.muted, fontSize: 10 },
      inRange: { color: ['rgba(79,140,255,0.10)', '#4f8cff', '#7c6cff'] },
    },
    series: [{
      type: 'heatmap', data: points,
      itemStyle: { borderColor: c.panel, borderWidth: 1, borderRadius: 2 },
      emphasis: { itemStyle: { shadowBlur: 6, shadowColor: 'rgba(0,0,0,0.3)' } },
    }],
  }
})

function fmtBand(col: { weekday: number; hour: number; minute: number }) {
  return `${WD[col.weekday]} ${String(col.hour).padStart(2, '0')}:${String(col.minute).padStart(2, '0')}`
}
</script>

<template>
  <section class="sl">
    <header class="sl-head" @click="open = !open">
      <component :is="open ? ChevronDown : ChevronRight" :size="15" class="sl-caret" />
      <h3><CalendarClock :size="15" /> Carico degli schedule</h3>
      <span v-if="data" class="sl-sub muted">
        {{ data.total_schedules }} schedule · {{ data.total_firings }} esecuzioni nei prossimi {{ data.days }}gg · fuso {{ data.timezone }}
      </span>
      <span v-if="data?.collisions.length" class="sl-badge crit">
        <TriangleAlert :size="12" /> {{ data.collisions.length }} fasce critiche
      </span>
      <span class="sl-spacer" />
      <select v-model.number="days" class="sl-range" @click.stop>
        <option v-for="h in HORIZONS" :key="h.days" :value="h.days">{{ h.label }}</option>
      </select>
    </header>

    <div v-if="open" class="sl-body">
      <p v-if="error" class="sl-msg err">{{ error }}</p>
      <p v-else-if="loading" class="sl-msg muted">Calcolo del carico…</p>
      <p v-else-if="!data || !data.total_schedules" class="sl-msg muted">
        Nessuno schedule attivo. Quando pianifichi flussi o refresh, qui vedrai le fasce orarie più cariche.
      </p>
      <template v-else>
        <VChart class="sl-chart" :option="option" autoresize />

        <!-- fasce critiche: collisioni oltre la capacità dei worker -->
        <div class="sl-crit">
          <template v-if="data.collisions.length">
            <div class="sl-crit-h">
              <TriangleAlert :size="13" /> Fasce critiche
              <span class="muted">— più di {{ data.worker_capacity }} job (capacità worker) nello stesso minuto → il resto va in coda</span>
            </div>
            <div v-for="(col, i) in data.collisions" :key="i" class="sl-band">
              <span class="sl-band-when">{{ fmtBand(col) }}</span>
              <span class="sl-band-n">{{ col.count }} simultanei · {{ col.queued }} in coda</span>
              <span class="sl-band-who muted">{{ col.schedules.join(', ') }}</span>
            </div>
          </template>
          <p v-else class="sl-ok muted">
            ✓ Nessuna collisione: nessun minuto supera la capacità dei worker ({{ data.worker_capacity }}).
          </p>
        </div>
      </template>
    </div>
  </section>
</template>

<style scoped>
.sl {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 12px;
  overflow: hidden;
}
.sl-head {
  display: flex; align-items: center; gap: 10px;
  padding: 11px 14px; cursor: pointer; user-select: none;
}
.sl-head:hover { background: var(--panel-2); }
.sl-caret { color: var(--muted); flex-shrink: 0; }
.sl-head h3 { display: inline-flex; align-items: center; gap: 7px; margin: 0; font-size: 14px; white-space: nowrap; }
.sl-sub { font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.sl-badge.crit {
  display: inline-flex; align-items: center; gap: 4px;
  font-size: 11px; font-weight: 600; padding: 2px 8px; border-radius: 999px;
  color: var(--danger); background: rgba(255, 107, 107, 0.12); border: 1px solid rgba(255, 107, 107, 0.35);
  white-space: nowrap;
}
.sl-spacer { flex: 1; }
.sl-range { width: auto; padding: 4px 8px; font-size: 12px; }

.sl-body { padding: 4px 14px 14px; border-top: 1px solid var(--border-soft); }
.sl-msg { padding: 20px 0; text-align: center; font-size: 13px; }
.sl-msg.err { color: var(--danger); }
.sl-chart { width: 100%; height: 230px; }

.sl-crit { margin-top: 8px; display: flex; flex-direction: column; gap: 5px; }
.sl-crit-h { display: flex; align-items: center; gap: 6px; font-size: 12.5px; font-weight: 600; color: var(--danger); flex-wrap: wrap; }
.sl-crit-h .muted { font-weight: 400; font-size: 11.5px; }
.sl-band {
  display: grid; grid-template-columns: 90px 170px 1fr; gap: 10px; align-items: baseline;
  padding: 5px 8px; border-radius: 7px; background: var(--panel-2); font-size: 12px;
}
.sl-band-when { font-weight: 600; }
.sl-band-n { color: var(--danger); }
.sl-band-who { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sl-ok { font-size: 12.5px; padding: 4px 0; }
</style>
