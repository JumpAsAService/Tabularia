<script setup lang="ts">
// Scripting environments (Admin panel): the bucket the Script nodes work in, and the
// Python and R environments their code runs in. An environment is built once into an
// image by the cluster; changing its language version or packages makes a new
// version, everything else applies to the next run. Kubernetes only: elsewhere the
// section says why. Observers read it all, without any secret, and change nothing.
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  ArrowLeft, CheckCircle2, CircleDot, Code2, Database, KeyRound, LoaderCircle, Plus, RotateCcw, Search, Send, Trash2,
  TriangleAlert, XCircle,
} from 'lucide-vue-next'
import { errMessage } from '~/composables/useApi'
import { useFormat } from '~/composables/useFormat'
import type { GroupOut } from '~/composables/useProjects'
import {
  useScripting, type Environment, type EnvironmentInput, type EnvironmentVersion, type PackageHit,
  type PackageVersion, type ScriptLanguage,
  type ScriptingState, type ScriptStorageTest, type VersionStatus,
} from '~/composables/useScripting'

const props = defineProps<{ groups: GroupOut[] }>()
const emit = defineEmits<{ (e: 'count', n: number): void }>()

const { t } = useI18n()
const fmt = useFormat()
const api = useScripting()
const toast = useToast()

const state = ref<ScriptingState | null>(null)
const loadError = ref('')
const editable = computed(() => !!state.value?.editable && !state.value?.disabled_reason)

async function load() {
  try {
    state.value = await api.get()
    loadError.value = ''
    emit('count', state.value.environments.length)
    fillStorage()
  } catch (e) {
    loadError.value = errMessage(e)
  }
}
onMounted(load)

// ── the bucket ──────────────────────────────────────────────────────────────
const storage = ref({ endpoint: '', region: '', bucket: '', access_key: '', secret_key: '' })
const savingStorage = ref(false)
const testing = ref(false)
const test = ref<ScriptStorageTest | null>(null)

function fillStorage() {
  const s = state.value?.storage
  if (s) storage.value = { endpoint: s.endpoint, region: s.region, bucket: s.bucket, access_key: s.access_key, secret_key: '' }
}

const storageChanges = computed(() => {
  const s = state.value?.storage
  if (!s) return {}
  const m = storage.value
  const out: Record<string, string> = {}
  for (const k of ['endpoint', 'region', 'bucket', 'access_key'] as const) {
    if (m[k].trim() !== s[k]) out[k] = m[k].trim()
  }
  if (m.secret_key.trim()) out.secret_key = m.secret_key.trim()
  return out
})
const storageDirty = computed(() => Object.keys(storageChanges.value).length > 0)

async function saveStorage(): Promise<boolean> {
  savingStorage.value = true
  try {
    const s = await api.saveStorage(storageChanges.value)
    if (state.value) state.value.storage = s
    fillStorage()
    toast.success(t('scripting.storageSaved'))
    return true
  } catch (e) {
    toast.error(errMessage(e))
    return false
  } finally {
    savingStorage.value = false
  }
}

async function testStorage() {
  testing.value = true
  test.value = null
  try {
    // a save refused: the test would answer for the settings saved before, not these
    if (storageDirty.value && !(await saveStorage())) return
    test.value = await api.testStorage()
  } catch (e) {
    test.value = { ok: false, message: errMessage(e), can_list: null }
  } finally {
    testing.value = false
  }
}

// ── one environment ─────────────────────────────────────────────────────────
// null = the list; 'new' = the creation form; a number = that environment
const open = ref<number | 'new' | null>(null)
const env = ref<Environment | null>(null)
const saving = ref(false)
const packageError = ref<{ line: number; text: string } | null>(null)

interface Form {
  name: string
  description: string
  language: ScriptLanguage
  language_version: string
  packages: string
  enabled: boolean
  cpu_request: string
  cpu_limit: string
  memory_request: string
  memory_limit: string
  max_timeout_minutes: number
  group_ids: number[]
}
const form = ref<Form>(blankForm('python'))

function versionsOf(language: ScriptLanguage): string[] {
  return state.value?.languages[language] ?? []
}

function blankForm(language: ScriptLanguage): Form {
  const versions = versionsOf(language)
  return {
    name: '', description: '', language, language_version: versions[versions.length - 1] ?? '', packages: '',
    enabled: true, cpu_request: '500m', cpu_limit: '2', memory_request: '1Gi', memory_limit: '4Gi',
    max_timeout_minutes: 60, group_ids: [],
  }
}

function fillForm(e: Environment) {
  form.value = {
    name: e.name, description: e.description, language: e.language, language_version: e.language_version ?? '',
    packages: e.packages, enabled: e.enabled, cpu_request: e.cpu_request, cpu_limit: e.cpu_limit,
    memory_request: e.memory_request, memory_limit: e.memory_limit,
    max_timeout_minutes: Math.round(e.max_timeout_seconds / 60), group_ids: [...e.group_ids],
  }
}

function startNew() {
  env.value = null
  form.value = blankForm('python')
  packageError.value = null
  open.value = 'new'
}

