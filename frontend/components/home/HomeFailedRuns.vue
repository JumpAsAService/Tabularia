<script setup lang="ts">
// The «Failed runs» widget: the last runs that failed, among those this person may see (the
// Runs search, failures only), within the days chosen; their own alone when the widget says so.
import { useI18n } from 'vue-i18n'
import { XCircle } from 'lucide-vue-next'
import HomeList from '~/components/home/HomeList.vue'
import { runTarget } from '~/composables/exploreList'
import { failedFor } from '~/composables/homeLayout'
import { knownError } from '~/composables/knownErrors'
import { useFormat } from '~/composables/useFormat'
import { useRuns, type RunInfo } from '~/composables/useRuns'

const props = defineProps<{ options: Record<string, any> }>()
const { t } = useI18n()
const fmt = useFormat()
const runsApi = useRuns()
const { user } = useAuth()
const runName = (r: RunInfo) => (r as any).flow_name || (r as any).group_name || (r as any).sql_script_name || (r as any).source_name || r.publish_name || `#${r.id}`
async function load(): Promise<RunInfo[]> {
  const page = await runsApi.search({ status: 'FAILURE', limit: 50 })
  return failedFor(page.items as RunInfo[], props.options, user.value?.id ?? null, Date.now()).slice(0, 20)
}
</script>

<template>
  <HomeList
    v-slot="{ rows }" :load="load" :every="60" :watch-key="JSON.stringify(options)"
    :empty-text="$t('home.failed.empty', { n: options.days ?? 7 })" :failed-text="$t('home.failed.failed')"
  >
    <li v-for="r in (rows as RunInfo[])" :key="r.id">
      <NuxtLink :to="runTarget(r)" class="row" :title="r.error ? knownError(r.error, t) : undefined">
        <XCircle :size="14" class="ic ko" aria-hidden="true" />
        <span class="name">{{ runName(r) }}</span>
        <span v-if="r.error" class="sub err">{{ knownError(r.error, t).split('\n')[0] }}</span>
        <span class="when">{{ fmt.ago((r as any).finished_at ?? (r as any).started_at) }}</span>
      </NuxtLink>
    </li>
    <li><NuxtLink to="/runs" class="row all">{{ $t('home.failed.all') }}</NuxtLink></li>
  </HomeList>
</template>
