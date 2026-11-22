<script setup lang="ts">
// Model node: a statistical model (linear or logistic regression, Kaplan–Meier, Cox, ARIMA) fitted
// on the rows that reach it, as a Kubernetes Job of the model's group (an administrator turns the
// groups on). Its input (data in, left top) is the table the model reads; its output (data out,
// right top) is the table of its results or its rows, as the node's `output` says; a node with
// nothing after it still runs, its report can be opened. Sequence handles below, as on the other
// step nodes. The Predict node (PredictNode.vue) is the same node's twin.
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { Handle, Position } from '@vue-flow/core'
import { modelCodeTitle, modelGroupOf, modelNameKey, modelOff, modelOf, modelSummary } from '~/composables/modelCatalog'
import { modelIconOf } from '~/composables/modelIcons'
import { useModelGroups } from '~/composables/useModelGroups'

const props = defineProps<{ id: string; data: any }>()
const { t, te } = useI18n()
const groups = useModelGroups()

const model = computed(() => modelOf(props.data?.model))
const group = computed(() => modelGroupOf(props.data?.model))
// a model unknown to this version of the interface keeps its node, greyed out
const off = computed(() => (model.value ? modelOff(model.value.group, groups.state.value) : 'unknown'))
// «unknown» is the time before the route answers: not a reason to grey the node out
const reason = computed(() => {
  switch (off.value) {
    case 'kubernetes': return t('scripting.kubernetesOnly')
    case 'not_configured': return t('models.node.notConfigured')
    case 'not_enabled': return t('models.node.groupOff', { group: group.value ? t(`models.groups.${group.value.id}`) : '' })
    case 'not_allowed': return t('models.node.groupNotAllowed')
    default: return model.value ? '' : t('models.node.unknownModel', { model: String(props.data?.model ?? '') })
  }
})
const greyed = computed(() => !!reason.value)
const icon = computed(() => modelIconOf(props.data?.model))
// an ARIMA node is named by what its orders make («SARIMA(1,1,1)(0,1,1)[12]»), a GARCH node by its variance model («GJR-GARCH(1,1), t»)
const title = computed(() => {
  if (props.data?.label) return props.data.label as string
  if (!model.value) return t('models.node.title')
  return modelCodeTitle(model.value.id, props.data?.params) ?? t(modelNameKey(model.value.id))
})
const summary = computed(() => (model.value ? modelSummary(model.value.id, props.data?.params, undefined, { unit: (u: string) => (te(`models.node.units.${u}`) ? t(`models.node.units.${u}`) : u) }) || t('models.node.setIt') : ''))
</script>

<template>
  <div class="node node-model" :class="{ off: greyed }" :title="reason || undefined">
    <Handle id="left" type="target" :position="Position.Left" class="h-left-top" />
    <Handle id="seq-in" type="target" :position="Position.Left" class="handle-seq h-left-bottom" />
    <div class="node-title">
      <component :is="icon" :size="13" class="node-icon" />
      {{ title }}
      <span v-if="data?.mlflow" class="tag">MLflow</span>
    </div>
    <div class="node-body muted">{{ summary }}</div>
    <div v-if="reason" class="node-body why">{{ reason }}</div>
    <Handle id="out" type="source" :position="Position.Right" class="h-right-top" />
    <Handle id="seq-out" type="source" :position="Position.Right" class="handle-seq h-right-bottom" />
  </div>
</template>

<style scoped>
.off { opacity: .55; }
.node-model { --node-accent: #2dd4bf; min-width: 180px; }
.node-icon { color: var(--node-accent); }
.node-body { max-width: 240px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.node-body.why { white-space: normal; font-size: 11px; color: var(--warning); }
.handle-seq { background: var(--node-accent) !important; }
.tag { margin-left: auto; font-size: 10px; padding: 0 6px; border-radius: 999px; border: 1px solid var(--border); color: var(--muted); font-weight: 500; }
</style>
