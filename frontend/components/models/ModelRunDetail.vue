<script setup lang="ts">
// What a model step's run recorded (gateway-go/internal/api/jobs_models.go): the model, its group
// and the group's version, the rows it read, the attempts and the exit code, the runner's sentence
// when it refused the data, and the end of its log; and where a flow run logged it in MLflow
// (jobs_models_mlflow.go): the experiment, the run and the registered version, with their links,
// or why it was not logged.
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { ExternalLink } from 'lucide-vue-next'
import { modelNameKey } from '~/composables/modelCatalog'
import { resolveSavedModelName, useBayesModels } from '~/composables/useBayesModels'

const props = defineProps<{ script: string }>()
const { t, te } = useI18n()

interface ModelRunInfo {
  label?: string
  model?: string
  group?: string
  version?: number
  input_rows?: number | null
  attempts?: number
  exit_code?: number | null
  refusal?: { code?: string; message?: string }
  /** a Bayesian model's step: the saved model it ran and the compile it waited for (gateway-go/internal/api/jobs_bayes.go) */
  bayes_model?: { id?: number; version?: number }
  compile?: { state?: string; detail?: string; waiting?: boolean; seconds?: number }
  check?: string
  log?: string
  log_expired?: boolean
  mlflow?: ModelRunMlflow
}

/** What the gateway kept of the MLflow run (null where nothing was made). */
interface ModelRunMlflow {
  connection?: string
  experiment?: string
  experiment_url?: string | null
  run_id?: string | null
  run_url?: string | null
  registered_model?: string
  model_version?: string
  model_url?: string
  warnings?: string[]
  error?: string
  continued?: boolean
  failed_step?: boolean
}

const info = computed<ModelRunInfo>(() => {
  try {
    return JSON.parse(props.script)
  } catch {
    return {}
  }
})
const modelName = computed(() => {
  const key = info.value.model ? modelNameKey(info.value.model) : ''
  return key && te(key) ? t(key) : (info.value.model ?? '')
})
const ml = computed(() => info.value.mlflow ?? null)
const bayes = computed(() => info.value.bayes_model ?? null)
// the run holds the saved model's id and version; the name is asked of the model, which a person who cannot see it is refused
const bayesApi = useBayesModels()
const savedName = ref<string | null>(null)
const savedHidden = ref(false)
watch(() => bayes.value?.id, async (id) => {
  savedName.value = null
  savedHidden.value = false
  if (!id) return
  const got = await resolveSavedModelName(bayesApi.get, id)
  if (bayes.value?.id !== id) return
  if ('name' in got) savedName.value = got.name
  else if ('hidden' in got) savedHidden.value = true
}, { immediate: true })
/** What the step did about the compiled model: waiting for it, how long it waited, or its refusal; nothing when the binary was
 *  there at once. A failed or timed-out compile says the gateway's own sentence. */
const compileText = computed(() => {
  const c = info.value.compile
  if (!c?.state) return ''
  if (c.waiting) return t('models.runDetail.compiling')
  if (c.state === 'failed' || c.state === 'timeout') return c.detail || t('models.runDetail.compileFailed')
  if (c.seconds) return t('models.runDetail.compiled', { s: c.seconds })
  return t('models.runDetail.compileReady')
})
const compileBad = computed(() => info.value.compile?.state === 'failed' || info.value.compile?.state === 'timeout')
/** A link only to an http(s) address (the connection's own, checked when it was saved). */
function link(url: string | null | undefined): string | undefined {
  return url && /^https?:\/\//i.test(url) ? url : undefined
}
const groupName = computed(() => {
  const key = `modelsAdmin.group.${info.value.group}.name`
  return info.value.group && te(key) ? t(key) : (info.value.group ?? '')
})
</script>

