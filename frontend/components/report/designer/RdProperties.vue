<script setup lang="ts">
// The right panel: the properties of what is selected — an item (its name, place and size
// in mm, what it shows, its style, when it is visible), a band (its height, the pages it is
// on), or, with nothing selected, the report (its page and a reminder of the keys).
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  ChartColumn, CircleAlert, Copy, FileText, Gauge, Image, Minus, PanelBottom, PanelTop, Square, Table2, Trash2, Type, Upload, X,
} from 'lucide-vue-next'
import Select from '~/components/ui/Select.vue'
import RdChartProps from '~/components/report/designer/RdChartProps.vue'
import RdExprField from '~/components/report/designer/RdExprField.vue'
import RdKpiProps from '~/components/report/designer/RdKpiProps.vue'
import RdNum from '~/components/report/designer/RdNum.vue'
import RdSection from '~/components/report/designer/RdSection.vue'
import RdSeg from '~/components/report/designer/RdSeg.vue'
import RdStyleFields from '~/components/report/designer/RdStyleFields.vue'
import RdTableProps from '~/components/report/designer/RdTableProps.vue'
import { useDesign } from '~/composables/reportDesignStore'
import {
  ITEM_ID_RE, MAX_IMAGE_BYTES, bodyPageHeight, contentWidth, pageSize, type DItem, type DText,
} from '~/composables/reportDesignModel'

const emit = defineEmits<{ (e: 'page-setup'): void }>()
const store = useDesign()
const { t } = useI18n()
const toast = useToast()

const sel = computed(() => store.selected.value)
const item = computed<DItem | null>(() => sel.value?.item ?? null)
const band = computed(() => (sel.value ? sel.value.band : store.selectedBand.value))
const def = computed(() => store.doc.value)

const ICONS: Record<string, unknown> = { text: Type, image: Image, line: Minus, rect: Square, table: Table2, chart: ChartColumn, kpi: Gauge }
const typeName = computed(() => {
  const it = item.value
  if (!it) return ''
  if (it.type === 'table' && it.matrix) return t('reportDesigner.items.matrix')
  return t(`reportDesigner.items.${it.type}`)
})

// another item: its properties from the top
const body = ref<HTMLElement | null>(null)
watch(() => store.selectedId.value, () => { if (body.value) body.value.scrollTop = 0 })

// ── the item's name ──
const idText = ref('')
watch(() => item.value?.id, (v) => { idText.value = v ?? '' }, { immediate: true })
const idError = computed(() => {
  const v = idText.value.trim()
  if (!item.value || v === item.value.id) return ''
  if (!ITEM_ID_RE.test(v)) return t('reportDesigner.props.idRule')
  if (store.find(v)) return t('reportDesigner.props.idTaken', { id: v })
  return ''
})
function renameItem() {
  const it = item.value
  const v = idText.value.trim()
  if (!it || v === it.id) return
  if (idError.value) { idText.value = it.id; return }
  const old = it.id
  it.id = v
  store.selectedId.value = v
  store.problems.value = store.problems.value.filter((p) => p.item !== old)
}

// problems of the whole item (outside the page, beside a table): at the top
const itemProblems = computed(() => (item.value ? store.problems.value.filter((p) => p.item === item.value!.id && !p.path) : []))

// ── layout ──
const width = computed(() => contentWidth(def.value))
const isLine = computed(() => item.value?.type === 'line')
function keep() {
  if (sel.value) store.keepInside(sel.value.band, sel.value.item)
}

// ── text ──
const textItem = computed(() => (item.value?.type === 'text' ? (item.value as DText) : null))
const textMode = computed(() => (textItem.value?.value.startsWith('=') ? 'expr' : 'text'))
function setTextMode(mode: 'text' | 'expr') {
  const it = textItem.value
  if (!it) return
  if (mode === 'expr' && !it.value.startsWith('=')) it.value = `="${it.value.replaceAll('"', '""')}"`
  else if (mode === 'text' && it.value.startsWith('=')) {
    const m = /^=\s*"((?:[^"]|"")*)"\s*$/.exec(it.value)
    it.value = m ? m[1].replaceAll('""', '"') : it.value.slice(1)
  }
}
const modeOptions = computed(() => [
  { value: 'text', label: t('reportDesigner.props.plainText') },
  { value: 'expr', label: t('reportDesigner.props.expression') },
])
const datasetChoices = computed(() => [
  { value: null, label: def.value.datasets.length === 1 ? t('reportDesigner.props.onlyDataset', { name: def.value.datasets[0].name }) : t('reportDesigner.props.noDataset') },
  ...def.value.datasets.map((d) => ({ value: d.name, label: d.name })),
])
const textFields = computed(() => (band.value === 'body' ? store.fieldsOf(store.datasetOf(item.value)) : null))