// a new environment starts from the newest version of the language chosen
watch(() => form.value.language, (lang) => {
  if (open.value !== 'new') return
  const versions = versionsOf(lang)
  if (!versions.includes(form.value.language_version)) form.value.language_version = versions[versions.length - 1] ?? ''
})

async function openEnvironment(id: number) {
  try {
    env.value = await api.environment(id)
    fillForm(env.value)
    packageError.value = null
    open.value = id
  } catch (e) {
    toast.error(errMessage(e))
  }
}

function back() {
  open.value = null
  env.value = null
  load()
}

// what would change: on an existing environment only the fields that differ go out,
// so saving the description alone never starts a build
const formChanges = computed<EnvironmentInput>(() => {
  const f = form.value
  const body: EnvironmentInput = {
    name: f.name.trim(), description: f.description.trim(), language_version: f.language_version,
    packages: f.packages.trim(), enabled: f.enabled, cpu_request: f.cpu_request.trim(), cpu_limit: f.cpu_limit.trim(),
    memory_request: f.memory_request.trim(), memory_limit: f.memory_limit.trim(),
    max_timeout_seconds: Math.round(f.max_timeout_minutes * 60), group_ids: [...f.group_ids].sort((a, b) => a - b),
  }
  const e = env.value
  if (!e) return { ...body, language: f.language }
  const before: EnvironmentInput = {
    name: e.name, description: e.description, language_version: e.language_version ?? '', packages: e.packages,
    enabled: e.enabled, cpu_request: e.cpu_request, cpu_limit: e.cpu_limit, memory_request: e.memory_request,
    memory_limit: e.memory_limit, max_timeout_seconds: e.max_timeout_seconds,
    group_ids: [...e.group_ids].sort((a, b) => a - b),
  }
  const out: EnvironmentInput = {}
  for (const k of Object.keys(body) as (keyof EnvironmentInput)[]) {
    if (JSON.stringify(body[k]) !== JSON.stringify(before[k])) (out as Record<string, unknown>)[k] = body[k]
  }
  return out
})
const formDirty = computed(() => Object.keys(formChanges.value).length > 0)
const rebuilds = computed(() => !!env.value && ('packages' in formChanges.value || 'language_version' in formChanges.value))

async function saveEnvironment() {
  saving.value = true
  packageError.value = null
  try {
    const saved = open.value === 'new'
      ? await api.create(formChanges.value)
      : await api.update(env.value!.id, formChanges.value)
    env.value = saved
    fillForm(saved)
    open.value = saved.id
    toast.success(t('scripting.saved'))
  } catch (e: any) {
    const detail = e?.data?.detail
    if (detail?.code === 'package') {
      packageError.value = { line: detail.line, text: detail.text }
    } else {
      toast.error(errMessage(e))
    }
  } finally {
    saving.value = false
  }
}

async function removeEnvironment() {
  const e = env.value
  if (!e || !confirm(t('scripting.confirmDelete', { name: e.name }))) return
  try {
    await api.remove(e.id)
    toast.success(t('scripting.deleted', { name: e.name }))
    back()
  } catch (err: any) {
    const detail = err?.data?.detail
    if (detail?.code === 'in_use') {
      // flows with Script nodes of it, saved scripts or notebooks of it
      toast.error(detail.flows?.length ? t('scripting.inUse', { flows: detail.flows.join(', ') })
        : detail.notebooks?.length ? t('scripting.inUseByNotebooks', { notebooks: detail.notebooks.join(', ') })
          : t('scripting.inUseByScripts', { scripts: (detail.scripts ?? []).join(', ') }))
    } else {
      toast.error(errMessage(err))
    }
  }
}

async function rebuild() {
  if (!env.value) return
  try {
    env.value = await api.rebuild(env.value.id)
  } catch (e) {
    toast.error(errMessage(e))
  }
}

async function makeCurrent(v: EnvironmentVersion) {
  if (!env.value || !confirm(t('scripting.confirmCurrent', { n: v.number }))) return
  try {
    env.value = await api.makeCurrent(env.value.id, v.id)
  } catch (e) {
    toast.error(errMessage(e))
  }
}

// while a version is being built, the page follows it
const building = computed(() => env.value?.versions.some((v) => ['queued', 'building', 'checking'].includes(v.status)))
let timer: ReturnType<typeof setInterval> | undefined
watch(building, (on) => {
  clearInterval(timer)
  if (!on) return
  timer = setInterval(async () => {
    if (!env.value) return
    try {
      const fresh = await api.environment(env.value.id)
      env.value = { ...fresh }
    } catch { /* the next tick */ }
  }, 5000)
})
onBeforeUnmount(() => clearInterval(timer))

// ── variables ───────────────────────────────────────────────────────────────
const variable = ref({ name: '', value: '', secret: false, from_tabularia: false })
const editingVariable = ref<string | null>(null)
const savingVariable = ref(false)

