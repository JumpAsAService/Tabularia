<script setup lang="ts">
// The group `bayes`'s own fields on its card in Administration → Statistical models
// (docs/specs/bayes-stan-compile.md, «The administration card»): the compile limits, «Compiles at once», the cache of
// binaries with its figures and «Empty the cache», the sampling Job's /tmp, and the last compiles. Every setting shows
// its default and can go back to it. Observers read it all and change nothing. Every control is a native button or
// field: Tab, Enter and Space reach all.
import { computed, onMounted, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { CheckCircle2, CircleDot, LoaderCircle, RefreshCw, RotateCcw, Trash2, TriangleAlert, XCircle } from 'lucide-vue-next'
import { errMessage } from '~/composables/useApi'
import { useFormat } from '~/composables/useFormat'
import {
  BAYES_CARD_FIELDS, bayesBytesLabel, bayesCardDraftOf, bayesCardInputOf, useModelsAdmin,
  type BayesCardAnswer, type BayesCardDraft, type BayesCardValues, type BayesCompileRowState,
} from '~/composables/useModelsAdmin'

const props = defineProps<{ editable: boolean }>()

const { t } = useI18n()
const fmt = useFormat()
const api = useModelsAdmin()
const toast = useToast()

// a size in the interface's language (the decimal mark)
const size = (n: number) => bayesBytesLabel(n, (v) => fmt.number(v, 1))

const card = ref<BayesCardAnswer | null>(null)
const loadError = ref('')
const draft = reactive<BayesCardDraft>(bayesCardDraftOf({
  compile_cpu: '', compile_memory: '', compile_seconds: 0, compile_tmp: '', compile_slots: 0, cache_max: '', sampling_tmp: '',
}))
const base = ref('')
const busy = ref<'' | 'save' | 'empty' | 'refresh'>('')

// the form follows the server until the person changes it
function take(a: BayesCardAnswer, force = false) {
  card.value = a
  const next = bayesCardDraftOf(a.limits)
  if (force || JSON.stringify({ ...draft }) === base.value || !base.value) {
    Object.assign(draft, next)
    base.value = JSON.stringify(next)
  }
}

async function load(kind: 'refresh' | '' = '') {
  if (kind) busy.value = kind
  try {
    take(await api.bayesCard())
    loadError.value = ''
  } catch (e) {
    loadError.value = errMessage(e)
  } finally {
    if (kind) busy.value = ''
  }
}
onMounted(() => load())

const dirty = computed(() => JSON.stringify({ ...draft }) !== base.value)
const defaults = computed(() => card.value?.defaults)
const isDefault = (k: keyof BayesCardValues) => !defaults.value || draft[k].trim() === String(defaults.value[k])
const reset = (k: keyof BayesCardValues) => { if (defaults.value) draft[k] = String(defaults.value[k]) }
const resetAll = () => { for (const k of BAYES_CARD_FIELDS) reset(k) }

async function save() {
  if (busy.value || !card.value) return
  busy.value = 'save'
  try {
    take(await api.saveBayesCard(bayesCardInputOf(draft, card.value.defaults)), true)
    toast.success(t('modelsAdmin.bayes.saved'))
  } catch (e) {
    toast.error(errMessage(e))
  } finally {
    busy.value = ''
  }
}

async function empty() {
  if (busy.value || !card.value) return
  if (!confirm(t('modelsAdmin.bayes.confirmEmpty'))) return
  busy.value = 'empty'
  try {
    const a = await api.emptyBayesCache()
    take(a)
    const e = a.emptied
    toast.success(e && e.kept ? t('modelsAdmin.bayes.emptiedKept', { n: e.binaries, size: size(e.bytes), kept: e.kept })
      : t('modelsAdmin.bayes.emptied', { n: e?.binaries ?? 0, size: size(e?.bytes ?? 0) }))
  } catch (e) {
    toast.error(errMessage(e))
  } finally {
    busy.value = ''
  }
}

const stateIcon = (s: BayesCompileRowState) => ({ ready: CheckCircle2, failed: XCircle, timeout: TriangleAlert, queued: CircleDot, running: LoaderCircle })[s]

// the fields, in the order of the card: [key, label, hint (or null)], per fieldset
const compileFields: [keyof BayesCardValues, string, string | null][] = [
  ['compile_cpu', 'modelsAdmin.bayes.cpu', null],
  ['compile_memory', 'modelsAdmin.bayes.memory', 'modelsAdmin.bayes.memoryHint'],
  ['compile_seconds', 'modelsAdmin.bayes.seconds', 'modelsAdmin.bayes.secondsHint'],
  ['compile_tmp', 'modelsAdmin.bayes.tmp', null],
  ['compile_slots', 'modelsAdmin.bayes.slots', 'modelsAdmin.bayes.slotsHint'],
]
const numeric = (k: keyof BayesCardValues) => k === 'compile_seconds' || k === 'compile_slots' || k === 'compile_cpu'
</script>

<template>
  <section class="bayes" aria-labelledby="bayes-card-title">
    <div class="head">
      <h5 id="bayes-card-title">{{ $t('modelsAdmin.bayes.title') }}</h5>
      <button type="button" class="mini ghost" :disabled="!!busy" @click="load('refresh')">
        <LoaderCircle v-if="busy === 'refresh'" :size="13" class="spin" /><RefreshCw v-else :size="13" /> {{ $t('modelsAdmin.bayes.refresh') }}
      </button>
    </div>
    <p class="hint muted small">{{ $t('modelsAdmin.bayes.note') }}</p>
    <p v-if="loadError" class="page-error" role="alert">{{ loadError }}</p>

    <template v-else-if="card">
      <fieldset class="field-group" :disabled="!props.editable">
        <legend>{{ $t('modelsAdmin.bayes.compileTitle') }}</legend>
        <p class="hint muted small">{{ $t('modelsAdmin.bayes.compileHint') }}</p>
        <div class="cells">
          <div v-for="[key, label, hint] in compileFields" :key="key" class="cell">
            <label class="field">
              <span>{{ $t(label) }}</span>
              <input v-model="draft[key]" type="text" :inputmode="numeric(key) ? 'numeric' : 'text'" spellcheck="false" autocomplete="off" />
              <small>{{ $t('modelsAdmin.bayes.default', { value: card.defaults[key] }) }}</small>
              <small v-if="hint" class="muted">{{ $t(hint) }}</small>
            </label>
            <button v-if="props.editable && !isDefault(key)" type="button" class="reset" :aria-label="$t('modelsAdmin.bayes.resetField', { field: $t(label) })" @click="reset(key)">
              <RotateCcw :size="12" /> {{ $t('modelsAdmin.bayes.reset') }}
            </button>
          </div>
        </div>
      </fieldset>

      <fieldset class="field-group" :disabled="!props.editable">
        <legend>{{ $t('modelsAdmin.bayes.samplingTitle') }}</legend>
        <div class="cells">
          <div class="cell">
            <label class="field">
              <span>{{ $t('modelsAdmin.bayes.samplingTmp') }}</span>
              <input v-model="draft.sampling_tmp" type="text" spellcheck="false" autocomplete="off" />
              <small>{{ $t('modelsAdmin.bayes.default', { value: card.defaults.sampling_tmp }) }}</small>
              <small class="muted">{{ $t('modelsAdmin.bayes.samplingTmpHint') }}</small>
            </label>
            <button v-if="props.editable && !isDefault('sampling_tmp')" type="button" class="reset" :aria-label="$t('modelsAdmin.bayes.resetField', { field: $t('modelsAdmin.bayes.samplingTmp') })" @click="reset('sampling_tmp')">
              <RotateCcw :size="12" /> {{ $t('modelsAdmin.bayes.reset') }}
            </button>
          </div>
        </div>
      </fieldset>

      <fieldset class="field-group" :disabled="!props.editable">
        <legend>{{ $t('modelsAdmin.bayes.cacheTitle') }}</legend>
        <p class="hint muted small">{{ $t('modelsAdmin.bayes.cacheHint') }}</p>
        <div class="cells">
          <div class="cell">
            <label class="field">
              <span>{{ $t('modelsAdmin.bayes.cacheMax') }}</span>
              <input v-model="draft.cache_max" type="text" spellcheck="false" autocomplete="off" />
              <small>{{ $t('modelsAdmin.bayes.default', { value: card.defaults.cache_max }) }}</small>
            </label>
            <button v-if="props.editable && !isDefault('cache_max')" type="button" class="reset" :aria-label="$t('modelsAdmin.bayes.resetField', { field: $t('modelsAdmin.bayes.cacheMax') })" @click="reset('cache_max')">
              <RotateCcw :size="12" /> {{ $t('modelsAdmin.bayes.reset') }}
            </button>
          </div>
        </div>
        <dl class="figures">
          <div>
            <dt>{{ $t('modelsAdmin.bayes.used') }}</dt>
            <dd>{{ size(card.cache.used_bytes) }}</dd>
          </div>
          <div>
            <dt>{{ $t('modelsAdmin.bayes.binaries') }}</dt>
            <dd>{{ fmt.count(card.cache.binaries) }}</dd>
          </div>
          <div>
            <dt>{{ $t('modelsAdmin.bayes.oldest') }}</dt>
            <dd>{{ card.cache.oldest_use ? fmt.dateTime(card.cache.oldest_use) : $t('modelsAdmin.bayes.noUse') }}</dd>
          </div>
        </dl>
        <p v-if="card.cache.older_image.binaries" class="notice" role="status">
          <TriangleAlert :size="13" />
          <span>{{ $t('modelsAdmin.bayes.olderImage', { n: card.cache.older_image.binaries, size: size(card.cache.older_image.bytes) }) }}</span>
        </p>
        <div v-if="props.editable" class="actions">
          <button type="button" class="mini danger" :disabled="!!busy || !card.cache.binaries" @click="empty">
            <LoaderCircle v-if="busy === 'empty'" :size="13" class="spin" /><Trash2 v-else :size="13" /> {{ $t('modelsAdmin.bayes.empty') }}
          </button>
        </div>
      </fieldset>

      <div v-if="props.editable" class="actions">
        <button type="button" class="primary mini" :disabled="!!busy || !dirty" @click="save">
          <LoaderCircle v-if="busy === 'save'" :size="13" class="spin" /> {{ $t('modelsAdmin.save') }}
        </button>
        <button type="button" class="mini" :disabled="!!busy" @click="resetAll">
          <RotateCcw :size="13" /> {{ $t('modelsAdmin.bayes.resetAll') }}
        </button>
      </div>

      <h5 class="sub">{{ $t('modelsAdmin.bayes.compilesTitle') }}</h5>
      <p v-if="!card.compiles.length" class="hint muted small">{{ $t('modelsAdmin.bayes.noCompiles') }}</p>
      <ul v-else class="compiles">
        <li v-for="c in card.compiles" :key="c.id" :class="c.state">
          <div class="line">
            <component :is="stateIcon(c.state)" :size="14" :class="{ spin: c.state === 'running' }" />
            <strong>{{ $t(`modelsAdmin.bayes.rowState.${c.state}`) }}</strong>
            <span>
              <template v-if="c.model_name">{{ $t('modelsAdmin.bayes.modelVersion', { name: c.model_name, n: c.version ?? 0 }) }}</template>
              <template v-else>{{ $t('modelsAdmin.bayes.hiddenModel') }}</template>
            </span>
            <span v-if="c.seconds" class="muted small">{{ $t('modelsAdmin.bayes.tookSeconds', { n: fmt.number(c.seconds, 1) }) }}</span>
            <span v-if="c.binary_bytes" class="muted small">{{ size(c.binary_bytes) }}</span>
            <span v-if="c.at" class="muted small">{{ fmt.ago(c.at) }}</span>
          </div>
          <p v-if="c.state === 'timeout' && c.deadline_seconds" class="muted small">
            {{ $t('modelsAdmin.bayes.ranUnder', { seconds: c.deadline_seconds, memory: c.memory_bytes ? size(c.memory_bytes) : '—' }) }}
          </p>
          <p v-else-if="c.state === 'failed' && c.detail" class="muted small"><code>{{ c.detail }}</code></p>
        </li>
      </ul>
    </template>
  </section>
</template>

<style scoped>
.bayes { display: flex; flex-direction: column; gap: 10px; margin-top: 6px; }
.head { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.bayes h5 { margin: 0; font-size: 12.5px; color: var(--muted); font-weight: 600; }
.bayes h5.sub { margin-top: 6px; }
.hint { margin: 0; line-height: 1.5; }
.small { font-size: 12px; }
.page-error { color: var(--danger); font-size: 13px; margin: 0; }
.notice { margin: 0; display: flex; gap: 6px; align-items: flex-start; font-size: 12.5px; color: var(--warning); }
.field-group { border: 1px solid var(--border-soft); border-radius: 8px; padding: 10px 12px; margin: 0; display: flex; flex-direction: column; gap: 8px; min-width: 0; }
.field-group legend { font-size: 12px; color: var(--muted); padding: 0 4px; }
.cells { display: grid; grid-template-columns: repeat(auto-fill, minmax(190px, 1fr)); gap: 10px 14px; align-items: start; }
.cell { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.field { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); min-width: 0; }
.field > span { display: flex; align-items: flex-end; min-height: 2.7em; line-height: 1.35; }
.field small { font-size: 11.5px; line-height: 1.4; }
.reset { align-self: flex-start; display: inline-flex; align-items: center; gap: 4px; padding: 0 2px; min-height: 22px; font-size: 11.5px; border: 0; background: none; color: var(--accent); cursor: pointer; }
.reset:hover { text-decoration: underline; }
.reset:focus-visible, .actions button:focus-visible, .head button:focus-visible, .field input:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 4px; }
.figures { display: flex; flex-wrap: wrap; gap: 6px 28px; margin: 0; }
.figures div { display: flex; flex-direction: column; gap: 2px; }
.figures dt { font-size: 11.5px; color: var(--muted); }
.figures dd { margin: 0; font-size: 13px; font-variant-numeric: tabular-nums; }
.actions { display: flex; gap: 8px; flex-wrap: wrap; }
button.mini { padding: 2px 10px; min-height: 26px; font-size: 12px; display: inline-flex; align-items: center; gap: 5px; }
button.mini.ghost { background: none; }
button.mini.danger { border-color: var(--danger); color: var(--danger-text); }
button.mini.danger:hover:not(:disabled) { background: var(--danger-fill); color: var(--on-danger); }
.compiles { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
.compiles li { border: 1px solid var(--border-soft); border-radius: 8px; padding: 6px 10px; display: flex; flex-direction: column; gap: 4px; }
.compiles .line { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 10px; font-size: 13px; }
.compiles p { margin: 0; }
.compiles code { font-size: 11.5px; white-space: pre-wrap; overflow-wrap: anywhere; }
.compiles li.ready svg { color: var(--accent-2); }
.compiles li.failed svg { color: var(--danger); }
.compiles li.timeout svg { color: var(--warning); }
</style>
