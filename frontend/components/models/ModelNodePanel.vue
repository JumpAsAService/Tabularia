<script setup lang="ts">
// The node panel of a Model node and of a Predict node: the title (an ARIMA node says what its orders
// make), the delete button, and the form drawn from the catalogue (ui/ModelForm.vue, ui/PredictForm.vue).
// What the node reads and writes comes from its edges, as on a Script node. The forms' slot `run`
// holds the trial's button and its outcome (ModelRun.vue: Run on the sample, the latest output,
// «not up to date»); the editor runs it (the panel's `run`, as a Script node's).
import { computed, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { Sparkles, Trash2 } from 'lucide-vue-next'
import type { Node } from '@vue-flow/core'
import type { ColumnInfo } from '~/composables/useApi'
import { modelArimaTitle, modelGroupLicence, modelGroupSizes, modelNameKey, modelOff, modelOf, modelUnset } from '~/composables/modelCatalog'
import { modelIconOf } from '~/composables/modelIcons'
import { useModelGroups } from '~/composables/useModelGroups'
import type { ScriptOutputs } from '~/composables/useScripting'
import ModelForm from '~/components/ui/ModelForm.vue'
import PredictForm from '~/components/ui/PredictForm.vue'
import ModelRun from '~/components/models/ModelRun.vue'

const props = defineProps<{
  node: Node
  inputColumns: ColumnInfo[]
  columnsLoading?: boolean
  fetchDistinct?: (column: string) => Promise<any[]>
  connections?: { id: number; name: string; db_type: string; project_id?: number }[]
  readOnly?: boolean
  // the node's latest trial or run and the trial in progress (the editor's, as a Script node's)
  outputs?: ScriptOutputs[string] | null
  trial?: { runId: number | null; status: string; startedAt: number; error: string } | null
  flowDirty?: boolean
}>()
const emit = defineEmits<{
  (e: 'update', patch: Record<string, any>): void
  (e: 'delete'): void
  (e: 'run'): void
}>()
const { t } = useI18n()
const groups = useModelGroups()
onMounted(() => { groups.load() })

const isPredict = computed(() => props.node.type === 'model_predict')
const model = computed(() => modelOf(props.node.data?.model))
const off = computed(() => (model.value ? modelOff(model.value.group, groups.state.value) : ''))
const maxTimeout = computed(() => groups.state.value?.groups.find((g) => g.id === model.value?.group)?.max_timeout_seconds ?? null)
// the sizes of the weights the group's ready version holds, and their licence: the form says them under the size
const sizes = computed(() => (model.value ? modelGroupSizes(model.value.group, groups.state.value) : null))
const licence = computed(() => (model.value ? modelGroupLicence(model.value.group, groups.state.value) : null))
const title = computed(() => {
  if (isPredict.value) return t('models.predict.title')
  if (!model.value) return t('models.node.title')
  return model.value.id === 'arima' ? modelArimaTitle(props.node.data?.params) : t(modelNameKey(model.value.id))
})
// Run waits for the group (the form says why it is off) and for what the node still lacks
const lacking = computed(() => (model.value ? modelUnset(model.value, props.node.data?.params) : []))
// a Predict node runs once it names its connection and its version; the gateway checks the rest
const predictReady = computed(() => typeof props.node.data?.connectionId === 'number' && !!props.node.data?.registeredModel && !!props.node.data?.version)
const icon = computed(() => (isPredict.value ? Sparkles : modelIconOf(props.node.data?.model)))
</script>

<template>
  <div class="model-panel">
    <div class="row">
      <h3><component :is="icon" :size="15" aria-hidden="true" /> {{ title }}</h3>
      <button v-if="!readOnly" class="del" :title="$t('nodePanel.deleteNodeTitle')" :aria-label="$t('nodePanel.deleteNodeTitle')" @click="emit('delete')"><Trash2 :size="14" /></button>
    </div>
    <PredictForm
      v-if="isPredict"
      :node-id="node.id"
      :data="node.data ?? {}"
      :input-columns="inputColumns"
      :connections="connections"
      :read-only="readOnly"
      @update="emit('update', $event)"
    >
      <template #run>
        <ModelRun
          :outputs="outputs"
          :trial="trial"
          :flow-dirty="flowDirty"
          :disabled="!predictReady"
          :why="predictReady ? '' : $t('models.predict.chooseModel')"
          :read-only="readOnly"
          @run="emit('run')"
        />
      </template>
    </PredictForm>
    <p v-else-if="!model" class="muted unknown">{{ $t('models.node.unknownModel', { model: String(node.data?.model ?? '') }) }}</p>
    <ModelForm
      v-else
      :node-id="node.id"
      :data="node.data ?? {}"
      :input-columns="inputColumns"
      :columns-loading="columnsLoading"
      :fetch-distinct="fetchDistinct"
      :connections="connections"
      :off="off"
      :max-timeout="maxTimeout"
      :sizes="sizes"
      :licence="licence"
      :read-only="readOnly"
      @update="emit('update', $event)"
    >
      <template #run>
        <ModelRun
          :outputs="outputs"
          :trial="trial"
          :flow-dirty="flowDirty"
          :disabled="!!off || lacking.length > 0"
          :why="!off && lacking.length ? $t('models.node.setIt') : ''"
          :read-only="readOnly"
          @run="emit('run')"
        />
      </template>
    </ModelForm>
  </div>
</template>

<style scoped>
.model-panel { display: flex; flex-direction: column; gap: 10px; }
.row { display: flex; align-items: center; gap: 8px; }
.row h3 { flex: 1; margin: 0; display: inline-flex; align-items: center; gap: 7px; font-size: 14px; }
.del { border: 0; background: none; color: var(--muted); cursor: pointer; padding: 4px; border-radius: 6px; }
.del:hover { color: var(--danger); background: var(--bg-soft); }
.unknown { margin: 0; font-size: 12px; }
</style>
