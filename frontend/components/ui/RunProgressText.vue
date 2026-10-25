<script setup lang="ts">
// An import's progress («1,250,000 rows received · 4m 05s») with its clock: time moves
// here, one second at a time, and only while there is progress — the page around is not
// redrawn at every tick.
// The ticking text does not reach the screen readers (they would announce it again every
// second): `srLabel` stays, stable; the page announces start and end.
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import type { RunInfo } from '~/composables/useRuns'
import { progressText } from '~/composables/useRunProgress'

const props = defineProps<{ run: RunInfo | undefined; srLabel: string }>()
const { t, locale } = useI18n()

const now = ref(0)
const arrivedAt = ref(0)
let timer: ReturnType<typeof setInterval> | undefined

function sync() {
  const p = props.run?.progress
  arrivedAt.value = now.value = Date.now() // every poll brings a new object: it starts again from there
  if (p && !timer) timer = setInterval(() => { now.value = Date.now() }, 1000)
  if (!p && timer) {
    clearInterval(timer)
    timer = undefined
  }
}
onMounted(() => {
  sync()
  watch(() => props.run?.progress, sync)
})
onUnmounted(() => clearInterval(timer))

const text = computed(() =>
  progressText(t, locale.value, props.run?.progress, Math.floor((now.value - arrivedAt.value) / 1000)),
)
</script>

<template>
  <span class="run-progress">
    <template v-if="text"><span aria-hidden="true">{{ text }}</span><span class="sr-only">{{ srLabel }}</span></template>
    <template v-else>{{ srLabel }}</template>
  </span>
</template>

<style scoped>
.run-progress { font-variant-numeric: tabular-nums; }
</style>
