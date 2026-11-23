<script setup lang="ts">
// One slot of a node in the inspector (docs/specs/bayes-graph-editor.md, «The inspector»): its words
// above the field (a symbol and what it is, the constraint at the right), the expression with the
// mirror's first error underlined and said under it, a warning when it has no error, the type in
// words when it checks, the «hyperprior» tag, and the names that fit at the caret.
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import BayesExprField from '~/components/bayes/BayesExprField.vue'
import type { BayesNameChoice, BayesSlotView } from '~/composables/bayesInspector'

const props = defineProps<{
  id: string
  view: BayesSlotView
  /** The symbol before the words («μ»), or ''. */
  symbol?: string
  words: string
  constraint?: string
  names: readonly BayesNameChoice[]
  say: (f: NonNullable<BayesSlotView['error']>) => string
  typeWords: (plate: string) => string
  nameHint: (c: BayesNameChoice) => string
  placeholder?: string
}>()
const emit = defineEmits<{ (e: 'input', text: string): void, (e: 'escape'): void }>()
const { t } = useI18n()

const error = computed(() => (props.view.error ? { pos: props.view.pos, len: props.view.len, message: props.say(props.view.error) } : null))
const warning = computed(() => (props.view.warning ? props.say(props.view.warning) : ''))
const okText = computed(() => (props.view.typed && !props.view.error ? props.typeWords(props.view.typed.type.plate) : ''))
</script>

<template>
  <div class="fl">
    <label :for="id" class="argl">
      <span v-if="symbol" class="sym">{{ symbol }}</span><span class="w">{{ words }}</span>
      <span v-if="constraint" class="c">{{ constraint }}</span>
    </label>
    <BayesExprField
      :id="id"
      :model-value="view.place.text"
      :error="error"
      :warning="warning"
      :ok-text="okText"
      :tag="view.hyper ? t('bayes.editor.node.hyperprior') : ''"
      :names="names"
      :name-hint="nameHint"
      :placeholder="placeholder"
      @update:model-value="(v: string) => emit('input', v)"
      @escape="emit('escape')"
    />
  </div>
</template>

<style scoped>
.fl { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.argl { display: flex; align-items: baseline; gap: 6px; min-width: 0; font-size: 12px; font-weight: 500; color: var(--muted); }
.argl .sym { flex: none; color: var(--text); font-weight: 600; }
.argl .w { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.argl .c { flex: none; margin-left: auto; font-weight: 400; font-size: 11.5px; }
</style>
