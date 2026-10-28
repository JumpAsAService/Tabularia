<script setup lang="ts">
// Every report the person may open (VIEW on its folder), paginated, with a server-side
// search over name, description and folders. By default the most read come first: the
// people who opened it in the viewer in the last 30 days, a person once a day
// (GET /reports/search, services/report_views.py in the gateway). A report is made in a
// folder, from Explore: here it is opened, edited and its API link copied.
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { FileChartColumn, Search, Folder, Pencil, Link2, Eye, FolderTree } from 'lucide-vue-next'
import { useFormat } from '~/composables/useFormat'
import { usePagedList } from '~/composables/usePagedList'
import { useReports, type ReportRow, type ReportSort } from '~/composables/useReports'
import ReportApiDialog from '~/components/report/ReportApiDialog.vue'
import ApiTokensDialog from '~/components/ui/ApiTokensDialog.vue'

const api = useReports()
const toast = useToast()
const { t } = useI18n()
const fmtx = useFormat()
const route = useRoute()

const SORTS: ReportSort[] = ['trending', 'name', 'updated']
const sort = ref<ReportSort>(SORTS.includes(route.query.sort as ReportSort) ? route.query.sort as ReportSort : 'trending')
const sortOptions = computed(() => [
  { value: 'trending', label: t('reportsTab.sortTrending') },
  { value: 'name', label: t('reportsTab.sortName') },
  { value: 'updated', label: t('reportsTab.sortUpdated') },
])

const { q, items, total, offset, pageSize, loading, error, load, next, prev } = usePagedList<ReportRow>(
  (p) => api.listPaged({ ...p, sort: sort.value }),
  { initialQuery: String(route.query.q ?? '') },
)
watch(sort, () => {
  offset.value = 0
  load()
})

// the trending bar: against the most read row on the page, so the first one of the
// default order is full and the others read as a share of it
const mostPeople = computed(() => Math.max(1, ...items.value.map((r) => r.people_30d)))
const heat = (r: ReportRow) => `${Math.round((r.people_30d / mostPeople.value) * 100)}%`

const viewerLink = (r: ReportRow) => `/report?id=${r.id}`
const folderOf = (r: ReportRow) => r.folder_path.join(' › ')

// a click anywhere on the row opens the report, except on its own links and buttons
function openRow(ev: MouseEvent, r: ReportRow) {
  if ((ev.target as HTMLElement).closest('a, button')) return
  if (window.getSelection()?.toString()) return // selecting a name to copy it is not a click
  navigateTo(viewerLink(r))
}

// «Copy API link»: the viewer's dialog, with the report's defaults
const NO_PARAMS = Object.freeze({}) as Record<string, never>
const apiFor = ref<ReportRow | null>(null)
const apiCopied = ref(false)
const tokensOpen = ref(false)
function copyApi(r: ReportRow) {
  apiCopied.value = false
  // the dialog first: a clipboard that never answers (Chrome, with the page not focused)
  // must not keep it closed
  apiFor.value = r
  navigator.clipboard?.writeText(api.apiUrl(r.id, 'pdf')).then(() => {
    apiCopied.value = true
    toast.success(t('report.apiLinkCopied'))
  }, () => { /* the dialog shows it, selected */ })
}
function openTokens() {
  apiFor.value = null
  tokensOpen.value = true
}
</script>

