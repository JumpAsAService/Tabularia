<script setup lang="ts">
// The «Queue» widget, for administrators: how many jobs are running and waiting and how many
// workers answer (the Queue page's own overview), read every ten seconds while the page shows.
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useQueue, type QueueOverview } from '~/composables/useQueue'

defineProps<{ options: Record<string, any> }>()
const api = useQueue()
const q = ref<QueueOverview | null>(null)
const failed = ref(false)
async function read() {
  try { q.value = await api.overview(); failed.value = false } catch { failed.value = true }
}
let timer: ReturnType<typeof setInterval> | undefined
const whenShown = () => { if (!document.hidden) read() }
onMounted(() => { read(); timer = setInterval(whenShown, 10_000); document.addEventListener('visibilitychange', whenShown) })
onBeforeUnmount(() => { clearInterval(timer); document.removeEventListener('visibilitychange', whenShown) })
const slots = computed(() => (q.value?.workers ?? []).reduce((n, w) => n + (w.concurrency ?? 0), 0))
</script>

<template>
  <div class="qu">
    <p v-if="failed && !q" class="msg" role="alert">{{ $t('home.queue.failed') }} <button type="button" class="retry" @click="read">{{ $t('home.retry') }}</button></p>
    <p v-else-if="!q" class="msg"><span class="sk" style="width: 50%" /></p>
    <template v-else>
      <dl class="nums">
        <div><dd>{{ q.running_count }}</dd><dt>{{ $t('home.queue.running') }}</dt></div>
        <div :class="{ hot: q.waiting > 0 }"><dd>{{ q.waiting }}</dd><dt>{{ $t('home.queue.waiting') }}</dt></div>
        <div :class="{ ko: !q.workers.length }"><dd>{{ q.workers.length }}</dd><dt>{{ $t('home.queue.workers') }}</dt></div>
      </dl>
      <div v-if="slots" class="load" role="img" :aria-label="$t('home.queue.load', { n: q.running_count, of: slots })">
        <span :style="{ width: `${Math.min(100, (q.running_count / slots) * 100)}%` }" />
      </div>
      <p v-if="slots" class="cap">{{ $t('home.queue.load', { n: q.running_count, of: slots }) }}</p>
      <NuxtLink to="/queue" class="more">{{ $t('home.queue.open') }}</NuxtLink>
    </template>
  </div>
</template>

<style scoped>
.qu { display: flex; flex-direction: column; gap: 8px; height: 100%; overflow: auto; }
.msg { margin: 2px 0; color: var(--muted); font-size: 13px; }
.msg .sk { display: inline-block; height: 12px; }
.retry { min-height: 0; padding: 0 2px; border: none; background: none; color: var(--accent); font-size: 13px; text-decoration: underline; }
.nums { display: flex; gap: 22px; margin: 0; }
.nums div { display: flex; flex-direction: column-reverse; gap: 1px; }
.nums dd { margin: 0; font-size: 26px; font-weight: 700; line-height: 1.05; font-variant-numeric: tabular-nums; }
.nums dt { color: var(--muted); font-size: 12px; }
.nums .hot dd { color: var(--warning); }
.nums .ko dd { color: var(--danger-text, var(--danger)); }
.load { height: 6px; border-radius: 3px; background: var(--panel-2); overflow: hidden; }
.load span { display: block; height: 100%; background: var(--accent); border-radius: 3px; transition: width var(--dur-2, 240ms) var(--ease-out, ease-out); }
.cap { margin: -2px 0 0; color: var(--muted); font-size: 12px; }
.more { margin-top: auto; color: var(--accent); font-size: 12.5px; text-decoration: none; }
.more:hover { text-decoration: underline; }
</style>
