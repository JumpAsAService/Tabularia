<script setup lang="ts">
// A notebook: a marimo notebook kept in a folder, with its versions, published as a web
// app (docs/specs/notebooks.md). The app reads Tabularia's data with the rights of whoever
// saved the version it runs: the page says who. A save that changes the source is a new
// version; an older one can be looked at and restored, as the newest, by whoever restores
// it. The source is written in marimo's own editor (/notebook-edit, for whoever has EDIT)
// or here, in the code editor of saved scripts — here only while nobody has marimo's editor
// open, so that the two never overwrite each other. The app opens on its own page
// (/notebook-app). Both run on Kubernetes: the page shows the live ones.
//
// /notebook?id=N opens a notebook; /notebook?project=P starts a new one in folder P.
import { computed, defineAsyncComponent, onMounted, onUnmounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { onBeforeRouteLeave, onBeforeRouteUpdate } from 'vue-router'
import {
  AppWindow, Code2, History, LoaderCircle, NotebookPen, PencilRuler, Power, Radio, RotateCcw, Save, ShieldCheck,
  TriangleAlert,
} from 'lucide-vue-next'
import { errMessage } from '~/composables/useApi'
import { useFormat } from '~/composables/useFormat'
import { useFolderPaths } from '~/composables/useFolderPaths'
import { useScriptingEditor } from '~/composables/useScripting'
import { versionNote } from '~/composables/useSavedScripts'
import {
  personName, useNotebooks, type NotebookDetail, type NotebookSession, type NotebookVersion,
} from '~/composables/useNotebooks'
import Select from '~/components/ui/Select.vue'

// Monaco weighs a few MB: loaded when the editor opens, not with the page
const ScriptEditor = defineAsyncComponent(() => import('~/components/scripting/ScriptEditor.vue'))

// the one file of a notebook, in the code editor
const FILE = 'notebook.py'

const { t } = useI18n()
const fmtx = useFormat()
const toast = useToast()
const route = useRoute()
const api = useNotebooks()
const scripting = useScriptingEditor()
const folderPaths = useFolderPaths()
const { user } = useAuth()

const notebookId = ref<number | null>(Number(route.query.id) || null)
const projectId = ref<number | null>(Number(route.query.project) || null)
const detail = ref<NotebookDetail | null>(null)
const loading = ref(true)
const loadError = ref('')
const saving = ref(false)

// what is on screen; `savedAs` is the same thing as last saved, to tell unsaved changes
const name = ref('')
const description = ref('')
const environmentId = ref<number | null>(null)
const source = ref('')
const savedAs = ref('')
const snapshot = () => JSON.stringify([name.value.trim(), description.value.trim(), environmentId.value, source.value])
const dirty = computed(() => snapshot() !== savedAs.value)

const canEdit = computed(() => (detail.value ? detail.value.editable : true))
const folderPath = computed(() => folderPaths.pathOf(projectId.value) ?? '')
// marimo is Python: only Python environments can hold a notebook
const environments = computed(() => (scripting.state.value?.environments ?? []).filter((e) => e.language === 'python'))
const environment = computed(() => environments.value.find((e) => e.id === environmentId.value) ?? null)
const envOptions = computed(() => environments.value.map((e) => ({
  value: e.id,
  label: e.name,
  hint: `Python ${e.language_version ?? ''}${e.ready ? '' : ` · ${t('scripting.node.notReady')}`}`,
})))
const lines = computed(() => (source.value ? source.value.split('\n').length : 0))
const author = computed(() => {
  const a = detail.value?.author
  return a ? (a.full_name.trim() || a.email) : ''
})

function apply(d: NotebookDetail) {
  detail.value = d
  notebookId.value = d.id
  projectId.value = d.project_id
  name.value = d.name
  description.value = d.description
  environmentId.value = d.environment_id
  source.value = d.content.source
  savedAs.value = snapshot()
}

async function load() {
  if (notebookId.value == null) {
    // a new notebook: the first Python environment one may use; the gateway writes its
    // starting source when it is created
    try {
      await scripting.load(true)
      environmentId.value = environments.value[0]?.id ?? null
      source.value = ''
    } finally {
      loading.value = false
    }
    savedAs.value = ''
    return
  }
  loading.value = true
  loadError.value = ''
  try {
    const [d] = await Promise.all([api.get(notebookId.value), scripting.load(true)])
    apply(d)
    await Promise.all([loadVersions(), loadSessions()])
  } catch (e) {
    loadError.value = errMessage(e)
  } finally {
    loading.value = false
  }
}

async function save() {
  if (saving.value || !canEdit.value) return
  const n = name.value.trim()
  if (!n) {
    toast.error(t('notebook.nameRequired'))
    return
  }
  if (environmentId.value == null) {
    toast.error(t('notebook.environmentRequired'))
    return
  }
  saving.value = true
  try {
    if (notebookId.value != null) {
      apply(await api.update(notebookId.value, {
        name: n, description: description.value.trim(), environment_id: environmentId.value,
        content: { source: source.value },
      }))
      await loadVersions()
      toast.success(t('notebook.saved', { name: n }))
    } else if (projectId.value != null) {
      const d = await api.create(projectId.value, {
        name: n, description: description.value.trim(), environment_id: environmentId.value,
      })
      apply(d)
      navigateTo(`/notebook?id=${d.id}`, { replace: true })
      await loadVersions()
      toast.success(t('notebook.created', { name: n }))
    }
  } catch (e) {
    toast.error(errMessage(e))
  } finally {
    saving.value = false
  }
}

// ── the live app and editor (Kubernetes) ─────────────────────────────────────
const sessions = ref<NotebookSession[]>([])
// a failed editor holds nothing: the source here stays editable
const editSession = computed(() => sessions.value.find((s) => s.mode === 'edit' && s.status !== 'failed') ?? null)
const editMine = computed(() => !!editSession.value && editSession.value.user.id === user.value?.id)
// marimo's editor is open: the source is written there, here it is read-only
const editLive = computed(() => !!editSession.value)
const runtimeOff = computed(() => !!scripting.state.value?.disabled_reason)
const stoppingApp = ref(false)

async function loadSessions() {
  if (notebookId.value == null || runtimeOff.value) {
    sessions.value = []
    return
  }
  // a help, not a requirement: without it the page shows no live session
  try { sessions.value = await api.sessions(notebookId.value) } catch { sessions.value = [] }
}

const openAppReason = computed(() => (runtimeOff.value ? t('notebook.off') : t('notebook.openAppTitle')))
const openEditorBlocked = computed(() => runtimeOff.value || (!!editSession.value && !editMine.value) || dirty.value)
const openEditorReason = computed(() => {
  if (runtimeOff.value) return t('notebook.off')
  if (editSession.value && !editMine.value) return t('notebook.beingEditedBy', { who: personName(editSession.value.user) })
  if (dirty.value) return t('notebook.saveBeforeEditor')
  return t('notebook.openEditorTitle')
})

function openApp() {
  if (notebookId.value == null || runtimeOff.value) return
  window.open(`/notebook-app?id=${notebookId.value}`, '_blank', 'noopener')
}
function openMarimo() {
  if (notebookId.value == null || openEditorBlocked.value) return
  navigateTo(`/notebook-edit?id=${notebookId.value}`)
}

// the app keeps the version it started with: stopping it lets the next opening run the
// current one
async function stopApp() {
  if (notebookId.value == null || stoppingApp.value || !confirm(t('notebook.stopAppConfirm'))) return
  stoppingApp.value = true
  try {
    await api.stopSession(notebookId.value, 'app')
    toast.success(t('notebook.appStopped'))
  } catch (e) {
    toast.error(errMessage(e))
  } finally {
    stoppingApp.value = false
    await loadSessions()
  }
}

function sessionLine(s: NotebookSession): string {
  const who = personName(s.user)
  if (s.mode === 'app') {
    if (s.status === 'starting') return t('notebook.appStarting', { who })
    if (s.status === 'failed') return t('notebook.appFailed', { error: s.error || '' })
    return t('notebook.appRunning', { who, since: fmtx.dateTime(s.started_at) })
  }
  if (s.status === 'failed') return t('notebook.editFailed', { error: s.error || '' })
  return s.user.id === user.value?.id ? t('notebook.editedByYou', { since: fmtx.dateTime(s.started_at) })
    : t('notebook.beingEditedBy', { who })
}

// back to this tab from the app's or the editor's: what runs may have changed, and a
// version saved in marimo's editor moves the current one on
function onVisible() {
  if (document.visibilityState !== 'visible' || loading.value || notebookId.value == null) return
  loadSessions()
  if (!dirty.value && !editorOpen.value && !saving.value) {
    const id = notebookId.value
    api.get(id).then((d) => { if (id === notebookId.value && !dirty.value) apply(d) }).catch(() => {})
    loadVersions()
  }
}

// ── the code editor ──────────────────────────────────────────────────────────
const editorOpen = ref(false)
// the version looked at in the editor, read-only; null = the source being edited
const viewing = ref<{ version: number; source: string } | null>(null)

async function openEditor() {
  await scripting.load(true) // variables and packages may have changed since
  viewing.value = null
  editorOpen.value = true
}

function onCode(patch: { files: { path: string; content: string }[] }) {
  if (viewing.value || editLive.value) return
  const file = patch.files.find((f) => f.path === FILE)
  if (file) source.value = file.content
}

// ── the editor's draft (written by marimo's editor; shown to whoever may edit) ──
const discarding = ref(false)
function useDraft() {
  if (detail.value?.draft == null || editLive.value) return
  source.value = detail.value.draft
}
async function discardDraft() {
  if (notebookId.value == null || discarding.value || !confirm(t('notebook.discardConfirm'))) return
  discarding.value = true
  try {
    const d = await api.discardDraft(notebookId.value)
    // only the draft changed: unsaved edits stay on screen
    if (detail.value) detail.value = { ...detail.value, draft: d.draft, draft_by: d.draft_by, draft_at: d.draft_at }
    toast.success(t('notebook.draftDiscarded'))
  } catch (e) {
    toast.error(errMessage(e))
  } finally {
    discarding.value = false
  }
}

// ── versions ─────────────────────────────────────────────────────────────────
const versions = ref<NotebookVersion[]>([])
const restoring = ref<number | null>(null)

async function loadVersions() {
  if (notebookId.value == null) return
  try { versions.value = await api.versions(notebookId.value) } catch { versions.value = [] }
}

async function viewVersion(v: NotebookVersion) {
  if (notebookId.value == null) return
  try {
    const c = await api.version(notebookId.value, v.version)
    viewing.value = { version: v.version, source: c.source }
    editorOpen.value = true
  } catch (e) {
    toast.error(errMessage(e))
  }
}

async function restore(v: NotebookVersion) {
  if (notebookId.value == null || restoring.value != null) return
  if (dirty.value && !confirm(t('notebook.restoreDiscards'))) return
  if (!confirm(t('notebook.restoreConfirm', { n: v.version }))) return
  restoring.value = v.version
  try {
    apply(await api.restore(notebookId.value, v.version))
    await loadVersions()
    toast.success(t('notebook.restored', { n: v.version }))
  } catch (e) {
    toast.error(errMessage(e))
  } finally {
    restoring.value = null
  }
}

// Ctrl/Cmd+S saves, also with the code editor open (it hands over its last keys first)
function onKey(e: KeyboardEvent) {
  if ((e.ctrlKey || e.metaKey) && !e.altKey && e.key.toLowerCase() === 's') {
    e.preventDefault()
    if (!viewing.value && (dirty.value || notebookId.value == null)) save()
  }
}
// closing the tab or reloading asks too, as the flow editor does
function onBeforeUnload(e: BeforeUnloadEvent) {
  if (!dirty.value || !canEdit.value) return
  e.preventDefault()
  e.returnValue = ''
}
onMounted(() => {
  document.addEventListener('keydown', onKey)
  document.addEventListener('visibilitychange', onVisible)
  window.addEventListener('beforeunload', onBeforeUnload)
  folderPaths.load()
  load()
})
onUnmounted(() => {
  document.removeEventListener('keydown', onKey)
  document.removeEventListener('visibilitychange', onVisible)
  window.removeEventListener('beforeunload', onBeforeUnload)
})
onBeforeRouteLeave(() => !dirty.value || !canEdit.value || confirm(t('notebook.leaveConfirm')))
// another notebook from the search: the same page with another id
onBeforeRouteUpdate((to) => (Number(to.query.id) || null) === notebookId.value || !dirty.value || !canEdit.value
  || confirm(t('notebook.leaveConfirm')))
watch(() => route.query.id, (id) => {
  const next = Number(id) || null
  if (next === notebookId.value) return // a notebook just created took its address
  notebookId.value = next
  versions.value = []
  sessions.value = []
  load()
})

const unavailable = computed(() => {
  const s = scripting.state.value
  if (!s) return ''
  if (s.disabled_reason) return t('notebook.off')
  if (!environments.value.length) return t('notebook.noEnvironment')
  if (environmentId.value != null && !environment.value) return t('scripting.node.environmentGone')
  return ''
})
</script>

<template>
  <AppShell>
    <div class="nb">
      <div class="page-head">
        <h1>
          <NotebookPen :size="18" />
          <input
            v-model="name"
            class="nname"
            type="text"
            maxlength="200"
            :readonly="!canEdit"
            :placeholder="$t('notebook.namePlaceholder')"
            :aria-label="$t('notebook.nameLabel')"
          />
        </h1>
        <span v-if="folderPath" class="where muted">{{ $t('notebook.inFolder', { path: folderPath }) }}</span>
        <div class="head-actions">
          <span v-if="detail" class="tag">v{{ detail.current_version }}</span>
          <span v-if="dirty && canEdit && detail" class="tag unsaved">{{ $t('notebook.unsaved') }}</span>
          <!-- the app opens in a tab of its own; marimo's editor takes this one -->
          <template v-if="detail">
            <button type="button" :disabled="runtimeOff" :title="openAppReason" @click="openApp">
              <AppWindow :size="14" aria-hidden="true" /> {{ $t('notebook.openApp') }}
            </button>
            <button v-if="canEdit" type="button" :disabled="openEditorBlocked" :title="openEditorReason" @click="openMarimo">
              <PencilRuler :size="14" aria-hidden="true" /> {{ $t('notebook.openEditor') }}
            </button>
          </template>
          <button v-if="canEdit" class="primary" :disabled="saving || loading || (!dirty && notebookId != null) || !!unavailable" @click="save">
            <LoaderCircle v-if="saving" :size="14" class="spin" />
            <Save v-else :size="14" />
            {{ notebookId == null ? $t('notebook.create') : $t('notebook.save') }}
          </button>
        </div>
      </div>

      <p v-if="loadError" class="err" role="alert">{{ loadError }}</p>
      <SkeletonRows v-else-if="loading" :rows="5" />

      <div v-else class="work">
        <section class="main">
          <p v-if="unavailable" class="warn" role="note"><TriangleAlert :size="13" /> {{ unavailable }}</p>

          <!-- what runs now: the app (read as its author) and marimo's editor -->
          <div v-if="sessions.length" class="card live">
            <h2><Radio :size="15" aria-hidden="true" /> {{ $t('notebook.liveTitle') }}</h2>
            <ul class="sessions">
              <li v-for="s in sessions" :key="s.mode" :class="s.status">
                <span class="dot" aria-hidden="true" />
                <span class="small">{{ sessionLine(s) }}</span>
                <button v-if="s.mode === 'app' && canEdit && s.status !== 'failed'" type="button" class="mini"
                        :disabled="stoppingApp" :title="$t('notebook.stopAppTitle')" @click="stopApp">
                  <LoaderCircle v-if="stoppingApp" :size="12" class="spin" aria-hidden="true" /><Power v-else :size="12" aria-hidden="true" />
                  {{ $t('notebook.stopApp') }}
                </button>
              </li>
            </ul>
          </div>

          <!-- whose rights the app reads with: the author of the version it runs -->
          <div class="card rights">
            <h2><ShieldCheck :size="15" /> {{ $t('notebook.rightsTitle') }}</h2>
            <p v-if="!detail" class="small">{{ $t('notebook.rightsNew') }}</p>
            <p v-else-if="author" class="small">{{ $t('notebook.readsAs', { name: author }) }}</p>
            <p v-else class="small warn-inline">{{ $t('notebook.noAuthor') }}</p>
            <p v-if="detail && canEdit" class="muted small">{{ $t('notebook.rightsOnSave') }}</p>
          </div>

          <div class="card">
            <label class="field">
              <span>{{ $t('notebook.environment') }}</span>
              <Select
                v-model="environmentId"
                :options="envOptions"
                :placeholder="$t('scripting.node.chooseEnvironment')"
                :disabled="!canEdit"
              />
            </label>
            <label class="field">
              <span>{{ $t('notebook.description') }}</span>
              <textarea v-model="description" rows="2" maxlength="2000" :readonly="!canEdit"
                        :placeholder="$t('notebook.descriptionPlaceholder')" />
            </label>
          </div>

          <div v-if="detail?.draft != null" class="card draft" role="note">
            <p class="small">
              <TriangleAlert :size="13" />
              {{ detail.draft_by && detail.draft_at
                ? $t('notebook.draftNotice', { who: detail.draft_by, when: fmtx.dateTime(detail.draft_at) })
                : $t('notebook.draftNoticeAnon') }}
            </p>
            <div class="draft-acts">
              <button type="button" class="mini" :disabled="source === detail.draft || editLive" @click="useDraft">{{ $t('notebook.useDraft') }}</button>
              <button type="button" class="mini" :disabled="discarding" @click="discardDraft">
                <LoaderCircle v-if="discarding" :size="12" class="spin" /> {{ $t('notebook.discardDraft') }}
              </button>
            </div>
          </div>

          <div class="card code">
            <div class="code-head">
              <h2><Code2 :size="15" /> {{ $t('notebook.source') }}</h2>
              <button v-if="notebookId != null" type="button" :class="{ primary: !editLive }" :disabled="!environment" @click="openEditor">
                <Code2 :size="14" /> {{ canEdit && !editLive ? $t('scripting.node.editCode') : $t('scripting.node.viewCode') }}
              </button>
            </div>
            <p class="muted small help">{{ $t('notebook.sourceHelp') }}</p>
            <p v-if="editLive && canEdit" class="small locked" role="note">
              <TriangleAlert :size="13" aria-hidden="true" />
              <span>
                {{ editMine ? $t('notebook.lockedMine') : $t('notebook.lockedOther', { who: personName(editSession?.user) }) }}
                <button v-if="editMine" type="button" class="linkish" :disabled="dirty" @click="openMarimo">{{ $t('notebook.openInEditor') }}</button>
              </span>
            </p>
            <p v-if="notebookId == null" class="muted small">{{ $t('notebook.starterNote') }}</p>
            <p v-else class="small"><code>{{ FILE }}</code> · <span class="muted">{{ $t('notebook.lines', { n: lines }, lines) }}</span></p>
          </div>
        </section>

        <aside v-if="notebookId != null" class="side card" :aria-label="$t('notebook.versions')">
          <h2><History :size="15" /> {{ $t('notebook.versions') }}</h2>
          <ol class="versions">
            <li v-for="v in versions" :key="v.version" :class="{ current: v.current }">
              <div class="vtop">
                <strong>v{{ v.version }}</strong>
                <span v-if="v.current" class="tag">{{ $t('notebook.currentVersion') }}</span>
                <span class="muted small">{{ versionNote(v.note, t) }}</span>
              </div>
              <div class="muted small">{{ fmtx.dateTime(v.created_at) }}<template v-if="v.created_by"> · {{ v.created_by }}</template></div>
              <div class="vacts">
                <button type="button" class="mini" :disabled="!environment" @click="viewVersion(v)">{{ $t('notebook.viewVersion') }}</button>
                <button v-if="canEdit && !v.current" type="button" class="mini" :disabled="restoring != null || editLive"
                        :title="editLive ? $t('notebook.restoreWhileEditing') : undefined" @click="restore(v)">
                  <LoaderCircle v-if="restoring === v.version" :size="12" class="spin" /><RotateCcw v-else :size="12" />
                  {{ $t('notebook.restore') }}
                </button>
              </div>
            </li>
          </ol>
        </aside>
      </div>

      <ScriptEditor
        v-if="editorOpen && environment"
        :open="editorOpen"
        :title="viewing ? `${name} · v${viewing.version}` : (name || t('notebook.newNotebook'))"
        :environment="environment"
        :files="[{ path: FILE, content: viewing ? viewing.source : source }]"
        :entrypoint="FILE"
        :tabularia-variables="[]"
        :read-only="!canEdit || !!viewing || editLive"
        output="none"
        saved
        single
        @update="onCode"
        @close="editorOpen = false; viewing = null"
      />
    </div>
  </AppShell>
</template>

<style scoped>
.nb { display: flex; flex-direction: column; gap: 12px; min-height: 0; }
.page-head { display: flex; align-items: center; gap: 12px; }
.page-head h1 { display: inline-flex; align-items: center; gap: 8px; margin: 0; flex: 0 1 380px; min-width: 0; }
.nname { font-size: 16px; font-weight: 600; padding: 4px 9px; min-width: 0; }
.nname[readonly] { border-color: transparent; background: transparent; padding-left: 0; }
.where { font-size: 12.5px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; min-width: 0; flex: 1; }
.head-actions { display: flex; align-items: center; gap: 8px; margin-left: auto; flex-shrink: 0; }
.head-actions button { display: inline-flex; align-items: center; gap: 6px; white-space: nowrap; }
.tag {
  font-size: 10px; text-transform: uppercase; letter-spacing: 0.04em; padding: 1px 7px;
  border-radius: 8px; background: var(--panel-2); border: 1px solid var(--border); color: var(--muted);
}
.err { color: var(--danger); }
.warn { margin: 0; display: flex; gap: 6px; align-items: flex-start; font-size: 12.5px; color: var(--warning); }
.warn-inline { color: var(--warning); }
.work { display: grid; grid-template-columns: minmax(0, 1fr) 300px; gap: 16px; align-items: start; }
.main { display: flex; flex-direction: column; gap: 12px; min-width: 0; }
.card { border: 1px solid var(--border-soft); border-radius: 10px; padding: 12px 14px; background: var(--panel); display: flex; flex-direction: column; gap: 10px; }
.card h2 { margin: 0; font-size: 13.5px; display: inline-flex; align-items: center; gap: 7px; }
.card p { margin: 0; }
.field { display: flex; flex-direction: column; gap: 5px; font-size: 12px; color: var(--muted); }
.field textarea { resize: vertical; font: inherit; color: var(--text); }
.code-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.code-head button { display: inline-flex; align-items: center; gap: 6px; }
.small { font-size: 12px; }
.draft { border-color: color-mix(in srgb, var(--warning) 45%, var(--border-soft)); }
.draft p { display: flex; align-items: flex-start; gap: 6px; color: var(--warning); }
.draft-acts { display: flex; gap: 6px; }
.draft-acts .mini, .vacts .mini { display: inline-flex; align-items: center; gap: 4px; }
.sessions { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
.sessions li { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.sessions .small { min-width: 0; overflow-wrap: anywhere; }
.sessions .mini { display: inline-flex; align-items: center; gap: 4px; margin-left: auto; }
.dot { width: 8px; height: 8px; border-radius: 50%; background: var(--accent-2); flex-shrink: 0; }
.sessions .starting .dot { background: var(--warning); }
.sessions .failed .dot { background: var(--danger); }
.sessions .failed .small { color: var(--danger); }
.locked { display: flex; align-items: flex-start; gap: 6px; color: var(--warning); }
.locked svg { flex-shrink: 0; margin-top: 2px; }
.linkish {
  display: inline; background: none; border: 0; padding: 0; color: var(--accent); text-decoration: underline;
  font: inherit; cursor: pointer; box-shadow: none;
}
.linkish:hover { box-shadow: none; }
.versions { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; max-height: 60vh; overflow: auto; }
.versions li { border: 1px solid var(--border-soft); border-radius: 8px; padding: 7px 9px; display: flex; flex-direction: column; gap: 3px; }
.versions li.current { border-color: var(--accent); }
.vtop { display: flex; align-items: center; gap: 7px; flex-wrap: wrap; }
.vacts { display: flex; gap: 6px; margin-top: 3px; }
@media (max-width: 900px) {
  .work { grid-template-columns: minmax(0, 1fr); }
  .page-head { flex-wrap: wrap; }
  /* four buttons do not fit a phone's width in one row */
  .head-actions { flex-wrap: wrap; flex-shrink: 1; min-width: 0; }
}
</style>
