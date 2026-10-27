<script setup lang="ts">
// A notebook's app, full screen (docs/specs/notebooks.md): a thin bar with its name and
// folder, then marimo in run mode, served by the gateway on the same origin. Opening the
// page starts the app's pod when nobody has it open; while the page is open the app's
// WebSocket keeps it alive, and the gateway stops it after a while with nobody on it. The
// app reads Tabularia's data with the rights of whoever saved the version it runs.
//
// /notebook-app?id=N
import { computed, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { ArrowLeft, NotebookPen, PencilRuler, RotateCw } from 'lucide-vue-next'
import { errMessage } from '~/composables/useApi'
import { useFolderPaths } from '~/composables/useFolderPaths'
import { useNotebooks, type NotebookDetail } from '~/composables/useNotebooks'
import { useNotebookSession } from '~/composables/useNotebookSession'
import BrandMark from '~/components/ui/BrandMark.vue'
import NotebookStage from '~/components/notebooks/NotebookStage.vue'

const { t } = useI18n()
const route = useRoute()
const api = useNotebooks()
const folderPaths = useFolderPaths()
const app = useNotebookSession('app')

const notebookId = computed(() => Number(route.query.id) || null)
const detail = ref<NotebookDetail | null>(null)
const loadError = ref('')
// a new value reloads the iframe (the «Reload» button)
const frameKey = ref(0)

const folderPath = computed(() => folderPaths.pathOf(detail.value?.project_id) ?? '')
const frameSrc = computed(() => (notebookId.value != null ? api.frameUrl(notebookId.value, 'app') : ''))

useHead({ title: computed(() => detail.value?.name || 'Tabularia') })

async function open() {
  const id = notebookId.value
  detail.value = null
  loadError.value = ''
  app.leave()
  if (id == null) {
    loadError.value = t('notebook.session.noId')
    return
  }
  try {
    detail.value = await api.get(id)
  } catch (e) {
    loadError.value = errMessage(e)
    return
  }
  await app.start(id)
}

// marimo shows its own «disconnected» once the pod is gone (stopped while the computer
// slept, say): asking for the session again starts it, then the iframe loads anew
async function reload() {
  if (notebookId.value == null) return
  await app.start(notebookId.value)
  frameKey.value++
}

onMounted(() => {
  folderPaths.load()
  open()
})
watch(notebookId, open)
</script>

<template>
  <div class="nbpage">
    <header class="bar">
      <NuxtLink to="/" class="home" :aria-label="t('notebook.session.home')">
        <BrandMark :size="20" />
      </NuxtLink>
      <NotebookPen :size="15" class="kind" aria-hidden="true" />
      <h1 class="name" :title="detail?.name">{{ detail?.name ?? '' }}</h1>
      <span v-if="folderPath" class="where muted" :title="folderPath">{{ folderPath }}</span>
      <div class="acts">
        <button
          v-if="app.phase.value === 'ready'"
          type="button"
          :title="t('notebook.session.reloadTitle')"
          :aria-label="t('notebook.session.reload')"
          @click="reload"
        >
          <RotateCw :size="14" aria-hidden="true" />
        </button>
        <NuxtLink
          v-if="detail?.editable && notebookId != null"
          class="btn"
          :to="`/notebook-edit?id=${notebookId}`"
          :title="t('notebook.session.editTitle')"
        >
          <PencilRuler :size="14" aria-hidden="true" /><span class="lbl">{{ t('notebook.session.edit') }}</span>
        </NuxtLink>
        <NuxtLink v-if="notebookId != null" class="btn" :to="`/notebook?id=${notebookId}`" :title="t('notebook.session.backTitle')">
          <ArrowLeft :size="14" aria-hidden="true" /><span class="lbl">{{ t('notebook.session.back') }}</span>
        </NuxtLink>
      </div>
    </header>

    <NotebookStage v-if="loadError" kind="failed" :title="t('notebook.session.loadFailed')" :detail="loadError">
      <NuxtLink class="btn" to="/">{{ t('notebook.session.home') }}</NuxtLink>
    </NotebookStage>
    <NotebookStage
      v-else-if="app.phase.value === 'idle' || app.phase.value === 'starting'"
      kind="starting"
      :title="t('notebook.session.startingApp')"
      :elapsed="app.elapsed.value"
    />
    <NotebookStage
      v-else-if="app.phase.value === 'failed'"
      kind="failed"
      :title="app.problem.value?.title ?? ''"
      :detail="app.problem.value?.detail"
    >
      <button type="button" class="primary" @click="open"><RotateCw :size="14" aria-hidden="true" /> {{ t('notebook.session.retry') }}</button>
    </NotebookStage>
    <iframe
      v-else
      :key="frameKey"
      class="frame"
      :src="frameSrc"
      :title="t('notebook.session.appFrame', { name: detail?.name ?? '' })"
      allow="clipboard-write; fullscreen"
    />
  </div>
</template>

<style scoped>
.nbpage {
  height: 100vh;
  height: 100dvh;
  display: flex;
  flex-direction: column;
  background: var(--bg);
  overflow: hidden;
}
.bar {
  flex-shrink: 0;
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 44px;
  padding: 4px 12px;
  background: var(--panel);
  border-bottom: 1px solid var(--border);
  min-width: 0;
}
.home { display: inline-flex; align-items: center; border-radius: 6px; }
.home:focus-visible, .btn:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.kind { color: var(--muted); flex-shrink: 0; }
.name {
  margin: 0;
  font-size: 14px;
  font-weight: 600;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  min-width: 0;
  flex: 0 1 auto;
}
.where {
  font-size: 12px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  min-width: 0;
  flex: 1 1 0;
}
.acts { display: flex; align-items: center; gap: 6px; margin-left: auto; flex-shrink: 0; }
.acts button { padding: 4px 9px; }
/* a link that looks like the other buttons */
.btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 10px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--panel-2);
  color: var(--text);
  text-decoration: none;
  white-space: nowrap;
  transition: border-color 0.15s, box-shadow 0.15s;
}
.btn:hover { border-color: var(--accent); box-shadow: var(--shadow-1); }
.frame { flex: 1; min-height: 0; width: 100%; border: 0; display: block; background: var(--bg); }
@media (max-width: 640px) {
  .where { display: none; }
  /* only the icons on a phone; the words stay for the screen readers */
  .lbl {
    position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px;
    overflow: hidden; clip-path: inset(50%); white-space: nowrap; border: 0;
  }
  .btn { padding: 5px 8px; }
}
</style>
