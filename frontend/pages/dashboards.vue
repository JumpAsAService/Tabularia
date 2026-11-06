<script setup lang="ts">
// Every dashboard the person may open (VIEW on its folder), paginated, with a server-side search
// over name, description and folders. By default the most read come first: the people who opened it
// in the viewer in the last 30 days, a person once a day (GET /dashboards/search). A dashboard is
// made in a folder, from Explore or here, where the person has EDIT, and goes to the designer. The
// order, the search and the page live in the address: a link, a reload and Back give the same list.
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { Eye, Folder, FolderTree, LayoutDashboard, LoaderCircle, PencilRuler, Plus, Search } from 'lucide-vue-next'
import Modal from '~/components/ui/Modal.vue'
import { errMessage } from '~/composables/useApi'
import { useAuth } from '~/composables/useAuth'
import { useAppInfo } from '~/composables/useAppInfo'
import { useFolderPaths } from '~/composables/useFolderPaths'
import { useProjects } from '~/composables/useProjects'
import { useFormat } from '~/composables/useFormat'
import { usePagedList } from '~/composables/usePagedList'
import { starterDashboard, useDashboards, type DashboardRow, type DashboardSort } from '~/composables/useDashboards'

const api = useDashboards()
const { t, locale } = useI18n()
const toast = useToast()
const fmtx = useFormat()
const route = useRoute()
const router = useRouter()

const PAGE_SIZE = 50
const SORTS: DashboardSort[] = ['trending', 'name', 'updated']
const sortOf = (query: typeof route.query) => (SORTS.includes(query.sort as DashboardSort) ? query.sort as DashboardSort : 'trending')
const offsetOf = (query: typeof route.query) => (Math.max(1, Math.floor(Number(query.page)) || 1) - 1) * PAGE_SIZE
const sort = ref<DashboardSort>(sortOf(route.query))
const sortOptions = computed(() => [
  { value: 'trending', label: t('dashboardsTab.sortTrending') },
  { value: 'name', label: t('dashboardsTab.sortName') },
  { value: 'updated', label: t('dashboardsTab.sortUpdated') },
])

const { q, items, total, offset, pageSize, loading, error, load, next, prev } = usePagedList<DashboardRow>(
  (p) => api.listPaged({ ...p, sort: sort.value }),
  { pageSize: PAGE_SIZE, initialQuery: String(route.query.q ?? ''), initialOffset: offsetOf(route.query) },
)
watch(sort, () => {
  offset.value = 0
  load()
})
// addresses on their way: a change of ours arriving back must not undo the keys typed since
let writing = 0
watch([sort, q, offset], () => {
  const query: Record<string, string> = {}
  if (sort.value !== 'trending') query.sort = sort.value
  if (q.value.trim()) query.q = q.value.trim()
  if (offset.value) query.page = String(offset.value / pageSize + 1)
  writing++
  router.replace({ query }).finally(() => nextTick(() => { writing-- }))
})
// the menu's link to the plain list, with the page already open: nothing mounts again
watch(() => route.query, (query) => {
  if (writing) return
  const [wantedSort, wantedQ, wantedOffset] = [sortOf(query), String(query.q ?? ''), offsetOf(query)]
  if (wantedSort === sort.value && wantedQ === q.value.trim() && wantedOffset === offset.value) return
  sort.value = wantedSort
  q.value = wantedQ
  offset.value = wantedOffset
  load()
})
// ── a new one, in a folder where this person has EDIT ──
const folders = useFolderPaths()
const { user } = useAuth()
const editable = ref<number[]>([])
const personal = ref<number | null>(null)
const folderOptions = computed(() => editable.value
  .map((id) => ({ value: id, label: folders.pathOf(id) ?? String(id) }))
  .sort((a, b) => Number(b.value === personal.value) - Number(a.value === personal.value) || a.label.localeCompare(b.label)))
const newOpen = ref(false)
const newName = ref('')
const newFolder = ref<number | null>(null)
const creating = ref(false)
onMounted(async () => {
  try {
    const [ids, list] = await Promise.all([api.folders(), useProjects().list(), folders.load()])
    editable.value = ids
    personal.value = list.find((p) => p.personal_of != null && p.personal_of === user.value?.id)?.id ?? null
  } catch { /* the list reads all the same; the button stays away */ }
})
function openNew() {
  newName.value = t('projectBrowser.newDashboardName')
  newFolder.value = folderOptions.value[0]?.value ?? null
  newOpen.value = true
}
async function createNew() {
  if (newFolder.value == null || creating.value) return
  creating.value = true
  try {
    const info = await useAppInfo().load()
    const created = await api.create(newFolder.value, { name: newName.value.trim(), content: starterDashboard(String(locale.value), info?.timezone ?? 'UTC') })
    newOpen.value = false
    await navigateTo(`/dashboard-design?id=${created.id}`)
  } catch (e) {
    toast.error(errMessage(e))
  } finally {
    creating.value = false
  }
}

