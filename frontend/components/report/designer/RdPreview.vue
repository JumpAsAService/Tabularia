<script setup lang="ts">
// The preview of the report as it is now, saved or not: the pages the PDF will have (the
// render's svg format, through the viewer's ReportPages), with values for the parameters
// to try in the viewer's form, the render's notes, and its error. The mistakes of the definition
// come back as a save's do, each with its item and field (the gateway's 422 `problems`, or
// the engine's text of an older gateway): listed here, each one a way to its field in Design.
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { CircleAlert, LoaderCircle, Play, RefreshCw, TriangleAlert } from 'lucide-vue-next'
import ReportPages from '~/components/report/ReportPages.vue'
import ReportParamsBar from '~/components/report/ReportParamsBar.vue'
import { useDesign } from '~/composables/reportDesignStore'
import { useReportDesign } from '~/composables/useReportDesign'
import {
  stateAfter, type ReportDefinition, type ReportFormValue, type ReportHotspot, type ReportParameter, type ReportParams, type ReportState,
} from '~/composables/useReports'
import { forSave, problemFromText, problemsOf, type DesignProblem } from '~/composables/reportDesignModel'
import { errMessage } from '~/composables/useApi'

const props = defineProps<{ reportId: number; zoom: number | 'fit'; where: (p: DesignProblem) => string }>()
const emit = defineEmits<{
  (e: 'problems', list: DesignProblem[]): void
  (e: 'go', p: DesignProblem): void
  (e: 'scale', s: number): void
}>()
const store = useDesign()
const api = useReportDesign()

// ── the values to try: the viewer's form, on the parameters as they are now. A value is kept
// once it is chosen; a field not touched is the default, which the report keeps moving ──
const values = useState<Record<string, ReportFormValue>>(`report-designer-preview-${props.reportId}`, () => ({}))
const parameters = computed<ReportParameter[]>(() => store.doc.value.parameters.map((p) => {
  const fixed = p.choices && 'values' in p.choices ? p.choices.values : null
  const read = fresh.value.find((f) => f.name === p.name)
  return {
    name: p.name, label: p.label || p.name, type: p.type, multi: !!p.multi, required: !!p.required, default: read ? read.default : p.default,
    choices: fixed ? fixed.map((v) => ({ value: v, label: String(v) })) : p.choices ? listOf(read) : null,
    choices_truncated: !!read?.choices_truncated,
    choices_error: read?.choices_error ?? null,
  }
}))
/** The list read from a dataset; until the server answers, one with nothing in it (not a
 *  text field that turns into a list); if it cannot answer, a field to write in. */
function listOf(read: ReportParameter | undefined): ReportParameter['choices'] {
  if (failed.value) return null
  return read ? read.choices : []
}
const free = (p: ReportParameter) => p.multi && !(p.choices && !p.choices_truncated)
function defaultOf(p: ReportParameter): ReportFormValue {
  const d = p.default
  const texts = (d === null || d === undefined ? [] : Array.isArray(d) ? d : [d]).map(String)
  if (!p.multi) return texts[0] ?? ''
  return free(p) ? texts.join(', ') : texts
}
const form = computed(() => Object.fromEntries(parameters.value.map((p) => [p.name, values.value[p.name] ?? defaultOf(p)])))
function onForm(next: Record<string, ReportFormValue>) {
  const changed = parameters.value.filter((p) => JSON.stringify(next[p.name]) !== JSON.stringify(form.value[p.name]))
  if (!changed.length) return
  values.value = { ...values.value, ...Object.fromEntries(changed.map((p) => [p.name, next[p.name]])) }
  readChoicesSoon()
}
/** What was chosen, as a render takes it: a list where the parameter takes several. */
function params(): ReportParams {
  const out: ReportParams = {}
  for (const p of parameters.value) {
    const v = values.value[p.name]
    if (v === undefined) continue
    const clean = (Array.isArray(v) ? v : p.multi ? v.split(',') : [v]).map((x) => x.trim()).filter(Boolean)
    if (clean.length) out[p.name] = p.multi ? clean : clean[0]
  }
  return out
}

