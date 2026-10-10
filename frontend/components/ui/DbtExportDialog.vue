<script setup lang="ts">
// Dialogo del download dbt (solo amministratori): il progetto per il team data come
// lo vuole il loro repository — dove gira (target), progetto completo o cartella da
// copiare in un progetto esistente, nomi, sorgenti che hanno già, schema,
// materializzazioni, test del contratto, email. Le scelte restano nel browser, per
// flusso: chi riesporta lo stesso flusso ritrova le sue.
import { computed, ref, watch } from 'vue'
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
const aiDescrizioni = ref(false)
const aiTraduzioni = ref(false)

const TARGETS: DbtTarget[] = ['clickhouse', 'duckdb', 'native']
const memoria = (id: number) => `tabularia.dbtExport.${id}`

watch(
  () => props.flow,
  async (f) => {
    if (!f) return
    plan.value = null
    error.value = ''
    loading.value = true
    try {
      const p = await api.dbtExportPlan(f.id)
      plan.value = p
      preimposta(p)
      ripristina(f.id, p)
    } catch (e) {
      error.value = errMessage(e)
    } finally {
      loading.value = false
    }
  },
  { immediate: true },
)

function preimposta(p: DbtExportPlan) {
  target.value = p.targets.find((x) => x.available)?.id ?? 'clickhouse'
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
  aiDescrizioni.value = false
  aiTraduzioni.value = false
}

/** Le scelte dell'ultimo download di questo flusso, solo quelle ancora valide. */
function ripristina(id: number, p: DbtExportPlan) {
  try {
    const raw = localStorage.getItem(memoria(id))
    if (!raw) return
    const s = JSON.parse(raw) as Partial<DbtExportOptions>
    if (s.target && p.targets.find((x) => x.id === s.target)?.available) target.value = s.target
    if (s.package === 'project' || s.package === 'folder') pkg.value = s.package
    if (typeof s.folder === 'string') folder.value = s.folder
    if (typeof s.prefix === 'string') prefix.value = s.prefix
    if (typeof s.layers === 'boolean') layers.value = s.layers
    if (typeof s.schema === 'string') schema.value = s.schema
    if (typeof s.tests === 'boolean') tests.value = s.tests
    if (typeof s.emails === 'boolean') emails.value = s.emails
    if (p.ai?.available) {
      if (typeof s.ai_descriptions === 'boolean') aiDescrizioni.value = s.ai_descriptions
      if (typeof s.ai_translations === 'boolean') aiTraduzioni.value = s.ai_translations
    }
    for (const [k, v] of Object.entries(s.sources ?? {})) {
      if (v?.name) sourceNames.value[k] = v.name
      if (v?.declared) declared.value[k] = true
    }
    for (const o of p.outputs) {
      const m = s.materializations?.[o.key]
      if (m && o.materializations.includes(m)) mats.value[o.key] = m
    }
  } catch {
    /* niente memoria (navigazione privata, dati cancellati): i valori di default */
  }
}

function ricorda(id: number, o: DbtExportOptions) {
  try {
    localStorage.setItem(memoria(id), JSON.stringify(o))
  } catch {
    /* non si ricorda: pazienza */
  }
}

const corrente = computed(() => plan.value?.targets.find((x) => x.id === target.value) ?? null)
const sorgenti = computed(() => corrente.value?.sources ?? [])
const cartella = computed(() =>
  pkg.value === 'folder' ? folder.value.trim() || plan.value?.default_folder || '' : folder.value.trim(),
)

// le stesse forme che il gateway accetta: un valore sbagliato si vede qui, non al download
const RE = {
  folder: /^[a-z0-9_]{0,40}$/,
  prefix: /^([a-z][a-z0-9_]{0,19})?$/,
  schema: /^([A-Za-z_][A-Za-z0-9_]{0,62})?$/,
  source: /^([a-z_][a-z0-9_]{0,62})?$/,
}
const errori = computed(() => {
  const e: Record<string, boolean> = {}
  if (!RE.folder.test(cartella.value)) e.folder = true
  if (!RE.prefix.test(prefix.value.trim())) e.prefix = true
  if (!RE.schema.test(schema.value.trim())) e.schema = true
  for (const s of sorgenti.value) if (!RE.source.test((sourceNames.value[s.key] ?? '').trim())) e[`source:${s.key}`] = true
  return e
})
const valido = computed(() => !Object.keys(errori.value).length && !!corrente.value?.available)

function opzioni(): DbtExportOptions {
  const sources: DbtExportOptions['sources'] = {}
  for (const s of sorgenti.value) {
    const name = (sourceNames.value[s.key] ?? '').trim()
    if (name || declared.value[s.key]) sources[s.key] = { name: name || null, declared: !!declared.value[s.key] }
  }
  const materializations: DbtExportOptions['materializations'] = {}
  for (const o of plan.value?.outputs ?? []) {
    const m = mats.value[o.key]
    if (m && m !== o.materialized) materializations[o.key] = m
  }
  return {
    target: target.value,
    package: pkg.value,
    folder: cartella.value,
    prefix: prefix.value.trim(),
    layers: layers.value,
    sources,
    ...(schema.value.trim() ? { schema: schema.value.trim() } : {}),
    materializations,
    tests: tests.value,
    emails: emails.value,
    // senza AI disponibile le opzioni non si mandano: il gateway rifiuterebbe l'export
    ...(plan.value?.ai?.available ? { ai_descriptions: aiDescrizioni.value, ai_translations: aiTraduzioni.value } : {}),
  }
}