function editVariable(name: string) {
  const v = env.value?.variable_list.find((x) => x.name === name)
  if (!v) return
  variable.value = { name: v.name, value: v.secret ? '' : v.value ?? '', secret: v.secret, from_tabularia: v.from_tabularia }
  editingVariable.value = v.name
}

function clearVariable() {
  variable.value = { name: '', value: '', secret: false, from_tabularia: false }
  editingVariable.value = null
}

async function saveVariable() {
  if (!env.value) return
  const v = variable.value
  const name = v.name.trim().toUpperCase()
  // a secret one being edited keeps its value when the field is left empty
  const keep = editingVariable.value === name && v.secret && !v.from_tabularia && !v.value
  savingVariable.value = true
  try {
    env.value = await api.setVariable(env.value.id, name, {
      ...(keep ? {} : { value: v.value }), secret: v.secret && !v.from_tabularia, from_tabularia: v.from_tabularia,
    })
    clearVariable()
  } catch (e) {
    toast.error(errMessage(e))
  } finally {
    savingVariable.value = false
  }
}

async function deleteVariable(name: string) {
  if (!env.value || !confirm(t('scripting.confirmDeleteVariable', { name }))) return
  try {
    env.value = await api.deleteVariable(env.value.id, name)
    if (editingVariable.value === name) clearVariable()
  } catch (e) {
    toast.error(errMessage(e))
  }
}

// ── how things read ─────────────────────────────────────────────────────────
function languageLabel(language: ScriptLanguage, version?: string | null): string {
  return `${language === 'python' ? 'Python' : 'R'}${version ? ` ${version}` : ''}`
}

function statusIcon(s: VersionStatus | null) {
  if (s === 'ready') return CheckCircle2
  if (s === 'failed') return XCircle
  if (s === 'queued' || s === 'building' || s === 'checking') return LoaderCircle
  return CircleDot
}

function duration(v: EnvironmentVersion): string {
  if (!v.started_at) return ''
  const end = v.finished_at ? new Date(v.finished_at).getTime() : Date.now()
  const s = Math.max(0, Math.round((end - new Date(v.started_at).getTime()) / 1000))
  return s < 60 ? `${s} s` : `${Math.floor(s / 60)} min ${s % 60} s`
}

// ── package search (PyPI or CRAN, or the mirrors the builds use) ──────────────
const pkgQuery = ref('')
const pkgHits = ref<PackageHit[]>([])
const pkgOpen = ref(false)
const pkgActive = ref(-1)
const pkgSearching = ref(false)
const pkgError = ref('')
const pkgChosen = ref<string | null>(null)
const pkgVersions = ref<PackageVersion[]>([])
const pkgVersion = ref('')
let pkgTimer: ReturnType<typeof setTimeout> | undefined
let pkgSeq = 0

function onPackageInput() {
  clearTimeout(pkgTimer)
  pkgError.value = ''
  const q = pkgQuery.value.trim()
  if (q.length < 2) {
    pkgHits.value = []
    pkgOpen.value = false
    return
  }
  pkgTimer = setTimeout(async () => {
    const seq = ++pkgSeq
    pkgSearching.value = true
    try {
      const hits = await api.searchPackages(form.value.language, q)
      if (seq !== pkgSeq) return // a newer search went out meanwhile
      pkgHits.value = hits
      pkgActive.value = hits.length ? 0 : -1
      pkgOpen.value = true
    } catch (e) {
      if (seq === pkgSeq) pkgError.value = errMessage(e)
    } finally {
      if (seq === pkgSeq) pkgSearching.value = false
    }
  }, 250)
}

function onPackageKey(e: KeyboardEvent) {
  if (!pkgOpen.value || !pkgHits.value.length) return
  if (e.key === 'ArrowDown') {
    e.preventDefault()
    pkgActive.value = (pkgActive.value + 1) % pkgHits.value.length
  } else if (e.key === 'ArrowUp') {
    e.preventDefault()
    pkgActive.value = (pkgActive.value - 1 + pkgHits.value.length) % pkgHits.value.length
  } else if (e.key === 'Enter' && pkgActive.value >= 0) {
    e.preventDefault()
    choosePackage(pkgHits.value[pkgActive.value]!)
  } else if (e.key === 'Escape') {
    pkgOpen.value = false
  }
}

async function loadVersions() {
  if (!pkgChosen.value) return
  try {
    const out = await api.packageVersions(form.value.language, pkgChosen.value,
      form.value.language === 'python' ? form.value.language_version : undefined)
    pkgVersions.value = out.versions
    // the newest release that installs without compiling, else the newest release
    const pick = out.versions.find((v) => !v.pre && v.wheel !== false) ?? out.versions.find((v) => !v.pre) ?? out.versions[0]
    pkgVersion.value = pick?.version ?? ''
  } catch (e) {
    pkgError.value = errMessage(e)
    pkgVersions.value = []
  }
}

async function choosePackage(hit: PackageHit) {
  pkgChosen.value = hit.name
  pkgQuery.value = hit.name
  pkgOpen.value = false
  pkgError.value = ''
  await loadVersions()
}