// a link to a page that is no longer there (fewer dashboards now): the last one that is
watch(items, () => {
  const last = Math.floor(Math.max(0, total.value - 1) / pageSize) * pageSize
  if (items.value.length || !total.value || offset.value <= last) return
  offset.value = last
  load()
})

// the trending bar: against the most read row on the page, so the first one of the default order
// is full and the others read as a share of it
const mostPeople = computed(() => Math.max(1, ...items.value.map((r) => r.people_30d)))
const heat = (r: DashboardRow) => `${Math.round((r.people_30d / mostPeople.value) * 100)}%`

const viewerLink = (r: DashboardRow) => `/dashboard?id=${r.id}`
const folderOf = (r: DashboardRow) => r.folder_path.join(' › ')

// a click anywhere on the row opens the dashboard, except on its own links and buttons
function openRow(ev: MouseEvent, r: DashboardRow) {
  if ((ev.target as HTMLElement).closest('a, button')) return
  if (window.getSelection()?.toString()) return // selecting a name to copy it is not a click
  navigateTo(viewerLink(r))
}
</script>

<template>
  <AppShell>
    <div class="page-head">
      <h1><LayoutDashboard :size="18" /> {{ $t('dashboardsTab.title') }} <span class="muted count">{{ total }}</span></h1>
      <div class="head-actions">
        <Select v-model="sort" :options="sortOptions" class="sortsel" :aria-label="$t('dashboardsTab.sortLabel')" />
        <span class="searchbox"><Search :size="14" /><input v-model="q" type="text" :placeholder="$t('dashboardsTab.searchPlaceholder')" :aria-label="$t('dashboardsTab.searchPlaceholder')" /></span>
        <button v-if="folderOptions.length" type="button" class="primary new-board" data-new-dashboard @click="openNew"><Plus :size="14" /> {{ $t('dashboardsTab.new') }}</button>
      </div>
    </div>

    <p v-if="error" class="err">{{ error }}</p>
    <SkeletonRows v-else-if="loading || (!items.length && total)" :rows="5" />
    <p v-else-if="!items.length && q" class="muted">{{ $t('dashboardsTab.noSearchResults') }}</p>
    <div v-else-if="!items.length" class="empty">
      <LayoutDashboard :size="22" class="empty-icon" />
      <p class="empty-title">{{ $t('dashboardsTab.emptyTitle') }}</p>
      <p class="muted">{{ $t('dashboardsTab.emptyHint') }}</p>
      <NuxtLink to="/" class="btn-link"><FolderTree :size="14" /> {{ $t('dashboardsTab.emptyAction') }}</NuxtLink>
    </div>

    <table v-else class="list">
      <thead>
        <tr>
          <th>{{ $t('dashboardsTab.colDashboard') }}</th>
          <th>{{ $t('dashboardsTab.colFolder') }}</th>
          <th :title="$t('dashboardsTab.colReadersHint')">{{ $t('dashboardsTab.colReaders') }}</th>
          <th>{{ $t('dashboardsTab.colLastOpened') }}</th>
          <th>{{ $t('dashboardsTab.colChanged') }}</th>
          <th><span class="sr-only">{{ $t('dashboardsTab.colActions') }}</span></th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="r in items" :key="r.id" class="row" @click="openRow($event, r)">
          <td class="name">
            <NuxtLink :to="viewerLink(r)" class="rowlink"><LayoutDashboard :size="14" /> {{ r.name }}</NuxtLink>
            <div v-if="r.description" class="sub desc clamp">{{ r.description }}</div>
            <div class="sub meta">
              {{ $t('dashboardsTab.tiles', { n: r.tiles }, r.tiles) }} · {{ $t('dashboardsTab.parameters', { n: r.parameters }, r.parameters) }}
            </div>
          </td>
          <td class="muted folder"><Folder :size="13" /> {{ folderOf(r) }}</td>
          <td class="readers">
            <template v-if="r.people_30d">
              <span class="heat" aria-hidden="true"><span :style="{ width: heat(r) }" /></span>
              <span class="people">{{ $t('dashboardsTab.people', { n: fmtx.count(r.people_30d) }, r.people_30d) }}</span>
              <div class="sub meta">
                {{ $t('dashboardsTab.opens', { n: fmtx.count(r.opens_30d) }, r.opens_30d) }} · {{ $t('dashboardsTab.ever', { n: fmtx.count(r.views_total) }) }}
              </div>
            </template>
            <template v-else>
              <span class="sub">{{ $t('dashboardsTab.noReaders') }}</span>
              <div v-if="r.views_total" class="sub meta">{{ $t('dashboardsTab.ever', { n: fmtx.count(r.views_total) }) }}</div>
            </template>
          </td>
          <td class="muted nowrap" :title="r.last_viewed_at ? fmtx.dateTime(r.last_viewed_at) : undefined">
            {{ r.last_viewed_at ? fmtx.ago(r.last_viewed_at) : $t('dashboardsTab.neverOpened') }}
          </td>
          <td class="muted changed" :title="fmtx.dateTime(r.updated_at)">
            <span class="nowrap">{{ fmtx.ago(r.updated_at) }}</span>
            <div class="sub meta">
              <span class="tag version">v{{ r.current_version }}</span>
              <span v-if="r.owner_name"> {{ r.owner_name }}</span>
            </div>
          </td>
          <td class="right">
            <NuxtLink :to="viewerLink(r)" class="mini iconbtn" :title="$t('dashboardsTab.open')" :aria-label="`${$t('dashboardsTab.open')}: ${r.name}`"><Eye :size="13" /></NuxtLink>
            <NuxtLink v-if="r.editable" :to="`/dashboard-design?id=${r.id}`" class="mini iconbtn" :title="$t('dashboardsTab.edit')" :aria-label="`${$t('dashboardsTab.edit')}: ${r.name}`"><PencilRuler :size="13" /></NuxtLink>
          </td>
        </tr>
      </tbody>
    </table>

    <Pager :offset="offset" :page-size="pageSize" :total="total" :loading="loading" @prev="prev" @next="next" />

    <Modal :open="newOpen" :title="$t('dashboardsTab.newTitle')" :width="460" @close="newOpen = false">
      <form id="new-dashboard" class="newform" @submit.prevent="createNew">
        <label>
          <span>{{ $t('dashboardsTab.newName') }}</span>
          <input v-model="newName" type="text" maxlength="200" data-new-name />
        </label>
        <label>
          <span>{{ $t('dashboardsTab.newFolder') }}</span>
          <Select v-model="newFolder" :options="folderOptions" searchable :aria-label="$t('dashboardsTab.newFolder')" />
        </label>
        <p class="muted hint">{{ $t('dashboardsTab.newHint') }}</p>
      </form>
      <template #footer>
        <span class="grow" />
        <button type="button" @click="newOpen = false">{{ $t('reportDesigner.cancel') }}</button>
        <button type="submit" form="new-dashboard" class="primary" :disabled="creating || !newName.trim() || newFolder == null">
          <LoaderCircle v-if="creating" :size="14" class="spin" /> {{ $t('dashboardsTab.newCreate') }}
        </button>
      </template>
    </Modal>
  </AppShell>