// ── image ──
const fileInput = ref<HTMLInputElement | null>(null)
const fitOptions = computed(() => [
  { value: 'contain', label: t('reportDesigner.props.fitContain') },
  { value: 'cover', label: t('reportDesigner.props.fitCover') },
  { value: 'stretch', label: t('reportDesigner.props.fitStretch') },
])
/** What an image file is, from its first bytes (as the server judges it), whatever its name says. */
function imageKind(head: Uint8Array): string | null {
  if (head[0] === 0x89 && head[1] === 0x50 && head[2] === 0x4e && head[3] === 0x47) return 'image/png'
  if (head[0] === 0xff && head[1] === 0xd8 && head[2] === 0xff) return 'image/jpeg'
  const text = new TextDecoder().decode(head).replace(/^\uFEFF/, '').trimStart().toLowerCase()
  return text.startsWith('<svg') || text.startsWith('<?xml') ? 'image/svg+xml' : null
}
async function onFile(e: Event) {
  const input = e.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  const it = item.value
  if (!file || it?.type !== 'image') return
  if (file.size > MAX_IMAGE_BYTES) {
    toast.error(t('reportDesigner.props.imageTooLarge', { kib: Math.ceil(file.size / 1024), max: MAX_IMAGE_BYTES / 1024 }))
    return
  }
  const bytes = new Uint8Array(await file.arrayBuffer())
  // a file that is not what it says (a page saved as .png, a broken download) is refused
  // here, not drawn as a broken picture and refused at the save
  const mime = imageKind(bytes.subarray(0, 256))
  if (!mime) {
    toast.error(t('reportDesigner.props.imageKind'))
    return
  }
  let binary = ''
  for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000))
  it.asset = `data:${mime};base64,${btoa(binary)}`
  store.clearProblem(it.id, 'asset')
}

// ── band and report ──
const bandMax = computed(() => {
  const [top, , bottom] = def.value.page.margins
  const other = band.value === 'page_header' ? def.value.sections.page_footer.height : def.value.sections.page_header.height
  return Math.max(0, pageSize(def.value).h - top - bottom - other - 20)
})
const bandMin = computed(() => {
  if (band.value !== 'page_header' && band.value !== 'page_footer') return 0
  return def.value.sections[band.value].items.reduce((m, i) => Math.max(m, i.y + Math.max(i.h, 0)), 0)
})
function setBandHeight(v: number | null) {
  if (v == null || (band.value !== 'page_header' && band.value !== 'page_footer')) return
  def.value.sections[band.value].height = Math.max(bandMin.value, Math.min(bandMax.value, v))
}
const pageLine = computed(() => {
  const p = def.value.page
  const size = typeof p.size === 'string' ? p.size : `${p.size.w} × ${p.size.h} mm`
  return t('reportDesigner.props.pageLine', {
    size, orientation: t(`reportDesigner.setup.${p.orientation}`), margins: p.margins.join(' · '),
  })
})
</script>

