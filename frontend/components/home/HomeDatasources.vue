<script setup lang="ts">
// The «Datasources» widget: when each datasource was last refreshed and how many rows it holds,
// for the ones chosen, or for those refreshed last when none is. Read from the Datasources page's
// own list, which holds what this person may see.
import { Database } from 'lucide-vue-next'
import HomeList from '~/components/home/HomeList.vue'
import { useDatasources, type DatasourceInfo } from '~/composables/useDatasources'
import { useFormat } from '~/composables/useFormat'
import { useFolderPaths } from '~/composables/useFolderPaths'

const props = defineProps<{ options: Record<string, any> }>()
const api = useDatasources()
const fmt = useFormat()
const { pathOf } = useFolderPaths()
const when = (d: DatasourceInfo) => (d as any).refreshed_at ?? (d as any).updated_at ?? (d as any).created_at ?? null
async function load(): Promise<DatasourceInfo[]> {
  const all = await api.list()
  const ids: number[] = props.options.ids ?? []
  if (ids.length) return ids.map((id) => all.find((d) => d.id === id)).filter((d): d is DatasourceInfo => !!d)
  return [...all].sort((a, b) => String(when(b) ?? '').localeCompare(String(when(a) ?? ''))).slice(0, 8)
}
</script>

<template>
  <HomeList
    v-slot="{ rows }" :load="load" :every="120" :watch-key="JSON.stringify(options)"
    :empty-text="$t('home.datasources.empty')" :failed-text="$t('home.datasources.failed')"
  >
    <li v-for="d in (rows as DatasourceInfo[])" :key="d.id">
      <NuxtLink :to="`/viewer?ds=${d.id}`" class="row">
        <Database :size="14" class="ic" aria-hidden="true" />
        <span class="name">{{ d.name }}</span>
        <span class="sub">{{ d.rows != null ? $t('home.datasources.rows', { n: fmt.count(d.rows) }) : pathOf(d.project_id) }}</span>
        <span class="when">{{ fmt.ago(when(d)) }}</span>
      </NuxtLink>
    </li>
  </HomeList>
</template>
