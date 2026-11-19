<script setup lang="ts">
// Predict node: applies a model registered in MLflow to the rows that reach it and adds the
// predicted column. Run as a Kubernetes Job of the group the model belongs to, with the numbers of
// the model's card only: nothing the server holds is loaded as code. Handles as the Model node's.
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { Handle, Position } from '@vue-flow/core'
import { Sparkles } from 'lucide-vue-next'
import { modelShownGroups } from '~/composables/modelCatalog'
import { useModelGroups } from '~/composables/useModelGroups'

const props = defineProps<{ id: string; data: any }>()
const { t } = useI18n()
const groups = useModelGroups()

// Predict runs in the group of the model it applies: with no group to use, nothing runs
const reason = computed(() => {
  const state = groups.state.value
  if (!state) return ''
  if (state.disabled_reason === 'kubernetes') return t('scripting.kubernetesOnly')
  if (state.disabled_reason) return t('models.node.notConfigured')
  return modelShownGroups(state).length ? '' : t('models.predict.noGroup')
})
const summary = computed(() => {
  const model = String(props.data?.registeredModel ?? '').trim()
  if (!model) return t('models.predict.chooseModel')
  const version = String(props.data?.version ?? '')
  return version === 'latest' ? t('models.predict.latest', { model }) : t('models.predict.version', { model, version })
})
</script>

<template>
  <div class="node node-predict" :class="{ off: !!reason }" :title="reason || undefined">
    <Handle id="left" type="target" :position="Position.Left" class="h-left-top" />
    <Handle id="seq-in" type="target" :position="Position.Left" class="handle-seq h-left-bottom" />
    <div class="node-title">
      <Sparkles :size="13" class="node-icon" />
      {{ data?.label || t('models.predict.title') }}
    </div>
    <div class="node-body muted">{{ summary }}</div>
    <div v-if="reason" class="node-body why">{{ reason }}</div>
    <Handle id="out" type="source" :position="Position.Right" class="h-right-top" />
    <Handle id="seq-out" type="source" :position="Position.Right" class="handle-seq h-right-bottom" />
  </div>
</template>

<style scoped>
.off { opacity: .55; }
.node-predict { --node-accent: #2dd4bf; min-width: 180px; }
.node-icon { color: var(--node-accent); }
.node-body { max-width: 240px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.node-body.why { white-space: normal; font-size: 11px; color: var(--warning); }
.handle-seq { background: var(--node-accent) !important; }
</style>