<template>
  <aside class="rpp" :aria-label="$t('reportDesigner.props.panel')">
    <!-- an item -->
    <template v-if="item && sel">
      <header class="rpp-head">
        <component :is="ICONS[item.type]" :size="16" class="rpp-ticon" />
        <div class="rpp-titles">
          <span class="rpp-type">{{ typeName }}</span>
          <input
            v-model="idText"
            class="rpp-id"
            type="text"
            maxlength="64"
            spellcheck="false"
            :aria-label="$t('reportDesigner.props.itemName')"
            :aria-invalid="!!idError"
            @change="renameItem"
            @keydown.enter="($event.target as HTMLInputElement).blur()"
          />
        </div>
        <button type="button" class="rp-icon" :title="$t('reportDesigner.props.duplicate')" :aria-label="$t('reportDesigner.props.duplicate')" @click="store.duplicate(item.id)"><Copy :size="14" /></button>
        <button type="button" class="rp-icon danger" :title="$t('reportDesigner.props.delete')" :aria-label="$t('reportDesigner.props.delete')" @click="store.remove(item.id)"><Trash2 :size="14" /></button>
      </header>
      <p v-if="idError" class="rp-err rpp-pad">{{ idError }}</p>
      <div v-for="(p, i) in itemProblems" :key="i" class="rpp-alert" role="alert"><CircleAlert :size="14" /> <span>{{ p.message }}</span></div>

      <div ref="body" class="rpp-body">
        <RdSection :title="$t('reportDesigner.props.layout')" skey="layout">
          <div class="rpp-grid">
            <label class="rp-f"><span>{{ isLine ? $t('reportDesigner.props.startX') : 'X' }}</span><RdNum v-model="item.x" unit="mm" :min="0" :max="width" @update:model-value="keep" /></label>
            <label class="rp-f"><span>{{ isLine ? $t('reportDesigner.props.startY') : 'Y' }}</span><RdNum v-model="item.y" unit="mm" :min="0" @update:model-value="keep" /></label>
            <label class="rp-f"><span>{{ isLine ? $t('reportDesigner.props.runX') : $t('reportDesigner.props.width') }}</span><RdNum v-model="item.w" unit="mm" :min="isLine ? -width : 0.1" :max="width" @update:model-value="keep" /></label>
            <label v-if="item.type !== 'table'" class="rp-f"><span>{{ isLine ? $t('reportDesigner.props.runY') : $t('reportDesigner.props.height') }}</span><RdNum v-model="item.h" unit="mm" :min="isLine ? -bodyPageHeight(def) : 0.1" @update:model-value="keep" /></label>
            <div v-else class="rp-f"><span>{{ $t('reportDesigner.props.height') }}</span><span class="rpp-auto">{{ $t('reportDesigner.props.tableHeight') }}</span></div>
          </div>
        </RdSection>

        <!-- what the item shows -->
        <RdSection v-if="textItem" :title="$t('reportDesigner.props.content')" skey="text-content" :bad="store.problems.value.some((p) => p.item === item!.id && (p.path === 'value' || p.path === 'dataset'))">
          <RdSeg :model-value="textMode" :options="modeOptions" :label="$t('reportDesigner.props.contentKind')" @update:model-value="setTextMode" />
          <RdExprField :obj="textItem" prop="value" :label="textMode === 'expr' ? $t('reportDesigner.props.expression') : $t('reportDesigner.props.text')" path="value" :item="item.id"
                       :fields="textFields" text-mode :placeholder="textMode === 'expr' ? '=&quot;Sales in &quot; & PARAM(&quot;region&quot;)' : $t('reportDesigner.props.textPh')"
                       :hint="textMode === 'expr' ? (band === 'body' ? $t('reportDesigner.props.exprHintBody') : $t('reportDesigner.props.exprHintBand')) : $t('reportDesigner.props.textHint')" />
          <label v-if="band === 'body' && def.datasets.length" class="rp-f">
            <span>{{ $t('reportDesigner.props.dataset') }}</span>
            <Select :model-value="textItem.dataset ?? null" :options="datasetChoices" :aria-label="$t('reportDesigner.props.dataset')" @update:model-value="textItem.dataset = $event" />
          </label>
        </RdSection>

        <RdSection v-else-if="item.type === 'image'" :title="$t('reportDesigner.props.image')" skey="image" :bad="!!store.problemAt(item.id, 'asset')">
          <div class="rpp-imgrow">
            <img v-if="item.asset.startsWith('data:image/')" :src="item.asset" alt="" class="rpp-thumb" />
            <div class="rpp-imgacts">
              <button type="button" @click="fileInput?.click()"><Upload :size="13" /> {{ item.asset ? $t('reportDesigner.props.replaceImage') : $t('reportDesigner.props.uploadImage') }}</button>
              <button v-if="item.asset" type="button" class="mini" @click="item.asset = ''"><X :size="12" /> {{ $t('reportDesigner.props.removeImage') }}</button>
            </div>
            <input ref="fileInput" type="file" accept="image/png,image/jpeg,image/svg+xml,.png,.jpg,.jpeg,.svg" class="sr-only" tabindex="-1" @change="onFile" />
          </div>
          <p class="rp-help">{{ $t('reportDesigner.props.imageHelp') }}</p>
          <p v-if="store.problemAt(item.id, 'asset')" class="rp-err" role="alert">{{ store.problemAt(item.id, 'asset')?.message }}</p>
          <div class="rp-f">
            <span>{{ $t('reportDesigner.props.fit') }}</span>
            <RdSeg :model-value="item.fit ?? 'contain'" :options="fitOptions" :label="$t('reportDesigner.props.fit')" @update:model-value="item.fit = $event" />
          </div>
        </RdSection>

        <RdTableProps v-else-if="item.type === 'table'" :item="item" />
        <RdChartProps v-else-if="item.type === 'chart'" :item="item" />
        <RdKpiProps v-else-if="item.type === 'kpi'" :item="item" />

        <!-- style -->
        <RdSection v-if="item.type !== 'table' && item.type !== 'chart'" :title="$t('reportDesigner.props.style')" skey="style" :bad="!!store.problemAt(item.id, 'style.format')">
          <RdStyleFields v-if="item.type === 'text'" :obj="item" :parts="['font', 'align', 'valign', 'color', 'fill', 'border', 'padding', 'radius', 'format', 'grow']" :item="item.id" path="" />
          <RdStyleFields v-else-if="item.type === 'line'" :obj="item" :parts="['stroke']" :item="item.id" path="" />
          <RdStyleFields v-else-if="item.type === 'rect'" :obj="item" :parts="['fill', 'border', 'radius']" :item="item.id" path="" />
          <RdStyleFields v-else-if="item.type === 'kpi'" :obj="item" :parts="['font', 'color', 'fill', 'radius']" :item="item.id" path="" />
          <RdStyleFields v-else-if="item.type === 'image'" :obj="item" :parts="['border', 'radius']" :item="item.id" path="" />
        </RdSection>

        <RdSection :title="$t('reportDesigner.props.visibility')" skey="visible" :closed="!item.visible" :bad="!!store.problemAt(item.id, 'visible')">
          <RdExprField :obj="item" prop="visible" :label="$t('reportDesigner.props.visibleWhen')" path="visible" :item="item.id"
                       :fields="band === 'body' ? store.fieldsOf(store.datasetOf(item)) : null" optional
                       placeholder="PARAM(&quot;detail&quot;) = TRUE" :hint="$t('reportDesigner.props.visibleHint')" />
        </RdSection>
      </div>
    </template>

    <!-- a band -->
    <template v-else-if="band">
      <header class="rpp-head">
        <component :is="band === 'page_header' ? PanelTop : band === 'page_footer' ? PanelBottom : FileText" :size="16" class="rpp-ticon" />
        <div class="rpp-titles"><span class="rpp-name">{{ $t(`reportDesigner.bands.${band}`) }}</span></div>
      </header>
      <div class="rpp-body">
        <template v-if="band !== 'body'">
          <label class="rp-f">
            <span>{{ $t('reportDesigner.props.bandHeight') }}</span>
            <RdNum :model-value="def.sections[band].height" unit="mm" :min="0" :max="bandMax" @update:model-value="setBandHeight" />
          </label>
          <label v-if="band === 'page_header'" class="rp-check">
            <input type="checkbox" :checked="def.sections.page_header.on_first_page !== false" @change="def.sections.page_header.on_first_page = ($event.target as HTMLInputElement).checked" />
            {{ $t('reportDesigner.props.onFirstPage') }}
          </label>
          <label v-else class="rp-check">
            <input type="checkbox" :checked="def.sections.page_footer.on_last_page !== false" @change="def.sections.page_footer.on_last_page = ($event.target as HTMLInputElement).checked" />
            {{ $t('reportDesigner.props.onLastPage') }}
          </label>
          <p class="rp-help">{{ $t('reportDesigner.props.bandHelp') }}</p>
        </template>
        <p v-else class="rp-help">{{ $t('reportDesigner.props.bodyHelp') }}</p>
      </div>
    </template>

    <!-- the report -->
    <template v-else>
      <header class="rpp-head">
        <FileText :size="16" class="rpp-ticon" />
        <div class="rpp-titles"><span class="rpp-name">{{ $t('reportDesigner.props.report') }}</span></div>
      </header>
      <div class="rpp-body">
        <p class="rp-help">{{ pageLine }}</p>
        <button type="button" class="rpp-setup" @click="emit('page-setup')"><FileText :size="13" /> {{ $t('reportDesigner.setup.open') }}</button>
        <p class="rp-help">{{ $t('reportDesigner.props.nothingSelected') }}</p>
        <dl class="rpp-keys">
          <div><dt><kbd>Ctrl</kbd> <kbd>S</kbd></dt><dd>{{ $t('reportDesigner.keys.save') }}</dd></div>
          <div><dt><kbd>Ctrl</kbd> <kbd>Z</kbd></dt><dd>{{ $t('reportDesigner.keys.undo') }}</dd></div>
          <div><dt><kbd>Ctrl</kbd> <kbd>⇧</kbd> <kbd>Z</kbd></dt><dd>{{ $t('reportDesigner.keys.redo') }}</dd></div>
          <div><dt><kbd>←</kbd> <kbd>→</kbd> <kbd>↑</kbd> <kbd>↓</kbd></dt><dd>{{ $t('reportDesigner.keys.nudge') }}</dd></div>
          <div><dt><kbd>Ctrl</kbd> <kbd>D</kbd></dt><dd>{{ $t('reportDesigner.keys.duplicate') }}</dd></div>
          <div><dt><kbd>Ctrl</kbd> <kbd>C</kbd> / <kbd>V</kbd></dt><dd>{{ $t('reportDesigner.keys.copyPaste') }}</dd></div>
          <div><dt><kbd>Del</kbd></dt><dd>{{ $t('reportDesigner.keys.delete') }}</dd></div>
        </dl>
      </div>
    </template>
  </aside>
