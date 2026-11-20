<script setup lang="ts">
// What a model step's run recorded (gateway-go/internal/api/jobs_models.go): the model, its group
// and the group's version, the rows it read, the attempts and the exit code, the runner's sentence
// when it refused the data, and the end of its log.
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { modelNameKey } from '~/composables/modelCatalog'

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
  log?: string
  log_expired?: boolean
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
const groupName = computed(() => {
  const key = `modelsAdmin.group.${info.value.group}.name`
  return info.value.group && te(key) ? t(key) : (info.value.group ?? '')
})
</script>

<template>
  <div class="model-run-detail">
    <dl>
      <div><dt>{{ $t('models.runDetail.model') }}</dt><dd>{{ modelName }}</dd></div>
      <div><dt>{{ $t('models.runDetail.group') }}</dt><dd>{{ groupName }}<template v-if="info.version"> · v{{ info.version }}</template></dd></div>
      <div v-if="info.input_rows != null"><dt>{{ $t('models.runDetail.inputRows') }}</dt><dd>{{ info.input_rows }}</dd></div>
      <div><dt>{{ $t('models.runDetail.attempts') }}</dt><dd>{{ info.attempts ?? 0 }}</dd></div>
      <div><dt>{{ $t('models.runDetail.exitCode') }}</dt><dd>{{ info.exit_code ?? '—' }}</dd></div>
      <div v-if="info.refusal?.message" class="largo"><dt>{{ $t('models.runDetail.refusal') }}</dt><dd>{{ info.refusal.message }}</dd></div>
    </dl>
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
details summary { cursor: pointer; font-size: 12px; color: var(--muted); }
.log { margin: 6px 0 0; max-height: 320px; overflow: auto; font-size: 11px; padding: 6px; border-radius: 6px; background: var(--bg-soft); white-space: pre-wrap; overflow-wrap: anywhere; }
.small { margin: 0; font-size: 12px; }
</style>