/** Il motivo vero di un rifiuto: col download la risposta d'errore arriva come Blob. */
async function motivo(e: any): Promise<string> {
  const d = e?.data
  if (typeof Blob !== 'undefined' && d instanceof Blob) {
    try {
      const j = JSON.parse(await d.text())
      if (typeof j?.detail === 'string') return j.detail
      if (Array.isArray(j?.detail)) return j.detail.map((x: any) => x?.msg ?? String(x)).join('; ')
    } catch {
      /* non era JSON */
    }
  }
  return errMessage(e)
}

async function scarica() {
  const f = props.flow
  if (!f || busy.value || !valido.value) return
  busy.value = true
  error.value = ''
  const o = opzioni()
  try {
    const blob = await api.exportFlowDbt(f.id, o)
    ricorda(f.id, o)
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    const base = f.name.replace(/[^\w-]+/g, '_').replace(/^_|_$/g, '') || 'flow'
    a.download = `${base}_dbt_${o.target}${o.package === 'folder' ? '_folder' : ''}.zip`
    a.click()
    URL.revokeObjectURL(url)
    emit('close')
  } catch (e) {
    error.value = await motivo(e)
  } finally {
    busy.value = false
  }
}

const etichettaTarget = (id: DbtTarget) =>
  ({ clickhouse: t('dbtExportDialog.targetClickhouse'), duckdb: t('dbtExportDialog.targetDuckdb'), native: t('dbtExportDialog.targetNative') })[id]
const suggerimentoTarget = (id: DbtTarget) =>
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
            <label v-for="id in TARGETS" :key="id" class="dx-choice" :class="{ off: !plan.targets.find((x) => x.id === id)?.available }">
              <input v-model="target" type="radio" name="dx-target" :value="id" :disabled="!plan.targets.find((x) => x.id === id)?.available" />
              <span>
                <strong>{{ etichettaTarget(id) }}</strong>
                <small v-if="plan.targets.find((x) => x.id === id)?.available">{{ suggerimentoTarget(id) }}</small>
                <small v-else class="dx-why">{{ $t('dbtExportDialog.unavailable') }}: {{ plan.targets.find((x) => x.id === id)?.reason }}</small>
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
                <input v-model="folder" type="text" spellcheck="false" :class="{ bad: errori.folder }"
                       :placeholder="pkg === 'folder' ? plan.default_folder : $t('dbtExportDialog.folderRoot')" />
              </label>
              <label class="dx-field">
                <span>{{ $t('dbtExportDialog.prefix') }}</span>
                <input v-model="prefix" type="text" spellcheck="false" placeholder="tab_" :class="{ bad: errori.prefix }" />
              </label>
              <label class="dx-field">
                <span>{{ $t('dbtExportDialog.schema') }}</span>
                <input v-model="schema" type="text" spellcheck="false" :class="{ bad: errori.schema }"
                       :placeholder="target === 'duckdb' ? 'main' : 'dbt_tabularia'" />
              </label>
            </div>
            <label class="dx-check"><input v-model="layers" type="checkbox" /> {{ $t('dbtExportDialog.layers') }}</label>
            <p v-if="errori.folder || errori.prefix || errori.schema" class="dx-bad">{{ $t('dbtExportDialog.invalidName') }}</p>
          </fieldset>

          <fieldset v-if="sorgenti.length" class="dx-group">
            <legend>{{ $t('dbtExportDialog.sources') }}</legend>
            <p class="muted dx-hint">{{ $t('dbtExportDialog.sourcesHint') }}</p>
            <div v-for="s in sorgenti" :key="s.key" class="dx-source">
              <div class="dx-source-what">
                <strong>{{ s.connection }} · {{ s.schema }}</strong>
                <small class="muted">{{ s.tables.join(', ') }}</small>
              </div>
              <input v-model="sourceNames[s.key]" type="text" spellcheck="false" :placeholder="s.default_name"
                     :aria-label="`${$t('dbtExportDialog.sourceName')} ${s.connection} ${s.schema}`" :class="{ bad: errori[`source:${s.key}`] }" />
              <label class="dx-check"><input v-model="declared[s.key]" type="checkbox" /> {{ $t('dbtExportDialog.declared') }}</label>
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
            <template v-if="plan.ai?.available">
              <label class="dx-check"><input v-model="aiDescrizioni" type="checkbox" /> {{ $t('dbtExportDialog.aiDescriptions') }}</label>
              <label class="dx-check"><input v-model="aiTraduzioni" type="checkbox" /> {{ $t('dbtExportDialog.aiTranslations') }}</label>
              <p v-if="aiDescrizioni || aiTraduzioni" class="muted dx-hint">
                {{ $t('dbtExportDialog.aiHint', { model: plan.ai.model }) }}
              </p>
            </template>
          </fieldset>
        </template>

        <p v-if="error" class="dx-error" role="alert">{{ error }}</p>

        <div class="dx-actions">
          <span class="dx-spacer" />
          <button :disabled="busy" @click="emit('close')">{{ $t('dbtExportDialog.cancel') }}</button>
          <button class="primary" :disabled="busy || loading || !plan || !valido" @click="scarica">
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
