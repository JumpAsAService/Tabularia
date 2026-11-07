<script setup lang="ts">
// A KPI: the number, what it measures, its change and its trend line. Shared by the
// Viewer's chart (one aggregate over the whole dataset) and the chat's (the rows the
// assistant returned). The number is text in the page, not paint on a canvas: it can be
// read aloud, selected and copied; only the trend line is a chart.
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
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
import { fitsWhole } from '~/composables/kpiFit'

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
  /** on a tile: the same figure in compact notation (kpiFit.compactFigure), shown only when the
   *  whole one does not fit even at the smallest size a figure is drawn */
  short?: string | null
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
// ── on a tile: the figure whole, never cut ──
// The style sizes it (`--kpi-em`: its width for each px of its size, measured once by the ruler);
// here only the choice between the whole figure and its compact form, when the card or the text
// changes. The card's size does not follow its content (a size container), so the observer
// does not loop.
const card = ref<HTMLElement | null>(null)
const figure = ref<HTMLElement | null>(null)
const rulerWhole = ref<HTMLElement | null>(null)
const rulerShort = ref<HTMLElement | null>(null)
const squeezed = ref(false)
const short = computed(() => (props.fit && props.short && props.short !== shown.value ? props.short : null))
const RULER_PX = 100
const emOf = (el: HTMLElement | null) => (el ? el.getBoundingClientRect().width / RULER_PX : 0)

function refit() {
  const el = figure.value
  if (!props.fit || !el) return
  const whole = emOf(rulerWhole.value)
  const compact = emOf(rulerShort.value)
  // the size the style gives the whole figure here: the floor when it is too wide
  el.style.setProperty('--kpi-em', String(whole || 0.01))
  const size = parseFloat(getComputedStyle(el).fontSize)
  squeezed.value = !!short.value && compact > 0 && compact < whole && !fitsWhole(whole, size, el.clientWidth)
  el.style.setProperty('--kpi-em', String((squeezed.value ? compact : whole) || 0.01))
}
let sized: ResizeObserver | null = null
const onFonts = () => refit()
onMounted(() => {
  if (!props.fit) return
  if (typeof ResizeObserver !== 'undefined' && card.value) {
    sized = new ResizeObserver(() => refit())
    sized.observe(card.value)
  }
  // a figure measured in the fallback font is narrower or wider than in its own
  document.fonts?.addEventListener?.('loadingdone', onFonts)
  document.fonts?.ready.then(onFonts)
  refit()
})
watch([shown, short], () => nextTick(refit))
onBeforeUnmount(() => {
  sized?.disconnect()
  document.fonts?.removeEventListener?.('loadingdone', onFonts)
})

const sparkOpt = computed(() => (props.spark
  ? { ...sparkOption(ctx.value, { ...props.spark, name: props.label }), ...(props.fit ? { animation: false } : {}) }
  : null))
</script>

<template>
  <div ref="card" class="kpi" :class="{ dense, fit }">
    <p class="kpi-label">{{ label }}</p>
    <p ref="figure" class="kpi-value" :title="squeezed ? shown : undefined">{{ squeezed ? short : shown }}</p>
    <span v-if="fit" class="kpi-rulers" aria-hidden="true">
      <span ref="rulerWhole" class="kpi-ruler">{{ shown }}</span>
      <span v-if="short" ref="rulerShort" class="kpi-ruler">{{ short }}</span>
    </span>
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
  position: relative;
  container-type: size;
  max-width: none;
  margin: 0;
  padding: 0;
  gap: 2px;
  justify-content: inherit;
  text-align: inherit;
}
.kpi.fit .kpi-label { font-size: 12.5px; line-height: 1.3; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; overflow-wrap: normal; }
/* the figure is never cut: the size the card gives it (or its author), smaller when it is wider than
   the card (`--kpi-em`, its width for each px of its size, is measured by the ruler), down to a floor
   (16px, kpiFit.FIGURE_FLOOR_PX, or the author's size when smaller), past which the component writes it
   in compact notation */
.kpi.fit .kpi-value {
  --kpi-base: var(--kpi-size, clamp(18px, min(14cqw, 34cqh), 40px));
  --kpi-fit: calc(100cqw / var(--kpi-em, 0.01) * 0.98);
  font-size: max(min(16px, var(--kpi-base)), min(var(--kpi-base), var(--kpi-fit)));
  margin-top: 0;
  color: inherit;
  white-space: nowrap;
  overflow: hidden;
}
/* the same texts at a known size, where they are neither seen nor take room: in a box of no size
   that clips them, whose measures they keep */
.kpi.fit .kpi-rulers { position: absolute; top: 0; left: 0; width: 0; height: 0; overflow: hidden; visibility: hidden; pointer-events: none; }
.kpi.fit .kpi-ruler { position: absolute; top: 0; left: 0; white-space: pre; font-size: 100px; font-weight: 600; letter-spacing: -0.01em; line-height: 1; }
/* on paper the card is what the page gives it, and the screen's choice is not made again: no floor */
@media print {
  .kpi.fit .kpi-value { font-size: min(var(--kpi-base), var(--kpi-fit)); }
}
.kpi.fit .kpi-change { margin-top: 0; font-size: 12px; flex-wrap: nowrap; white-space: nowrap; overflow: hidden; }
.kpi.fit .kpi-spark { flex: 1 1 0; min-height: 0; height: auto; margin-top: 4px; }
@container (max-height: 96px) { .kpi.fit .kpi-spark { display: none; } }
@container (max-height: 62px) { .kpi.fit .kpi-change { display: none; } }
</style>
