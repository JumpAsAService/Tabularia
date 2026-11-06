<script setup lang="ts">
// Explore's top level as a starting page: above the folders, a compact band with what one
// changed last («Recent», GET /explore/recent) and what failed in the last 24 hours (the
// Runs search, failures only). Two rows per section at most, then «Show all»; the band can
// be folded, and stays as each person left it (in this browser).
import { computed, onMounted, ref, watch } from 'vue'
import { ChevronDown, ChevronUp, XCircle, Workflow, Rows3, Database, Bookmark, FileCode2, DatabaseSearch, NotebookPen, FileChartColumn, LayoutDashboard, Folder } from 'lucide-vue-next'
import { useI18n } from 'vue-i18n'
import { knownError } from '~/composables/knownErrors'
import { useFormat } from '~/composables/useFormat'
import { useRuns, type RunInfo } from '~/composables/useRuns'
import { failedWithin, objectTarget, runTarget, type RecentItem } from '~/composables/exploreList'
import type { Project } from '~/composables/useProjects'

const props = defineProps<{ projects: Project[]; userId: number | null }>()
const { t } = useI18n()
const fmt = useFormat()
const runsApi = useRuns()
const { apiFetch } = useApiClient()

const SHOWN = 2
const KIND_ICON: Record<string, any> = {
  flow: Workflow, group: Rows3, datasource: Database, view: Bookmark, script: FileCode2,
  sql: DatabaseSearch, notebook: NotebookPen, report: FileChartColumn, dashboard: LayoutDashboard,
}

// folded or open: per person, in this browser (a preference of the view, not data)
const storeKey = computed(() => `tabularia-explore-start:${props.userId ?? 'anon'}`)
const folded = ref(false)
function readFolded() {
  try { folded.value = localStorage.getItem(storeKey.value) === 'folded' } catch { folded.value = false }
}
function toggle() {
  folded.value = !folded.value
  try { localStorage.setItem(storeKey.value, folded.value ? 'folded' : 'open') } catch { /* storage denied: it holds for this visit */ }
}

const recent = ref<RecentItem[] | null>(null)
const failed = ref<RunInfo[] | null>(null)
const recentAll = ref(false)
const recentError = ref(false)
const failedError = ref(false)

async function load() {
  await Promise.all([
    apiFetch<{ items: RecentItem[] }>('/explore/recent?limit=10').then(
      (r) => { recent.value = r.items; recentError.value = false },
      () => { recent.value = []; recentError.value = true },
    ),
    runsApi.search({ status: 'FAILURE', limit: 20 }).then(
      (p) => { failed.value = failedWithin(p.items, Date.now()); failedError.value = false },
      () => { failed.value = []; failedError.value = true },
    ),
  ])
}
// the person may arrive after the page (a reload): their own choice then
watch(storeKey, readFolded, { immediate: true })
onMounted(load)
defineExpose({ load })

const folderName = (id: number) => props.projects.find((p) => p.id === id)?.name ?? ''
const recentShown = computed(() => (recent.value ?? []).slice(0, recentAll.value ? undefined : SHOWN))
const runName = (r: RunInfo) => r.flow_name || r.group_name || r.sql_script_name || r.source_name || r.publish_name || `#${r.id}`
</script>

