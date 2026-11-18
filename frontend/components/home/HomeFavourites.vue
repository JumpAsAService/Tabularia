<script setup lang="ts">
// The «Favourites» widget: the objects this person pinned from Explore (a row's menu, «Add to
// home»), in the order pinned. Only what they may still see is listed; the star unpins.
import { onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { Bookmark, Braces, Database, DatabaseSearch, FileChartColumn, FileCode2, Folder, LayoutDashboard, NotebookPen, Rows3, StarOff, Workflow } from 'lucide-vue-next'
import { objectTarget } from '~/composables/exploreList'
import { useFolderPaths } from '~/composables/useFolderPaths'
import { useHomeFavourites } from '~/composables/useHomeFavourites'

defineProps<{ options: Record<string, any> }>()
const favs = useHomeFavourites()
const { pathOf } = useFolderPaths()
const toast = useToast()
const { t } = useI18n()
const KIND_ICON: Record<string, any> = {
  flow: Workflow, group: Rows3, datasource: Database, view: Bookmark, script: FileCode2,
  sql: DatabaseSearch, notebook: NotebookPen, report: FileChartColumn, dashboard: LayoutDashboard, dataapi: Braces,
}
const failed = ref(false)
const read = () => favs.load(true).then(() => { failed.value = false }, () => { failed.value = true })
onMounted(read)
const unpin = (kind: string, id: number) => favs.toggle(kind, id).catch(() => toast.error(t('home.saveFailed')))
</script>

<template>
  <div class="fv" :aria-busy="favs.items.value === null && !failed">
    <p v-if="failed && !favs.items.value?.length" class="msg" role="alert">{{ $t('home.recent.failed') }} <button type="button" class="retry" @click="read">{{ $t('home.retry') }}</button></p>
    <p v-else-if="favs.items.value === null" class="msg"><span class="sk" style="width: 55%" /></p>
    <p v-else-if="!favs.items.value.length" class="msg">{{ $t('home.favourites.empty') }}</p>
    <ul v-else class="rows">
      <li v-for="it in favs.items.value" :key="`${it.kind}-${it.id}`">
        <NuxtLink :to="objectTarget(it.kind, it.id)" class="row">
          <component :is="KIND_ICON[it.kind] ?? Folder" :size="14" class="ic" aria-hidden="true" />
          <span class="name">{{ it.name }}</span>
          <span class="sub">{{ pathOf(it.project_id) }}</span>
        </NuxtLink>
        <button type="button" class="un" :title="$t('home.unpin')" :aria-label="$t('home.unpinOf', { name: it.name })" @click="unpin(it.kind, it.id)"><StarOff :size="13" aria-hidden="true" /></button>
      </li>
    </ul>
  </div>
</template>

<style scoped>
.fv { height: 100%; overflow: auto; }
.msg { margin: 2px 0; color: var(--muted); font-size: 13px; }
.msg .sk { display: inline-block; height: 12px; }
.retry { min-height: 0; padding: 0 2px; border: none; background: none; color: var(--accent); font-size: 13px; text-decoration: underline; }
.rows { list-style: none; margin: 0; padding: 0; }
.rows li { display: flex; align-items: center; gap: 2px; margin: 0 -6px; border-radius: 7px; }
.rows li:hover, .rows li:focus-within { background: var(--tint-accent); }
.row { flex: 1; min-width: 0; display: flex; align-items: center; gap: 8px; min-height: 30px; padding: 3px 6px; color: var(--text); text-decoration: none; font-size: 13px; border-radius: 7px; }
.ic { color: var(--muted); flex-shrink: 0; }
.name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; flex: 0 1 auto; }
.sub { color: var(--muted); font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; flex: 1 1 0; }
.un { display: grid; place-items: center; width: 26px; height: 26px; min-height: 0; padding: 0; border: none; border-radius: 7px; background: none; color: var(--muted); opacity: 0; flex-shrink: 0; }
.rows li:hover .un, .un:focus-visible { opacity: 1; }
.un:hover { color: var(--text); background: var(--panel-2); box-shadow: none; }
@media (hover: none) { .un { opacity: 1; } }
</style>
