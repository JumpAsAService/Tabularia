<script setup lang="ts">
// A chart tile: the numbers the server worked out, drawn in the app's theme by
// composables/dashboardCharts.ts. The option is replaced whole, or a series that went away would
// stay on the canvas. No entrance animation: the numbers change, the picture does not perform.
//
// A chart is made when it comes near the window: all forty of a large dashboard at once kept the
// page from answering for half a second (measured on Chrome, 540 ms); the ones out of sight wait.
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { BarChart, FunnelChart, GaugeChart, HeatmapChart, LineChart, PieChart, ScatterChart, TreemapChart } from 'echarts/charts'
import { GridComponent, LegendComponent, TooltipComponent, VisualMapContinuousComponent } from 'echarts/components'
import VChart from 'vue-echarts'
import type { KindCtx } from '~/composables/chartKinds'
import { greySeries, tileChartOption } from '~/composables/dashboardCharts'
import type { TileNumbers } from '~/composables/useDashboards'

use([
  CanvasRenderer, BarChart, LineChart, PieChart, TreemapChart, ScatterChart, GaugeChart, FunnelChart, HeatmapChart,
  GridComponent, LegendComponent, TooltipComponent, VisualMapContinuousComponent,
])

const props = defineProps<{ answer: TileNumbers; ctx: KindCtx; label: string; small?: boolean }>()
const { t } = useI18n()

const box = ref<HTMLElement | null>(null)
const near = ref(typeof IntersectionObserver === 'undefined')
let watching: IntersectionObserver | null = null
// by steps: the names along an axis fit the tile, and a window being dragged draws no chart at every pixel
const WIDTH_STEP_PX = 40
const width = ref(0)
const measure = () => { width.value = Math.round((box.value?.clientWidth ?? 0) / WIDTH_STEP_PX) * WIDTH_STEP_PX }
let resizing: ResizeObserver | null = null
onMounted(() => {
  measure()
  if (typeof ResizeObserver !== 'undefined' && box.value) {
    resizing = new ResizeObserver(measure)
    resizing.observe(box.value)
  }
  if (near.value || !box.value) return
  watching = new IntersectionObserver((entries) => {
    if (!entries.some((e) => e.isIntersecting)) return
    near.value = true
    watching?.disconnect()
  }, { rootMargin: '300px' })
  watching.observe(box.value)
})
onBeforeUnmount(() => {
  watching?.disconnect()
  resizing?.disconnect()
})

const option = computed(() => tileChartOption(props.ctx, props.answer, { small: props.small, width: width.value }))
const notes = computed(() => {
  const a = props.answer
  const out: string[] = []
  if (a.data?.left_out) out.push(t('chartPanel.notDrawn', { n: a.data.left_out }, a.data.left_out))
  const grey = greySeries(a)
  if (grey) out.push(t('dashboard.greySeries', { n: grey }, grey))
  return out
})
</script>

<template>
  <div ref="box" class="tc">
    <p v-if="answer.empty" class="nodata">{{ $t('dashboard.noData') }}</p>
    <template v-else>
      <VChart v-if="near" class="drawn" :option="option" :update-options="{ notMerge: true }" autoresize role="img" :aria-label="$t('dashboard.chartLabel', { name: label })" />
      <div v-else class="drawn" />
      <p v-for="n in notes" :key="n" class="note">{{ n }}</p>
    </template>
  </div>
</template>

<style scoped>
.tc { display: flex; flex-direction: column; flex: 1; min-height: 0; min-width: 0; }
.drawn { flex: 1; min-height: 0; width: 100%; }
.note { margin: 2px 0 0; font-size: 11.5px; line-height: 1.35; color: var(--muted); }
.nodata { margin: auto; font-size: 12.5px; color: var(--muted); }
</style>
