<script setup lang="ts">
// The «Recent» widget: the objects this person changed last (GET /explore/recent, which lists
// only what they may read), of every kind or of the kinds chosen.
import { Bookmark, Braces, Database, DatabaseSearch, FileChartColumn, FileCode2, Folder, LayoutDashboard, NotebookPen, Rows3, Workflow } from 'lucide-vue-next'
import HomeList from '~/components/home/HomeList.vue'
import { objectTarget, type RecentItem } from '~/composables/exploreList'
import { useFormat } from '~/composables/useFormat'
import { useFolderPaths } from '~/composables/useFolderPaths'

const props = defineProps<{ options: Record<string, any> }>()
const { apiFetch } = useApiClient()
const fmt = useFormat()
const { pathOf } = useFolderPaths()
const KIND_ICON: Record<string, any> = {
  flow: Workflow, group: Rows3, datasource: Database, view: Bookmark, script: FileCode2,
  sql: DatabaseSearch, notebook: NotebookPen, report: FileChartColumn, dashboard: LayoutDashboard, dataapi: Braces,
}
async function load(): Promise<RecentItem[]> {
  const limit = Number(props.options.limit) || 8
  const kinds: string[] = props.options.kinds ?? []
  // narrowed here: the gateway answers the last twenty of every kind
  const r = await apiFetch<{ items: RecentItem[] }>(`/explore/recent?limit=${kinds.length ? 20 : limit}`)
  return (kinds.length ? r.items.filter((i) => kinds.includes(i.kind)) : r.items).slice(0, limit)
}
</script>

<template>
  <HomeList
    v-slot="{ rows }" :load="load" :every="60" :watch-key="JSON.stringify(options)"
    :empty-text="$t('home.recent.empty')" :failed-text="$t('home.recent.failed')"
  >
    <li v-for="it in (rows as RecentItem[])" :key="`${it.kind}-${it.id}`">
      <NuxtLink :to="objectTarget(it.kind, it.id)" class="row">
        <component :is="KIND_ICON[it.kind] ?? Folder" :size="14" class="ic" aria-hidden="true" />
        <span class="name">{{ it.name }}</span>
        <span class="sub">{{ pathOf(it.project_id) }}</span>
        <span class="when">{{ fmt.ago(it.at) }}</span>
      </NuxtLink>
    </li>
  </HomeList>
</template>
