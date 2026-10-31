<script setup lang="ts">
// A dashboard on screen (docs/specs/dashboards.md). For now only the object: its name, its
// folder, and whether it holds anything; the grid and its numbers come with the viewer.
//
// /dashboard?id=N
import { computed, ref, watch } from 'vue'
import { FolderTree, LayoutDashboard, RefreshCw, TriangleAlert } from 'lucide-vue-next'
import { errMessage } from '~/composables/useApi'
import { useFolderPaths } from '~/composables/useFolderPaths'
import { useDashboards, type DashboardDetail } from '~/composables/useDashboards'

const route = useRoute()
const api = useDashboards()
const folderPaths = useFolderPaths()

const detail = ref<DashboardDetail | null>(null)
const loading = ref(true)
const notFound = ref(false)
const loadError = ref('')
const folderPath = computed(() => (detail.value ? folderPaths.pathOf(detail.value.project_id) ?? '' : ''))

async function load() {
  const id = Number(route.query.id)
  detail.value = null
  notFound.value = false
  loadError.value = ''
  if (!Number.isInteger(id) || id <= 0) {
    notFound.value = true
    loading.value = false
    return
  }
  loading.value = true
  try {
    const found = await api.get(id)
    if (Number(route.query.id) !== id) return
    detail.value = found
    // the audit and the count of who reads it; never in the way of reading
    api.opened(id).catch(() => {})
  } catch (e: any) {
    if ((e?.response?.status ?? e?.statusCode) === 404) notFound.value = true
    else loadError.value = errMessage(e)
  } finally {
    loading.value = false
  }
}

// another page's address is not this page's business: no «not found» on the way out
watch(() => route.query.id, () => { if (route.path === '/dashboard') load() }, { immediate: true })
</script>

<template>
  <AppShell>
    <div class="dv">
      <!-- a dashboard that is not there, or not for this person: the same answer -->
      <div v-if="notFound" class="gone" role="alert">
        <LayoutDashboard :size="28" aria-hidden="true" />
        <h1>{{ $t('dashboard.notFoundTitle') }}</h1>
        <p>{{ $t('dashboard.notFoundBody') }}</p>
        <NuxtLink to="/" class="back">{{ $t('dashboard.backToExplore') }}</NuxtLink>
      </div>
      <div v-else-if="loadError" class="gone" role="alert">
        <TriangleAlert :size="28" aria-hidden="true" />
        <h1>{{ $t('dashboard.loadFailed') }}</h1>
        <p>{{ loadError }}</p>
        <button type="button" class="retry" @click="load"><RefreshCw :size="14" aria-hidden="true" /> {{ $t('dashboard.tryAgain') }}</button>
      </div>

      <template v-else-if="detail">
        <header class="head">
          <h1 :title="detail.name"><LayoutDashboard :size="18" aria-hidden="true" /><span class="name">{{ detail.name }}</span></h1>
          <NuxtLink v-if="folderPath" :to="`/?folder=${detail.project_id}`" class="where">
            <FolderTree :size="13" aria-hidden="true" /> {{ folderPath }}
          </NuxtLink>
        </header>
        <p v-if="detail.description" class="description">{{ detail.description }}</p>

        <div class="empty">
          <LayoutDashboard :size="32" aria-hidden="true" />
          <template v-if="detail.tiles === 0">
            <h2>{{ $t('dashboard.emptyTitle') }}</h2>
            <p>{{ $t('dashboard.emptyBody') }}</p>
          </template>
          <p v-else>{{ $t('dashboard.tilesSaved', { n: detail.tiles }) }}</p>
        </div>
      </template>

      <p v-else-if="loading" class="loading" role="status">{{ $t('dashboard.loading') }}</p>
    </div>
  </AppShell>
</template>

<style scoped>
.dv { display: flex; flex-direction: column; gap: 12px; min-width: 0; }
.head { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; min-width: 0; }
.head h1 { display: flex; align-items: center; gap: 8px; margin: 0; font-size: 17px; font-weight: 600; min-width: 0; color: var(--text); }
.head h1 svg { flex-shrink: 0; color: var(--accent); }
.name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.where { display: inline-flex; align-items: center; gap: 5px; font-size: 12px; color: var(--muted); }
.where:hover { color: var(--text); }
.description { margin: 0; color: var(--muted); line-height: 1.5; }
.empty {
  margin-top: 8px; padding: 56px 24px; display: flex; flex-direction: column; align-items: center; gap: 8px; text-align: center;
  color: var(--muted); border: 1px dashed var(--border); border-radius: var(--radius, 8px);
}
.empty h2 { margin: 6px 0 0; font-size: 15px; font-weight: 600; color: var(--text); }
.empty p { margin: 0; line-height: 1.5; }
.gone { margin: 12vh auto 0; max-width: 520px; display: flex; flex-direction: column; align-items: center; gap: 8px; text-align: center; color: var(--muted); }
.gone h1 { margin: 6px 0 0; font-size: 17px; color: var(--text); }
.gone p { margin: 0; line-height: 1.5; }
.back { margin-top: 8px; color: var(--accent-hi); }
.retry { display: inline-flex; align-items: center; gap: 6px; margin-top: 8px; }
.loading { color: var(--muted); }
</style>
