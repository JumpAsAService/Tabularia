<script setup lang="ts">
// The dbt download's dialog (administrators only): the project for the data team as their
// repository wants it — where it runs (target), complete project or folder to copy into
// an existing project, names, sources they have already, schema, materializations, the
// contract's tests, email. The choices stay in the browser, per flow: whoever exports the
// same flow again finds theirs.
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { Download, X } from 'lucide-vue-next'
import {
  errMessage,
  useApi,
  type DbtExportOptions,
  type DbtExportPlan,
  type DbtMaterialization,
  type DbtTarget,
} from '~/composables/useApi'
import { useDialogA11y } from '~/composables/useDialogA11y'

const props = defineProps<{ flow: { id: number; name: string } | null }>()
const emit = defineEmits<{ (e: 'close'): void }>()
const { t } = useI18n()
const api = useApi()
const card = ref<HTMLElement | null>(null)
useDialogA11y(card, () => !!props.flow, () => emit('close'))

const plan = ref<DbtExportPlan | null>(null)
const loading = ref(false)
const busy = ref(false)
const error = ref('')

const target = ref<DbtTarget>('clickhouse')
const pkg = ref<'project' | 'folder'>('project')
const folder = ref('')
const prefix = ref('')
const layers = ref(false)
const schema = ref('')
const sourceNames = ref<Record<string, string>>({})
const declared = ref<Record<string, boolean>>({})
const mats = ref<Record<string, DbtMaterialization>>({})
const tests = ref(true)
const emails = ref(true)
const aiDescriptions = ref(false)
const aiTranslations = ref(false)

const TARGETS: DbtTarget[] = ['clickhouse', 'duckdb', 'native']
const memory = (id: number) => `tabularia.dbtExport.${id}`

type PlanTarget = DbtExportPlan['targets'][number]
/** A target can be chosen when the plan offers it, or, in the folder package, when it only needs
 *  sources the team's own project already declares (`requires`: ClickHouse with an Oracle source):
 *  the gateway accepts it there, with those sources sent «already declared». */
const usable = (x: PlanTarget | null | undefined, pack: 'project' | 'folder') =>
  !!x && (x.available || (pack === 'folder' && x.requires?.package === 'folder'))

// The component stays mounted between openings: each opening (and each closing) gets a new
// number and aborts the requests of the one before. An answer that still arrives for an
// earlier opening is dropped, so a slow plan or download of one flow never lands in the
// dialog of the next one. Closing the dialog, however it is closed, cancels its download.
let opening = 0
let requests = new AbortController()

watch(
  () => props.flow,
  async (f) => {
    const mine = ++opening
    requests.abort()
    requests = new AbortController()
    const { signal } = requests
    plan.value = null
    error.value = ''
    busy.value = false
    loading.value = !!f
    if (!f) return
    try {
      const p = await api.dbtExportPlan(f.id, { signal })
      if (mine !== opening) return
      plan.value = p
      preimposta(p)
      restore(f.id, p)
    } catch (e) {
      if (mine === opening) error.value = errMessage(e)
    } finally {
      if (mine === opening) loading.value = false
    }
  },
  { immediate: true },
)

// Leaving the page (a link, the back button) unmounts the dialog without closing it: its
// download goes as well, or the zip would still be saved on whatever page the person is on.
onBeforeUnmount(() => {
  opening++
  requests.abort()
})

function preimposta(p: DbtExportPlan) {
  // with nothing available, the target the folder package can still take (if any) is the one shown
  target.value = p.targets.find((x) => x.available)?.id ?? p.targets.find((x) => x.requires)?.id ?? 'clickhouse'
  pkg.value = 'project'
  folder.value = ''
  prefix.value = ''
  layers.value = false
  schema.value = ''
  sourceNames.value = {}
  declared.value = {}
  mats.value = Object.fromEntries(p.outputs.map((o) => [o.key, o.materialized]))
  tests.value = true
  emails.value = true
  aiDescriptions.value = false
  aiTranslations.value = false
}