// ── the lists of the choices: read from the definition as it is, asked again when another
// value may change them; the report's fixed lists are there already ──
const fresh = ref<ReportParameter[]>([])
const failed = ref(false)
let choicesCall: AbortController | null = null
let choicesTimer: ReturnType<typeof setTimeout> | null = null
/** a list read from a dataset, or a default such as {today-30}, which only the server resolves */
const asksServer = computed(() => store.doc.value.parameters.some((p) => (!!p.choices && 'dataset' in p.choices) || /^\{.+\}$/.test(String(p.default ?? ''))))
async function readChoices() {
  choicesCall?.abort()
  if (!asksServer.value) { fresh.value = []; return }
  const mine = new AbortController()
  choicesCall = mine
  try {
    const got = await api.parametersOf(props.reportId, forSave(store.doc.value), params(), mine.signal)
    if (choicesCall === mine) { fresh.value = got; failed.value = false }
  } catch {
    // half written, or the server cannot say: a field to write in, the render says the rest
    if (choicesCall === mine) failed.value = true
  }
}
function readChoicesSoon() {
  if (choicesTimer) clearTimeout(choicesTimer)
  if (asksServer.value && store.doc.value.parameters.length > 1) choicesTimer = setTimeout(readChoices, 400)
}
watch(() => JSON.stringify([store.doc.value.parameters, store.doc.value.datasets]), readChoices, { immediate: true })
onBeforeUnmount(() => {
  choicesCall?.abort()
  if (choicesTimer) clearTimeout(choicesTimer)
})

// ── the render ──
const busy = ref(false)
const pages = ref<string[]>([])
const hotspots = ref<ReportHotspot[]>([])
const warnings = ref<string[]>([])
const error = ref<{ message: string; problems: DesignProblem[] } | null>(null)
const shownDef = ref<ReportDefinition | null>(null)
const view = ref<ReportState>({ items: {} })
const renderedText = ref('')
let controller: AbortController | null = null

async function run(keepView = false) {
  controller?.abort()
  const mine = new AbortController()
  controller = mine
  busy.value = true
  error.value = null
  if (!keepView) view.value = { items: {} }
  const definition = forSave(store.doc.value)
  const text = store.text.value
  try {
    const out = await api.preview(props.reportId, definition, params(), Object.keys(view.value.items).length ? view.value : null, mine.signal)
    if (controller !== mine) return
    pages.value = out.pages
    hotspots.value = out.hotspots
    warnings.value = out.warnings
    shownDef.value = definition as unknown as ReportDefinition
    renderedText.value = text
  } catch (e: any) {
    if (e?.name === 'AbortError' || controller !== mine) return
    warnings.value = [] // the notes of an earlier render are not this one's
    const detail = e?.data?.detail
    const code = e?.statusCode ?? e?.response?.status
    if (code === 422 && detail && typeof detail === 'object' && !Array.isArray(detail) && 'message' in detail) {
      const list = problemsOf(store.doc.value, detail)
      emit('problems', list)
      error.value = { message: list[0]?.message ?? errMessage(e), problems: list }
    } else {
      // a gateway that renders before it checks: the engine's text, read for its item
      const named = code === 422 && typeof detail === 'string' ? problemFromText(detail) : null
      if (named) emit('problems', [named])
      error.value = { message: errMessage(e), problems: named ? [named] : [] }
    }
  } finally {
    if (controller === mine) {
      busy.value = false
      controller = null
    }
  }
}
defineExpose({ run })
onBeforeUnmount(() => controller?.abort())

function onHotspot(h: ReportHotspot) {
  view.value = stateAfter(view.value, h)
  run(true)
}

const stale = computed(() => !!renderedText.value && renderedText.value !== store.text.value)
const notesOpen = ref(false)
watch(warnings, () => { notesOpen.value = false })
</script>