// the wheels depend on the Python chosen
watch(() => form.value.language_version, () => { if (form.value.language === 'python') loadVersions() })
watch(() => form.value.language, () => {
  pkgQuery.value = ''
  pkgHits.value = []
  pkgChosen.value = null
  pkgVersions.value = []
})

const chosenVersion = computed(() => pkgVersions.value.find((v) => v.version === pkgVersion.value) ?? null)

function samePackage(line: string, name: string): boolean {
  const norm = (x: string) => (form.value.language === 'python' ? x.toLowerCase().replace(/[-_.]+/g, '-') : x)
  const head = line.split('#')[0]!.trim().split(/[\s=<>!~;@[]/)[0] ?? ''
  return !!head && norm(head) === norm(name)
}

function addPackage() {
  const name = pkgChosen.value
  if (!name || !pkgVersion.value) return
  const entry = form.value.language === 'python' ? `${name}==${pkgVersion.value}` : `${name}@${pkgVersion.value}`
  const lines = form.value.packages.split('\n').filter((l) => l.trim() && !samePackage(l, name))
  form.value.packages = [...lines, entry].join('\n')
  packageError.value = null
  pkgQuery.value = ''
  pkgChosen.value = null
  pkgVersions.value = []
}

const groupName = (id: number) => props.groups.find((g) => g.id === id)?.name ?? `#${id}`
const basePackages = computed(() => (form.value.language === 'python' ? 'polars, pyarrow, marimo' : 'arrow'))
</script>

<template>
  <div class="card">
    <h4><Code2 :size="14" /> {{ $t('scripting.title') }}</h4>
    <p class="hint muted small">{{ $t('scripting.lead') }}</p>
    <p v-if="loadError" class="page-error" role="alert">{{ loadError }}</p>
    <p v-if="state?.disabled_reason" class="note" role="note">
      <TriangleAlert :size="13" />
      {{ state.disabled_reason === 'kubernetes' ? $t('scripting.offKubernetes') : $t('scripting.offConfiguration') }}
    </p>
  </div>

  <template v-if="state && open === null">
    <!-- the bucket -->
    <form class="card" @submit.prevent="saveStorage">
      <h4><Database :size="14" /> {{ $t('scripting.storageTitle') }}</h4>
      <p class="hint muted small">{{ $t('scripting.storageLead') }}</p>
      <div class="fields">
        <label class="field largo">
          <span>{{ $t('scripting.endpoint') }}</span>
          <input v-model="storage.endpoint" type="url" autocomplete="off" placeholder="https://s3.example.com" :disabled="!editable || savingStorage" />
          <small>{{ $t('scripting.endpointHint') }}</small>
        </label>
        <label class="field">
          <span>{{ $t('scripting.bucket') }}</span>
          <input v-model="storage.bucket" type="text" autocomplete="off" placeholder="tabularia-scripts" :disabled="!editable || savingStorage" />
        </label>
        <label class="field">
          <span>{{ $t('scripting.region') }}</span>
          <input v-model="storage.region" type="text" autocomplete="off" placeholder="eu-west-1" :disabled="!editable || savingStorage" />
        </label>
        <label class="field">
          <span>{{ $t('scripting.accessKey') }}</span>
          <input v-model="storage.access_key" type="text" autocomplete="off" :disabled="!editable || savingStorage" />
        </label>
        <label class="field">
          <span>{{ $t('scripting.secretKey') }}</span>
          <input
            v-model="storage.secret_key"
            type="password"
            autocomplete="new-password"
            :placeholder="state.storage.secret_key_set ? $t('scripting.secretSet') : ''"
            :disabled="!editable || savingStorage"
          />
        </label>
      </div>
      <p class="hint muted small"><KeyRound :size="12" /> {{ $t('scripting.keyAdvice') }}</p>
      <div v-if="editable" class="actions">
        <button type="submit" class="primary" :disabled="savingStorage || !storageDirty">
          <LoaderCircle v-if="savingStorage" :size="13" class="spin" /> {{ $t('scripting.save') }}
        </button>
        <button
          type="button"
          :disabled="testing || savingStorage || !(storage.bucket && storage.access_key && (state.storage.secret_key_set || storage.secret_key))"
          @click="testStorage"
        >
          <LoaderCircle v-if="testing" :size="13" class="spin" /><Send v-else :size="13" /> {{ $t('scripting.test') }}
        </button>
      </div>
      <p v-if="test" class="outcome" :class="test.ok ? (test.can_list ? 'caution' : 'ok') : 'ko'" role="status">
        <component :is="test.ok ? (test.can_list ? TriangleAlert : CheckCircle2) : XCircle" :size="14" />
        <span>
          {{ test.ok ? (test.can_list ? $t('scripting.testCanList') : $t('scripting.testOk')) : $t('scripting.testFailed') }}
          <code>{{ test.message }}</code>
        </span>
      </p>
    </form>

    <!-- the environments -->
    <div class="card">
      <div class="heading">
        <h4><Code2 :size="14" /> {{ $t('scripting.environmentsTitle') }}</h4>
        <button v-if="editable" type="button" class="primary mini" @click="startNew">
          <Plus :size="13" /> {{ $t('scripting.newEnvironment') }}
        </button>
      </div>
      <p v-if="!state.environments.length" class="hint muted small">{{ $t('scripting.noEnvironments') }}</p>
      <ul v-else class="environments">
        <li v-for="e in state.environments" :key="e.id">
          <button type="button" class="environment" @click="openEnvironment(e.id)">
            <span class="name">{{ e.name }}<em v-if="!e.enabled" class="off">{{ $t('scripting.disabled') }}</em></span>
            <span class="language">{{ languageLabel(e.language, e.language_version) }}</span>
            <span class="version-state" :class="e.latest_status ?? ''">
              <component :is="statusIcon(e.latest_status)" :size="13" :class="{ spin: e.latest_status && e.latest_status !== 'ready' && e.latest_status !== 'failed' }" />
              <template v-if="e.latest_status && e.latest_status !== 'ready'">
                {{ $t(`scripting.status.${e.latest_status}`) }} · v{{ e.latest_number }}
              </template>
              <template v-else-if="e.current_number">{{ $t('scripting.currentVersion', { n: e.current_number }) }}</template>
            </span>
            <span class="muted small">
              {{ e.groups.length ? e.groups.join(', ') : $t('scripting.adminsOnly') }}
              · {{ $t('scripting.variablesCount', { n: e.variables }) }}
            </span>
          </button>
        </li>
      </ul>
    </div>
  </template>

  <!-- one environment: its settings, its variables, its versions -->
  <template v-else-if="state && open !== null">
    <form class="card" @submit.prevent="saveEnvironment">
      <div class="heading">
        <button type="button" class="mini" @click="back"><ArrowLeft :size="13" /> {{ $t('scripting.back') }}</button>
        <h4>{{ open === 'new' ? $t('scripting.newEnvironment') : env?.name }}</h4>
        <!-- the latest version's state, without scrolling down to the versions -->
        <span v-if="env?.versions.length" class="version-state" :class="env.versions[0].status" role="status">
          <component :is="statusIcon(env.versions[0].status)" :size="13" :class="{ spin: building }" />
          v{{ env.versions[0].number }} · {{ $t(`scripting.status.${env.versions[0].status}`) }}
        </span>
        <button v-if="editable && env" type="button" class="mini danger-zone" @click="removeEnvironment">
          <Trash2 :size="13" /> {{ $t('scripting.delete') }}
        </button>
      </div>
      <div class="fields">
        <label class="field">
          <span>{{ $t('scripting.name') }}</span>
          <input v-model="form.name" type="text" maxlength="120" required :disabled="!editable || saving" />
        </label>
        <div class="field-double">
          <label class="field">
            <span>{{ $t('scripting.language') }}</span>
            <select v-model="form.language" :disabled="!editable || saving || open !== 'new'">
              <option value="python">Python</option>
              <option value="r">R</option>
            </select>
          </label>
          <label class="field">
            <span>{{ $t('scripting.version') }}</span>
            <select v-model="form.language_version" :disabled="!editable || saving">
              <option v-for="v in versionsOf(form.language)" :key="v" :value="v">{{ v }}</option>
            </select>
          </label>
        </div>
        <label class="field largo">
          <span>{{ $t('scripting.description') }}</span>
          <input v-model="form.description" type="text" maxlength="4000" :disabled="!editable || saving" />
        </label>
        <div class="field largo">
          <label for="environment-packages">{{ $t('scripting.packages') }}</label>
          <div v-if="editable" class="package-search">
            <div class="combo">
              <Search :size="13" class="lens" aria-hidden="true" />
              <input
                v-model="pkgQuery"
                type="search"
                role="combobox"
                aria-autocomplete="list"
                aria-controls="packages-found"
                :aria-expanded="pkgOpen"
                :aria-activedescendant="pkgOpen && pkgActive >= 0 ? `pacchetto-${pkgActive}` : undefined"
                :aria-label="form.language === 'python' ? $t('scripting.searchPyPI') : $t('scripting.searchCRAN')"
                :placeholder="form.language === 'python' ? $t('scripting.searchPyPI') : $t('scripting.searchCRAN')"
                autocomplete="off"
                spellcheck="false"
                :disabled="saving"
                @input="onPackageInput"
                @keydown="onPackageKey"
                @blur="pkgOpen = false"
              />
              <LoaderCircle v-if="pkgSearching" :size="13" class="spin waiting" />
              <ul v-if="pkgOpen" id="packages-found" role="listbox" class="found">
                <li
                  v-for="(h, i) in pkgHits"
                  :id="`pacchetto-${i}`"
                  :key="h.name"
                  role="option"
                  :aria-selected="i === pkgActive"
                  :class="{ 'is-on': i === pkgActive }"
                  @mousedown.prevent="choosePackage(h)"
                  @mouseenter="pkgActive = i"
                >
                  <span>{{ h.name }}</span><span v-if="h.version" class="muted">{{ h.version }}</span>
                </li>
                <li v-if="!pkgHits.length" class="empty muted">{{ $t('scripting.noPackageFound') }}</li>
              </ul>
            </div>
            <template v-if="pkgChosen && pkgVersions.length">
              <select v-model="pkgVersion" :aria-label="$t('scripting.packageVersion')">
                <option v-for="v in pkgVersions" :key="v.version" :value="v.version">
                  {{ v.version }}{{ v.pre ? ` · ${$t('scripting.preRelease')}` : '' }}{{ v.wheel === false ? ` · ${$t('scripting.compiles')}` : '' }}
                </option>
              </select>
              <button type="button" class="mini" @click="addPackage"><Plus :size="13" /> {{ $t('scripting.addPackage') }}</button>
            </template>
          </div>
          <small v-if="pkgError" class="error" role="alert">{{ pkgError }}</small>
          <small v-else-if="chosenVersion?.wheel === false" class="package-warning" role="note">
            {{ $t('scripting.noWheel', { name: pkgChosen, version: chosenVersion.version, python: form.language_version }) }}
          </small>
          <textarea
            id="environment-packages"
            v-model="form.packages"
            class="packages"
            rows="7"
            spellcheck="false"
            :placeholder="form.language === 'python' ? 'scikit-learn==1.5.2\nrequests' : 'dplyr\ndata.table@1.16.0'"
            :disabled="!editable || saving"
            :aria-invalid="!!packageError"
          />
          <small v-if="packageError" class="error" role="alert">
            {{ $t('scripting.packageError', { line: packageError.line, text: packageError.text }) }}
          </small>
          <small v-else>
            {{ form.language === 'python' ? $t('scripting.packagesHintPython') : $t('scripting.packagesHintR') }}
            {{ $t('scripting.packagesAlways', { list: basePackages }) }}
          </small>
        </div>
      </div>

      <fieldset class="field-group">
        <legend>{{ $t('scripting.resources') }}</legend>
        <div class="resources">
          <label class="field"><span>{{ $t('scripting.cpuRequest') }}</span><input v-model="form.cpu_request" type="text" :disabled="!editable || saving" /></label>
          <label class="field"><span>{{ $t('scripting.cpuLimit') }}</span><input v-model="form.cpu_limit" type="text" :disabled="!editable || saving" /></label>
          <label class="field"><span>{{ $t('scripting.memoryRequest') }}</span><input v-model="form.memory_request" type="text" :disabled="!editable || saving" /></label>
          <label class="field"><span>{{ $t('scripting.memoryLimit') }}</span><input v-model="form.memory_limit" type="text" :disabled="!editable || saving" /></label>
          <label class="field"><span>{{ $t('scripting.maxTimeout') }}</span><input v-model.number="form.max_timeout_minutes" type="number" min="1" max="1440" :disabled="!editable || saving" /></label>
        </div>
      </fieldset>

      <fieldset class="field-group">
        <legend>{{ $t('scripting.groups') }}</legend>
        <p class="hint muted small">{{ $t('scripting.groupsHint') }}</p>
        <div class="groups">
          <label v-for="g in props.groups" :key="g.id" class="tick">
            <input v-model="form.group_ids" type="checkbox" :value="g.id" :disabled="!editable || saving" />
            <span>{{ g.name }}</span>
          </label>
          <span v-if="!props.groups.length" class="muted small">{{ $t('scripting.noGroups') }}</span>
        </div>
      </fieldset>

      <label class="tick">
        <input v-model="form.enabled" type="checkbox" :disabled="!editable || saving" />
        <span>{{ $t('scripting.enabled') }}</span>
      </label>
      <p v-if="env?.flows_using.length" class="hint muted small">
        {{ $t('scripting.usedBy', { flows: env.flows_using.join(', ') }) }}
      </p>

      <div v-if="editable" class="actions">
        <button type="submit" class="primary" :disabled="saving || !formDirty || !form.name.trim() || !form.language_version">
          <LoaderCircle v-if="saving" :size="13" class="spin" />
          {{ open === 'new' ? $t('scripting.createAndBuild') : rebuilds ? $t('scripting.saveAndBuild') : $t('scripting.save') }}
        </button>
      </div>
    </form>

    <template v-if="env">
      <!-- variables -->
      <div class="card">
        <h4><KeyRound :size="14" /> {{ $t('scripting.variablesTitle') }}</h4>
        <p class="hint muted small">{{ $t('scripting.variablesLead') }}</p>
        <table v-if="env.variable_list.length" class="variables">
          <tbody>
            <tr v-for="v in env.variable_list" :key="v.name">
              <td><code>{{ v.name }}</code></td>
              <td>
                <span v-if="v.from_tabularia" class="exception"><TriangleAlert :size="12" /> {{ $t('scripting.fromTabularia', { name: v.value }) }}</span>
                <span v-else-if="v.secret" class="muted">•••••••• <em>{{ $t('scripting.secret') }}</em></span>
                <code v-else class="value">{{ v.value }}</code>
              </td>
              <td class="muted small">{{ fmt.ago(v.updated_at) }}</td>
              <td v-if="editable" class="row-actions">
                <button type="button" class="mini" @click="editVariable(v.name)">{{ $t('scripting.change') }}</button>
                <button type="button" class="mini icon-only" :aria-label="$t('scripting.delete')" @click="deleteVariable(v.name)"><Trash2 :size="13" /></button>
              </td>
            </tr>
          </tbody>
        </table>
        <form v-if="editable" class="new-variable" @submit.prevent="saveVariable">
          <input v-model="variable.name" type="text" :placeholder="$t('scripting.variableName')" :disabled="!!editingVariable || savingVariable" spellcheck="false" />
          <input
            v-model="variable.value"
            :type="variable.secret && !variable.from_tabularia ? 'password' : 'text'"
            autocomplete="new-password"
            :placeholder="variable.from_tabularia ? $t('scripting.tabulariaVariableName') : editingVariable && variable.secret ? $t('scripting.secretKeep') : $t('scripting.variableValue')"
            :disabled="savingVariable"
            spellcheck="false"
          />
          <label class="tick"><input v-model="variable.secret" type="checkbox" :disabled="variable.from_tabularia || savingVariable" /> {{ $t('scripting.secret') }}</label>
          <label class="tick"><input v-model="variable.from_tabularia" type="checkbox" :disabled="savingVariable" /> {{ $t('scripting.fromTabulariaToggle') }}</label>
          <button type="submit" class="primary mini" :disabled="savingVariable || !variable.name.trim()">
            {{ editingVariable ? $t('scripting.save') : $t('scripting.add') }}
          </button>
          <button v-if="editingVariable" type="button" class="mini" @click="clearVariable">{{ $t('scripting.cancel') }}</button>
        </form>
        <p v-if="editable && variable.from_tabularia" class="notice" role="note">
          <TriangleAlert :size="13" /> {{ $t('scripting.fromTabulariaWarning') }}
        </p>
      </div>

      <!-- versions -->
      <div class="card">
        <div class="heading">
          <h4>{{ $t('scripting.versionsTitle') }}</h4>
          <button v-if="editable && env.versions.length && !building" type="button" class="mini" @click="rebuild">
            <RotateCcw :size="13" /> {{ $t('scripting.rebuild') }}
          </button>
        </div>
        <p class="hint muted small">{{ $t('scripting.versionsLead') }}</p>
        <ul class="versions">
          <li v-for="v in env.versions" :key="v.id" :class="v.status">
            <div class="version-row">
              <component :is="statusIcon(v.status)" :size="14" :class="{ spin: ['queued', 'building', 'checking'].includes(v.status) }" />
              <strong>v{{ v.number }}</strong>
              <span>{{ languageLabel(env.language, v.language_version) }}</span>
              <span class="label">{{ $t(`scripting.status.${v.status}`) }}</span>
              <span v-if="v.current" class="label current">{{ $t('scripting.current') }}</span>
              <span class="muted small">{{ v.created_by }} · {{ fmt.ago(v.created_at) }}<template v-if="duration(v)"> · {{ duration(v) }}</template></span>
              <button v-if="editable && v.status === 'ready' && !v.current" type="button" class="mini" @click="makeCurrent(v)">
                {{ $t('scripting.makeCurrent') }}
              </button>
            </div>
            <p v-if="v.error" class="version-error"><code>{{ v.error }}</code></p>
            <details v-if="v.packages">
              <summary>{{ $t('scripting.requested') }}</summary>
              <pre>{{ v.packages }}</pre>
            </details>
            <details v-if="v.lock">
              <summary>{{ $t('scripting.installed', { n: v.lock.split('\n').length }) }}</summary>
              <pre>{{ v.lock }}</pre>
            </details>
            <details v-if="v.build_log" :open="v.status === 'failed'">
              <summary>{{ $t('scripting.buildLog') }}</summary>
              <pre>{{ v.build_log }}</pre>
            </details>
            <p v-if="v.image_ref" class="image muted small"><code>{{ v.image_ref }}</code></p>
          </li>
        </ul>
      </div>
    </template>
  </template>

  <p v-if="state && !state.editable" class="note">{{ $t('scripting.readOnly') }}</p>
</template>

<style scoped>
.card { display: flex; flex-direction: column; gap: 10px; padding: 16px; background: var(--panel); border: 1px solid var(--border); border-radius: var(--radius); }
.card h4 { margin: 0; display: inline-flex; align-items: center; gap: 7px; }
.hint { margin: 0; line-height: 1.5; }
.small { font-size: 12px; }
.page-error, .error { color: var(--danger); }
.page-error { font-size: 13px; margin: 0; }
.note { margin: 0; display: flex; gap: 6px; align-items: flex-start; font-size: 12.5px; padding: 8px 10px; border-radius: 8px; background: var(--bg-soft); border: 1px solid var(--border); }
.heading { display: flex; align-items: center; gap: 10px; }
.heading h4 { flex: 1; }
button.mini { padding: 2px 10px; min-height: 26px; font-size: 12px; display: inline-flex; align-items: center; gap: 5px; }
button.mini.icon-only { padding: 2px 6px; }
button.danger-zone { color: var(--danger); }

.fields { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px 14px; }
.field { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); min-width: 0; }
.field.largo { grid-column: 1 / -1; }
.field small { font-size: 11.5px; line-height: 1.45; }
.field-double { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.package-search { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.combo { position: relative; flex: 1 1 260px; min-width: 0; }
.combo input { width: 100%; padding-left: 28px; }
.combo .lens { position: absolute; left: 9px; top: 50%; transform: translateY(-50%); color: var(--muted); }
.combo .waiting { position: absolute; right: 9px; top: 50%; margin-top: -6.5px; color: var(--muted); }
.found {
  position: absolute; z-index: 20; left: 0; right: 0; top: calc(100% + 4px); margin: 0; padding: 4px; list-style: none;
  max-height: 280px; overflow: auto; background: var(--panel); border: 1px solid var(--border); border-radius: 8px;
  box-shadow: 0 8px 24px color-mix(in srgb, var(--text) 12%, transparent);
}
.found li { display: flex; justify-content: space-between; gap: 10px; padding: 6px 8px; border-radius: 6px; font-size: 13px; color: var(--text); cursor: pointer; }
.found li.is-on { background: var(--bg-soft); }
.found li.empty { cursor: default; }
.package-search select { flex: 0 1 260px; min-width: 0; }
.package-warning { color: var(--warning); }
.packages { font-family: var(--font-mono, ui-monospace, monospace); font-size: 12.5px; resize: vertical; }
.field-group { border: 1px solid var(--border-soft); border-radius: 8px; padding: 10px 12px; margin: 0; display: flex; flex-direction: column; gap: 8px; }
.field-group legend { font-size: 12px; color: var(--muted); padding: 0 4px; }
.resources { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 8px; }
.groups { display: flex; flex-wrap: wrap; gap: 6px 16px; }
.tick { display: inline-flex; align-items: center; gap: 6px; font-size: 13px; }
.actions { display: flex; gap: 8px; flex-wrap: wrap; }
.notice { margin: 0; display: flex; gap: 6px; align-items: flex-start; font-size: 12.5px; color: var(--warning); }
.outcome { margin: 0; display: flex; gap: 8px; align-items: flex-start; font-size: 13px; }
.outcome code { display: block; margin-top: 4px; font-size: 11.5px; white-space: pre-wrap; overflow-wrap: anywhere; color: var(--muted); }
.outcome.ok svg { color: var(--accent-2); }
.outcome.caution svg { color: var(--warning); }
.outcome.ko svg { color: var(--danger); }

.environments { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
.environment {
  width: 100%; display: grid; grid-template-columns: minmax(0, 1.4fr) minmax(0, 0.8fr) minmax(0, 1fr) minmax(0, 1.4fr);
  gap: 10px; align-items: center; text-align: left; padding: 10px 12px; border: 1px solid var(--border-soft);
  border-radius: 8px; background: var(--bg-soft); font-size: 13px; min-height: 0;
}
.environment:hover { border-color: var(--border); }
.name { font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.off { font-style: normal; font-weight: 400; font-size: 11px; color: var(--muted); margin-left: 8px; }
.version-state { display: inline-flex; align-items: center; gap: 5px; font-size: 12px; }
.version-state.ready svg, .versions li.ready > .version-row svg { color: var(--accent-2); }
.version-state.failed, .version-state.failed svg, .versions li.failed > .version-row svg { color: var(--danger); }

.variables { width: 100%; border-collapse: collapse; font-size: 13px; }
.variables td { padding: 6px 8px; border-top: 1px solid var(--border-soft); vertical-align: middle; }
.variables .value { overflow-wrap: anywhere; }
.variables em { font-size: 11px; }
.exception { display: inline-flex; gap: 5px; align-items: center; color: var(--warning); font-size: 12.5px; }
.row-actions { display: flex; gap: 6px; justify-content: flex-end; }
.new-variable { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1.4fr) auto auto auto auto; gap: 8px; align-items: center; }

.versions { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; }
.versions li { border: 1px solid var(--border-soft); border-radius: 8px; padding: 8px 10px; display: flex; flex-direction: column; gap: 6px; }
.version-row { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; font-size: 13px; }
.label { font-size: 11px; padding: 1px 7px; border-radius: 999px; border: 1px solid var(--border); }
.label.current { border-color: color-mix(in srgb, var(--accent-2) 55%, var(--border)); color: var(--accent-2); }
.version-error { margin: 0; }
.version-error code { display: block; font-size: 11.5px; white-space: pre-wrap; overflow-wrap: anywhere; color: var(--danger); }
details summary { cursor: pointer; font-size: 12px; color: var(--muted); }
details pre { margin: 6px 0 0; max-height: 280px; overflow: auto; font-size: 11.5px; padding: 8px; border-radius: 6px; background: var(--bg-soft); white-space: pre-wrap; overflow-wrap: anywhere; }
.image { margin: 0; overflow-wrap: anywhere; }

@media (max-width: 720px) {
  .fields, .resources, .environment, .new-variable { grid-template-columns: 1fr; }
}
</style>
