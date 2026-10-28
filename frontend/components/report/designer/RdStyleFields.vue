<script setup lang="ts">
// The style of an item, a column or a conditional rule (`style` of the definition): only
// the properties that kind of thing uses. Empty means the theme's.
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  AlignVerticalJustifyCenter, AlignVerticalJustifyEnd, AlignVerticalJustifyStart, Italic, TextAlignCenter,
  TextAlignEnd, TextAlignStart,
} from 'lucide-vue-next'
import RdNum from '~/components/report/designer/RdNum.vue'
import RdColor from '~/components/report/designer/RdColor.vue'
import RdSeg from '~/components/report/designer/RdSeg.vue'
import RdFormatInput from '~/components/report/designer/RdFormatInput.vue'
import Select from '~/components/ui/Select.vue'
import type { DStyle } from '~/composables/reportDesignModel'

type Part = 'font' | 'color' | 'fill' | 'align' | 'valign' | 'padding' | 'border' | 'radius' | 'format' | 'grow' | 'stroke'
const props = defineProps<{ obj: { style?: DStyle }; parts: Part[]; item: string | null; path: string }>()
const { t } = useI18n()

const has = (p: Part) => props.parts.includes(p)
function st(): DStyle {
  if (!props.obj.style) props.obj.style = {}
  return props.obj.style
}
const style = computed<DStyle>(() => props.obj.style ?? {})
function set<K extends keyof DStyle>(k: K, v: DStyle[K] | null) {
  const s = st()
  if (v == null || v === '') delete s[k]
  else s[k] = v as DStyle[K]
}

const weights = computed(() => [
  { value: null, label: t('reportDesigner.style.weightTheme') },
  { value: 'normal', label: t('reportDesigner.style.normal') },
  { value: 'semibold', label: t('reportDesigner.style.semibold') },
  { value: 'bold', label: t('reportDesigner.style.bold') },
])
const aligns = computed(() => [
  { value: 'left', label: t('reportDesigner.style.left'), icon: TextAlignStart },
  { value: 'center', label: t('reportDesigner.style.center'), icon: TextAlignCenter },
  { value: 'right', label: t('reportDesigner.style.right'), icon: TextAlignEnd },
])
const valigns = computed(() => [
  { value: 'top', label: t('reportDesigner.style.top'), icon: AlignVerticalJustifyStart },
  { value: 'middle', label: t('reportDesigner.style.middle'), icon: AlignVerticalJustifyCenter },
  { value: 'bottom', label: t('reportDesigner.style.bottom'), icon: AlignVerticalJustifyEnd },
])

function toggleBorder(on: boolean) {
  set('border', on ? { width: 0.2, color: '#1f2933' } : null)
}
function borderPart(k: 'width' | 'color', v: number | string | null) {
  const b = { width: 0.2, color: '#1f2933', ...(style.value.border ?? {}) }
  if (v == null) return
  ;(b as any)[k] = v
  set('border', b)
}
</script>

