<script setup lang="ts">
// The «Running now» widget: what is running or waiting to start, among the runs this person may
// see (the Runs search, by state), read every ten seconds while the page shows.
import { LoaderCircle } from 'lucide-vue-next'
import HomeList from '~/components/home/HomeList.vue'
import { runTarget } from '~/composables/exploreList'
import { innermostRuns } from '~/composables/homeLayout'
import { useFormat } from '~/composables/useFormat'
import { useRuns, type RunInfo } from '~/composables/useRuns'

defineProps<{ options: Record<string, any> }>()
const fmt = useFormat()
const runsApi = useRuns()
const runName = (r: RunInfo) => (r as any).flow_name || (r as any).group_name || (r as any).sql_script_name || (r as any).source_name || r.publish_name || `#${r.id}`
async function load(): Promise<RunInfo[]> {
  const [started, pending] = await Promise.all([runsApi.search({ status: 'STARTED', limit: 20 }), runsApi.search({ status: 'PENDING', limit: 20 })])
  return innermostRuns([...(started.items as RunInfo[]), ...(pending.items as RunInfo[])]).slice(0, 20)
}
</script>

<template>
  <HomeList v-slot="{ rows }" :load="load" :every="10" :empty-text="$t('home.running.empty')" :failed-text="$t('home.running.failed')">
    <li v-for="r in (rows as RunInfo[])" :key="r.id">
      <NuxtLink :to="runTarget(r)" class="row">
        <LoaderCircle :size="14" class="ic spin" :class="{ wait: r.status === 'PENDING' }" aria-hidden="true" />
        <span class="name">{{ runName(r) }}</span>
        <span class="sub">{{ $t(r.status === 'PENDING' ? 'home.running.waiting' : 'home.running.running') }}</span>
        <span class="when">{{ fmt.ago((r as any).started_at) }}</span>
      </NuxtLink>
    </li>
  </HomeList>
</template>

<style scoped>
:deep(.ic.spin) { color: var(--accent); }
:deep(.ic.spin.wait) { color: var(--muted); }
@media (prefers-reduced-motion: no-preference) {
  :deep(.ic.spin:not(.wait)) { animation: turn 1.1s linear infinite; }
}
@keyframes turn { to { transform: rotate(360deg); } }
</style>
