<script setup lang="ts">
// The «MLflow» section of a Bayesian model node's panel (docs/specs/bayes-mlflow.md, lot M3): the connection, the experiment's
// name (the server's experiments offered), the registered model's name and «Continue without MLflow», in the Model node's words
// (`models.mlflow.*`). It writes the same `data.mlflow` object, through the pure rules of composables/bayesMlflow.ts. It reads
// nothing of ModelForm.vue, which every other group uses. The gateway's refusals (no experiment, no CONNECT) are said by the
// run's own line under «Run».
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { TriangleAlert } from 'lucide-vue-next'
import { bayesMlflowConnection, bayesMlflowProblem, bayesMlflowSet } from '~/composables/bayesMlflow'
import { vDraft } from '~/composables/draftField'
import { useModelGroups } from '~/composables/useModelGroups'
import Select from '~/components/ui/Select.vue'

const props = defineProps<{
  nodeId: string
  mlflow?: Record<string, any> | null
  connections?: { id: number; name: string; db_type: string }[]
  readOnly?: boolean
}>()
const emit = defineEmits<{ (e: 'update', patch: Record<string, any>): void }>()
const { t } = useI18n()
const groups = useModelGroups()

const idOf = (part: string) => `bn-mlflow-${props.nodeId}-${part}`
const block = computed(() => props.mlflow ?? null)
const servers = computed(() => (props.connections ?? []).filter((c) => c.db_type === 'mlflow'))
const options = computed(() => {
  const list: { value: number | null; label: string }[] = [
    { value: null, label: t('models.mlflow.none') },
    ...servers.value.map((c) => ({ value: c.id, label: c.name })),
  ]
  const id = block.value?.connectionId
  // a connection the person cannot use any more stays listed: the node still holds it
  if (typeof id === 'number' && !servers.value.some((c) => c.id === id)) list.push({ value: id, label: t('models.mlflow.unknownConnection', { id }) })
  return list
})
const problem = computed(() => bayesMlflowProblem(block.value))

// the experiments of the server, offered while the name is typed; a server that does not answer offers none (the name is still free)
const experiments = ref<string[]>([])
let asked = 0
watch(() => block.value?.connectionId, async (id) => {
  const seq = ++asked
  experiments.value = []
  if (typeof id !== 'number') return
  try {
    const list = await groups.experiments(id)
    if (seq === asked) experiments.value = list.map((e) => e.name).filter((n) => typeof n === 'string' && n)
  } catch { /* the name stays free to type */ }
}, { immediate: true })

const pickConnection = (id: unknown) => emit('update', { mlflow: bayesMlflowConnection(block.value, id) })
const set = (key: 'experiment' | 'registerAs' | 'continueWithout', value: unknown) => emit('update', { mlflow: bayesMlflowSet(block.value, key, value) })
const text = (e: Event) => (e.target as HTMLInputElement).value
</script>

<template>
  <fieldset class="mlflow block" :disabled="readOnly" :aria-labelledby="idOf('title')">
    <h4 :id="idOf('title')">{{ $t('models.mlflow.title') }}</h4>
    <div class="field">
      <span :id="idOf('connection-label')" class="lbl">{{ $t('models.mlflow.connection') }}</span>
      <Select
        :model-value="block?.connectionId ?? null"
        :options="options"
        :disabled="readOnly"
        :aria-labelledby="idOf('connection-label')"
        @update:model-value="pickConnection"
      />
      <p v-if="!servers.length" class="hint">{{ $t('models.mlflow.noConnection') }}</p>
      <p v-else-if="!block" class="hint">{{ $t('models.mlflow.hint') }}</p>
    </div>
    <template v-if="block">
      <div class="field">
        <label :for="idOf('experiment')" class="lbl">{{ $t('models.mlflow.experiment') }}<span class="req" aria-hidden="true"> *</span></label>
        <input
          :id="idOf('experiment')"
          type="text"
          autocomplete="off"
          :list="experiments.length ? idOf('experiments') : undefined"
          :value="block.experiment ?? ''"
          v-draft="block.experiment ?? ''"
          :placeholder="$t('models.mlflow.experimentPlaceholder')"
          :aria-invalid="problem ? 'true' : undefined"
          :aria-describedby="problem ? idOf('problem') : undefined"
          @change="set('experiment', text($event))"
        />
        <datalist v-if="experiments.length" :id="idOf('experiments')">
          <option v-for="name in experiments" :key="name" :value="name" />
        </datalist>
        <p v-if="problem" :id="idOf('problem')" class="note warn" role="alert"><TriangleAlert :size="13" aria-hidden="true" /> {{ $t('models.mlflow.experimentNeeded') }}</p>
      </div>
      <div class="field">
        <label :for="idOf('register')" class="lbl">{{ $t('models.mlflow.registerAs') }}</label>
        <input
          :id="idOf('register')"
          type="text"
          autocomplete="off"
          :value="block.registerAs ?? ''"
          v-draft="block.registerAs ?? ''"
          :placeholder="$t('models.mlflow.registerPlaceholder')"
          :aria-describedby="idOf('register-hint')"
          @change="set('registerAs', text($event))"
        />
        <p :id="idOf('register-hint')" class="hint">{{ $t('bayesNode.mlflowRegisterHint') }}</p>
      </div>
      <div class="field">
        <label class="chk">
          <input type="checkbox" role="switch" :checked="block.continueWithout === true" @change="set('continueWithout', ($event.target as HTMLInputElement).checked)" />
          {{ $t('models.mlflow.continueWithout') }}
        </label>
        <p class="hint">{{ $t('models.mlflow.continueHint') }}</p>
      </div>
      <p class="hint">{{ $t('bayesNode.mlflowNote') }}</p>
    </template>
  </fieldset>
</template>

<style scoped>
.block { display: flex; flex-direction: column; gap: 8px; padding: 10px 0 0; border: 0; border-top: 1px solid var(--border-soft); margin: 0; min-width: 0; }
.block h4 { margin: 0; padding: 0; font-size: 12px; font-weight: 600; color: var(--text); }
.field { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.lbl { font-size: 12px; color: var(--muted); padding: 0; }
.req { color: var(--danger); }
.hint { margin: 0; font-size: 11.5px; line-height: 1.45; color: var(--muted); }
.note { margin: 0; display: flex; align-items: flex-start; gap: 6px; font-size: 12px; line-height: 1.4; color: var(--muted); }
.note svg { flex: none; margin-top: 2px; }
.note.warn { color: var(--warning); }
.chk { display: flex; align-items: flex-start; gap: 7px; font-size: 13px; color: var(--text); }
.chk input { width: auto; flex: none; margin-top: 3px; }
input[type='text'] { min-width: 0; width: 100%; }
</style>