<template>
  <AppShell>
    <div class="page-head">
      <h1><FileChartColumn :size="18" /> {{ $t('reportsTab.title') }} <span class="muted count">{{ total }}</span></h1>
      <div class="head-actions">
        <Select v-model="sort" :options="sortOptions" class="sortsel" :aria-label="$t('reportsTab.sortLabel')" />
        <span class="searchbox"><Search :size="14" /><input v-model="q" type="text" :placeholder="$t('reportsTab.searchPlaceholder')" :aria-label="$t('reportsTab.searchPlaceholder')" /></span>
      </div>
    </div>

    <p v-if="error" class="err">{{ error }}</p>
    <SkeletonRows v-else-if="loading" :rows="5" />
    <p v-else-if="!items.length && q" class="muted">{{ $t('reportsTab.noSearchResults') }}</p>
    <div v-else-if="!items.length" class="empty">
      <FileChartColumn :size="22" class="empty-icon" />
      <p class="empty-title">{{ $t('reportsTab.emptyTitle') }}</p>
      <p class="muted">{{ $t('reportsTab.emptyHint') }}</p>
      <NuxtLink to="/" class="btn-link"><FolderTree :size="14" /> {{ $t('reportsTab.emptyAction') }}</NuxtLink>
    </div>

    <table v-else class="list">
      <thead>
        <tr>
          <th>{{ $t('reportsTab.colReport') }}</th>
          <th>{{ $t('reportsTab.colFolder') }}</th>
          <th :title="$t('reportsTab.colReadersHint')">{{ $t('reportsTab.colReaders') }}</th>
          <th>{{ $t('reportsTab.colLastOpened') }}</th>
          <th>{{ $t('reportsTab.colChanged') }}</th>
          <th><span class="sr-only">{{ $t('reportsTab.colActions') }}</span></th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="r in items" :key="r.id" class="row" @click="openRow($event, r)">
          <td class="name">
            <NuxtLink :to="viewerLink(r)" class="rowlink"><FileChartColumn :size="14" /> {{ r.name }}</NuxtLink>
            <div v-if="r.description" class="sub desc clamp">{{ r.description }}</div>
            <div class="sub meta">
              {{ $t('reportsTab.datasets', { n: r.datasets }, r.datasets) }} · {{ $t('reportsTab.parameters', { n: r.parameters }, r.parameters) }}
            </div>
          </td>
          <td class="muted folder"><Folder :size="13" /> {{ folderOf(r) }}</td>
          <td class="readers">
            <template v-if="r.people_30d">
              <span class="heat" aria-hidden="true"><span :style="{ width: heat(r) }" /></span>
              <span class="people">{{ $t('reportsTab.people', { n: fmtx.count(r.people_30d) }, r.people_30d) }}</span>
              <div class="sub meta">
                {{ $t('reportsTab.opens', { n: fmtx.count(r.opens_30d) }, r.opens_30d) }} · {{ $t('reportsTab.ever', { n: fmtx.count(r.views_total) }) }}
              </div>
            </template>
            <template v-else>
              <span class="sub">{{ $t('reportsTab.noReaders') }}</span>
              <div v-if="r.views_total" class="sub meta">{{ $t('reportsTab.ever', { n: fmtx.count(r.views_total) }) }}</div>
            </template>
          </td>
          <td class="muted nowrap" :title="r.last_viewed_at ? fmtx.dateTime(r.last_viewed_at) : undefined">
            {{ r.last_viewed_at ? fmtx.ago(r.last_viewed_at) : $t('reportsTab.neverOpened') }}
          </td>
          <td class="muted changed" :title="fmtx.dateTime(r.updated_at)">
            <span class="nowrap">{{ fmtx.ago(r.updated_at) }}</span>
            <div class="sub meta">
              <span class="tag version">v{{ r.current_version }}</span>
              <span v-if="r.owner_name"> {{ r.owner_name }}</span>
            </div>
          </td>
          <td class="right">
            <NuxtLink :to="viewerLink(r)" class="mini iconbtn" :title="$t('reportsTab.open')" :aria-label="`${$t('reportsTab.open')}: ${r.name}`"><Eye :size="13" /></NuxtLink>
            <NuxtLink v-if="r.editable" :to="`/report-design?id=${r.id}`" class="mini iconbtn" :title="$t('reportsTab.edit')" :aria-label="`${$t('reportsTab.edit')}: ${r.name}`"><Pencil :size="13" /></NuxtLink>
            <button class="mini" :title="$t('reportsTab.copyApi')" :aria-label="`${$t('reportsTab.copyApi')}: ${r.name}`" @click="copyApi(r)"><Link2 :size="13" /></button>
          </td>
        </tr>
      </tbody>
    </table>

    <Pager :offset="offset" :page-size="pageSize" :total="total" :loading="loading" @prev="prev" @next="next" />

    <ReportApiDialog
      v-if="apiFor"
      :open="!!apiFor"
      :report-id="apiFor.id"
      :report-name="apiFor.name"
      :params="NO_PARAMS"
      :copied="apiCopied"
      @close="apiFor = null"
      @tokens="openTokens"
    />
    <ApiTokensDialog :open="tokensOpen" @close="tokensOpen = false" />
  </AppShell>
</template>

<style scoped src="~/assets/listpage.css" />

<style scoped>
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
  transition: border-color 0.15s, box-shadow 0.15s;
}
a.iconbtn:hover { border-color: var(--accent); box-shadow: var(--shadow-1); }
a.iconbtn:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.right .mini { vertical-align: middle; }
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
