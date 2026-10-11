<script setup lang="ts">
// L'avanzamento di un import («1.250.000 righe ricevute · 4m 05s») col suo
// orologio: il tempo avanza qui, un secondo alla volta, e solo mentre c'è un
// avanzamento — la pagina intorno non si ridisegna a ogni scatto.
// Agli screen reader il testo che ticchetta non arriva (lo riannuncerebbero a
// ogni secondo): resta `srLabel`, stabile; inizio e fine li annuncia la pagina.
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
  arrivedAt.value = now.value = Date.now() // ogni poll porta un oggetto nuovo: si riparte da lì
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
