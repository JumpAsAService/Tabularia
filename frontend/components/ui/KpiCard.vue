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
  /** the figure already written in its format (a dashboard's tile, by the server) */
  text?: string | null
  /** on a dashboard's tile: the figure takes the room there is, the trend line what is left, and
   *  it does not animate at every refresh */
  fit?: boolean
}>()

const { t } = useI18n()
const fmtx = useFormat()
const { ui } = useChartTheme()
const ctx = useKindCtx(ui)

const shown = computed(() => props.text ?? (props.value == null ? '—' : headline(ctx.value, props.value)))
// the exact figure under a shortened one: «4.2M» alone loses what the data says. One written in
// its format is what its author wanted to say
const exact = computed(() => (props.value == null || props.text != null ? '' : fmtx.number(props.value, 2)))
const dir = computed(() => {
  const r = props.change?.ratio ?? 0
  return r > 0 ? 'up' : r < 0 ? 'down' : 'flat'
})
const arrow = computed(() => ({ up: ArrowUpRight, down: ArrowDownRight, flat: ArrowRight })[dir.value])
const changeText = computed(() =>
  props.change
    ? props.change.shown ?? new Intl.NumberFormat(fmtx.locale.value, { style: 'percent', maximumFractionDigits: 1, signDisplay: 'exceptZero' })
      .format(props.change.ratio)
    : '',
)
const sparkOpt = computed(() => (props.spark
  ? { ...sparkOption(ctx.value, { ...props.spark, name: props.label }), ...(props.fit ? { animation: false } : {}) }
  : null))
</script>

<template>
  <div class="kpi" :class="{ dense, fit }">
    <p class="kpi-label">{{ label }}</p>
    <p class="kpi-value">{{ shown }}</p>
    <p v-if="exact && exact !== shown" class="kpi-exact">{{ exact }}</p>
    <p v-if="change" class="kpi-change" :class="dir">
      <component :is="arrow" :size="dense ? 13 : 15" aria-hidden="true" />
      <span class="kpi-pct">{{ changeText }}</span>
      <span v-if="change.text" class="kpi-vs">{{ change.text }}</span>
    </p>
    <VChart v-if="sparkOpt" class="kpi-spark" :option="sparkOpt" :update-options="{ notMerge: true }" autoresize :aria-label="t('chartPanel.trendOf', { name: label })" role="img" />
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

/* on a tile, whose size the author chose: the figure grows with the card, within reason, and the
   trend line is what stays when there is no room (a short card says the number, nothing else) */
.kpi.fit {
  container-type: size;
  max-width: none;
  margin: 0;
  padding: 0;
  gap: 2px;
  justify-content: inherit;
  text-align: inherit;
}
.kpi.fit .kpi-label { font-size: 12.5px; line-height: 1.3; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; overflow-wrap: normal; }
.kpi.fit .kpi-value {
  font-size: var(--kpi-size, clamp(18px, min(14cqw, 34cqh), 40px));
  margin-top: 0;
  color: inherit;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.kpi.fit .kpi-change { margin-top: 0; font-size: 12px; flex-wrap: nowrap; white-space: nowrap; overflow: hidden; }
.kpi.fit .kpi-spark { flex: 1 1 0; min-height: 0; height: auto; margin-top: 4px; }
@container (max-height: 96px) { .kpi.fit .kpi-spark { display: none; } }
@container (max-height: 62px) { .kpi.fit .kpi-change { display: none; } }
</style>