/** The choices of this flow's last download, only those still valid. */
function restore(id: number, p: DbtExportPlan) {
  try {
    const raw = localStorage.getItem(memory(id))
    if (!raw) return
    const s = JSON.parse(raw) as Partial<DbtExportOptions>
    if (s.package === 'project' || s.package === 'folder') pkg.value = s.package
    // the package first: a target that needs the folder package is valid only with it
    if (s.target && usable(p.targets.find((x) => x.id === s.target), pkg.value)) target.value = s.target
    if (typeof s.folder === 'string') folder.value = s.folder
    if (typeof s.prefix === 'string') prefix.value = s.prefix
    if (typeof s.layers === 'boolean') layers.value = s.layers
    if (typeof s.schema === 'string') schema.value = s.schema
    if (typeof s.tests === 'boolean') tests.value = s.tests
    if (typeof s.emails === 'boolean') emails.value = s.emails
    // only the options the dialog offers for this plan: the AI on, or texts kept for this version
    if (aiOffered(p, 'descriptions') && typeof s.ai_descriptions === 'boolean') aiDescriptions.value = s.ai_descriptions
    if (aiOffered(p, 'translations') && typeof s.ai_translations === 'boolean') aiTranslations.value = s.ai_translations
    for (const [k, v] of Object.entries(s.sources ?? {})) {
      if (v?.name) sourceNames.value[k] = v.name
      if (v?.declared) declared.value[k] = true
    }
    for (const o of p.outputs) {
      const m = s.materializations?.[o.key]
      if (m && o.materializations.includes(m)) mats.value[o.key] = m
    }
  } catch {
    /* no memory (private browsing, data deleted): the default values */
  }
}

function remember(id: number, o: DbtExportOptions) {
  try {
    localStorage.setItem(memory(id), JSON.stringify(o))
  } catch {
    /* not remembered: never mind */
  }
}

const current_ = computed(() => plan.value?.targets.find((x) => x.id === target.value) ?? null)
const planSources = computed(() => current_.value?.sources ?? [])
// the sources the chosen target needs «already declared» (folder package only): ticked, locked, sent
const required = computed(() => new Set(pkg.value === 'folder' ? (current_.value?.requires?.declared ?? []) : []))
const folderName = computed(() =>
  pkg.value === 'folder' ? folder.value.trim() || plan.value?.default_folder || '' : folder.value.trim(),
)

// the same forms the gateway accepts: a wrong value shows here, not at the download
const RE = {
  folder: /^[a-z0-9_]{0,40}$/,
  prefix: /^([a-z][a-z0-9_]{0,19})?$/,
  schema: /^([A-Za-z_][A-Za-z0-9_]{0,62})?$/,
  source: /^([a-z_][a-z0-9_]{0,62})?$/,
}
// The name each source will have in sources.yml, as the gateway gives it (`_nomi_sorgenti`, in
// the plan's order): the typed one, else the unsuffixed default, moved aside with a suffix (_2,
// _3…) when a typed name or an earlier source already took it. The plan's default_name already
// carries the suffix of the collisions with nothing typed, so it is not the base to suffix from.
const effectiveSourceNames = computed(() => {
  const names: Record<string, string> = {}
  const used = new Set<string>()
  for (const s of planSources.value) {
    const typed = (sourceNames.value[s.key] ?? '').trim()
    if (typed) used.add((names[s.key] = typed))
  }
  for (const s of planSources.value) {
    if (names[s.key]) continue
    const base = s.default_base || s.default_name
    let name = base
    for (let n = 2; used.has(name); n++) name = `${base}_${n}`
    used.add((names[s.key] = name))
  }
  return names
})

// each error says the rule it breaks, on its own field (and to assistive technology too)
type FieldError = { rule: 'folder' | 'prefix' | 'schema' | 'source' } | { rule: 'duplicate'; name: string; table: string }
const errors = computed(() => {
  const e: Record<string, FieldError> = {}
  if (!RE.folder.test(folderName.value)) e.folder = { rule: 'folder' }
  if (!RE.prefix.test(prefix.value.trim())) e.prefix = { rule: 'prefix' }
  if (pkg.value === 'project' && !RE.schema.test(schema.value.trim())) e.schema = { rule: 'schema' }
  // dbt refuses two sources with the same name that both list the same table (a shared name
  // with different tables is fine): the name each source will have, typed or default
  const seen = new Map<string, string>()
  for (const s of planSources.value) {
    const typed = (sourceNames.value[s.key] ?? '').trim()
    if (!RE.source.test(typed)) {
      e[`source:${s.key}`] = { rule: 'source' }
      continue
    }
    const name = effectiveSourceNames.value[s.key]
    for (const table of s.tables) {
      const other = seen.get(`${name}\u0000${table}`)
      if (other === undefined) seen.set(`${name}\u0000${table}`, s.key)
      else for (const k of [other, s.key]) e[`source:${k}`] ??= { rule: 'duplicate', name, table }
    }
  }
  return e
})
function errorText(x: FieldError | undefined): string {
  if (!x) return ''
  if (x.rule === 'duplicate') return t('dbtExportDialog.duplicateSource', { name: x.name, table: x.table })
  return t({ folder: 'dbtExportDialog.invalidFolder', prefix: 'dbtExportDialog.invalidPrefix', schema: 'dbtExportDialog.invalidSchema', source: 'dbtExportDialog.invalidSource' }[x.rule])
}
const valid = computed(() => !Object.keys(errors.value).length && usable(current_.value, pkg.value))

