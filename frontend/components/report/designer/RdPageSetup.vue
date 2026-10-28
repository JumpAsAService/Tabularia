<script setup lang="ts">
// The page of the report: its size and orientation, the margins in mm, the language (the
// separators of the numbers, the names of the months, the texts the renderer writes) and
// the time zone of its dates, and the theme — the font, its size, the colour of the text
// and the accent of tables and charts. Applied as one change (one step of undo).
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import Modal from '~/components/ui/Modal.vue'
import Select from '~/components/ui/Select.vue'
import RdNum from '~/components/report/designer/RdNum.vue'
import RdSeg from '~/components/report/designer/RdSeg.vue'
import RdColor from '~/components/report/designer/RdColor.vue'
import { BANDS, useDesign } from '~/composables/reportDesignStore'
import { PAGE_SIZES, type DDefinition, type PageSizeName } from '~/composables/reportDesignModel'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ (e: 'close'): void }>()
const store = useDesign()
const { t } = useI18n()

type Draft = Pick<DDefinition, 'page' | 'locale' | 'timezone' | 'theme'>
const draft = ref<Draft | null>(null)
const custom = ref({ w: 210, h: 297 })
watch(() => props.open, (o) => {
  if (!o) return
  const d = store.doc.value
  draft.value = JSON.parse(JSON.stringify({ page: d.page, locale: d.locale, timezone: d.timezone, theme: d.theme }))
  if (typeof d.page.size === 'object') custom.value = { ...d.page.size }
}, { immediate: true })

const sizeName = computed<string>(() => (draft.value && typeof draft.value.page.size === 'string' ? draft.value.page.size : 'custom'))
const sizeOptions = computed(() => [
  ...(Object.keys(PAGE_SIZES) as PageSizeName[]).map((k) => ({ value: k, label: k, hint: `${PAGE_SIZES[k][0]} × ${PAGE_SIZES[k][1]} mm` })),
  { value: 'custom', label: t('reportDesigner.setup.custom') },
])
function setSize(v: string) {
  if (!draft.value) return
  draft.value.page.size = v === 'custom' ? { ...custom.value } : (v as PageSizeName)
}
function setCustom(k: 'w' | 'h', v: number | null) {
  if (v == null || !draft.value) return
  custom.value[k] = v
  draft.value.page.size = { ...custom.value }
}
const orientations = computed(() => [
  { value: 'portrait', label: t('reportDesigner.setup.portrait') },
  { value: 'landscape', label: t('reportDesigner.setup.landscape') },
])
const LANGUAGES = [
  { value: 'en', label: 'English' }, { value: 'it', label: 'Italiano' }, { value: 'de', label: 'Deutsch' },
  { value: 'es', label: 'Español' }, { value: 'fr', label: 'Français' },
]
const zones = computed(() => {
  let list: string[] = []
  try { list = (Intl as any).supportedValuesOf?.('timeZone') ?? [] } catch { list = [] }
  if (!list.includes('UTC')) list = ['UTC', ...list]
  const cur = draft.value?.timezone
  if (cur && !list.includes(cur)) list = [cur, ...list]
  return list.map((z) => ({ value: z, label: z }))
})
const fonts = [{ value: 'IBM Plex Sans', label: 'IBM Plex Sans' }]

// what the margins and the bands leave for the body
const problem = computed(() => {
  const d = draft.value
  if (!d) return ''
  const s = d.page.size
  const [w0, h0] = typeof s === 'string' ? PAGE_SIZES[s] : [s.w, s.h]
  const [w, h] = d.page.orientation === 'landscape' ? [Math.max(w0, h0), Math.min(w0, h0)] : [w0, h0]
  const [top, right, bottom, left] = d.page.margins
  const bands = store.doc.value.sections.page_header.height + store.doc.value.sections.page_footer.height
  if (w - left - right < 20) return t('reportDesigner.setup.tooNarrow')
  if (h - top - bottom - bands < 20) return t('reportDesigner.setup.tooShort')
  return ''
})

function apply() {
  const d = draft.value
  if (!d || problem.value) return
  const doc = store.doc.value
  doc.page = d.page
  doc.locale = d.locale
  doc.timezone = d.timezone
  doc.theme = d.theme
  // what no longer fits goes inside the new page
  for (const b of BANDS) for (const it of doc.sections[b].items) store.keepInside(b, it)
  emit('close')
}
const MARGINS = ['top', 'right', 'bottom', 'left'] as const
</script>

