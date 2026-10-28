<script setup lang="ts">
// A field's kind as a small icon: number, text, date, true/false, calculated by the report.
import { computed } from 'vue'
import { Calendar, CaseSensitive, Hash, SquareFunction, ToggleLeft, Variable, Braces } from 'lucide-vue-next'
import { fieldKind } from '~/composables/reportDesignModel'

const props = defineProps<{ type: string; size?: number }>()
const icon = computed(() => {
  if (props.type === 'calculated' || props.type === 'fn') return SquareFunction
  if (props.type === 'param') return Variable
  if (props.type === 'kw') return Braces
  return { number: Hash, text: CaseSensitive, date: Calendar, boolean: ToggleLeft, other: Braces }[fieldKind(props.type)]
})
</script>

<template>
  <component :is="icon" :size="size ?? 13" aria-hidden="true" />
</template>