function options(): DbtExportOptions {
  const sources: DbtExportOptions['sources'] = {}
  for (const s of planSources.value) {
    const name = (sourceNames.value[s.key] ?? '').trim()
    // «already declared» only means something for a folder copied into the team's project: a
    // complete project has no other sources.yml that declares the source
    const isDeclared = pkg.value === 'folder' && (required.value.has(s.key) || !!declared.value[s.key])
    if (name || isDeclared) sources[s.key] = { name: name || null, declared: isDeclared }
  }
  const materializations: DbtExportOptions['materializations'] = {}
  for (const o of plan.value?.outputs ?? []) {
    const m = mats.value[o.key]
    if (m && m !== o.materialized) materializations[o.key] = m
  }
  return {
    target: target.value,
    package: pkg.value,
    folder: folderName.value,
    prefix: prefix.value.trim(),
    layers: layers.value,
    sources,
    // the schema is the profile's, and the folder package has no profiles.yml: there it is the
    // team's own profile that decides, so the option is neither shown nor sent
    ...(pkg.value === 'project' && schema.value.trim() ? { schema: schema.value.trim() } : {}),
    materializations,
    tests: tests.value,
    emails: emails.value,
    // only what the dialog offers: with the AI off, an option nothing was kept for would ask the
    // AI and the gateway would refuse the export
    ...(aiDescriptionsOffered.value ? { ai_descriptions: aiDescriptions.value } : {}),
    ...(aiTranslationsOffered.value ? { ai_translations: aiTranslations.value } : {}),
  }
}

/** What to remember of a download: what the person chose, which is not always what was sent.
 *  An empty folder stays empty (the default follows the flow's name, and only the folder
 *  package has one); the schema and the «already declared» ticks stay for when their
 *  package is chosen again. */
function remembered(o: DbtExportOptions): DbtExportOptions {
  const sources: DbtExportOptions['sources'] = {}
  for (const s of planSources.value) {
    const name = (sourceNames.value[s.key] ?? '').trim()
    if (name || declared.value[s.key]) sources[s.key] = { name: name || null, declared: !!declared.value[s.key] }
  }
  return { ...o, folder: folder.value.trim(), sources, schema: schema.value.trim() || undefined }
}

/** The real reason of a refusal: with the download the error answer arrives as a Blob. */
async function reason(e: any): Promise<string> {
  const d = e?.data
  if (typeof Blob !== 'undefined' && d instanceof Blob) {
    try {
      const j = JSON.parse(await d.text())
      if (typeof j?.detail === 'string') return j.detail
      if (Array.isArray(j?.detail)) return j.detail.map((x: any) => x?.msg ?? String(x)).join('; ')
    } catch {
      /* it was not JSON */
    }
  }
  return errMessage(e)
}

async function download() {
  const f = props.flow
  if (!f || busy.value || !valid.value) return
  const mine = opening
  const { signal } = requests
  busy.value = true
  error.value = ''
  const o = options()
  const memo = remembered(o)
  try {
    const { blob, filename } = await api.exportFlowDbt(f.id, o, { signal })
    // closed meanwhile (or opened on another flow): this zip is no one's any more
    if (mine !== opening) return
    remember(f.id, memo)
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    // the gateway's name when it can be read; otherwise the same rule here: letters and digits
    // of any alphabet stay (`\w` alone is ASCII and turned «Qualità» into «Qualit_»)
    const base = f.name.replace(/[^\p{L}\p{N}_-]/gu, '_').replace(/^_+|_+$/g, '') || 'flow'
    a.download = filename ?? `${base}_dbt_${o.target}${o.package === 'folder' ? '_folder' : ''}.zip`
    a.click()
    URL.revokeObjectURL(url)
    emit('close')
  } catch (e) {
    const m = await reason(e)
    if (mine === opening) error.value = m
  } finally {
    if (mine === opening) busy.value = false
  }
}