<template>
  <section class="start" :class="{ folded }" :aria-label="$t('projectBrowser.startLabel')">
    <button class="fold" :aria-expanded="!folded" @click="toggle">
      <component :is="folded ? ChevronDown : ChevronUp" :size="14" />
      {{ folded ? $t('projectBrowser.startShow') : $t('projectBrowser.startHide') }}
    </button>

    <div v-if="!folded" class="cols">
      <div class="col">
        <h2>{{ $t('projectBrowser.startRecent') }}</h2>
        <p v-if="recent === null" class="note"><span class="sk" style="width: 60%" /></p>
        <p v-else-if="recentError" class="note">{{ $t('projectBrowser.startRecentFailed') }}</p>
        <p v-else-if="!recent.length" class="note">{{ $t('projectBrowser.startNoRecent') }}</p>
        <ul v-else class="items">
          <li v-for="it in recentShown" :key="`${it.kind}-${it.id}`">
            <NuxtLink :to="objectTarget(it.kind, it.id)" class="item">
              <component :is="KIND_ICON[it.kind] ?? Folder" :size="14" class="ic" />
              <span class="name">{{ it.name }}</span>
              <span class="where"><Folder :size="11" /> {{ folderName(it.project_id) }}</span>
              <span class="when">{{ fmt.ago(it.at) }}</span>
            </NuxtLink>
          </li>
        </ul>
        <button v-if="(recent?.length ?? 0) > SHOWN" class="more" @click="recentAll = !recentAll">
          {{ recentAll ? $t('projectBrowser.startShowLess') : $t('projectBrowser.startShowAll') }}
        </button>
      </div>

      <div class="col">
        <h2>
          {{ $t('projectBrowser.startFailed') }}
          <span v-if="failed?.length" class="n">{{ failed.length >= 20 ? '20+' : failed.length }}</span>
        </h2>
        <p v-if="failed === null" class="note"><span class="sk" style="width: 50%" /></p>
        <p v-else-if="failedError" class="note">{{ $t('projectBrowser.startFailedFailed') }}</p>
        <p v-else-if="!failed.length" class="note">{{ $t('projectBrowser.startNoFailed') }}</p>
        <ul v-else class="items">
          <li v-for="r in failed.slice(0, SHOWN)" :key="r.id">
            <NuxtLink :to="runTarget(r)" class="item" :title="r.error ? knownError(r.error, t) : undefined">
              <XCircle :size="14" class="ic ko" />
              <span class="name">{{ runName(r) }}</span>
              <span v-if="r.error" class="err">{{ knownError(r.error, t) }}</span>
              <span class="when">{{ fmt.ago(r.finished_at ?? r.started_at) }}</span>
            </NuxtLink>
          </li>
        </ul>
        <NuxtLink v-if="(failed?.length ?? 0) > SHOWN" to="/runs" class="more">{{ $t('projectBrowser.startShowAll') }}</NuxtLink>
      </div>
    </div>
  </section>
</template>

<style scoped>
.start {
  position: relative;
  margin: 0 0 14px;
  padding: 10px 12px 8px;
  border: 1px solid var(--border-soft);
  border-radius: 10px;
  background: var(--bg-soft);
}
.start.folded { padding: 4px 6px; background: transparent; border-color: transparent; margin-bottom: 6px; }
.fold {
  position: absolute;
  top: 6px;
  right: 8px;
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 3px 8px;
  min-height: 0;
  border: none;
  background: none;
  color: var(--muted);
  font-size: 12px;
  border-radius: 6px;
}
.start.folded .fold { position: static; }
.fold:hover { color: var(--text); background: var(--panel-2); box-shadow: none; }
.cols { display: grid; grid-template-columns: 1fr 1fr; gap: 6px 28px; }
.col { min-width: 0; }
h2 { display: flex; align-items: center; gap: 7px; margin: 0 0 4px; font-size: 12px; font-weight: 600; color: var(--muted); }
h2 .n { font-variant-numeric: tabular-nums; color: var(--danger); font-weight: 600; }
.note { margin: 4px 0 6px; font-size: 12.5px; color: var(--muted); min-height: 18px; }
.note .sk { display: inline-block; height: 12px; }
.items { list-style: none; margin: 0; padding: 0; }
.item {
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 28px;
  padding: 2px 6px;
  margin: 0 -6px;
  border-radius: 7px;
  color: var(--text);
  text-decoration: none;
  font-size: 13px;
}
.item:hover, .item:focus-visible { background: var(--tint-accent); }
.ic { color: var(--muted); flex-shrink: 0; }
.ic.ko { color: var(--danger); }
.name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; flex: 0 1 auto; }
.where, .err { display: inline-flex; align-items: center; gap: 4px; color: var(--muted); font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; flex: 1 1 0; }
.err { display: block; color: var(--danger); opacity: 0.85; }
.when { margin-left: auto; color: var(--muted); font-size: 12px; white-space: nowrap; font-variant-numeric: tabular-nums; flex-shrink: 0; }
.more {
  display: inline-block;
  margin-top: 2px;
  padding: 2px 0;
  min-height: 0;
  border: none;
  background: none;
  color: var(--accent);
  font-size: 12.5px;
  text-decoration: none;
}
.more:hover { text-decoration: underline; box-shadow: none; }
@media (max-width: 900px) {
  .cols { grid-template-columns: 1fr; }
  .fold { position: static; margin: -4px 0 2px -6px; }
}
</style>
