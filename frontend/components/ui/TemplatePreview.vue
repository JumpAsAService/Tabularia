<script setup lang="ts">
// One line under a templated field: what it gives NOW («now: sales_20261018.csv»),
// the first and last of several expansions («7 values: … … …»), or what is wrong,
// in the danger colour. With `global` it shows instead what is wrong with the form
// as a whole (the time zone, the variables): one line, not one per field.
// Nothing for a template without braces: there is nothing to resolve.
import { computed } from 'vue'
import { isTemplated, type PlaceholderPreview } from '~/composables/usePlaceholders'

const props = defineProps<{
  preview: PlaceholderPreview
  name?: string // the template's name in the request
  global?: boolean
  id?: string // for aria-describedby on the field
  label?: string // which value, when one field holds several (the query parameters)
}>()

const template = computed(() => (props.name ? props.preview.templates[props.name] ?? '' : ''))
const field = computed(() => (props.name ? props.preview.answer?.fields[props.name] ?? null : null))
const many = computed(() => props.preview.kind === 'input' && (props.preview.answer?.expansions ?? 1) > 1)
const shown = computed(() =>
  props.global ? !!props.preview.error : isTemplated(template.value) && !props.preview.error && !!field.value,
)
</script>

<template>
  <p
    v-if="shown"
    :id="id"
    class="tp-line"
    :class="global || field?.error ? 'tp-err' : 'tp-ok'"
    :data-preview="global ? 'global' : name"
  >
    <span v-if="label && !global" class="tp-label">{{ label }} · </span>
    <template v-if="global">{{ preview.error }}</template>
    <template v-else-if="field?.error">{{ field.error }}</template>
    <template v-else-if="many && field?.last">{{ $t('placeholders.previewMany', { n: preview.answer?.expansions ?? 0, first: field.value ?? '', last: field.last }) }}</template>
    <template v-else>{{ $t('placeholders.previewNow', { value: field?.value ?? '' }) }}</template>
  </p>
</template>

<style scoped>
.tp-line { margin: 0; font-size: 11.5px; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; overflow-wrap: anywhere; }
.tp-ok { color: var(--muted); }
.tp-err { color: var(--danger); font-family: inherit; }
.tp-label { color: var(--text); }
</style>