/** An AI option is offered when the AI is on, or when the AI kept texts of that kind for the flow's
 *  current version: a re-export reuses them without a call, so the same version gives the same zip. */
function aiOffered(p: DbtExportPlan | null, kind: 'descriptions' | 'translations'): boolean {
  return !!p?.ai && (p.ai.available || !!p.ai.stored?.[kind])
}
const aiDescriptionsOffered = computed(() => aiOffered(plan.value, 'descriptions'))
const aiTranslationsOffered = computed(() => aiOffered(plan.value, 'translations'))
// Who writes what the ticked options bring: the models that wrote the kept texts for a kind that has
// some, the configured model for a kind nothing was kept for (the AI on).
const aiHint = computed(() => {
  const ai = plan.value?.ai
  const choices = [aiDescriptionsOffered.value && aiDescriptions.value && 'descriptions', aiTranslationsOffered.value && aiTranslations.value && 'translations']
    .filter((k): k is 'descriptions' | 'translations' => !!k)
  if (!ai || !choices.length) return ''
  const models = new Set<string>()
  if (choices.some((k) => ai.stored?.[k])) for (const m of ai.stored?.models ?? []) models.add(m)
  if (ai.available && ai.model && choices.some((k) => !ai.stored?.[k])) models.add(ai.model)
  const model = [...models].sort().join(', ')
  if (ai.available) return t('dbtExportDialog.aiHint', { model: model || ai.model || '' })
  return model ? t('dbtExportDialog.aiStoredHint', { model }) : t('dbtExportDialog.aiStoredHintUnnamed')
})

const item = (id: DbtTarget) => plan.value?.targets.find((x) => x.id === id)
const targetLabel = (id: DbtTarget) =>
  ({ clickhouse: t('dbtExportDialog.targetClickhouse'), duckdb: t('dbtExportDialog.targetDuckdb'), native: t('dbtExportDialog.targetNative') })[id]
const targetHint = (id: DbtTarget) =>
  ({ clickhouse: t('dbtExportDialog.targetClickhouseHint'), duckdb: t('dbtExportDialog.targetDuckdbHint'), native: t('dbtExportDialog.targetNativeHint') })[id]
</script>

