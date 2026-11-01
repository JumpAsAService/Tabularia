<script setup lang="ts">
// A chart tile: the numbers the server worked out, drawn in the app's theme by
// composables/dashboardCharts.ts. The option is replaced whole, or a series that went away would
// stay on the canvas. No entrance animation: the numbers change, the picture does not perform.
//
// A chart is made when it comes near the window: all forty of a large dashboard at once kept the
// page from answering for half a second (measured on Chrome, 540 ms); the ones out of sight wait.
//
// A click on a bar, a slice or a cell names its raw value (composables/dashboardClicks.ts), going up
// as a choice; Ctrl or Cmd adds it. A line's points are small, and hidden when crowded: there a click
// anywhere over a category names it. The canvas cannot be reached from the keyboard, so a list of the
// chart's categories shows when it takes the focus; choosing there adds a value, as Ctrl does.
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { BarChart, FunnelChart, GaugeChart, HeatmapChart, LineChart, PieChart, ScatterChart, TreemapChart } from 'echarts/charts'
import { GridComponent, LegendComponent, TooltipComponent, VisualMapContinuousComponent } from 'echarts/components'
import VChart from 'vue-echarts'
import Select from '~/components/ui/Select.vue'
import type { KindCtx } from '~/composables/chartKinds'
import { greySeries, tileChartOption } from '~/composables/dashboardCharts'
import { clickedValue, valueText, type ClickValue } from '~/composables/dashboardClicks'
import type { TileNumbers } from '~/composables/useDashboards'

use([
  CanvasRenderer, BarChart, LineChart, PieChart, TreemapChart, ScatterChart, GaugeChart, FunnelChart, HeatmapChart,
  GridComponent, LegendComponent, TooltipComponent, VisualMapContinuousComponent,
])

const props = defineProps<{ answer: TileNumbers; ctx: KindCtx; label: string; small?: boolean; filters?: boolean; chosen?: ClickValue[] }>()
const emit = defineEmits<{ (e: 'pick', value: ClickValue, add: boolean): void }>()
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

const option = computed(() => tileChartOption(props.ctx, props.answer, { small: props.small, width: width.value, chosen: props.chosen }))

// ── a click ──────────────────────────────────────────────────────────────────
const drawn = ref<any>(null)
const byPlace = computed(() => props.answer.kind === 'line' || props.answer.kind === 'area')
const adding = (e: any) => !!(e?.ctrlKey || e?.metaKey)
function onItem(p: any) {
  if (!props.filters || byPlace.value) return
  const value = clickedValue(props.answer, p)
  if (value !== undefined) emit('pick', value, adding(p.event?.event))
}
function onPlot(e: any) {
  if (!props.filters || !byPlace.value || !drawn.value) return
  const pixel = [e.offsetX, e.offsetY]
  if (!drawn.value.containPixel({ gridIndex: 0 }, pixel)) return
  const value = clickedValue(props.answer, { dataIndex: Math.round(drawn.value.convertFromPixel({ gridIndex: 0 }, pixel)?.[0]) })
  // the axis's category alone: over a line, nothing says which series was meant
  if (value !== undefined) emit('pick', Array.isArray(value) ? value[0] : value, adding(e.event))
}

// the keyboard's way: the categories of the chart, in a list that shows when it has the focus
const choices = computed(() => {
  const d = props.answer.data ?? {}
  if (!props.filters || props.answer.kind === 'scatter' || !d.keys?.length) return []
  const set = new Set((props.chosen ?? []).map(valueText))
  return (d.keys as any[]).map((key, i) => ({ value: i, label: `${set.has(valueText(key)) ? '✓ ' : ''}${d.cats?.[i] ?? d.xCats?.[i] ?? ''}` }))
})
function onChoice(i: number) {
  emit('pick', props.answer.data.keys[i], true)
}
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
      <VChart v-if="near" ref="drawn" class="drawn" :option="option" :update-options="{ notMerge: true }" autoresize role="img" :aria-label="$t('dashboard.chartLabel', { name: label })" @click="onItem" @zr:click="onPlot" />
      <div v-else class="drawn" />
      <Select
        v-if="choices.length"
        class="bykeys"
        :model-value="null"
        :options="choices"
        :placeholder="$t('dashboard.chartFilter')"
        :aria-label="$t('dashboard.chartFilterLabel', { name: label })"
        @update:model-value="onChoice"
      />
      <p v-for="n in notes" :key="n" class="note">{{ n }}</p>
    </template>
  </div>
</template>

<style scoped>
.tc { position: relative; display: flex; flex-direction: column; flex: 1; min-height: 0; min-width: 0; }
/* out of the eye's way until the keyboard reaches it; a pointer has the chart */
.bykeys { position: absolute; top: 0; right: 0; width: 170px; max-width: 100%; opacity: 0; pointer-events: none; z-index: 2; }
.bykeys:focus-visible, .bykeys:focus, .bykeys.open { opacity: 1; pointer-events: auto; }
.drawn { flex: 1; min-height: 0; width: 100%; }
.note { margin: 2px 0 0; font-size: 11.5px; line-height: 1.35; color: var(--muted); }
.nodata { margin: auto; font-size: 12.5px; color: var(--muted); }
</style>