<template>
  <section class="rpv" :aria-label="$t('reportDesigner.preview.label')">
    <div class="rpv-bar">
      <ReportParamsBar v-if="parameters.length" class="rpv-form" :parameters="parameters" :model-value="form" :busy="busy" @update:model-value="onForm" @submit="run()" />
      <form v-else class="rpv-params" @submit.prevent="run()">
        <span class="rpv-none">{{ $t('reportDesigner.preview.noParams') }}</span>
        <button type="submit" class="rpv-run" :disabled="busy">
          <LoaderCircle v-if="busy" :size="13" class="spin" /><Play v-else :size="13" /> {{ $t('reportDesigner.preview.run') }}
        </button>
      </form>
      <span v-if="stale && !busy" class="rpv-stale">{{ $t('reportDesigner.preview.stale') }}</span>
      <button v-if="warnings.length" type="button" class="mini rpv-notes" :aria-expanded="notesOpen" @click="notesOpen = !notesOpen">
        <TriangleAlert :size="12" /> {{ $t('reportDesigner.preview.notes', { n: warnings.length }, warnings.length) }}
      </button>
      <span class="sr-only" role="status" aria-live="polite">{{ busy ? $t('reportDesigner.preview.rendering') : '' }}</span>
    </div>
    <ul v-if="notesOpen && warnings.length" class="rpv-list">
      <li v-for="(w, i) in warnings" :key="i">{{ w }}</li>
    </ul>
    <div class="rpv-stage">
      <div v-if="error" class="rpv-state" role="alert">
        <CircleAlert v-if="error.problems.length" :size="22" class="rpv-icon" aria-hidden="true" />
        <TriangleAlert v-else :size="22" class="rpv-icon" aria-hidden="true" />
        <h2>{{ error.problems.length ? $t('reportDesigner.preview.definitionError') : $t('reportDesigner.preview.renderError') }}</h2>
        <ul v-if="error.problems.length" class="rpv-problems">
          <li v-for="(p, i) in error.problems.slice(0, 8)" :key="i">
            <button type="button" @click="emit('go', p)">
              <span class="rpv-where">{{ where(p) }}</span>
              <span class="rpv-pmsg">{{ p.message }}</span>
            </button>
          </li>
        </ul>
        <p v-else class="rpv-msg">{{ error.message }}</p>
        <p class="rpv-hint">{{ error.problems.length ? $t('reportDesigner.preview.definitionHint') : $t('reportDesigner.preview.renderHint') }}</p>
        <button type="button" @click="run()"><RefreshCw :size="14" /> {{ $t('reportDesigner.preview.again') }}</button>
      </div>
      <ReportPages
        v-else
        :pages="pages"
        :hotspots="hotspots"
        :zoom="zoom"
        :busy="busy"
        :definition="shownDef"
        :label="$t('reportDesigner.preview.label')"
        @hotspot="onHotspot"
        @scale="emit('scale', $event)"
      />
    </div>
  </section>
</template>

<style scoped>
.rpv { display: flex; flex-direction: column; min-height: 0; height: 100%; background: var(--bg); }
.rpv-bar { display: flex; align-items: center; gap: 10px; padding: 8px 12px; border-bottom: 1px solid var(--border-soft); background: var(--panel); min-width: 0; }
.rpv-params { display: flex; align-items: flex-end; gap: 10px; flex: 1; min-width: 0; overflow-x: auto; padding-bottom: 1px; }
.rpv-none { font-size: 12px; color: var(--muted); align-self: center; }
.rpv-form { flex: 1; min-width: 0; padding: 0; border: 0; background: transparent; }
.rpv-run { flex-shrink: 0; white-space: nowrap; padding: 5px 11px; }
.rpv-stale { font-size: 12px; color: var(--warning); white-space: nowrap; flex-shrink: 0; }
.rpv-notes { color: var(--warning); border-color: color-mix(in srgb, var(--warning) 40%, var(--border)); white-space: nowrap; flex-shrink: 0; display: inline-flex; gap: 4px; align-items: center; }
.rpv-list {
  margin: 0; padding: 8px 12px 8px 30px; font-size: 12px; line-height: 1.45; color: var(--text); background: var(--panel);
  border-bottom: 1px solid var(--border-soft); display: flex; flex-direction: column; gap: 3px; max-height: 28vh; overflow: auto; overflow-wrap: anywhere;
}
.rpv-stage { flex: 1; min-height: 0; position: relative; display: flex; flex-direction: column; }
.rpv-state {
  margin: 48px auto; max-width: 520px; display: flex; flex-direction: column; align-items: center; gap: 8px; text-align: center;
  padding: 24px; border: 1px solid var(--border); border-radius: 14px; background: var(--panel);
}
.rpv-state h2 { margin: 4px 0 0; font-size: 16px; }
.rpv-icon { color: var(--warning); }
.rpv-msg { margin: 0; font-size: 13px; color: var(--text); overflow-wrap: anywhere; line-height: 1.45; }
.rpv-hint { margin: 0 0 6px; font-size: 12px; color: var(--muted); line-height: 1.45; }
.rpv-problems { list-style: none; margin: 4px 0; padding: 0; display: flex; flex-direction: column; gap: 2px; align-self: stretch; text-align: left; }
.rpv-problems button {
  width: 100%; justify-content: flex-start; align-items: baseline; gap: 8px; border: 0; background: transparent; box-shadow: none;
  padding: 6px 8px; font-size: 12.5px; text-align: left; line-height: 1.4;
}
.rpv-problems button:hover { background: var(--panel-2); box-shadow: none; }
.rpv-where { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11.5px; color: var(--muted); white-space: nowrap; flex: none; }
.rpv-pmsg { min-width: 0; overflow-wrap: anywhere; }
</style>
