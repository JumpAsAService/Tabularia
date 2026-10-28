<script setup lang="ts">
// A KPI: the number, what it measures, its change and its trend line. Shared by the
// Viewer's chart (one aggregate over the whole dataset) and the chat's (the rows the
// assistant returned). The number is text in the page, not paint on a canvas: it can be
// read aloud, selected and copied; only the trend line is a chart.
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { LineChart } from 'echarts/charts'
import { GridComponent, TooltipComponent } from 'echarts/components'
import VChart from 'vue-echarts'
import { ArrowDownRight, ArrowRight, ArrowUpRight } from 'lucide-vue-next'
import { useFormat } from '~/composables/useFormat'
import { useChartTheme } from '~/composables/useChartTheme'
import { headline, sparkOption, useKindCtx, type KpiChange, type Spark } from '~/composables/chartKinds'

use([CanvasRenderer, LineChart, GridComponent, TooltipComponent])

const props = defineProps<{
  label: string
  value: number | null
  change?: KpiChange | null
  spark?: Spark | null
  /** the chat's size: smaller, aligned to the left */
  dense?: boolean
}>()

const { t } = useI18n()
const fmtx = useFormat()
const { ui } = useChartTheme()
const ctx = useKindCtx(ui)

const shown = computed(() => (props.value == null ? '—' : headline(ctx.value, props.value)))
// the exact figure under a shortened one: «4.2M» alone loses what the data says
const exact = computed(() => (props.value == null ? '' : fmtx.number(props.value, 2)))
const dir = computed(() => {
  const r = props.change?.ratio ?? 0
  return r > 0 ? 'up' : r < 0 ? 'down' : 'flat'
})
const arrow = computed(() => ({ up: ArrowUpRight, down: ArrowDownRight, flat: ArrowRight })[dir.value])
const changeText = computed(() =>
  props.change
    ? new Intl.NumberFormat(fmtx.locale.value, { style: 'percent', maximumFractionDigits: 1, signDisplay: 'exceptZero' })
      .format(props.change.ratio)
    : '',
)
const sparkOpt = computed(() => (props.spark ? sparkOption(ctx.value, { ...props.spark, name: props.label }) : null))
</script>

<template>
  <div class="kpi" :class="{ dense }">
    <p class="kpi-label">{{ label }}</p>
    <p class="kpi-value">{{ shown }}</p>
    <p v-if="exact && exact !== shown" class="kpi-exact">{{ exact }}</p>
    <p v-if="change" class="kpi-change" :class="dir">
      <component :is="arrow" :size="dense ? 13 : 15" aria-hidden="true" />
      <span class="kpi-pct">{{ changeText }}</span>
      <span class="kpi-vs">{{ change.text }}</span>
    </p>
    <VChart v-if="sparkOpt" class="kpi-spark" :option="sparkOpt" autoresize :aria-label="t('chartPanel.trendOf', { name: label })" role="img" />
  </div>
</template>

<style scoped>
.kpi {
  display: flex;
  flex-direction: column;
  justify-content: center;
  width: 100%;
  max-width: 640px;
  height: 100%;
  margin: 0 auto;
  padding: 8px 4px;
  min-width: 0;
}
.kpi p { margin: 0; }
.kpi-label { font-size: 13px; color: var(--muted); overflow-wrap: anywhere; }
/* the figure in the interface's sans, proportional digits: at this size tabular ones read loose */
.kpi-value { font-size: 52px; font-weight: 600; line-height: 1.1; letter-spacing: -0.01em; color: var(--text); margin-top: 4px; }
.kpi-exact { font-size: 12px; color: var(--muted); font-variant-numeric: tabular-nums; }
.kpi-change { display: inline-flex; align-items: center; gap: 6px; margin-top: 10px; font-size: 13px; flex-wrap: wrap; }
.kpi-change.up { color: var(--accent-2); }
.kpi-change.down { color: var(--danger); }
.kpi-change.flat { color: var(--muted); }
.kpi-pct { font-weight: 600; }
.kpi-vs { color: var(--muted); }
.kpi-spark { width: 100%; height: 140px; margin-top: 16px; }

.kpi.dense { max-width: 420px; margin: 0; height: auto; padding: 6px 0 2px; }
.kpi.dense .kpi-value { font-size: 34px; }
.kpi.dense .kpi-change { margin-top: 6px; font-size: 12px; }
.kpi.dense .kpi-spark { height: 64px; margin-top: 8px; }
</style>
