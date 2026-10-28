<script setup lang="ts">
// The report designer (docs/specs/reports.md): a page in millimetres with its three bands —
// the page header and footer, of fixed heights, and the body, which grows with its tables —
// the elements to drag onto it, the datasets and their fields, the properties of what is
// selected, and the preview of the report as it is now. Saving makes a new version; a
// definition the server refuses comes back as problems at their item and property.
//
// /report-design?id=N. It needs EDIT on the report's folder: with VIEW only, the viewer
// (/report?id=N) opens instead.
import { computed, onBeforeUnmount, onMounted, provide, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { onBeforeRouteLeave } from 'vue-router'
import {
  ArrowLeft, Check, CircleAlert, FileText, LoaderCircle, Magnet, Maximize2, PencilRuler, Eye, Redo2, Save, Undo2, ZoomIn, ZoomOut,
} from 'lucide-vue-next'
import BrandMark from '~/components/ui/BrandMark.vue'
import RdCanvas from '~/components/report/designer/RdCanvas.vue'
import RdSidebar from '~/components/report/designer/RdSidebar.vue'
import RdProperties from '~/components/report/designer/RdProperties.vue'
import RdPageSetup from '~/components/report/designer/RdPageSetup.vue'
import RdDatasetDialog from '~/components/report/designer/RdDatasetDialog.vue'
import RdParameterDialog from '~/components/report/designer/RdParameterDialog.vue'
import RdPreview from '~/components/report/designer/RdPreview.vue'
import RdSeg from '~/components/report/designer/RdSeg.vue'
import { DESIGN, createDesignStore } from '~/composables/reportDesignStore'
import { useReportDesign } from '~/composables/useReportDesign'
import { errMessage } from '~/composables/useApi'
import { useFolderPaths } from '~/composables/useFolderPaths'
import {
  boxOf, contentWidth, forSave, mm1, normalize, problemsOf, type BandName, type DDataset, type DItem, type DesignProblem,
} from '~/composables/reportDesignModel'

const { t } = useI18n()
const route = useRoute()
const toast = useToast()
const api = useReportDesign()
const folderPaths = useFolderPaths()

const store = createDesignStore()
provide(DESIGN, store)

const reportId = ref<number | null>(Number(route.query.id) || null)
const projectId = ref<number | null>(null)
const name = ref('')
const savedName = ref('')
const savedText = ref('')
const version = ref<number | null>(null)
const loading = ref(true)
const loadError = ref('')
const saving = ref(false)

const dirty = computed(() => !loading.value && (store.text.value !== savedText.value || name.value.trim() !== savedName.value))

async function load() {
  loading.value = true
  loadError.value = ''
  if (reportId.value == null) {
    loadError.value = t('reportDesigner.notFound')
    loading.value = false
    return
  }
  try {
    const d = await api.get(reportId.value)
    if (!d.editable) {
      // reading is all this person may do: the viewer
      await navigateTo(`/report?id=${d.id}`, { replace: true })
      return
    }
    projectId.value = d.project_id
    name.value = d.name
    savedName.value = d.name
    version.value = d.current_version
    store.reset(normalize(d.content))
    savedText.value = store.text.value
  } catch (e: any) {
    loadError.value = (e?.statusCode ?? e?.response?.status) === 404 ? t('reportDesigner.notFound') : errMessage(e)
  } finally {
    loading.value = false
  }
}

// ── saving ───────────────────────────────────────────────────────────────────
async function save(): Promise<boolean> {
  if (saving.value || reportId.value == null || loading.value) return false
  const n = name.value.trim()
  if (!n) {
    toast.error(t('reportDesigner.nameRequired'))
    return false
  }
  store.record()
  saving.value = true
  const text = store.text.value
  try {
    const d = await api.save(reportId.value, { ...(n !== savedName.value ? { name: n } : {}), content: forSave(store.doc.value) })
    savedName.value = d.name
    name.value = d.name
    version.value = d.current_version
    savedText.value = text
    store.problems.value = []
    toast.success(t('reportDesigner.saved', { n: d.current_version }))
    return true
  } catch (e: any) {
    const code = e?.statusCode ?? e?.response?.status
    const detail = e?.data?.detail
    if (code === 422 && detail && typeof detail === 'object' && !Array.isArray(detail)) {
      showProblems(problemsOf(store.doc.value, detail))
    } else {
      toast.error(errMessage(e))
    }
    return false
  } finally {
    saving.value = false
  }
}

/** The problems of a refused save (or preview): the first one's item is selected and its
 *  field shows the message; a dataset's or a parameter's bring the Data tab forward. */
function showProblems(list: DesignProblem[]) {
  store.setProblems(list)
  const first = list[0]
  if (!first) return
  if (first.item && store.find(first.item)) {
    mode.value = 'design'
    store.select(first.item)
  } else if (first.dataset || first.parameter) {
    store.sideTab.value = 'data'
  }
  problemsOpen.value = list.length > 1 || (!first.item && !first.dataset && !first.parameter)
  toast.error(list.length > 1 ? t('reportDesigner.problemsN', { n: list.length, first: first.message }) : first.message)
}

function goToProblem(p: DesignProblem) {
  problemsOpen.value = false
  mode.value = 'design'
  if (p.item && store.find(p.item)) store.select(p.item)
  else if (p.dataset) { store.sideTab.value = 'data'; datasetDialog.value = { open: true, name: p.dataset } }
  else if (p.parameter) { store.sideTab.value = 'data'; parameterDialog.value = { open: true, name: p.parameter } }
  else if (p.path.startsWith('page') || p.path.startsWith('theme') || p.path === 'locale' || p.path === 'timezone') setupOpen.value = true
}
const problemsOpen = ref(false)
function problemWhere(p: DesignProblem): string {
  if (p.item) return p.path ? `${p.item} · ${p.path}` : p.item
  if (p.dataset) return t('reportDesigner.problemDataset', { name: p.dataset })
  if (p.parameter) return t('reportDesigner.problemParameter', { name: p.parameter })
  return p.path || t('reportDesigner.problemReport')
}

async function done() {
  if (reportId.value == null) return
  if (dirty.value && !(await save())) return
  savedText.value = store.text.value
  navigateTo(`/report?id=${reportId.value}`)
}

// ── design and preview ───────────────────────────────────────────────────────
const mode = ref<'design' | 'preview'>('design')
const modeOptions = computed(() => [
  { value: 'design', label: t('reportDesigner.design'), icon: PencilRuler },
  { value: 'preview', label: t('reportDesigner.preview.tab'), icon: Eye },
])
const previewRef = ref<InstanceType<typeof RdPreview> | null>(null)
const previewZoom = ref<number | 'fit'>(1)
const previewScale = ref(1)
watch(mode, (m) => {
  if (m === 'preview') requestAnimationFrame(() => previewRef.value?.run())
})

// ── zoom ─────────────────────────────────────────────────────────────────────
const STEPS = [0.25, 0.33, 0.5, 0.67, 0.75, 0.9, 1, 1.1, 1.25, 1.5, 1.75, 2, 2.5, 3, 4]
const canvasRef = ref<InstanceType<typeof RdCanvas> | null>(null)
const scale = computed(() => (mode.value === 'design' ? store.zoom.value : previewScale.value))
const pct = computed(() => `${Math.round(scale.value * 100)}%`)
function zoomBy(d: number) {
  const now = scale.value
  const next = d > 0 ? STEPS.find((s) => s > now + 0.001) ?? STEPS[STEPS.length - 1] : [...STEPS].reverse().find((s) => s < now - 0.001) ?? STEPS[0]
  if (mode.value === 'design') store.zoom.value = next
  else previewZoom.value = next
}
function zoomActual() {
  if (mode.value === 'design') store.zoom.value = 1
  else previewZoom.value = 1
}
function zoomFit() {
  if (mode.value === 'design') canvasRef.value?.fitWidth()
  else previewZoom.value = 'fit'
}

// ── dialogs ──────────────────────────────────────────────────────────────────
const setupOpen = ref(false)
const datasetDialog = ref<{ open: boolean; name: string | null }>({ open: false, name: null })
const parameterDialog = ref<{ open: boolean; name: string | null }>({ open: false, name: null })
const refreshing = ref<string | null>(null)

async function refreshFields(dataset: string) {
  if (reportId.value == null || refreshing.value) return
  refreshing.value = dataset
  try {
    const got = await api.fields(reportId.value, dataset, forSave(store.doc.value))
    const ds = store.doc.value.datasets.find((d) => d.name === dataset)
    if (ds) ds.fields = got.fields
    toast.success(t('reportDesigner.data.fieldsRead', { n: got.fields.length, name: dataset }, got.fields.length))
  } catch (e) {
    toast.error(errMessage(e))
  } finally {
    refreshing.value = null
  }
}
function onDatasetApplied(dataset: string) {
  const ds = store.doc.value.datasets.find((d) => d.name === dataset)
  if (ds && !(ds.fields ?? []).length) refreshFields(dataset)
}
function removeDataset(dataset: string) {
  const users = (['page_header', 'body', 'page_footer'] as const).flatMap((b) => store.doc.value.sections[b].items)
    .filter((it) => 'dataset' in it && it.dataset === dataset).map((it) => it.id)
  const msg = users.length ? t('reportDesigner.data.removeDatasetUsed', { name: dataset, items: users.join(', ') }) : t('reportDesigner.data.removeDatasetConfirm', { name: dataset })
  if (!confirm(msg)) return
  store.doc.value.datasets = store.doc.value.datasets.filter((d) => d.name !== dataset)
}
function removeParameter(param: string) {
  if (!confirm(t('reportDesigner.data.removeParameterConfirm', { name: param }))) return
  store.doc.value.parameters = store.doc.value.parameters.filter((p) => p.name !== param)
}
function sourceLabel(d: DDataset): string {
  const s = d.source
  if (s.kind === 'sql') return s.connection_id == null ? t('reportDesigner.data.srcCatalog') : t('reportDesigner.data.srcConnection')
  if (s.kind === 'datasource') return t('reportDesigner.data.srcDatasource')
  if (s.kind === 'view') return t('reportDesigner.data.srcView')
  return t('reportDesigner.data.srcScript')
}

// what the editor did on its own (an item moved out of a table's row) is said once
watch(() => store.notice.value, (n) => { if (n) toast.info(n) })

// ── keys ─────────────────────────────────────────────────────────────────────
function typing(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null
  return !!el?.closest?.('input, textarea, select, [contenteditable="true"], .monaco-editor, [role="combobox"], .sel-panel')
}
const dialogOpen = () => setupOpen.value || datasetDialog.value.open || parameterDialog.value.open || !!document.querySelector('[role="dialog"][aria-modal="true"]')

function onKey(e: KeyboardEvent) {
  const mod = e.ctrlKey || e.metaKey
  const key = e.key.toLowerCase()
  if (mod && !e.altKey && key === 's') {
    e.preventDefault()
    save()
    return
  }
  if (loading.value || dialogOpen() || typing(e.target)) return
  if (mod && key === 'z') {
    e.preventDefault()
    if (e.shiftKey) store.redo()
    else store.undo()
    return
  }
  if (mod && key === 'y') {
    e.preventDefault()
    store.redo()
    return
  }
  if (mode.value !== 'design') return
  const sel = store.selected.value
  if (mod && key === 'v') {
    if (store.paste()) e.preventDefault()
    return
  }
  if (!sel) return
  if (e.key === 'Delete' || e.key === 'Backspace') {
    e.preventDefault()
    store.remove(sel.item.id)
  } else if (mod && key === 'd') {
    e.preventDefault()
    store.duplicate(sel.item.id)
  } else if (mod && key === 'c') {
    store.copy(sel.item.id)
  } else if (e.key === 'Escape') {
    store.select(null)
  } else if (e.key.startsWith('Arrow')) {
    e.preventDefault()
    nudge(sel.item, sel.band, e.key, e.shiftKey ? 5 : 1)
  }
}

/** An item moved with the arrows, inside its band and the page. */
function nudge(it: DItem, band: BandName, key: string, step: number) {
  const dx = key === 'ArrowLeft' ? -step : key === 'ArrowRight' ? step : 0
  const dy = key === 'ArrowUp' ? -step : key === 'ArrowDown' ? step : 0
  const b = boxOf(it)
  const width = contentWidth(store.doc.value)
  const room = band === 'body' ? Infinity : store.doc.value.sections[band].height
  const nx = Math.max(0, Math.min(b.x + dx, width - b.w))
  const ny = Math.max(0, Math.min(b.y + dy, room - b.h))
  it.x = mm1(it.x + (nx - b.x))
  it.y = mm1(it.y + (ny - b.y))
}

// ── leaving ──────────────────────────────────────────────────────────────────
function onBeforeUnload(e: BeforeUnloadEvent) {
  if (!dirty.value) return
  e.preventDefault()
  e.returnValue = ''
}
onBeforeRouteLeave(() => !dirty.value || confirm(t('reportDesigner.leaveConfirm')))

onMounted(() => {
  document.addEventListener('keydown', onKey)
  window.addEventListener('beforeunload', onBeforeUnload)
  folderPaths.load()
  load()
})
onBeforeUnmount(() => {
  document.removeEventListener('keydown', onKey)
  window.removeEventListener('beforeunload', onBeforeUnload)
})

const folderPath = computed(() => folderPaths.pathOf(projectId.value) ?? '')
useHead(() => ({ title: name.value ? `${name.value} · ${t('reportDesigner.titleSuffix')}` : t('reportDesigner.titleSuffix') }))
</script>

<template>
  <div class="rdz">
    <header class="rdz-top">
      <NuxtLink :to="projectId ? `/?folder=${projectId}` : '/'" class="rdz-back" :title="$t('reportDesigner.backToExplore')" :aria-label="$t('reportDesigner.backToExplore')">
        <ArrowLeft :size="15" />
      </NuxtLink>
      <BrandMark :size="20" />
      <div class="rdz-name">
        <FileText :size="15" class="rdz-nicon" />
        <input v-model="name" type="text" maxlength="200" :aria-label="$t('reportDesigner.nameLabel')" :placeholder="$t('reportDesigner.namePlaceholder')" :disabled="loading" />
        <span v-if="folderPath" class="rdz-folder" :title="folderPath">{{ folderPath }}</span>
      </div>
      <span class="rdz-state" role="status" aria-live="polite">
        <template v-if="saving"><LoaderCircle :size="13" class="spin" /> {{ $t('reportDesigner.saving') }}</template>
        <template v-else-if="dirty"><span class="rdz-dot" aria-hidden="true" /> {{ $t('reportDesigner.unsaved') }}</template>
        <template v-else-if="version != null"><Check :size="13" /> {{ $t('reportDesigner.savedState', { n: version }) }}</template>
      </span>
      <button v-if="store.problems.value.length" type="button" class="mini rdz-problems" :aria-expanded="problemsOpen" @click="problemsOpen = !problemsOpen">
        <CircleAlert :size="13" /> {{ $t('reportDesigner.problems', { n: store.problems.value.length }, store.problems.value.length) }}
      </button>

      <span class="rdz-grow" />

      <div class="rdz-tools">
        <button type="button" class="rdz-icon" :disabled="!store.canUndo.value" :title="`${$t('reportDesigner.undo')} (Ctrl+Z)`" :aria-label="$t('reportDesigner.undo')" @click="store.undo()"><Undo2 :size="15" /></button>
        <button type="button" class="rdz-icon" :disabled="!store.canRedo.value" :title="`${$t('reportDesigner.redo')} (Ctrl+Shift+Z)`" :aria-label="$t('reportDesigner.redo')" @click="store.redo()"><Redo2 :size="15" /></button>
        <span class="rdz-sep" aria-hidden="true" />
        <button type="button" class="rdz-icon" :title="$t('reportDesigner.zoomOut')" :aria-label="$t('reportDesigner.zoomOut')" @click="zoomBy(-1)"><ZoomOut :size="15" /></button>
        <button type="button" class="rdz-pct" :title="$t('reportDesigner.zoomActual')" :aria-label="$t('reportDesigner.zoomLevel', { pct })" @click="zoomActual">{{ pct }}</button>
        <button type="button" class="rdz-icon" :title="$t('reportDesigner.zoomIn')" :aria-label="$t('reportDesigner.zoomIn')" @click="zoomBy(1)"><ZoomIn :size="15" /></button>
        <button type="button" class="rdz-icon" :title="$t('reportDesigner.zoomFit')" :aria-label="$t('reportDesigner.zoomFit')" @click="zoomFit"><Maximize2 :size="14" /></button>
        <button
          v-if="mode === 'design'"
          type="button"
          class="rdz-icon"
          :class="{ on: store.snap.value }"
          :aria-pressed="store.snap.value"
          :title="$t('reportDesigner.snapTitle')"
          :aria-label="$t('reportDesigner.snap')"
          @click="store.snap.value = !store.snap.value"
        >
          <Magnet :size="15" />
        </button>
        <span class="rdz-sep" aria-hidden="true" />
        <button type="button" class="rdz-setup" @click="setupOpen = true"><FileText :size="14" /> {{ $t('reportDesigner.setup.open') }}</button>
      </div>

      <RdSeg v-model="mode" :options="modeOptions" :label="$t('reportDesigner.modeLabel')" class="rdz-mode" />

      <button type="button" class="primary rdz-save" :disabled="saving || loading || !!loadError || (!dirty && version != null)" :title="`${$t('reportDesigner.save')} (Ctrl+S)`" @click="save">
        <LoaderCircle v-if="saving" :size="14" class="spin" /><Save v-else :size="14" /> {{ $t('reportDesigner.save') }}
      </button>
      <button type="button" class="rdz-done" :disabled="loading || !!loadError || saving" :title="$t('reportDesigner.doneTitle')" @click="done">
        {{ $t('reportDesigner.done') }}
      </button>
    </header>

    <div v-if="problemsOpen && store.problems.value.length" class="rdz-plist" role="region" :aria-label="$t('reportDesigner.problemsLabel')">
      <ul>
        <li v-for="(p, i) in store.problems.value" :key="i">
          <button type="button" @click="goToProblem(p)">
            <CircleAlert :size="13" />
            <span class="rdz-pwhere">{{ problemWhere(p) }}</span>
            <span class="rdz-pmsg">{{ p.message }}</span>
          </button>
        </li>
      </ul>
    </div>

    <p v-if="loadError" class="rdz-error" role="alert">{{ loadError }}</p>
    <div v-else-if="loading" class="rdz-loading"><SkeletonRows :rows="6" /></div>
    <div v-else class="rdz-work" :class="{ preview: mode === 'preview' }">
      <RdSidebar
        v-show="mode === 'design'"
        class="rdz-left"
        :refreshing="refreshing"
        :source-label="sourceLabel"
        @edit-dataset="datasetDialog = { open: true, name: $event }"
        @remove-dataset="removeDataset"
        @refresh-fields="refreshFields"
        @edit-parameter="parameterDialog = { open: true, name: $event }"
        @remove-parameter="removeParameter"
      />
      <main class="rdz-center">
        <RdCanvas v-show="mode === 'design'" ref="canvasRef" />
        <RdPreview
          v-if="mode === 'preview' && reportId != null"
          ref="previewRef"
          :report-id="reportId"
          :zoom="previewZoom"
          @problems="showProblems"
          @scale="(s: number) => (previewScale = s)"
        />
      </main>
      <RdProperties v-show="mode === 'design'" class="rdz-right" @page-setup="setupOpen = true" />
    </div>

    <RdPageSetup :open="setupOpen" @close="setupOpen = false" />
    <RdDatasetDialog
      :open="datasetDialog.open"
      :name="datasetDialog.name"
      :report-id="reportId"
      @close="datasetDialog = { open: false, name: null }"
      @applied="onDatasetApplied"
    />
    <RdParameterDialog :open="parameterDialog.open" :name="parameterDialog.name" @close="parameterDialog = { open: false, name: null }" />
  </div>
</template>

<style scoped>
.rdz { height: 100vh; display: flex; flex-direction: column; background: var(--bg); color: var(--text); overflow: hidden; }
.rdz-top {
  display: flex; align-items: center; gap: 10px; padding: 7px 12px; min-width: 0; flex: none;
  background: var(--topbar-bg); backdrop-filter: blur(8px); border-bottom: 1px solid var(--border-soft); position: relative; z-index: 10;
}
.rdz-back { display: grid; place-items: center; width: 28px; height: 28px; border-radius: 8px; color: var(--muted); flex: none; }
.rdz-back:hover { color: var(--text); background: var(--panel-2); }
.rdz-back:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.rdz-name { display: flex; align-items: center; gap: 6px; min-width: 0; flex: 0 1 380px; }
.rdz-nicon { color: var(--accent); flex: none; }
.rdz-name input {
  font-size: 15px; font-weight: 600; padding: 4px 8px; min-width: 120px; flex: 1 1 auto;
  border-color: transparent; background: transparent;
}
.rdz-name input:hover { border-color: var(--control-border); }
.rdz-name input:focus { background: var(--bg-soft); }
.rdz-folder { font-size: 12px; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; min-width: 0; flex: 0 1 auto; max-width: 180px; }
.rdz-state { display: inline-flex; align-items: center; gap: 5px; font-size: 12px; color: var(--muted); white-space: nowrap; flex-shrink: 0; }
.rdz-state :deep(svg) { color: var(--accent-2); }
.rdz-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--warning); }
.rdz-state:has(.rdz-dot) { color: var(--warning); }
.rdz-problems { color: var(--danger); border-color: color-mix(in srgb, var(--danger) 45%, var(--border)); white-space: nowrap; flex-shrink: 0; display: inline-flex; gap: 5px; align-items: center; }
.rdz-grow { flex: 1; min-width: 8px; }
.rdz-tools { display: flex; align-items: center; gap: 2px; flex-shrink: 0; }
.rdz-icon, .rdz-pct {
  border: 0; background: transparent; box-shadow: none; padding: 5px 6px; color: var(--muted); border-radius: 7px; min-height: 0;
}
.rdz-icon:hover:not(:disabled), .rdz-pct:hover { color: var(--text); background: var(--panel-2); box-shadow: none; }
.rdz-icon.on { color: var(--accent-hi); background: var(--tint-accent); }
.rdz-pct { font-size: 12px; font-variant-numeric: tabular-nums; min-width: 46px; }
.rdz-sep { width: 1px; height: 18px; background: var(--border); margin: 0 6px; }
.rdz-setup { padding: 5px 10px; font-size: 12.5px; white-space: nowrap; flex-shrink: 0; }
.rdz-mode { flex-shrink: 0; }
.rdz-save, .rdz-done { white-space: nowrap; flex-shrink: 0; }
.rdz-plist { flex: none; border-bottom: 1px solid var(--border-soft); background: var(--panel); max-height: 30vh; overflow: auto; }
.rdz-plist ul { list-style: none; margin: 0; padding: 6px 12px; display: flex; flex-direction: column; gap: 2px; }
.rdz-plist button {
  width: 100%; justify-content: flex-start; gap: 8px; border: 0; background: transparent; box-shadow: none; padding: 5px 8px;
  font-size: 12.5px; text-align: left;
}
.rdz-plist button:hover { background: var(--panel-2); box-shadow: none; }
.rdz-plist button :deep(svg) { color: var(--danger); flex: none; }
.rdz-pwhere { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; color: var(--muted); white-space: nowrap; flex: none; }
.rdz-pmsg { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.rdz-error { margin: 32px; color: var(--danger); }
.rdz-loading { padding: 24px; }
.rdz-work {
  flex: 1; min-height: 0; display: grid; grid-template-columns: 252px minmax(0, 1fr) 320px; gap: 1px; background: var(--border-soft);
}
.rdz-work.preview { grid-template-columns: minmax(0, 1fr); }
.rdz-left, .rdz-right { min-height: 0; overflow: hidden; }
.rdz-center { min-width: 0; min-height: 0; display: flex; flex-direction: column; background: var(--bg); }
.rdz-center > * { flex: 1; min-height: 0; }
@media (max-width: 1180px) {
  .rdz-folder, .rdz-setup { display: none; }
  .rdz-work { grid-template-columns: 220px minmax(0, 1fr) 290px; }
}
</style>