<template>
  <div class="model-run-detail">
    <dl>
      <div><dt>{{ $t('models.runDetail.model') }}</dt><dd>{{ modelName }}<template v-if="info.check === 'prior_predictive'"> · {{ $t('models.runDetail.priorCheck') }}</template></dd></div>
      <div v-if="bayes"><dt>{{ $t('models.runDetail.savedModel') }}</dt><dd>{{ savedHidden ? $t('models.runDetail.savedModelHidden') : (savedName ?? `#${bayes.id ?? ''}`) }}<template v-if="bayes.version"> · v{{ bayes.version }}</template></dd></div>
      <div v-if="compileText" class="largo"><dt>{{ $t('models.runDetail.compile') }}</dt><dd :class="{ bad: compileBad }">{{ compileText }}</dd></div>
      <div><dt>{{ $t('models.runDetail.group') }}</dt><dd>{{ groupName }}<template v-if="info.version"> · v{{ info.version }}</template></dd></div>
      <div v-if="info.input_rows != null"><dt>{{ $t('models.runDetail.inputRows') }}</dt><dd>{{ info.input_rows }}</dd></div>
      <div><dt>{{ $t('models.runDetail.attempts') }}</dt><dd>{{ info.attempts ?? 0 }}</dd></div>
      <div><dt>{{ $t('models.runDetail.exitCode') }}</dt><dd>{{ info.exit_code ?? '—' }}</dd></div>
      <div v-if="info.refusal?.message" class="largo"><dt>{{ $t('models.runDetail.refusal') }}</dt><dd>{{ info.refusal.message }}</dd></div>
    </dl>
    <section v-if="ml" class="mlflow" :aria-label="$t('models.runDetail.mlflow')">
      <h5>{{ $t('models.runDetail.mlflow') }}<template v-if="ml.connection"> · {{ ml.connection }}</template></h5>
      <dl>
        <div>
          <dt>{{ $t('models.runDetail.mlflowExperiment') }}</dt>
          <dd>
            <a v-if="link(ml.experiment_url)" :href="link(ml.experiment_url)" target="_blank" rel="noopener noreferrer"
              :title="$t('models.runDetail.mlflowOpens')">{{ ml.experiment }}<ExternalLink :size="11" aria-hidden="true" /></a>
            <template v-else>{{ ml.experiment }}</template>
          </dd>
        </div>
        <div v-if="ml.run_id">
          <dt>{{ $t('models.runDetail.mlflowRun') }}</dt>
          <dd>
            <a v-if="link(ml.run_url)" :href="link(ml.run_url)" target="_blank" rel="noopener noreferrer"
              :title="$t('models.runDetail.mlflowOpens')">{{ ml.run_id }}<ExternalLink :size="11" aria-hidden="true" /></a>
            <template v-else>{{ ml.run_id }}</template>
            <span v-if="ml.failed_step" class="muted"> · {{ $t('models.runDetail.mlflowFailedStep') }}</span>
          </dd>
        </div>
        <div v-if="ml.model_version">
          <dt>{{ $t('models.runDetail.mlflowVersion') }}</dt>
          <dd>
            <a v-if="link(ml.model_url)" :href="link(ml.model_url)" target="_blank" rel="noopener noreferrer"
              :title="$t('models.runDetail.mlflowOpens')">{{ ml.registered_model }} · v{{ ml.model_version }}<ExternalLink :size="11" aria-hidden="true" /></a>
            <template v-else>{{ ml.registered_model }} · v{{ ml.model_version }}</template>
          </dd>
        </div>
        <div v-if="ml.error" class="largo">
          <dt>{{ ml.continued ? $t('models.runDetail.mlflowContinued') : $t('models.runDetail.mlflowNotLogged') }}</dt>
          <dd>{{ ml.error }}</dd>
        </div>
      </dl>
      <ul v-if="ml.warnings?.length" class="warnings">
        <li v-for="(w, i) in ml.warnings" :key="i">{{ w }}</li>
      </ul>
    </section>
    <details v-if="info.log">
      <summary>{{ $t('models.runDetail.log') }}</summary>
      <pre class="log">{{ info.log }}</pre>
    </details>
    <p v-else-if="info.log_expired" class="muted small">{{ $t('scripting.run.logExpired') }}</p>
    <p v-else class="muted small">{{ $t('models.runDetail.noLog') }}</p>
  </div>
</template>

<style scoped>
.model-run-detail { display: flex; flex-direction: column; gap: 8px; }
dl { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 8px 16px; margin: 0; }
dl > div { min-width: 0; }
.largo { grid-column: 1 / -1; }
dt { font-size: 11px; color: var(--muted); }
dd { margin: 2px 0 0; font-size: 12.5px; overflow-wrap: anywhere; }
dd.bad { color: var(--danger); }
details summary { cursor: pointer; font-size: 12px; color: var(--muted); }
.log { margin: 6px 0 0; max-height: 320px; overflow: auto; font-size: 11px; padding: 6px; border-radius: 6px; background: var(--bg-soft); white-space: pre-wrap; overflow-wrap: anywhere; }
.small { margin: 0; font-size: 12px; }
.mlflow { display: flex; flex-direction: column; gap: 6px; padding-top: 6px; border-top: 1px solid var(--border); }
.mlflow h5 { margin: 0; font-size: 12px; font-weight: 600; }
.mlflow a { display: inline-flex; align-items: center; gap: 3px; color: var(--accent); text-decoration: none; }
.mlflow a:hover, .mlflow a:focus-visible { text-decoration: underline; }
.warnings { margin: 0; padding-left: 16px; font-size: 12px; color: var(--muted); }
</style>
