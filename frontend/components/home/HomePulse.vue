<script setup lang="ts">
// The band under the greeting: the day in four numbers, each leading to its page. Runs today and
// how many failed (the Runs page's calendar, today's cell), what is running now, what is due in
// the next 24 hours. One line, read as a sentence; it is the same for every layout, so the page
// says something even with no widget on it.
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { innermostRuns } from '~/composables/homeLayout'
import { useFormat } from '~/composables/useFormat'
import { useRuns } from '~/composables/useRuns'

const runsApi = useRuns()
const { apiFetch } = useApiClient()
const fmt = useFormat()
const n = ref<{ today: number; failed: number; running: number; due: number } | null>(null)
async function read() {
  const now = new Date()
  const key = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`
  const [activity, running, due] = await Promise.allSettled([
    runsApi.activity({ days: 2 }),
    runsApi.search({ status: 'STARTED', limit: 100 }),
    apiFetch<{ items: unknown[] }>('/home/next-runs?hours=24'),
  ])
  const cell = activity.status === 'fulfilled' ? activity.value.buckets.find((b) => b.key === key) : undefined
  n.value = {
    today: cell?.total ?? 0,
    failed: cell?.failure ?? 0,
    // a flow running is two rows, the orchestration and the flow: counted once
    running: running.status === 'fulfilled' ? innermostRuns(running.value.items).length : 0,
    due: due.status === 'fulfilled' ? due.value.items.length : 0,
  }
}
let timer: ReturnType<typeof setInterval> | undefined
const whenShown = () => { if (!document.hidden) read() }
onMounted(() => { read(); timer = setInterval(whenShown, 60_000); document.addEventListener('visibilitychange', whenShown) })
onBeforeUnmount(() => { clearInterval(timer); document.removeEventListener('visibilitychange', whenShown) })
</script>

<template>
  <nav class="pulse" :aria-label="$t('home.pulse.label')">
    <NuxtLink to="/runs" class="cell">
      <span class="num">{{ n ? fmt.count(n.today) : '–' }}</span><span class="what">{{ $t('home.pulse.today', n?.today ?? 2) }}</span>
    </NuxtLink>
    <NuxtLink to="/runs" class="cell" :class="{ ko: !!n?.failed }">
      <span class="num">{{ n ? fmt.count(n.failed) : '–' }}</span><span class="what">{{ $t('home.pulse.failed', n?.failed ?? 2) }}</span>
    </NuxtLink>
    <NuxtLink to="/runs" class="cell" :class="{ live: !!n?.running }">
      <span class="num">{{ n ? fmt.count(n.running) : '–' }}</span><span class="what">{{ $t('home.pulse.running', n?.running ?? 2) }}</span>
    </NuxtLink>
    <NuxtLink to="/flows" class="cell">
      <span class="num">{{ n ? (n.due >= 50 ? '50+' : fmt.count(n.due)) : '–' }}</span><span class="what">{{ $t('home.pulse.due', n?.due ?? 2) }}</span>
    </NuxtLink>
  </nav>
</template>

<style scoped>
.pulse { display: flex; flex-wrap: wrap; margin: 0 0 16px; border-top: 1px solid var(--border-soft); border-bottom: 1px solid var(--border-soft); }
.cell {
  display: flex; align-items: baseline; gap: 9px; padding: 12px 26px 12px 0; margin-right: 26px; min-width: 0;
  color: var(--muted); text-decoration: none; font-size: 13px; border-right: 1px solid var(--border-soft);
}
.cell:last-child { border-right: none; margin-right: 0; }
.num { color: var(--text); font-size: 26px; font-weight: 700; line-height: 1; font-variant-numeric: tabular-nums; letter-spacing: -0.01em; }
.cell.ko .num { color: var(--danger-text, var(--danger)); }
.cell.live .num { color: var(--accent-hi, var(--accent)); }
.cell:hover .what, .cell:focus-visible .what { color: var(--text); text-decoration: underline; }
@media (max-width: 700px) {
  .cell { flex: 1 1 40%; border-right: none; margin-right: 0; padding: 10px 0; }
  .num { font-size: 22px; }
}
</style>
