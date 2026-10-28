<script setup lang="ts">
// An item of the report in the design view, drawn on the sheet at its place in mm: a text
// with its style (an expression's quoted parts as they print, the rest as expressions), an
// image, a line, a rectangle, a table's rows, a chart's or a KPI's outline. A press starts
// a move (the canvas follows it); the handles are the canvas's.
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { CircleAlert, ImagePlus } from 'lucide-vue-next'
import RdChartGlyph from '~/components/report/designer/RdChartGlyph.vue'
import RdTableFace from '~/components/report/designer/RdTableFace.vue'
import { concatParts } from '~/composables/reportExpressions'
import { PAPER, PT_MM, boxOf, type DDefinition, type DItem, type DStyle } from '~/composables/reportDesignModel'

const props = defineProps<{
  item: DItem
  def: DDefinition
  ppm: number
  selected?: boolean
  invalid?: boolean
  conflict?: boolean
  dropTarget?: boolean
  tagLeft?: number
}>()
const emit = defineEmits<{ (e: 'grab', ev: PointerEvent): void; (e: 'open', ev: MouseEvent): void; (e: 'pick'): void }>()
const { t } = useI18n()

const box = computed(() => boxOf(props.item))
const st = computed<DStyle>(() => props.item.style ?? {})
const px = (mm: number | null | undefined) => `${(mm ?? 0) * props.ppm}px`

const weight = (w: DStyle['weight']) => (w === 'bold' ? 700 : w === 'semibold' ? 600 : 400)

const textStyle = computed(() => {
  const s = st.value
  return {
    fontSize: `${(s.size ?? props.def.theme.size) * PT_MM * props.ppm}px`,
    fontWeight: weight(s.weight),
    fontStyle: s.italic ? 'italic' : 'normal',
    color: s.color || props.def.theme.color,
    background: s.background || 'transparent',
    padding: px(s.padding),
    border: s.border ? `${Math.max(1, (s.border.width ?? 0.2) * props.ppm)}px solid ${s.border.color ?? '#1f2933'}` : 'none',
    borderRadius: px(s.radius),
    justifyContent: s.align === 'right' ? 'flex-end' : s.align === 'center' ? 'center' : 'flex-start',
    textAlign: (s.align ?? 'left') as 'left' | 'right' | 'center',
    alignItems: s.valign === 'bottom' ? 'flex-end' : s.valign === 'middle' ? 'center' : 'flex-start',
  }
})

const textParts = computed(() => {
  if (props.item.type !== 'text') return []
  const v = props.item.value ?? ''
  return v.startsWith('=') ? concatParts(v) : [{ lit: true, text: v }]
})

const lineStroke = computed(() => Math.max(1, (st.value.border?.width ?? 0.3) * props.ppm))

const rootStyle = computed(() => ({
  left: px(box.value.x),
  top: px(box.value.y),
  width: `${Math.max(box.value.w * props.ppm, props.item.type === 'line' ? 1 : 4)}px`,
  height: `${Math.max(box.value.h * props.ppm, props.item.type === 'line' ? 1 : 4)}px`,
}))

const kindLabel = computed(() => (props.item.type === 'chart' ? t(`reportDesigner.kinds.${props.item.kind}`) : ''))
</script>

<template>
  <div
    class="ritem"
    :class="[item.type, { selected, invalid, conflict, drop: dropTarget }]"
    :style="rootStyle"
    :data-item="item.id"
    tabindex="0"
    role="button"
    :aria-label="t('reportDesigner.canvas.itemLabel', { type: t(`reportDesigner.items.${item.type === 'table' && item.matrix ? 'matrix' : item.type}`), id: item.id })"
    :aria-pressed="!!selected"
    @focus="emit('pick')"
    @pointerdown="emit('grab', $event)"
    @dblclick="emit('open', $event)"
  >
    <!-- text -->
    <div v-if="item.type === 'text'" class="rtext" :style="textStyle">
      <span v-if="!item.value" class="rhole">{{ $t('reportDesigner.face.emptyText') }}</span>
      <span v-else class="rflow">
        <template v-for="(p, i) in textParts" :key="i">
          <span v-if="p.lit">{{ p.text }}</span>
          <span v-else class="rchip">{{ p.text }}</span>
        </template>
      </span>
    </div>

    <!-- image: only ever an <img>, from the data URL the person uploaded -->
    <template v-else-if="item.type === 'image'">
      <img v-if="item.asset.startsWith('data:image/')" class="rimg" :src="item.asset" alt="" :style="{ objectFit: item.fit === 'stretch' ? 'fill' : item.fit ?? 'contain' }" draggable="false" />
      <div v-else class="rplace"><ImagePlus :size="Math.max(12, Math.min(28, box.h * ppm * 0.4))" /></div>
    </template>

    <!-- line: from (x, y) to (x + w, y + h) -->
    <svg v-else-if="item.type === 'line'" class="rline" :width="Math.max(box.w * ppm, lineStroke)" :height="Math.max(box.h * ppm, lineStroke)" aria-hidden="true">
      <line
        :x1="(item.w >= 0 ? 0 : -item.w) * ppm" :y1="(item.h >= 0 ? 0 : -item.h) * ppm + (item.h === 0 ? lineStroke / 2 : 0)"
        :x2="(item.w >= 0 ? item.w : 0) * ppm" :y2="(item.h >= 0 ? item.h : 0) * ppm + (item.h === 0 ? lineStroke / 2 : 0)"
        :stroke="st.border?.color ?? '#1f2933'" :stroke-width="lineStroke"
      />
    </svg>

    <!-- rectangle -->
    <div
      v-else-if="item.type === 'rect'"
      class="rrect"
      :style="{ background: st.background || 'transparent', borderRadius: px(st.radius),
                border: st.border ? `${Math.max(1, (st.border.width ?? 0.2) * ppm)}px solid ${st.border.color ?? '#1f2933'}` : 'none' }"
    />

    <!-- table, matrix, list -->
    <RdTableFace v-else-if="item.type === 'table'" :table="item" :def="def" :ppm="ppm" :tags="selected" :tag-left="tagLeft" />

    <!-- chart: its kind's outline -->
    <div v-else-if="item.type === 'chart'" class="rchart" :style="{ color: def.theme.accent, fontSize: `${8 * PT_MM * ppm}px` }">
      <div v-if="item.title" class="rtitle">{{ item.title }}</div>
      <div class="rglyph"><RdChartGlyph :kind="item.kind" :size="Math.max(24, Math.min(box.w, box.h * 1.4) * ppm * 0.6)" /></div>
      <div class="rcap">{{ kindLabel }}<template v-if="item.category"> · <span class="rmono">{{ item.category }}</span></template></div>
    </div>

    <!-- KPI card -->
    <div
      v-else-if="item.type === 'kpi'"
      class="rkpi"
      :style="{ background: st.background || PAPER.band, borderRadius: px(st.radius ?? 1.5), padding: px(2.5), fontSize: `${7.5 * PT_MM * ppm}px` }"
    >
      <div class="rklabel">{{ item.label || $t('reportDesigner.face.kpiLabel') }}</div>
      <div class="rkvalue" :style="{ fontSize: `${(st.size ?? 16) * PT_MM * ppm}px`, color: st.color || def.theme.color }">
        <span v-if="item.value" class="rchip big">{{ item.value }}</span>
        <span v-else class="rhole">{{ $t('reportDesigner.face.value') }}</span>
      </div>
      <div v-if="item.compare" class="rkcmp">↑ <span class="rmono">{{ item.compare }}</span></div>
      <svg v-if="item.sparkline" class="rspark" viewBox="0 0 100 20" preserveAspectRatio="none" aria-hidden="true">
        <polyline points="0,16 14,12 28,14 42,8 56,10 70,5 84,7 100,2" fill="none" :stroke="def.theme.accent" stroke-width="1.6" vector-effect="non-scaling-stroke" />
      </svg>
    </div>

    <span v-if="invalid" class="rbadge" :title="$t('reportDesigner.face.hasProblem')"><CircleAlert :size="12" /></span>
  </div>