</template>

<style scoped src="~/assets/reportprops.css" />
<style scoped>
.rpp { display: flex; flex-direction: column; min-height: 0; background: var(--panel); }
.rpp-head { display: flex; align-items: center; gap: 8px; padding: 12px 12px 10px; border-bottom: 1px solid var(--border-soft); }
.rpp-ticon { color: var(--accent); flex: none; }
.rpp-titles { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 2px; }
.rpp-type { font-size: 11px; font-weight: 600; letter-spacing: 0.05em; text-transform: uppercase; color: var(--muted); }
.rpp-name { font-size: 14px; font-weight: 600; }
.rpp-id {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 13px; padding: 2px 6px; margin-left: -6px;
  border-color: transparent; background: transparent;
}
.rpp-id:hover { border-color: var(--control-border); }
.rpp-id[aria-invalid='true'] { border-color: var(--danger); }
.rpp-pad { padding: 4px 12px 0; }
.rpp-alert {
  display: flex; gap: 8px; align-items: flex-start; margin: 8px 12px 0; padding: 8px 10px; border-radius: 8px; font-size: 12px; line-height: 1.4;
  color: var(--danger); background: color-mix(in srgb, var(--danger) 8%, transparent); border: 1px solid color-mix(in srgb, var(--danger) 40%, transparent);
}
.rpp-alert svg { flex: none; margin-top: 1px; }
.rpp-body { flex: 1; min-height: 0; overflow: auto; padding: 4px 12px 16px; display: flex; flex-direction: column; gap: 10px; }
.rpp-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
.rpp-auto { font-size: 12px; color: var(--muted); padding: 6px 0; font-weight: 400; }
.rpp-imgrow { display: flex; gap: 10px; align-items: center; }
.rpp-thumb { width: 64px; height: 48px; object-fit: contain; border-radius: 6px; background: #fff; border: 1px solid var(--border-soft); }
.rpp-imgacts { display: flex; flex-direction: column; gap: 6px; align-items: flex-start; }
.rpp-setup { align-self: flex-start; }
.rpp-keys { margin: 4px 0 0; display: flex; flex-direction: column; gap: 6px; font-size: 12px; }
.rpp-keys div { display: flex; justify-content: space-between; gap: 10px; align-items: center; }
.rpp-keys dt { white-space: nowrap; }
.rpp-keys dd { margin: 0; color: var(--muted); text-align: right; }
kbd {
  font: 500 10.5px/1 ui-monospace, SFMono-Regular, Menlo, monospace; padding: 2px 5px; border-radius: 4px;
  border: 1px solid var(--border); border-bottom-width: 2px; background: var(--bg-soft); color: var(--text);
}
</style>
