<script setup lang="ts">
// An expression of an item, with its label: the value is `obj[prop]`, the problems of the
// last save are those of `path` in the item (or the dataset, the parameter), and they go
// away once the text is changed; so do those of the live check, which follow the text.
import { computed } from 'vue'
import RdExprInput from '~/components/report/designer/RdExprInput.vue'
import { useDesign } from '~/composables/reportDesignStore'
import type { DField } from '~/composables/reportDesignModel'

const props = defineProps<{
  obj: Record<string, any>
  prop: string
  label: string
  path: string
  item: string | null
  fields?: DField[] | null
  hint?: string
  placeholder?: string
  textMode?: boolean
  /** an empty field is null (an optional expression) instead of "" */
  optional?: boolean
}>()
const store = useDesign()
const uid = `rxf-${Math.random().toString(36).slice(2, 9)}`

const problem = computed(() => store.problemAt(props.item, props.path))
const live = computed(() => store.liveAt(props.item, props.path))
const value = computed({
  get: () => (props.obj[props.prop] ?? '') as string,
  set: (v: string) => { props.obj[props.prop] = props.optional && !v.trim() ? null : v },
})
</script>

<template>
  <div class="rxf" :data-path="path">
    <label :for="uid" class="rxf-label">{{ label }}</label>
    <RdExprInput
      :id="uid"
      v-model="value"
      :fields="fields ?? null"
      :params="store.params.value"
      :problem="problem"
      :live="live"
      :text-mode="textMode"
      :placeholder="placeholder"
      :label="label"
      @edited="store.clearProblem(item, path)"
    />
    <p v-if="hint" class="rxf-hint">{{ hint }}</p>
  </div>
</template>

<style scoped>
.rxf { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.rxf-label { font-size: 12px; color: var(--muted); font-weight: 500; }
.rxf-hint { margin: 0; font-size: 11.5px; color: var(--muted); line-height: 1.4; }
</style>