</template>

<style scoped src="~/assets/listpage.css" />

<style scoped>
.new-board { display: inline-flex; align-items: center; gap: 6px; white-space: nowrap; }
.newform { display: flex; flex-direction: column; gap: 12px; }
.newform label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); }
.hint { margin: 0; font-size: 12px; line-height: 1.45; }
.grow { flex: 1; }
.sortsel { width: 170px; }
tr.row { cursor: pointer; }
td.name { max-width: 420px; }
.clamp {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
/* the lines under a cell's first: muted, as blocks (listpage.css makes `td .muted` a
   table cell, which put them side by side) */
.sub { color: var(--muted); }
.meta { font-size: 12px; margin-top: 2px; }
td.folder { font-size: 12.5px; }
.tag.version { margin-left: 0; font-variant-numeric: tabular-nums; }
/* the readers: a short bar against the page's most read, then the count. Encoding, so
   the accent; the track is the neutral second step */
td.readers { white-space: nowrap; font-variant-numeric: tabular-nums; }
.heat {
  display: inline-block;
  width: 44px;
  height: 5px;
  border-radius: 3px;
  background: var(--panel-2);
  border: 1px solid var(--border-soft);
  margin-right: 8px;
  vertical-align: 2px;
  overflow: hidden;
}
.heat > span { display: block; height: 100%; background: var(--accent); border-radius: 3px; }
.people { font-weight: 500; }
/* an anchor that looks like the row's mini buttons */
a.iconbtn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  margin-left: 4px;
  min-height: 24px;
  padding: 3px 8px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--panel-2);
  color: var(--text);
  vertical-align: middle;
  transition: border-color var(--dur-2), box-shadow var(--dur-2);
}
a.iconbtn:hover { border-color: var(--accent); box-shadow: var(--shadow-1); }
a.iconbtn:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.empty {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 6px;
  padding: 22px 24px;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  max-width: 560px;
}
.empty p { margin: 0; }
.empty-icon { color: var(--muted); }
.empty-title { font-weight: 600; font-size: 15px; }
.empty .btn-link { margin-top: 8px; }
@media (max-width: 720px) {
  th:nth-child(2), td:nth-child(2), th:nth-child(4), td:nth-child(4) { display: none; }
  .sortsel { width: 140px; }
}
</style>
