<script setup lang="ts">
// A data API's runtime as an administrator sets it (docs/specs/data-apis.md, «Runtime»): a fixed
// number of replicas or autoscaling on CPU between two, the CPU and memory of each pod, within the
// bounds the gateway checks. Used by the API's page and by Administration → Data APIs; the
// caller saves it (PUT /admin/data-apis/{id}/runtime).
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { runtimeProblem, type DataApiRuntime, type DataApiRuntimeLimits } from '~/composables/useDataApis'

const props = defineProps<{ limits: DataApiRuntimeLimits | null | undefined; disabled?: boolean }>()
const model = defineModel<DataApiRuntime>({ required: true })
const { t } = useI18n()

const problem = computed(() => {
  if (!props.limits) return ''
  const p = runtimeProblem(model.value, props.limits)
  return p ? t(`dataApi.${p.key}`, p.values ?? {}) : ''
})
defineExpose({ problem })
</script>

<template>
  <div class="rtf">
    <label class="check">
      <input v-model="model.autoscale" type="checkbox" :disabled="disabled" data-rt="autoscale" /> {{ $t('dataApi.autoscale') }}
    </label>
    <div class="rtf-row">
      <label v-if="!model.autoscale" class="field">
        <span>{{ $t('dataApi.replicas') }}</span>
        <input v-model.number="model.replicas" type="number" min="1" :max="limits?.max_replicas" :disabled="disabled" data-rt="replicas" />
      </label>
      <template v-else>
        <label class="field">
          <span>{{ $t('dataApi.minReplicas') }}</span>
          <input v-model.number="model.min_replicas" type="number" min="1" :max="limits?.max_replicas" :disabled="disabled" data-rt="min" />
        </label>
        <label class="field">
          <span>{{ $t('dataApi.maxReplicas') }}</span>
          <input v-model.number="model.max_replicas" type="number" min="1" :max="limits?.max_replicas" :disabled="disabled" data-rt="max" />
        </label>
      </template>
      <label class="field">
        <span>{{ $t('dataApi.cpu') }}</span>
        <input v-model="model.cpu" type="text" class="mono" spellcheck="false" autocomplete="off" :disabled="disabled" data-rt="cpu" />
      </label>
      <label class="field">
        <span>{{ $t('dataApi.memory') }}</span>
        <input v-model="model.memory" type="text" class="mono" spellcheck="false" autocomplete="off" :disabled="disabled" data-rt="memory" />
      </label>
    </div>
    <p v-if="limits" class="muted small">
      {{ $t('dataApi.runtimeBounds', { minCpu: limits.min_cpu, maxCpu: limits.max_cpu, minMemory: limits.min_memory,
                                       maxMemory: limits.max_memory, replicas: limits.max_replicas }) }}
    </p>
    <p v-if="problem" class="err small" role="alert" data-rt="problem">{{ problem }}</p>
  </div>
</template>

<style scoped>
.rtf { display: flex; flex-direction: column; gap: 10px; min-width: 0; }
.rtf-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 10px; }
.field { display: flex; flex-direction: column; gap: 4px; min-width: 0; font-size: 12.5px; }
.field span { color: var(--muted); }
.field input { width: 100%; min-width: 0; box-sizing: border-box; }
.check { display: inline-flex; align-items: center; gap: 6px; font-size: 13px; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.small { font-size: 12px; margin: 0; line-height: 1.45; }
.muted { color: var(--muted); }
.err { color: var(--danger); }
</style>
