<script setup lang="ts">
// A saved script: Python or R code kept in a folder, with its versions
// (docs/specs/saved-scripts.md). The code is written in the Script node's editor; a
// save that changes it is a new version, the one that runs; an older one can be
// looked at and restored. It reads the datasources declared as its inputs, each by
// an alias, may leave an output (previewed, and published as a datasource when asked)
// and runs on its own: «Run» queues it, and the page follows it to its end.
//
// /script?id=N opens a script; /script?project=P starts a new one in folder P.
import { computed, defineAsyncComponent, onMounted, onUnmounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { onBeforeRouteLeave, onBeforeRouteUpdate } from 'vue-router'
import {
  CalendarClock, CircleCheck, CircleX, Clock, Code2, Database, FileCode2, History, LoaderCircle, Play, RotateCcw,
  Save, Table2, TriangleAlert, X,
} from 'lucide-vue-next'
import { errMessage } from '~/composables/useApi'
import { FIRST_PAUSE_MS, nextPause } from '~/composables/waits'
import { useFormat } from '~/composables/useFormat'
import { useFolderPaths } from '~/composables/useFolderPaths'
import { useScriptingEditor } from '~/composables/useScripting'
import { starterFiles } from '~/composables/scriptTemplates'
import {
  ALIAS_RE, proposeAlias, savedScriptVariables, useSavedScripts, versionNote,
  type OutputPreview, type SavedScriptDetail, type ScriptContent, type ScriptFile, type ScriptRun, type ScriptVersion,
} from '~/composables/useSavedScripts'
import { useDatasources, type DatasourceInfo } from '~/composables/useDatasources'
import { useProjects, type Project } from '~/composables/useProjects'
import Select from '~/components/ui/Select.vue'

// Monaco weighs a few MB: loaded when the editor opens, not with the page
const ScriptEditor = defineAsyncComponent(() => import('~/components/scripting/ScriptEditor.vue'))

const { t } = useI18n()
const fmtx = useFormat()
const toast = useToast()
const route = useRoute()
const scriptsApi = useSavedScripts()
const scripting = useScriptingEditor()
const folderPaths = useFolderPaths()
const datasourcesApi = useDatasources()
const projectsApi = useProjects()

const scriptId = ref<number | null>(Number(route.query.id) || null)
const projectId = ref<number | null>(Number(route.query.project) || null)
const detail = ref<SavedScriptDetail | null>(null)
const loading = ref(!!scriptId.value)
const loadError = ref('')
const saving = ref(false)

// what is on screen; `savedAs` is the same thing as last saved, to tell unsaved changes
const name = ref('')
const description = ref('')
const environmentId = ref<number | null>(null)
const content = ref<ScriptContent | null>(null)
const savedAs = ref('')
const snapshot = () => JSON.stringify([name.value.trim(), description.value.trim(), environmentId.value, content.value])
const dirty = computed(() => snapshot() !== savedAs.value)

const canEdit = computed(() => (detail.value ? detail.value.editable : true))
const folderPath = computed(() => folderPaths.pathOf(projectId.value) ?? '')
const environments = computed(() => scripting.state.value?.environments ?? [])
const environment = computed(() => environments.value.find((e) => e.id === environmentId.value) ?? null)
const envOptions = computed(() => environments.value.map((e) => ({
  value: e.id,
  label: e.name,
  hint: `${e.language === 'r' ? 'R' : 'Python'} ${e.language_version ?? ''}${e.ready ? '' : ` · ${t('scripting.node.notReady')}`}`,
})))

function apply(d: SavedScriptDetail) {
  detail.value = d
  scriptId.value = d.id
  projectId.value = d.project_id
  name.value = d.name
  description.value = d.description
  environmentId.value = d.environment_id
  content.value = JSON.parse(JSON.stringify(d.content))
  savedAs.value = snapshot()
}

async function load() {
  if (scriptId.value == null) {
    // a new script: the first environment one may use, and its language's template
    loading.value = false
    await scripting.load(true)
    const first = environments.value[0]
    environmentId.value = first?.id ?? null
    content.value = {
      files: first ? starterFiles(first.language) : [], entrypoint: first ? starterFiles(first.language)[0]!.path : '',
      inputs: [], output: { publish: false, name: '', project_id: null, overwrite: true },
      timeout_seconds: 900, retries: 0, retry_delay_seconds: 60,
    }
    savedAs.value = ''
    return
  }
  loading.value = true
  loadError.value = ''
  try {
    const [d] = await Promise.all([scriptsApi.get(scriptId.value), scripting.load(true)])
    apply(d)
    await Promise.all([loadVersions(), loadRuns()])
    const live = runs.value.find((r) => r.status === 'PENDING' || r.status === 'STARTED')
    if (live) follow(live.run_id)
  } catch (e) {
    loadError.value = errMessage(e)
  } finally {
    loading.value = false
  }
}

// a different environment of another language starts again from that language's template,
// as long as the code is still the template
function pickEnvironment(id: number | null) {
  const next = environments.value.find((e) => e.id === id)
  if (!next || !content.value) return
  const before = environment.value
  environmentId.value = next.id
  if (before && before.language !== next.language
      && JSON.stringify(content.value.files) === JSON.stringify(starterFiles(before.language))) {
    const starter = starterFiles(next.language)
    content.value = { ...content.value, files: starter, entrypoint: starter[0]!.path }
  }
}

function numberField(key: 'timeout_seconds' | 'retries' | 'retry_delay_seconds', value: string, min: number, max: number) {
  const n = Math.round(Number(value))
  if (!Number.isFinite(n) || !content.value) return
  content.value = { ...content.value, [key]: Math.min(max, Math.max(min, n)) }
}

async function save() {
  if (saving.value || !canEdit.value || !content.value) return
  const n = name.value.trim()
  if (!n) {
    toast.error(t('scripts.nameRequired'))
    return
  }
  if (environmentId.value == null) {
    toast.error(t('scripts.environmentRequired'))
    return
  }
  saving.value = true
  try {
    if (scriptId.value != null) {
      apply(await scriptsApi.update(scriptId.value, {
        name: n, description: description.value.trim(), environment_id: environmentId.value, content: content.value,
      }))
      await loadVersions()
      toast.success(t('scripts.saved', { name: n }))
    } else if (projectId.value != null) {
      const d = await scriptsApi.create(projectId.value, {
        name: n, description: description.value.trim(), environment_id: environmentId.value, content: content.value,
      })
      apply(d)
      navigateTo(`/script?id=${d.id}`, { replace: true })
      await loadVersions()
      toast.success(t('scripts.created', { name: n }))
    }
  } catch (e) {
    toast.error(errMessage(e))
  } finally {
    saving.value = false
  }
}

// ── the code editor ──────────────────────────────────────────────────────────
const editorOpen = ref(false)
// the version looked at in the editor, read-only; null = the code being edited
const viewing = ref<{ version: number; files: ScriptFile[]; entrypoint: string } | null>(null)

async function openEditor() {
  await scripting.load(true) // variables and packages may have changed since
  viewing.value = null
  editorOpen.value = true
}

function onCode(patch: { files: ScriptFile[]; entrypoint: string }) {
  if (viewing.value || !content.value) return
  content.value = { ...content.value, files: patch.files, entrypoint: patch.entrypoint }
}

// ── versions ─────────────────────────────────────────────────────────────────
const versions = ref<ScriptVersion[]>([])
const restoring = ref<number | null>(null)

async function loadVersions() {
  if (scriptId.value == null) return
  try { versions.value = await scriptsApi.versions(scriptId.value) } catch { versions.value = [] }
}

async function viewVersion(v: ScriptVersion) {
  if (scriptId.value == null) return
  try {
    const c = await scriptsApi.version(scriptId.value, v.version)
    viewing.value = { version: v.version, files: c.files, entrypoint: c.entrypoint }
    editorOpen.value = true
  } catch (e) {
    toast.error(errMessage(e))
  }
}

async function restore(v: ScriptVersion) {
  if (scriptId.value == null || restoring.value != null) return
  if (dirty.value && !confirm(t('scripts.restoreDiscards'))) return
  if (!confirm(t('scripts.restoreConfirm', { n: v.version }))) return
  restoring.value = v.version
  try {
    apply(await scriptsApi.restore(scriptId.value, v.version))
    await loadVersions()
    toast.success(t('scripts.restored', { n: v.version }))
  } catch (e) {
    toast.error(errMessage(e))
  } finally {
    restoring.value = null
  }
}

// ── inputs ───────────────────────────────────────────────────────────────────
// the datasources one may read (to add an input) and the folders (to publish the output)
const datasources = ref<DatasourceInfo[]>([])
const projects = ref<Project[]>([])
async function loadPickers() {
  try {
    [datasources.value, projects.value] = await Promise.all([datasourcesApi.list(), projectsApi.list()])
  } catch { /* the pickers stay empty: what is declared still shows from the script itself */ }
}

const inputRows = computed(() => {
  const list = content.value?.inputs ?? []
  const counts = new Map<string, number>()
  for (const i of list) counts.set(i.alias, (counts.get(i.alias) ?? 0) + 1)
  return list.map((i, idx) => {
    const ds = datasources.value.find((d) => d.id === i.datasource_id)
    const known = detail.value?.inputs.find((x) => x.datasource_id === i.datasource_id)
    const state = ds ? (ds.key ? 'ok' : 'empty') : (known?.state ?? 'missing')
    return {
      idx, alias: i.alias, state,
      name: ds?.name ?? known?.name ?? null,
      folder: ds ? (folderPaths.pathOf(ds.project_id) ?? '') : (known?.folder ?? ''),
      invalid: !ALIAS_RE.test(i.alias), duplicate: (counts.get(i.alias) ?? 0) > 1,
    }
  })
})
const inputOptions = computed(() => {
  const taken = new Set((content.value?.inputs ?? []).map((i) => i.datasource_id))
  return datasources.value.filter((d) => !taken.has(d.id))
    .map((d) => ({ value: d.id, label: d.name, hint: folderPaths.pathOf(d.project_id) }))
})
const inputsInvalid = computed(() => inputRows.value.some((r) => r.invalid || r.duplicate))

function addInput(id: number | null) {
  const ds = datasources.value.find((d) => d.id === id)
  if (!ds || !content.value) return
  const taken = new Set(content.value.inputs.map((i) => i.alias))
  const folder = projects.value.find((p) => p.id === ds.project_id)?.name ?? ''
  content.value = { ...content.value, inputs: [...content.value.inputs, { datasource_id: ds.id, alias: proposeAlias(ds.name, folder, taken) }] }
}
function setAlias(idx: number, value: string) {
  if (!content.value) return
  const alias = value.toUpperCase().replace(/[^A-Z0-9_]/g, '_').slice(0, 63)
  content.value = { ...content.value, inputs: content.value.inputs.map((i, k) => (k === idx ? { ...i, alias } : i)) }
}
function removeInput(idx: number) {
  if (!content.value) return
  content.value = { ...content.value, inputs: content.value.inputs.filter((_, k) => k !== idx) }
}

// ── output ───────────────────────────────────────────────────────────────────
const folderOptions = computed(() => projects.value.map((p) => ({ value: p.id, label: p.name, hint: folderPaths.pathOf(p.parent_id) })))
function setOutput(patch: Partial<ScriptContent['output']>) {
  if (!content.value) return
  const next = { ...content.value.output, ...patch }
  if (patch.publish && next.project_id == null) next.project_id = projectId.value
  if (patch.publish && !next.name.trim()) next.name = name.value.trim()
  content.value = { ...content.value, output: next }
}

// ── runs ─────────────────────────────────────────────────────────────────────
const runs = ref<ScriptRun[]>([])
const selectedRunId = ref<number | null>(null)
const selectedRun = computed(() => runs.value.find((r) => r.run_id === selectedRunId.value) ?? null)
const launching = ref(false)
const live = computed(() => runs.value.some((r) => r.status === 'PENDING' || r.status === 'STARTED'))
const preview = ref<OutputPreview | null>(null)
const previewLoading = ref(false)
let following: number | null = null
let pageGone = false

async function loadRuns() {
  if (scriptId.value == null) return
  try {
    runs.value = await scriptsApi.runs(scriptId.value)
  } catch {
    return
  }
  if (!runs.value.some((r) => r.run_id === selectedRunId.value)) selectedRunId.value = runs.value[0]?.run_id ?? null
}

async function runNow() {
  if (scriptId.value == null || launching.value || live.value) return
  if (dirty.value) {
    toast.error(t('scripts.saveBeforeRun'))
    return
  }
  launching.value = true
  try {
    const { run_id } = await scriptsApi.run(scriptId.value)
    await loadRuns()
    selectedRunId.value = run_id
    toast.success(t('scripts.runQueued', { name: name.value }))
    follow(run_id)
  } catch (e) {
    toast.error(errMessage(e))
  } finally {
    launching.value = false
  }
}

// checks often at first, then less: most runs end within seconds, some last hours
// (composables/waits.ts)
async function follow(runId: number) {
  if (following === runId) return
  following = runId
  let delay = FIRST_PAUSE_MS
  const since = Date.now()
  while (!pageGone && following === runId) {
    await new Promise((r) => setTimeout(r, delay))
    delay = nextPause(delay, since, 5000)
    if (pageGone || following !== runId) return
    await loadRuns()
    const r = runs.value.find((x) => x.run_id === runId)
    if (!r || r.status === 'SUCCESS' || r.status === 'FAILURE') {
      following = null
      if (r) {
        selectedRunId.value = r.run_id
        if (r.status === 'SUCCESS') toast.success(t('scripts.runSucceeded', { name: name.value }))
        else toast.error(t('scripts.runFailed', { name: name.value }))
      }
      return
    }
  }
}

watch(selectedRun, async (r, before) => {
  if (r?.run_id === before?.run_id && r?.output === before?.output) return
  preview.value = null
  if (!r?.output || scriptId.value == null) return
  previewLoading.value = true
  try {
    preview.value = await scriptsApi.preview(scriptId.value, r.run_id)
  } catch {
    preview.value = null
  } finally {
    previewLoading.value = false
  }
})

const runIcon = (s: ScriptRun['status']) => (s === 'SUCCESS' ? CircleCheck : s === 'FAILURE' ? CircleX : s === 'STARTED' ? LoaderCircle : Clock)
const statusLabel = (s: ScriptRun['status']) => t(`scripts.status${s.charAt(0)}${s.slice(1).toLowerCase()}`)
function duration(r: ScriptRun): string {
  if (!r.started_at || !r.finished_at) return ''
  const secs = Math.max(0, Math.round((Date.parse(r.finished_at) - Date.parse(r.started_at)) / 1000))
  return secs < 60 ? `${secs} s` : `${Math.floor(secs / 60)} min ${secs % 60} s`
}
// ── schedule (RUN on the folder; it runs with the authority of whoever sets it) ──
const scheduleOpen = ref(false)
const savingSchedule = ref(false)
async function saveSchedule(cron: string) {
  if (scriptId.value == null || savingSchedule.value) return
  savingSchedule.value = true
  try {
    const d = await scriptsApi.setSchedule(scriptId.value, cron)
    // only the schedule changed: unsaved edits stay on screen
    if (detail.value) detail.value = { ...detail.value, run_schedule: d.run_schedule, run_scheduled_by: d.run_scheduled_by, next_run_at: d.next_run_at }
    scheduleOpen.value = false
    toast.success(d.run_schedule ? t('flowGroups.scheduleSaved') : t('flowGroups.scheduleCleared'))
  } catch (e) {
    toast.error(errMessage(e))
  } finally {
    savingSchedule.value = false
  }
}

const editorVariables = computed(() => savedScriptVariables((content.value?.inputs ?? []).map((i) => i.alias)))

// Ctrl/Cmd+S saves, also with the code editor open (it hands over its last keys first)
function onKey(e: KeyboardEvent) {
  if ((e.ctrlKey || e.metaKey) && !e.altKey && e.key.toLowerCase() === 's') {
    e.preventDefault()
    if (!viewing.value) save()
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
  window.addEventListener('beforeunload', onBeforeUnload)
  folderPaths.load()
  load()
  loadPickers()
})
onUnmounted(() => {
  pageGone = true
  document.removeEventListener('keydown', onKey)
  window.removeEventListener('beforeunload', onBeforeUnload)
})
onBeforeRouteLeave(() => !dirty.value || confirm(t('scripts.leaveConfirm')))
// another script from the search: the same page with another id
onBeforeRouteUpdate((to) => (Number(to.query.id) || null) === scriptId.value || !dirty.value || confirm(t('scripts.leaveConfirm')))
watch(() => route.query.id, (id) => {
  const next = Number(id) || null
  if (next === scriptId.value) return // a script just created took its address
  scriptId.value = next
  following = null
  runs.value = []
  selectedRunId.value = null
  load()
})

const files = computed(() => content.value?.files ?? [])
const timeoutMax = computed(() => environment.value?.max_timeout_seconds ?? 86400)
const unavailable = computed(() => {
  const s = scripting.state.value
  if (!s) return ''
  if (s.disabled_reason) return t('scripting.node.off')
  if (!environments.value.length) return t('scripts.noEnvironment')
  if (environmentId.value != null && !environment.value) return t('scripting.node.environmentGone')
  return ''
})
</script>

<template>
  <AppShell>
    <div class="sp">
      <div class="page-head">
        <h1>
          <FileCode2 :size="18" />
          <input
            v-model="name"
            class="sname"
            type="text"
            maxlength="200"
            :readonly="!canEdit"
            :placeholder="$t('scripts.namePlaceholder')"
            :aria-label="$t('scripts.nameLabel')"
          />
        </h1>
        <span v-if="folderPath" class="where muted">{{ $t('scripts.inFolder', { path: folderPath }) }}</span>
        <div class="head-actions">
          <span v-if="detail" class="tag">v{{ detail.current_version }}</span>
          <span v-if="dirty && canEdit && detail" class="tag unsaved">{{ $t('scripts.unsaved') }}</span>
          <button
            v-if="detail?.runnable"
            type="button"
            :title="detail.run_schedule ? $t('flowGroups.scheduleTitle', { cron: detail.run_schedule }) : $t('flowGroups.schedule')"
            @click="scheduleOpen = true"
          >
            <CalendarClock :size="14" />
            <span v-if="detail.run_schedule" class="cron">{{ detail.run_schedule }}</span>
            <template v-else>{{ $t('flowGroups.schedule') }}</template>
          </button>
          <button
            v-if="detail?.runnable"
            type="button"
            :disabled="launching || live || !!unavailable"
            :title="dirty ? $t('scripts.saveBeforeRun') : $t('scripts.runTitle')"
            @click="runNow"
          >
            <LoaderCircle v-if="launching || live" :size="14" class="spin" />
            <Play v-else :size="14" />
            {{ $t('scripts.run') }}
          </button>
          <button v-if="canEdit" class="primary" :disabled="saving || (!dirty && scriptId != null) || !!unavailable || inputsInvalid" @click="save">
            <LoaderCircle v-if="saving" :size="14" class="spin" />
            <Save v-else :size="14" />
            {{ scriptId == null ? $t('scripts.create') : $t('scripts.save') }}
          </button>
        </div>
      </div>

      <p v-if="loadError" class="err" role="alert">{{ loadError }}</p>
      <SkeletonRows v-else-if="loading" :rows="5" />

      <div v-else-if="content" class="work">
        <section class="main">
          <p v-if="unavailable" class="warn" role="note"><TriangleAlert :size="13" /> {{ unavailable }}</p>

          <div class="card">
            <label class="field">
              <span>{{ $t('scripts.environment') }}</span>
              <Select
                :model-value="environmentId"
                :options="envOptions"
                :placeholder="$t('scripting.node.chooseEnvironment')"
                :disabled="!canEdit"
                @update:model-value="pickEnvironment"
              />
            </label>
            <label class="field">
              <span>{{ $t('scripts.description') }}</span>
              <textarea v-model="description" rows="2" maxlength="2000" :readonly="!canEdit"
                        :placeholder="$t('scripts.descriptionPlaceholder')" />
            </label>
          </div>

          <div class="card code">
            <div class="code-head">
              <h2><Code2 :size="15" /> {{ $t('scripts.code') }}</h2>
              <button type="button" class="primary" :disabled="!environment" @click="openEditor">
                <Code2 :size="14" /> {{ canEdit ? $t('scripting.node.editCode') : $t('scripting.node.viewCode') }}
              </button>
            </div>
            <ul v-if="files.length" class="files">
              <li v-for="f in files" :key="f.path">
                <code>{{ f.path }}</code>
                <span v-if="f.path === content.entrypoint" class="tag">{{ $t('scripts.entrypoint') }}</span>
                <span class="muted small">{{ $t('scripts.lines', { n: f.content.split('\n').length }) }}</span>
              </li>
            </ul>
            <p v-else class="muted small">{{ $t('scripting.node.noFiles') }}</p>
          </div>

          <div class="card">
            <h2><Database :size="15" /> {{ $t('scripts.inputs') }}</h2>
            <p class="muted small help">{{ $t('scripts.inputsHelp') }}</p>
            <ul v-if="inputRows.length" class="inputs">
              <li v-for="r in inputRows" :key="r.idx" :class="{ bad: r.state !== 'ok' || r.invalid || r.duplicate }">
                <input
                  class="alias"
                  type="text"
                  :value="r.alias"
                  :readonly="!canEdit"
                  :aria-label="$t('scripts.alias')"
                  :aria-invalid="r.invalid || r.duplicate"
                  @input="setAlias(r.idx, ($event.target as HTMLInputElement).value)"
                />
                <div class="ds">
                  <template v-if="r.state === 'missing'"><TriangleAlert :size="12" /> {{ $t('scripts.inputMissing') }}</template>
                  <template v-else-if="r.state === 'hidden'"><TriangleAlert :size="12" /> {{ $t('scripts.inputHidden') }}</template>
                  <template v-else>
                    <span class="dsname">{{ r.name }}</span>
                    <span v-if="r.folder" class="muted small">{{ r.folder }}</span>
                    <span v-if="r.state === 'empty'" class="warn-inline small">{{ $t('scripts.inputEmpty') }}</span>
                  </template>
                  <span v-if="r.invalid" class="warn-inline small">{{ $t('scripts.aliasInvalid') }}</span>
                  <span v-else-if="r.duplicate" class="warn-inline small">{{ $t('scripts.aliasDuplicate') }}</span>
                </div>
                <button v-if="canEdit" type="button" class="icon-btn" :aria-label="$t('scripts.removeInput')" :title="$t('scripts.removeInput')" @click="removeInput(r.idx)">
                  <X :size="14" />
                </button>
              </li>
            </ul>
            <p v-else class="muted small">{{ $t('scripts.noInputs') }}</p>
            <Select
              v-if="canEdit"
              :model-value="null"
              :options="inputOptions"
              :placeholder="$t('scripts.addInput')"
              searchable
              @update:model-value="addInput"
            />
          </div>

          <div class="card">
            <h2><Table2 :size="15" /> {{ $t('scripts.output') }}</h2>
            <p class="muted small help">{{ $t('scripts.outputHelp') }}</p>
            <label class="check">
              <input type="checkbox" :checked="content.output.publish" :disabled="!canEdit"
                     @change="setOutput({ publish: ($event.target as HTMLInputElement).checked })" />
              {{ $t('scripts.publish') }}
            </label>
            <div v-if="content.output.publish" class="publish">
              <label class="field">
                <span>{{ $t('scripts.publishName') }}</span>
                <input type="text" maxlength="200" :value="content.output.name" :readonly="!canEdit"
                       @input="setOutput({ name: ($event.target as HTMLInputElement).value })" />
              </label>
              <label class="field">
                <span>{{ $t('scripts.publishFolder') }}</span>
                <Select
                  :model-value="content.output.project_id"
                  :options="folderOptions"
                  :disabled="!canEdit"
                  searchable
                  @update:model-value="(v: number | null) => setOutput({ project_id: v })"
                />
              </label>
              <label class="check">
                <input type="checkbox" :checked="content.output.overwrite" :disabled="!canEdit"
                       @change="setOutput({ overwrite: ($event.target as HTMLInputElement).checked })" />
                {{ $t('scripts.overwrite') }}
              </label>
            </div>
          </div>

          <div class="card numbers">
            <label>
              <span>{{ $t('scripting.node.timeout') }}</span>
              <input type="number" min="10" :max="timeoutMax" :value="content.timeout_seconds" :disabled="!canEdit"
                     @change="numberField('timeout_seconds', ($event.target as HTMLInputElement).value, 10, timeoutMax)" />
            </label>
            <label>
              <span>{{ $t('scripting.node.retries') }}</span>
              <input type="number" min="0" max="5" :value="content.retries" :disabled="!canEdit"
                     @change="numberField('retries', ($event.target as HTMLInputElement).value, 0, 5)" />
            </label>
            <label>
              <span>{{ $t('scripting.node.retryDelay') }}</span>
              <input type="number" min="0" max="3600" :value="content.retry_delay_seconds" :disabled="!canEdit || !(content.retries > 0)"
                     @change="numberField('retry_delay_seconds', ($event.target as HTMLInputElement).value, 0, 3600)" />
            </label>
          </div>

          <div v-if="scriptId != null" class="card runs">
            <h2><Play :size="15" /> {{ $t('scripts.runs') }}</h2>
            <p v-if="detail?.next_run_at && detail.run_schedule" class="muted small next">
              <CalendarClock :size="13" /> {{ $t('flowGroups.nextRun', { when: fmtx.dateTime(detail.next_run_at) }) }}
            </p>
            <p v-if="!runs.length" class="muted small">{{ $t('scripts.noRuns') }}</p>
            <div v-else class="runs-body">
              <ol class="run-list">
                <li v-for="r in runs" :key="r.run_id">
                  <button type="button" :class="['run-item', r.status.toLowerCase(), { on: r.run_id === selectedRunId }]" @click="selectedRunId = r.run_id">
                    <component :is="runIcon(r.status)" :size="13" :class="{ spin: r.status === 'STARTED' }" />
                    <span class="rwhen">{{ r.started_at ? fmtx.dateTime(r.started_at) : '' }}</span>
                    <span class="muted small">{{ r.script_version ? `v${r.script_version}` : '' }}</span>
                  </button>
                </li>
              </ol>
              <div v-if="selectedRun" class="run-detail">
                <div class="rmeta small">
                  <strong :class="['rstatus', selectedRun.status.toLowerCase()]">{{ statusLabel(selectedRun.status) }}</strong>
                  <span>{{ selectedRun.trigger_type === 'schedule' ? $t('scripts.triggerSchedule') : $t('scripts.triggerManual') }}<template v-if="selectedRun.launched_by"> · {{ selectedRun.launched_by }}</template></span>
                  <span v-if="duration(selectedRun)">{{ duration(selectedRun) }}</span>
                  <span v-if="(selectedRun.attempts ?? 0) > 1">{{ $t('scripts.attempts', { n: selectedRun.attempts }) }}</span>
                  <span v-if="selectedRun.exit_code != null && selectedRun.status === 'FAILURE'">{{ $t('scripts.exitCode', { n: selectedRun.exit_code }) }}</span>
                  <NuxtLink v-if="selectedRun.datasource_id" :to="`/datasources?q=${encodeURIComponent(content.output.name)}&ds=${selectedRun.datasource_id}`">{{ $t('scripts.published') }}</NuxtLink>
                </div>
                <p v-if="selectedRun.error" class="err small" role="alert">{{ selectedRun.error }}</p>
                <h3 class="small">{{ $t('scripts.log') }}</h3>
                <pre class="log">{{ selectedRun.log || (selectedRun.log_expired ? $t('scripting.run.logExpired')
                  : selectedRun.status === 'SUCCESS' || selectedRun.status === 'FAILURE' ? $t('scripts.emptyLog') : '…') }}</pre>
                <template v-if="selectedRun.status === 'SUCCESS'">
                  <h3 class="small">{{ $t('scripts.outputPreview') }}<template v-if="selectedRun.rows != null"> · {{ $t('scripts.rows', { n: fmtx.number(selectedRun.rows) }) }}</template></h3>
                  <p v-if="!selectedRun.output" class="muted small">{{ $t('scripts.noOutput') }}</p>
                  <SkeletonRows v-else-if="previewLoading" :rows="3" />
                  <div v-else-if="preview" class="preview">
                    <table>
                      <thead><tr><th v-for="c in preview.columns" :key="c.name">{{ c.name }}</th></tr></thead>
                      <tbody>
                        <tr v-for="(row, i) in preview.rows" :key="i">
                          <td v-for="c in preview.columns" :key="c.name">{{ row[c.name] ?? '' }}</td>
                        </tr>
                      </tbody>
                    </table>
                  </div>
                </template>
              </div>
            </div>
          </div>
        </section>

        <aside v-if="scriptId != null" class="side card" :aria-label="$t('scripts.versions')">
          <h2><History :size="15" /> {{ $t('scripts.versions') }}</h2>
          <ol class="versions">
            <li v-for="v in versions" :key="v.version" :class="{ current: v.current }">
              <div class="vtop">
                <strong>v{{ v.version }}</strong>
                <span v-if="v.current" class="tag">{{ $t('scripts.currentVersion') }}</span>
                <span class="muted small">{{ versionNote(v.note, t) }}</span>
              </div>
              <div class="muted small">{{ fmtx.dateTime(v.created_at) }}<template v-if="v.created_by"> · {{ v.created_by }}</template></div>
              <div class="vacts">
                <button type="button" class="mini" @click="viewVersion(v)">{{ $t('scripts.viewVersion') }}</button>
                <button v-if="canEdit && !v.current" type="button" class="mini" :disabled="restoring != null" @click="restore(v)">
                  <LoaderCircle v-if="restoring === v.version" :size="12" class="spin" /><RotateCcw v-else :size="12" />
                  {{ $t('scripts.restore') }}
                </button>
              </div>
            </li>
          </ol>
        </aside>
      </div>

      <ScriptEditor
        v-if="editorOpen && environment"
        :open="editorOpen"
        :title="viewing ? `${name} · v${viewing.version}` : (name || t('scripts.newScript'))"
        :environment="environment"
        :files="viewing ? viewing.files : content?.files ?? []"
        :entrypoint="viewing ? viewing.entrypoint : content?.entrypoint ?? ''"
        :tabularia-variables="editorVariables"
        :read-only="!canEdit || !!viewing"
        saved
        @update="onCode"
        @close="editorOpen = false; viewing = null"
      />

      <ScheduleDialog
        :open="scheduleOpen"
        :title="name"
        :subtitle="$t('scripts.scheduleSubtitle')"
        :current="detail?.run_schedule ?? null"
        :busy="savingSchedule"
        @save="saveSchedule"
        @cancel="scheduleOpen = false"
      />
    </div>
  </AppShell>
</template>

<style scoped>
.sp { display: flex; flex-direction: column; gap: 12px; min-height: 0; }
.page-head { display: flex; align-items: center; gap: 12px; }
.page-head h1 { display: inline-flex; align-items: center; gap: 8px; margin: 0; flex: 0 1 380px; min-width: 0; }
.sname { font-size: 16px; font-weight: 600; padding: 4px 9px; min-width: 0; }
.sname[readonly] { border-color: transparent; background: transparent; padding-left: 0; }
.where { font-size: 12.5px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; min-width: 0; flex: 1; }
.head-actions { display: flex; align-items: center; gap: 8px; margin-left: auto; flex-shrink: 0; }
.head-actions button { display: inline-flex; align-items: center; gap: 6px; white-space: nowrap; }
.tag {
  font-size: 10px; text-transform: uppercase; letter-spacing: 0.04em; padding: 1px 7px;
  border-radius: 8px; background: var(--panel-2); border: 1px solid var(--border); color: var(--muted);
}
.err { color: var(--danger); }
.warn { margin: 0; display: flex; gap: 6px; align-items: flex-start; font-size: 12.5px; color: var(--warning); }
.work { display: grid; grid-template-columns: minmax(0, 1fr) 300px; gap: 16px; align-items: start; }
.main { display: flex; flex-direction: column; gap: 12px; min-width: 0; }
.card { border: 1px solid var(--border-soft); border-radius: 10px; padding: 12px 14px; background: var(--panel); display: flex; flex-direction: column; gap: 10px; }
.card h2 { margin: 0; font-size: 13.5px; display: inline-flex; align-items: center; gap: 7px; }
.field { display: flex; flex-direction: column; gap: 5px; font-size: 12px; color: var(--muted); }
.field textarea { resize: vertical; font: inherit; color: var(--text); }
.code-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.code-head button { display: inline-flex; align-items: center; gap: 6px; }
.files { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 5px; }
.files li { display: flex; align-items: center; gap: 8px; font-size: 12.5px; }
.small { font-size: 12px; }
.numbers { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }
.numbers label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); }
.numbers input { min-width: 0; }
.versions { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; max-height: 60vh; overflow: auto; }
.versions li { border: 1px solid var(--border-soft); border-radius: 8px; padding: 7px 9px; display: flex; flex-direction: column; gap: 3px; }
.versions li.current { border-color: var(--accent); }
.vtop { display: flex; align-items: center; gap: 7px; flex-wrap: wrap; }
.vacts { display: flex; gap: 6px; margin-top: 3px; }
.vacts .mini { display: inline-flex; align-items: center; gap: 4px; }
.help { margin: 0; }
.cron { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
.next { margin: 0; display: inline-flex; align-items: center; gap: 6px; }
.inputs { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
.inputs li { display: grid; grid-template-columns: 230px minmax(0, 1fr) auto; gap: 10px; align-items: center; }
.inputs li.bad .alias { border-color: var(--warning); }
.alias { font-family: var(--mono, ui-monospace, monospace); font-size: 12.5px; min-width: 0; }
.ds { display: flex; flex-direction: column; min-width: 0; font-size: 12.5px; }
.ds .dsname { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.warn-inline { color: var(--warning); }
.icon-btn { display: inline-flex; align-items: center; padding: 4px; }
.check { display: inline-flex; align-items: center; gap: 7px; font-size: 12.5px; }
.publish { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 10px; align-items: end; }
.publish .check { grid-column: 1 / -1; }
.runs-body { display: grid; grid-template-columns: 190px minmax(0, 1fr); gap: 12px; align-items: start; }
.run-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 3px; max-height: 420px; overflow: auto; }
.run-item { width: 100%; display: flex; align-items: center; gap: 6px; padding: 5px 7px; border-radius: 6px; font-size: 12px; text-align: left; background: transparent; border: 1px solid transparent; }
.run-item.on { border-color: var(--accent); background: var(--panel-2); }
.run-item.success { color: var(--success, inherit); }
.run-item.failure { color: var(--danger); }
.rwhen { flex: 1; color: var(--text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.run-detail { display: flex; flex-direction: column; gap: 6px; min-width: 0; }
.rmeta { display: flex; flex-wrap: wrap; gap: 4px 12px; color: var(--muted); }
.rstatus.success { color: var(--success, var(--text)); }
.rstatus.failure { color: var(--danger); }
.run-detail h3 { margin: 4px 0 0; font-size: 12px; color: var(--muted); font-weight: 600; }
.log { margin: 0; max-height: 260px; overflow: auto; font-size: 11.5px; background: var(--panel-2); border: 1px solid var(--border-soft); border-radius: 6px; padding: 8px; white-space: pre-wrap; word-break: break-word; }
.preview { overflow: auto; max-height: 300px; border: 1px solid var(--border-soft); border-radius: 6px; }
.preview table { border-collapse: collapse; font-size: 12px; width: max-content; min-width: 100%; }
.preview th, .preview td { padding: 4px 8px; border-bottom: 1px solid var(--border-soft); text-align: left; white-space: nowrap; }
.preview th { position: sticky; top: 0; background: var(--panel-2); }
@media (max-width: 900px) {
  .work { grid-template-columns: minmax(0, 1fr); }
  .inputs li { grid-template-columns: minmax(0, 1fr) auto; }
  .inputs li .ds { grid-column: 1 / -1; grid-row: 2; }
  .runs-body, .publish { grid-template-columns: minmax(0, 1fr); }
}
</style>