<template>
  <div class="rsf">
    <div v-if="has('font')" class="rsf-row">
      <label class="rsf-f small">
        <span>{{ $t('reportDesigner.style.size') }}</span>
        <RdNum :model-value="style.size ?? null" unit="pt" :step="0.5" :min="4" :max="96" nullable :placeholder="$t('reportDesigner.style.themeShort')" @update:model-value="set('size', $event)" />
      </label>
      <label class="rsf-f">
        <span>{{ $t('reportDesigner.style.weight') }}</span>
        <Select :model-value="style.weight ?? null" :options="weights" :aria-label="$t('reportDesigner.style.weight')" @update:model-value="set('weight', $event)" />
      </label>
      <div class="rsf-f tiny">
        <span>&nbsp;</span>
        <button type="button" class="rsf-tog" :class="{ on: style.italic }" :aria-pressed="!!style.italic" :aria-label="$t('reportDesigner.style.italic')" :title="$t('reportDesigner.style.italic')" @click="set('italic', style.italic ? null : true)">
          <Italic :size="14" />
        </button>
      </div>
    </div>
    <div v-if="has('align') || has('valign')" class="rsf-row">
      <div v-if="has('align')" class="rsf-f">
        <span>{{ $t('reportDesigner.style.align') }}</span>
        <RdSeg :model-value="style.align ?? 'left'" :options="aligns" :label="$t('reportDesigner.style.align')" icons-only @update:model-value="set('align', $event === 'left' ? null : $event)" />
      </div>
      <div v-if="has('valign')" class="rsf-f">
        <span>{{ $t('reportDesigner.style.valign') }}</span>
        <RdSeg :model-value="style.valign ?? 'top'" :options="valigns" :label="$t('reportDesigner.style.valign')" icons-only @update:model-value="set('valign', $event === 'top' ? null : $event)" />
      </div>
    </div>
    <div v-if="has('color') || has('fill')" class="rsf-row">
      <div v-if="has('color')" class="rsf-f">
        <span>{{ $t('reportDesigner.style.color') }}</span>
        <RdColor :model-value="style.color" clearable :label="$t('reportDesigner.style.color')" :placeholder="$t('reportDesigner.style.themeShort')" @update:model-value="set('color', $event)" />
      </div>
      <div v-if="has('fill')" class="rsf-f">
        <span>{{ $t('reportDesigner.style.fill') }}</span>
        <RdColor :model-value="style.background" clearable :label="$t('reportDesigner.style.fill')" :placeholder="$t('reportDesigner.style.none')" @update:model-value="set('background', $event)" />
      </div>
    </div>
    <div v-if="has('stroke')" class="rsf-row">
      <label class="rsf-f small">
        <span>{{ $t('reportDesigner.style.lineWidth') }}</span>
        <RdNum :model-value="style.border?.width ?? 0.3" unit="mm" :step="0.1" :min="0.1" :max="5" @update:model-value="borderPart('width', $event)" />
      </label>
      <div class="rsf-f">
        <span>{{ $t('reportDesigner.style.lineColor') }}</span>
        <RdColor :model-value="style.border?.color ?? '#1f2933'" :label="$t('reportDesigner.style.lineColor')" @update:model-value="borderPart('color', $event)" />
      </div>
    </div>
    <template v-if="has('border')">
      <label class="rsf-check">
        <input type="checkbox" :checked="!!style.border" @change="toggleBorder(($event.target as HTMLInputElement).checked)" />
        {{ $t('reportDesigner.style.border') }}
      </label>
      <div v-if="style.border" class="rsf-row">
        <label class="rsf-f small">
          <span>{{ $t('reportDesigner.style.borderWidth') }}</span>
          <RdNum :model-value="style.border.width ?? 0.2" unit="mm" :step="0.1" :min="0" :max="5" @update:model-value="borderPart('width', $event)" />
        </label>
        <div class="rsf-f">
          <span>{{ $t('reportDesigner.style.borderColor') }}</span>
          <RdColor :model-value="style.border.color ?? '#1f2933'" :label="$t('reportDesigner.style.borderColor')" @update:model-value="borderPart('color', $event)" />
        </div>
      </div>
    </template>
    <div v-if="has('padding') || has('radius')" class="rsf-row">
      <label v-if="has('padding')" class="rsf-f">
        <span>{{ $t('reportDesigner.style.padding') }}</span>
        <RdNum :model-value="style.padding ?? null" unit="mm" :min="0" :max="50" nullable placeholder="0" @update:model-value="set('padding', $event)" />
      </label>
      <label v-if="has('radius')" class="rsf-f">
        <span>{{ $t('reportDesigner.style.radius') }}</span>
        <RdNum :model-value="style.radius ?? null" unit="mm" :min="0" :max="50" nullable placeholder="0" @update:model-value="set('radius', $event)" />
      </label>
    </div>
    <RdFormatInput v-if="has('format')" :model-value="style.format" :label="$t('reportDesigner.style.format')" :item="item" :path="`${path ? path + '.' : ''}style.format`" @update:model-value="set('format', $event)" />
    <label v-if="has('grow')" class="rsf-check">
      <input type="checkbox" :checked="!!style.can_grow" @change="set('can_grow', ($event.target as HTMLInputElement).checked || null)" />
      {{ $t('reportDesigner.style.canGrow') }}
    </label>
  </div>
</template>

<style scoped>
.rsf { display: flex; flex-direction: column; gap: 9px; min-width: 0; }
.rsf-row { display: flex; gap: 8px; align-items: flex-end; min-width: 0; }
.rsf-f { display: flex; flex-direction: column; gap: 4px; min-width: 0; flex: 1; font-size: 12px; color: var(--muted); font-weight: 500; }
.rsf-f.small { flex: 0 0 88px; }
.rsf-f.tiny { flex: 0 0 auto; }
.rsf-f :deep(.sel-trigger) { padding: 5px 8px; }
.rsf-tog { padding: 5px 8px; background: var(--bg-soft); border-color: var(--control-border); color: var(--muted); }
.rsf-tog.on { background: var(--tint-accent); color: var(--accent-hi); border-color: var(--accent); }
.rsf-check { display: flex; align-items: center; gap: 7px; font-size: 12.5px; color: var(--text); cursor: pointer; }
.rsf-check input { width: auto; margin: 0; }
</style>
