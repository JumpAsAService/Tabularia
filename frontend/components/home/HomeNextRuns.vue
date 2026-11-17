<script setup lang="ts">
// The «Next runs» widget: the schedules due in the hours chosen, soonest first, among what this
// person may see (GET /home/next-runs); their own alone when the widget says so.
import { Clock3, Database, DatabaseSearch, FileCode2, Rows3, Workflow } from 'lucide-vue-next'
import HomeList from '~/components/home/HomeList.vue'
import { objectTarget } from '~/composables/exploreList'
import { useFormat } from '~/composables/useFormat'

interface NextRun { kind: string; id: number; name: string; project_id: number; at: string | null; schedule: string }
const props = defineProps<{ options: Record<string, any> }>()
const { apiFetch } = useApiClient()
const fmt = useFormat()
const KIND_ICON: Record<string, any> = { flow: Workflow, group: Rows3, datasource: Database, script: FileCode2, sql: DatabaseSearch }
const load = async () => (await apiFetch<{ items: NextRun[] }>(`/home/next-runs?hours=${Number(props.options.hours) || 24}&scope=${props.options.scope === 'mine' ? 'mine' : 'all'}`)).items
const timeOf = (at: string | null) => (at ? fmt.dateTime(at) : '')
</script>

<template>
  <HomeList
    v-slot="{ rows }" :load="load" :every="60" :watch-key="JSON.stringify(options)"
    :empty-text="$t('home.nextRuns.empty', { n: options.hours ?? 24 })" :failed-text="$t('home.nextRuns.failed')"
  >
    <li v-for="r in (rows as NextRun[])" :key="`${r.kind}-${r.id}`">
      <NuxtLink :to="objectTarget(r.kind, r.id)" class="row" :title="r.schedule">
        <component :is="KIND_ICON[r.kind] ?? Clock3" :size="14" class="ic" aria-hidden="true" />
        <span class="name">{{ r.name }}</span>
        <code class="sub">{{ r.schedule }}</code>
        <time class="when" :datetime="r.at ?? undefined">{{ timeOf(r.at) }}</time>
      </NuxtLink>
    </li>
  </HomeList>
</template>