<template>
  <Teleport to="body">
    <div v-if="flow" class="dx-backdrop" @mousedown.self="emit('close')">
      <div ref="card" class="dx-card" role="dialog" aria-modal="true" aria-labelledby="dx-title" tabindex="-1">
        <div class="dx-head">
          <h3 id="dx-title">{{ $t('dbtExportDialog.heading') }}</h3>
          <button class="dx-x" :aria-label="$t('dbtExportDialog.cancel')" @click="emit('close')"><X :size="14" /></button>
        </div>
        <p class="muted dx-sub">{{ $t('dbtExportDialog.subtitle') }} <strong>{{ flow.name }}</strong></p>

        <p v-if="loading" class="muted">{{ $t('dbtExportDialog.loading') }}</p>

        <template v-else-if="plan">
          <fieldset class="dx-group">
            <legend>{{ $t('dbtExportDialog.target') }}</legend>
            <label v-for="id in TARGETS" :key="id" class="dx-choice" :class="{ off: !usable(item(id), pkg) }">
              <input v-model="target" type="radio" name="dx-target" :value="id" :disabled="!usable(item(id), pkg)" />
              <span>
                <strong>{{ targetLabel(id) }}</strong>
                <small v-if="item(id)?.available">{{ targetHint(id) }}</small>
                <small v-else-if="usable(item(id), pkg)">{{ $t('dbtExportDialog.targetOnlyDeclared') }}</small>
                <small v-else class="dx-why">{{ $t('dbtExportDialog.unavailable') }}: {{ item(id)?.reason }}</small>
              </span>
            </label>
          </fieldset>

          <fieldset class="dx-group">
            <legend>{{ $t('dbtExportDialog.package') }}</legend>
            <label class="dx-choice">
              <input v-model="pkg" type="radio" name="dx-package" value="project" />
              <span><strong>{{ $t('dbtExportDialog.packageProject') }}</strong><small>{{ $t('dbtExportDialog.packageProjectHint') }}</small></span>
            </label>
            <label class="dx-choice">
              <input v-model="pkg" type="radio" name="dx-package" value="folder" />
              <span><strong>{{ $t('dbtExportDialog.packageFolder') }}</strong><small>{{ $t('dbtExportDialog.packageFolderHint') }}</small></span>
            </label>
            <div class="dx-row">
              <label class="dx-field">
                <span>{{ $t('dbtExportDialog.folder') }}</span>
                <input v-model="folder" type="text" spellcheck="false" :class="{ bad: errors.folder }"
                       :aria-invalid="errors.folder ? 'true' : undefined" :aria-describedby="errors.folder ? 'dx-bad-folder' : undefined"
                       :placeholder="pkg === 'folder' ? plan.default_folder : $t('dbtExportDialog.folderRoot')" />
              </label>
              <label class="dx-field">
                <span>{{ $t('dbtExportDialog.prefix') }}</span>
                <input v-model="prefix" type="text" spellcheck="false" placeholder="tab_" :class="{ bad: errors.prefix }"
                       :aria-invalid="errors.prefix ? 'true' : undefined" :aria-describedby="errors.prefix ? 'dx-bad-prefix' : undefined" />
              </label>
              <label v-if="pkg === 'project'" class="dx-field">
                <span>{{ $t('dbtExportDialog.schema') }}</span>
                <input v-model="schema" type="text" spellcheck="false" :class="{ bad: errors.schema }"
                       :aria-invalid="errors.schema ? 'true' : undefined" :aria-describedby="errors.schema ? 'dx-bad-schema' : undefined"
                       :placeholder="target === 'duckdb' ? 'main' : 'dbt_tabularia'" />
              </label>
            </div>
            <label class="dx-check"><input v-model="layers" type="checkbox" /> {{ $t('dbtExportDialog.layers') }}</label>
            <p v-if="errors.folder" id="dx-bad-folder" class="dx-bad">{{ errorText(errors.folder) }}</p>
            <p v-if="errors.prefix" id="dx-bad-prefix" class="dx-bad">{{ errorText(errors.prefix) }}</p>
            <p v-if="errors.schema" id="dx-bad-schema" class="dx-bad">{{ errorText(errors.schema) }}</p>
          </fieldset>

          <fieldset v-if="planSources.length" class="dx-group">
            <legend>{{ $t('dbtExportDialog.sources') }}</legend>
            <p class="muted dx-hint">{{ pkg === 'folder' ? $t('dbtExportDialog.sourcesHint') : $t('dbtExportDialog.sourcesProjectHint') }}</p>
            <div v-for="(s, i) in planSources" :key="s.key" class="dx-source">
              <div class="dx-source-what">
                <strong>{{ s.connection }} · {{ s.schema }}</strong>
                <small class="muted">{{ s.tables.join(', ') }}</small>
              </div>
              <input v-model="sourceNames[s.key]" type="text" spellcheck="false" :placeholder="effectiveSourceNames[s.key]"
                     :aria-label="`${$t('dbtExportDialog.sourceName')} ${s.connection} ${s.schema}`" :class="{ bad: errors[`source:${s.key}`] }"
                     :aria-invalid="errors[`source:${s.key}`] ? 'true' : undefined" :aria-describedby="errors[`source:${s.key}`] ? `dx-bad-source-${i}` : undefined" />
              <!-- a source the chosen target cannot read: the team's project must declare it, so it stays ticked -->
              <label v-if="required.has(s.key)" class="dx-check">
                <input type="checkbox" checked disabled :aria-describedby="`dx-req-source-${i}`" /> {{ $t('dbtExportDialog.declared') }}
              </label>
              <label v-else-if="pkg === 'folder'" class="dx-check"><input v-model="declared[s.key]" type="checkbox" /> {{ $t('dbtExportDialog.declared') }}</label>
              <p v-if="required.has(s.key)" :id="`dx-req-source-${i}`" class="muted dx-hint dx-source-note">
                {{ $t('dbtExportDialog.declaredRequired', { target: targetLabel(target) }) }}
              </p>
              <p v-if="errors[`source:${s.key}`]" :id="`dx-bad-source-${i}`" class="dx-bad dx-source-bad">{{ errorText(errors[`source:${s.key}`]) }}</p>
            </div>
          </fieldset>

          <fieldset v-if="plan.outputs.length" class="dx-group">
            <legend>{{ $t('dbtExportDialog.outputs') }}</legend>
            <div v-for="o in plan.outputs" :key="o.key" class="dx-output">
              <span class="dx-output-what"><code>{{ prefix.trim() }}{{ o.model }}</code><small class="muted">{{ o.label }}<template v-if="o.flow !== flow.name"> · {{ o.flow }}</template></small></span>
              <select v-model="mats[o.key]" :aria-label="`${$t('dbtExportDialog.materialization')} ${o.model}`">
                <option v-for="m in o.materializations" :key="m" :value="m">{{ m }}</option>
              </select>
            </div>
          </fieldset>

          <fieldset class="dx-group">
            <legend>{{ $t('dbtExportDialog.include') }}</legend>
            <label class="dx-check"><input v-model="tests" type="checkbox" /> {{ $t('dbtExportDialog.tests') }}</label>
            <label class="dx-check"><input v-model="emails" type="checkbox" /> {{ $t('dbtExportDialog.emails') }}</label>
            <!-- with the AI off, only what it kept for this version: reusing it asks the AI nothing -->
            <label v-if="aiDescriptionsOffered" class="dx-check">
              <input v-model="aiDescriptions" type="checkbox" />
              {{ plan.ai?.available ? $t('dbtExportDialog.aiDescriptions') : $t('dbtExportDialog.aiStoredDescriptions') }}
            </label>
            <label v-if="aiTranslationsOffered" class="dx-check">
              <input v-model="aiTranslations" type="checkbox" />
              {{ plan.ai?.available ? $t('dbtExportDialog.aiTranslations') : $t('dbtExportDialog.aiStoredTranslations') }}
            </label>
            <p v-if="aiHint" class="muted dx-hint">{{ aiHint }}</p>
          </fieldset>
        </template>

        <p v-if="error" class="dx-error" role="alert">{{ error }}</p>

        <div class="dx-actions">
          <span class="dx-spacer" />
          <!-- also while preparing: closing cancels the download -->
          <button @click="emit('close')">{{ $t('dbtExportDialog.cancel') }}</button>
          <button class="primary" :disabled="busy || loading || !plan || !valid" @click="download">
            <Download :size="14" /> {{ busy ? $t('dbtExportDialog.downloading') : $t('dbtExportDialog.download') }}
          </button>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.dx-backdrop {
  position: fixed; inset: 0; background: var(--scrim); backdrop-filter: blur(2px);
  display: flex; align-items: center; justify-content: center; z-index: 2000;
}
.dx-card {
  width: min(640px, calc(100vw - 32px)); max-height: calc(100vh - 32px); overflow-y: auto;
  background: var(--panel); border: 1px solid var(--border); border-radius: 14px;
  box-shadow: var(--shadow-2); padding: 18px 20px; display: flex; flex-direction: column; gap: 10px;
}
.dx-head { display: flex; align-items: center; justify-content: space-between; }
.dx-head h3 { margin: 0; font-size: 16px; }
.dx-x { padding: 3px 7px; }
.dx-sub { font-size: 12px; margin: 0; }
.dx-group { border: 1px solid var(--border); border-radius: 10px; padding: 8px 12px 10px; margin: 0; display: flex; flex-direction: column; gap: 6px; }
.dx-group legend { font-size: 12px; color: var(--muted); padding: 0 4px; }
.dx-choice { display: flex; gap: 8px; align-items: flex-start; cursor: pointer; }
.dx-choice input { margin-top: 3px; }
.dx-choice span { display: flex; flex-direction: column; }
.dx-choice small { font-size: 12px; color: var(--muted); }
.dx-choice.off { opacity: 0.6; cursor: default; }
.dx-why { color: var(--danger) !important; }
.dx-row { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 4px; }
.dx-field { display: flex; flex-direction: column; gap: 2px; flex: 1 1 150px; font-size: 12px; color: var(--muted); }
.dx-field input, .dx-source input { font-family: ui-monospace, monospace; }
input.bad { border-color: var(--danger); }
.dx-check { display: inline-flex; align-items: center; gap: 6px; font-size: 13px; }
.dx-hint { font-size: 12px; margin: 0; }
.dx-source { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 180px) auto; gap: 8px; align-items: center; }
.dx-source-what { display: flex; flex-direction: column; min-width: 0; }
.dx-source-what small { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.dx-source-bad, .dx-source-note { grid-column: 1 / -1; }
.dx-output { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.dx-output-what { display: flex; flex-direction: column; min-width: 0; }
.dx-output-what code, .dx-output-what small { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.dx-output select { flex: 0 0 180px; }
.dx-bad, .dx-error { font-size: 12.5px; color: var(--danger); margin: 0; }
.dx-actions { display: flex; align-items: center; gap: 8px; margin-top: 4px; }
.dx-actions .primary { display: inline-flex; align-items: center; gap: 6px; }
.dx-spacer { flex: 1; }
@media (max-width: 560px) {
  .dx-source { grid-template-columns: 1fr; }
}
</style>
