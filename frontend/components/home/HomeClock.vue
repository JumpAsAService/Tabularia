<script setup lang="ts">
// The clock widget: the time and the date, in the browser's zone or the one chosen. It ticks in
// the browser, once a second when seconds show and once a minute otherwise; a screen reader is
// not told at every tick (the time is not a live region), it reads it when it gets there.
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { clockFace } from '~/composables/homeLayout'

const props = defineProps<{ options: Record<string, any> }>()
const { locale } = useI18n()
const now = ref<Date | null>(null) // null on the server and until mounted: no time to disagree on
let timer: ReturnType<typeof setTimeout> | undefined
function tick() {
  now.value = new Date()
  const step = props.options.seconds ? 1000 : 60_000
  timer = setTimeout(tick, step - (Date.now() % step) + 20)
}
onMounted(tick)
onBeforeUnmount(() => clearTimeout(timer))
const face = computed(() => (now.value ? clockFace(now.value, props.options, locale.value) : null))
</script>

<template>
  <div class="clock">
    <template v-if="face">
      <time class="time" :datetime="now!.toISOString()">{{ face.time }}</time>
      <span class="date">{{ face.date }}</span>
      <span class="zone">{{ face.zone }}</span>
    </template>
  </div>
</template>

<style scoped>
.clock { display: flex; flex-direction: column; justify-content: center; gap: 2px; height: 100%; min-height: 0; container-type: size; }
.time { font-size: clamp(22px, 34cqh, 56px); font-weight: 650; line-height: 1.05; font-variant-numeric: tabular-nums; letter-spacing: -0.01em; }
.date { font-size: 13px; color: var(--text); }
.date::first-letter { text-transform: uppercase; }
.zone { font-size: 12px; color: var(--muted); }
</style>