<template>
  <Modal :open="open" :title="$t('reportDesigner.setup.title')" :width="560" @close="emit('close')">
    <div v-if="draft" class="rps">
      <fieldset class="rps-group">
        <legend>{{ $t('reportDesigner.setup.page') }}</legend>
        <div class="rp-row">
          <label class="rp-f">
            <span>{{ $t('reportDesigner.setup.size') }}</span>
            <Select :model-value="sizeName" :options="sizeOptions" :aria-label="$t('reportDesigner.setup.size')" @update:model-value="setSize" />
          </label>
          <div class="rp-f">
            <span>{{ $t('reportDesigner.setup.orientation') }}</span>
            <RdSeg v-model="draft.page.orientation" :options="orientations" :label="$t('reportDesigner.setup.orientation')" />
          </div>
        </div>
        <div v-if="sizeName === 'custom'" class="rp-row">
          <label class="rp-f"><span>{{ $t('reportDesigner.props.width') }}</span><RdNum :model-value="custom.w" unit="mm" :min="50" :max="2000" @update:model-value="setCustom('w', $event)" /></label>
          <label class="rp-f"><span>{{ $t('reportDesigner.props.height') }}</span><RdNum :model-value="custom.h" unit="mm" :min="50" :max="2000" @update:model-value="setCustom('h', $event)" /></label>
        </div>
        <div class="rps-margins">
          <label v-for="(m, i) in MARGINS" :key="m" class="rp-f" :class="`m-${m}`">
            <span>{{ $t(`reportDesigner.setup.${m}`) }}</span>
            <RdNum :model-value="draft.page.margins[i]" unit="mm" :min="0" :max="100" @update:model-value="draft.page.margins[i] = $event ?? 0" />
          </label>
          <div class="rps-sheet" :class="{ wide: draft.page.orientation === 'landscape' }" aria-hidden="true"><div class="rps-inner" /></div>
        </div>
      </fieldset>

      <fieldset class="rps-group">
        <legend>{{ $t('reportDesigner.setup.languageAndTime') }}</legend>
        <div class="rp-row">
          <label class="rp-f">
            <span>{{ $t('reportDesigner.setup.language') }}</span>
            <Select v-model="draft.locale" :options="LANGUAGES" :aria-label="$t('reportDesigner.setup.language')" />
          </label>
          <label class="rp-f">
            <span>{{ $t('reportDesigner.setup.timezone') }}</span>
            <Select v-model="draft.timezone" :options="zones" searchable :aria-label="$t('reportDesigner.setup.timezone')" />
          </label>
        </div>
        <p class="rp-help">{{ $t('reportDesigner.setup.languageHelp') }}</p>
      </fieldset>

      <fieldset class="rps-group">
        <legend>{{ $t('reportDesigner.setup.theme') }}</legend>
        <div class="rp-row">
          <label class="rp-f">
            <span>{{ $t('reportDesigner.setup.font') }}</span>
            <Select v-model="draft.theme.font" :options="fonts" :aria-label="$t('reportDesigner.setup.font')" />
          </label>
          <label class="rp-f narrow">
            <span>{{ $t('reportDesigner.style.size') }}</span>
            <RdNum v-model="draft.theme.size" unit="pt" :step="0.5" :min="5" :max="24" />
          </label>
        </div>
        <div class="rp-row">
          <div class="rp-f">
            <span>{{ $t('reportDesigner.setup.textColor') }}</span>
            <RdColor v-model="draft.theme.color" :label="$t('reportDesigner.setup.textColor')" />
          </div>
          <div class="rp-f">
            <span>{{ $t('reportDesigner.setup.accent') }}</span>
            <RdColor v-model="draft.theme.accent" :label="$t('reportDesigner.setup.accent')" />
          </div>
        </div>
        <p class="rp-help">{{ $t('reportDesigner.setup.fontHelp') }}</p>
      </fieldset>
      <p v-if="problem" class="rp-err" role="alert">{{ problem }}</p>
    </div>
    <template #footer>
      <span class="rps-grow" />
      <button type="button" @click="emit('close')">{{ $t('reportDesigner.cancel') }}</button>
      <button type="button" class="primary" :disabled="!!problem" @click="apply">{{ $t('reportDesigner.apply') }}</button>
    </template>
  </Modal>
</template>

<style scoped src="~/assets/reportprops.css" />
<style scoped>
.rps { display: flex; flex-direction: column; gap: 14px; }
.rps-group { border: 0; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 10px; min-width: 0; }
.rps-group legend { padding: 0 0 6px; font-size: 11px; font-weight: 600; letter-spacing: 0.05em; text-transform: uppercase; color: var(--muted); }
.rps-margins {
  display: grid; grid-template-columns: 1fr 120px 1fr; grid-template-rows: auto auto auto; gap: 8px; align-items: center;
  grid-template-areas: ". top ." "left sheet right" ". bottom .";
}
.m-top { grid-area: top; } .m-bottom { grid-area: bottom; } .m-left { grid-area: left; } .m-right { grid-area: right; }
.rps-sheet { grid-area: sheet; justify-self: center; width: 70px; height: 92px; background: #fff; border-radius: 2px; box-shadow: var(--shadow-1); position: relative; }
.rps-sheet.wide { width: 92px; height: 66px; }
.rps-inner { position: absolute; inset: 9px 8px; border: 1px dashed rgba(59, 91, 219, 0.5); }
.rps-grow { flex: 1; }
</style>
