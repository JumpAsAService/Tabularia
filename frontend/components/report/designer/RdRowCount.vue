<script setup lang="ts">
// The rows of a dataset as «Refresh fields» counted them (composables/reportRowCount.ts): a
// quiet line with the number; past SLOW_ROWS a warning with the time a render takes; past
// the cap, that the report will not render. Nothing when they were not counted.
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { Rows3, TriangleAlert } from 'lucide-vue-next'
import { rowCountNote, type RowCount } from '~/composables/reportRowCount'

const props = defineProps<{ count: RowCount | null }>()
const { t, locale } = useI18n()
const note = computed(() => rowCountNote(props.count))
const number = (n: number) => new Intl.NumberFormat(locale.value).format(n)
const text = computed(() => {
  const n = note.value
  if (!n) return ''
  if (n.level === 'over') return t('reportDesigner.rows.over', { n: number(n.rows) })
  if (n.level === 'slow') return t('reportDesigner.rows.slow', { n: number(n.rows), s: n.seconds })
  return t('reportDesigner.rows.plain', { n: number(n.rows) }, n.rows)
})
</script>

<template>
  <p v-if="note" class="rdc" :class="note.level" :title="$t('reportDesigner.rows.title')" :data-rows="note.level">
    <TriangleAlert v-if="note.level !== 'plain'" :size="12" aria-hidden="true" /><Rows3 v-else :size="12" aria-hidden="true" />
    <span>{{ text }}</span>
  </p>
</template>

<style scoped>
.rdc { display: flex; align-items: flex-start; gap: 5px; margin: 0; font-size: 11.5px; line-height: 1.4; color: var(--muted); overflow-wrap: anywhere; }
.rdc svg { flex: none; margin-top: 2px; }
.rdc.slow, .rdc.over { padding: 5px 7px; border-radius: 6px; }
.rdc.slow {
  color: var(--warning);
  background: color-mix(in srgb, var(--warning) 8%, transparent); border: 1px solid color-mix(in srgb, var(--warning) 35%, transparent);
}
.rdc.over {
  color: var(--danger);
  background: color-mix(in srgb, var(--danger) 8%, transparent); border: 1px solid color-mix(in srgb, var(--danger) 40%, transparent);
}
</style>