</template>

<style scoped>
.ritem { position: absolute; box-sizing: border-box; cursor: move; user-select: none; touch-action: none; }
.ritem::after {
  content: ''; position: absolute; inset: -1px; border: 1px dashed transparent; border-radius: 2px; pointer-events: none;
  transition: border-color 0.12s;
}
.ritem:hover::after { border-color: color-mix(in srgb, var(--accent) 55%, transparent); }
.ritem.selected::after { border: 1px solid var(--accent); }
.ritem.invalid::after { border: 1px solid var(--danger); }
.ritem.conflict::after { border: 1.5px solid var(--danger); background: color-mix(in srgb, var(--danger) 8%, transparent); }
.ritem.drop::after { border: 2px solid var(--accent); background: color-mix(in srgb, var(--accent) 8%, transparent); }
.ritem.line::after { inset: -3px; }
.ritem:focus { outline: none; }
.ritem:focus-visible::after { border: 2px solid var(--accent); inset: -3px; }

.rtext { width: 100%; height: 100%; display: flex; overflow: hidden; line-height: 1.2; font-family: 'IBM Plex Sans', system-ui, sans-serif; box-sizing: border-box; }
.rflow { min-width: 0; overflow-wrap: anywhere; }
.rchip {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.82em; font-weight: 500; font-style: normal;
  color: #2f4ac0; background: rgba(59, 91, 219, 0.09); border-radius: 3px; padding: 0 0.3em; margin: 0 0.08em;
  box-decoration-break: clone; -webkit-box-decoration-break: clone;
}
.rchip.big { font-size: 0.6em; }
.rhole { color: #9aa3b5; font-style: italic; font-weight: 400; font-size: 0.92em; }
.rimg { width: 100%; height: 100%; display: block; pointer-events: none; }
.rplace {
  width: 100%; height: 100%; display: grid; place-items: center; color: #9aa3b5;
  border: 1px dashed #c3c9d5; border-radius: 2px; background: #fafbfc;
}
.rline { display: block; overflow: visible; }
.rrect { width: 100%; height: 100%; box-sizing: border-box; }
.rchart {
  width: 100%; height: 100%; display: flex; flex-direction: column; overflow: hidden;
  border: 1px solid #e6e9ef; border-radius: 2px; background: #fbfcfe; box-sizing: border-box; padding: 4px 6px;
}
.rtitle { color: #1f2933; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.rglyph { flex: 1; min-height: 0; display: grid; place-items: center; opacity: 0.55; }
.rcap { color: #6b7590; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.rmono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.rkpi { position: relative; width: 100%; height: 100%; box-sizing: border-box; overflow: hidden; color: #1f2933; display: flex; flex-direction: column; gap: 0.3em; }
.rklabel { color: #6b7590; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.rkvalue { font-weight: 700; line-height: 1.1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.rkcmp { color: #2b8a3e; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.rspark { position: absolute; left: 8%; right: 8%; bottom: 8%; width: 84%; height: 24%; }
.rbadge {
  position: absolute; top: -8px; right: 18%; display: grid; place-items: center; width: 16px; height: 16px;
  border-radius: 50%; background: var(--danger-fill); color: var(--on-danger); pointer-events: none;
}
.ritem.table { overflow: visible; }
.ritem:not(.table) > :not(.rbadge) { pointer-events: none; }
</style>
