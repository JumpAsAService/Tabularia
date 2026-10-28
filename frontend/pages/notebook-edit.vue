<script setup lang="ts">
// marimo's own editor for a notebook, full screen (docs/specs/notebooks.md): a bar with
// «Save version», «Open app» and «Close editor», then marimo in edit mode, served by the
// gateway on the notebooks' own address (composables/useNotebookFrame.ts says why). The
// editor's pod reads Tabularia's data with the rights of
// whoever edits — the bar says so. One person at a time: a second one is told who is
// editing.
//
// «Save version» makes a new version of the file marimo last saved, with whoever edits as
// its author (the app then reads with their rights). «Close editor» stops the pod; its file
// stays as the notebook's draft. Leaving the page without closing leaves the pod to the
// gateway, which stops it after a while with nobody on it.
//
// /notebook-edit?id=N
import { computed, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { AppWindow, LoaderCircle, NotebookPen, RotateCw, Save, ShieldAlert, X } from 'lucide-vue-next'
import { errMessage } from '~/composables/useApi'
import { usePopover } from '~/composables/usePopover'
import { useFolderPaths } from '~/composables/useFolderPaths'
import { personName, useNotebooks, type NotebookDetail } from '~/composables/useNotebooks'
import { useNotebookFrame } from '~/composables/useNotebookFrame'
import { useNotebookSession } from '~/composables/useNotebookSession'
import BrandMark from '~/components/ui/BrandMark.vue'
import NotebookStage from '~/components/notebooks/NotebookStage.vue'

const { t } = useI18n()
const toast = useToast()
const route = useRoute()
const api = useNotebooks()
const folderPaths = useFolderPaths()
const edit = useNotebookSession('edit')
const frame = useNotebookFrame('edit')

const notebookId = computed(() => Number(route.query.id) || null)
const detail = ref<NotebookDetail | null>(null)
const loadError = ref('')

const folderPath = computed(() => folderPaths.pathOf(detail.value?.project_id) ?? '')
const ready = computed(() => edit.phase.value === 'ready')
// VIEW only (an observer, a reader of the folder): the app, never the editor
const readOnly = computed(() => !!detail.value && !detail.value.editable)
const busyTitle = computed(() => {
  const who = personName(edit.editor.value)
  return who ? t('notebook.session.busy', { who }) : (edit.problem.value?.detail ?? '')
})

useHead({ title: computed(() => (detail.value ? t('notebook.session.editTabTitle', { name: detail.value.name }) : 'Tabularia')) })

async function open() {
  const id = notebookId.value
  detail.value = null
  loadError.value = ''
  edit.leave()
  frame.leave()
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
  if (!detail.value.editable) return
  await edit.start(id)
}

// the editor ready: a fresh ticket for its frame, every time (a ticket opens it once)
watch(() => edit.phase.value, (phase) => {
  if (phase === 'ready' && notebookId.value != null) frame.open(notebookId.value)
  else frame.leave()
})

function openApp() {
  if (notebookId.value == null) return
  window.open(`/notebook-app?id=${notebookId.value}`, '_blank', 'noopener')
}

// ── Save version (with an optional note) ──────────────────────────────────────
const saveOpen = ref(false)
const savePop = ref<HTMLElement | null>(null)
usePopover(saveOpen, savePop, () => { saveOpen.value = false })
const note = ref('')
const saving = ref(false)

async function saveVersion() {
  if (notebookId.value == null || saving.value || !ready.value) return
  saving.value = true
  const before = detail.value?.current_version ?? 0
  try {
    const d = await api.saveFromEditor(notebookId.value, note.value.trim() || null)
    detail.value = d
    saveOpen.value = false
    note.value = ''
    if (d.current_version > before) toast.success(t('notebook.session.versionSaved', { n: d.current_version }))
    else toast.info(t('notebook.session.nothingToSave', { n: d.current_version }))
  } catch (e) {
    toast.error(errMessage(e))
  } finally {
    saving.value = false
  }
}

// ── Close editor ──────────────────────────────────────────────────────────────
const closing = ref(false)
async function closeEditor() {
  if (notebookId.value == null || closing.value) return
  const id = notebookId.value
  closing.value = true
  try {
    await api.stopSession(id, 'edit')
  } catch (e: any) {
    // already gone (stopped for inactivity): there is nothing to close
    if ((e?.response?.status ?? e?.statusCode) !== 404) {
      toast.error(errMessage(e))
      closing.value = false
      return
    }
  }
  edit.leave()
  await navigateTo(`/notebook?id=${id}`)
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
      <p v-if="ready" class="editing" role="note">
        <ShieldAlert :size="14" aria-hidden="true" />
        <span>{{ t('notebook.session.editingNote') }}</span>
      </p>
      <div class="acts">
        <div v-if="ready" class="savewrap">
          <button
            type="button"
            class="primary"
            aria-haspopup="dialog"
            :aria-expanded="saveOpen"
            :disabled="saving"
            :title="t('notebook.session.saveTitle')"
            @click="saveOpen = !saveOpen"
          >
            <LoaderCircle v-if="saving" :size="14" class="spin" aria-hidden="true" />
            <Save v-else :size="14" aria-hidden="true" />
            <span class="lbl">{{ t('notebook.session.save') }}</span>
          </button>
          <div v-if="saveOpen" class="backdrop" aria-hidden="true" @click="saveOpen = false" />
          <div
            v-if="saveOpen"
            ref="savePop"
            class="savepop"
            role="dialog"
            :aria-label="t('notebook.session.save')"
          >
            <label class="field">
              <span>{{ t('notebook.session.noteLabel') }}</span>
              <input
                v-model="note"
                type="text"
                maxlength="500"
                :placeholder="t('notebook.session.notePlaceholder')"
                @keydown.enter.prevent="saveVersion"
              />
            </label>
            <p class="muted small">{{ t('notebook.session.saveHint') }}</p>
            <button type="button" class="primary" :disabled="saving" @click="saveVersion">
              <LoaderCircle v-if="saving" :size="14" class="spin" aria-hidden="true" />
              {{ t('notebook.session.saveConfirm') }}
            </button>
          </div>
        </div>
        <button v-if="ready" type="button" :title="t('notebook.session.reloadTitle')" :aria-label="t('notebook.session.reload')"
                @click="notebookId != null && edit.start(notebookId)">
          <RotateCw :size="14" aria-hidden="true" />
        </button>
        <button v-if="detail" type="button" :title="t('notebook.session.openAppTitle')" @click="openApp">
          <AppWindow :size="14" aria-hidden="true" /><span class="lbl">{{ t('notebook.openApp') }}</span>
        </button>
        <button
          v-if="detail?.editable && edit.phase.value !== 'busy'"
          type="button"
          :disabled="closing"
          :title="t('notebook.session.closeTitle')"
          @click="closeEditor"
        >
          <LoaderCircle v-if="closing" :size="14" class="spin" aria-hidden="true" />
          <X v-else :size="14" aria-hidden="true" />
          <span class="lbl">{{ t('notebook.session.close') }}</span>
        </button>
        <NuxtLink v-else-if="notebookId != null" class="btn" :to="`/notebook?id=${notebookId}`">
          <span>{{ t('notebook.session.back') }}</span>
        </NuxtLink>
      </div>
    </header>
    <p v-if="frame.sameOrigin.value" class="sameorigin" role="note">
      <ShieldAlert :size="14" aria-hidden="true" />
      <span>{{ t('notebook.session.sameOrigin') }}</span>
    </p>

    <NotebookStage v-if="loadError" kind="failed" :title="t('notebook.session.loadFailed')" :detail="loadError">
      <NuxtLink class="btn" to="/">{{ t('notebook.session.home') }}</NuxtLink>
    </NotebookStage>
    <NotebookStage v-else-if="readOnly" kind="info" :title="t('notebook.session.viewOnlyTitle')" :detail="t('notebook.session.viewOnly')">
      <button type="button" class="primary" @click="openApp"><AppWindow :size="14" aria-hidden="true" /> {{ t('notebook.openApp') }}</button>
    </NotebookStage>
    <NotebookStage v-else-if="edit.phase.value === 'busy'" kind="info" :title="busyTitle" :detail="t('notebook.session.busyHelp')">
      <button type="button" class="primary" @click="openApp"><AppWindow :size="14" aria-hidden="true" /> {{ t('notebook.openApp') }}</button>
      <NuxtLink v-if="notebookId != null" class="btn" :to="`/notebook?id=${notebookId}`">{{ t('notebook.session.back') }}</NuxtLink>
    </NotebookStage>
    <NotebookStage
      v-else-if="edit.phase.value === 'failed'"
      kind="failed"
      :title="edit.problem.value?.title ?? ''"
      :detail="edit.problem.value?.detail"
    >
      <button type="button" class="primary" @click="open"><RotateCw :size="14" aria-hidden="true" /> {{ t('notebook.session.retry') }}</button>
      <NuxtLink v-if="notebookId != null" class="btn" :to="`/notebook?id=${notebookId}`">{{ t('notebook.session.back') }}</NuxtLink>
    </NotebookStage>
    <NotebookStage
      v-else-if="!ready"
      kind="starting"
      :title="t('notebook.session.startingEditor')"
      :elapsed="edit.elapsed.value"
    />
    <NotebookStage
      v-else-if="frame.problem.value"
      kind="failed"
      :title="t('notebook.session.frameFailedTitle')"
      :detail="frame.problem.value"
    >
      <button type="button" class="primary" @click="notebookId != null && frame.open(notebookId)">
        <RotateCw :size="14" aria-hidden="true" /> {{ t('notebook.session.retry') }}
      </button>
    </NotebookStage>
    <NotebookStage v-else-if="!frame.src.value" kind="starting" :title="t('notebook.session.startingEditor')" />
    <iframe
      v-else
      class="frame"
      :src="frame.src.value"
      :title="t('notebook.session.editFrame', { name: detail?.name ?? '' })"
      allow="clipboard-read; clipboard-write; fullscreen"
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
/* whose rights the editor reads with: always in sight while editing */
.editing {
  margin: 0;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--warning);
  padding: 3px 9px;
  border: 1px solid color-mix(in srgb, var(--warning) 45%, var(--border));
  border-radius: 8px;
  background: color-mix(in srgb, var(--warning) 8%, transparent);
  min-width: 0;
}
.editing span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.editing svg { flex-shrink: 0; }
.acts { display: flex; align-items: center; gap: 6px; margin-left: auto; flex-shrink: 0; }
.acts button { padding: 4px 10px; white-space: nowrap; }
.savewrap { position: relative; }
/* over the iframe too: a click on marimo closes the panel instead of being lost in it */
.backdrop { position: fixed; inset: 0; z-index: 20; }
.savepop {
  position: absolute;
  top: calc(100% + 6px);
  right: 0;
  z-index: 21;
  width: 300px;
  max-width: calc(100vw - 24px);
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 10px;
  box-shadow: 0 8px 24px rgb(0 0 0 / 0.18);
}
.savepop p { margin: 0; }
.field { display: flex; flex-direction: column; gap: 5px; font-size: 12px; color: var(--muted); }
.field input { width: 100%; }
.small { font-size: 12px; }
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
/* NOTEBOOKS__ALLOW_SAME_ORIGIN: always in sight while the notebook is open */
.sameorigin {
  flex-shrink: 0;
  margin: 0;
  display: flex;
  align-items: flex-start;
  gap: 6px;
  padding: 6px 12px;
  font-size: 12px;
  color: var(--warning);
  background: color-mix(in srgb, var(--warning) 8%, var(--panel));
  border-bottom: 1px solid color-mix(in srgb, var(--warning) 45%, var(--border));
}
.sameorigin svg { flex-shrink: 0; margin-top: 2px; }
@media (max-width: 1100px) {
  /* the note takes its own row under the name and the buttons */
  .bar { flex-wrap: wrap; }
  .editing { order: 3; flex-basis: 100%; }
  .editing span { white-space: normal; }
}
@media (max-width: 640px) {
  .where { display: none; }
  /* only the icons on a phone; the words stay for the screen readers */
  .lbl {
    position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px;
    overflow: hidden; clip-path: inset(50%); white-space: nowrap; border: 0;
  }
  .acts button { padding: 5px 8px; }
}
</style>
