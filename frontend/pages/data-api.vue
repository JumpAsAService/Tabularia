<script setup lang="ts">
// A data API: datasources published as a read-only HTTP API for other systems, answered by pods
// of its own on Kubernetes (docs/specs/data-apis.md). Its address (/data/{slug}/), its
// resources (a datasource each, the columns that go out, the ones one may filter on and how,
// the default order, the largest page), its runtime, its state, its keys and its versions.
// Whoever has MANAGE on the folder edits it and manages its keys; VIEW reads it. The runtime
// (replicas, CPU, memory) is an administrator's alone: shown to the others, changed here only
// when an administrator opens the page (and in Administration → Data APIs). Its pods' status
// and traffic are DataApiStatus's.
//
// /data-api?id=N opens a data API; /data-api?project=P starts a new one in folder P.
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { onBeforeRouteLeave, onBeforeRouteUpdate } from 'vue-router'
import {
  BookOpen, Braces, ChevronDown, ChevronRight, CircleAlert, Cpu, History, KeyRound, LoaderCircle, Pause, Play, Plus, RefreshCw, RotateCcw,
  Save, Server, SlidersHorizontal, Trash2, X,
} from 'lucide-vue-next'
import { errMessage } from '~/composables/useApi'
import { useFormat } from '~/composables/useFormat'
import { useFolderPaths } from '~/composables/useFolderPaths'
import { versionNote } from '~/composables/useSavedScripts'
import { useSqlScripts, type SqlCatalogItem } from '~/composables/useSqlScripts'
import { dayAfter, endOfDay } from '~/composables/shareLinks'
import {
  cidrsOf, dataApiOrigin, keyAnswers, newResource, operatorsFor, runtimeProblem, slugOf, tidyResource, tidyRuntime, useDataApis, validLabel,
  type DataApiDetail, type DataApiKey, type DataApiLimits, type DataApiOperator, type DataApiResource, type DataApiRuntime,
  type DataApiVersion,
} from '~/composables/useDataApis'
import Select from '~/components/ui/Select.vue'
import SecretOnce from '~/components/ui/SecretOnce.vue'
import DataApiStatus from '~/components/dataapi/DataApiStatus.vue'
import DataApiDocs from '~/components/dataapi/DataApiDocs.vue'
import DataApiRuntimeForm from '~/components/dataapi/DataApiRuntimeForm.vue'

const { t } = useI18n()
const fmtx = useFormat()
const toast = useToast()
const route = useRoute()
const api = useDataApis()
const sqlApi = useSqlScripts()
const folderPaths = useFolderPaths()

const apiId = ref<number | null>(Number(route.query.id) || null)
const projectId = ref<number | null>(Number(route.query.project) || null)
const detail = ref<DataApiDetail | null>(null)
const limits = ref<DataApiLimits | null>(null)
const loading = ref(true)
const loadError = ref('')
const saving = ref(false)

// what is on screen; `savedAs` is the same thing as last saved, to tell unsaved changes
const name = ref('')
const slug = ref('')
const slugTouched = ref(false)
const description = ref('')
const resources = ref<DataApiResource[]>([])
const runtime = ref<DataApiRuntime>({ autoscale: false, replicas: 1, min_replicas: 1, max_replicas: 1, cpu: '250m', memory: '256Mi' })
const savedAs = ref('')
// the runtime is not the page's to save (an administrator's, saved on its own): not in it
const snapshot = () => JSON.stringify([name.value.trim(), slug.value.trim(), description.value.trim(),
  resources.value.map(tidyResource)])
const dirty = computed(() => snapshot() !== savedAs.value)

const canEdit = computed(() => (detail.value ? detail.value.can_manage : true))
const folderPath = computed(() => folderPaths.pathOf(projectId.value) ?? '')
const off = computed(() => !!limits.value && !limits.value.enabled)

// the page's tabs: the definition, the pods and their traffic, the documentation (a saved data
// API only); the tab is in the address, so a link opens on it
type Tab = 'definition' | 'pods' | 'docs'
const TABS: Tab[] = ['definition', 'pods', 'docs']
const tabOf = (q: unknown): Tab => (TABS.includes(q as Tab) ? (q as Tab) : 'definition')
const tab = ref<Tab>(tabOf(route.query.tab))
function setTab(next: Tab) {
  tab.value = next
  const query = { ...route.query, tab: next === 'definition' ? undefined : next }
  navigateTo({ query }, { replace: true })
}
watch(() => route.query.tab, (q) => { tab.value = tabOf(q) })
const shownTab = computed<Tab>(() => (detail.value ? tab.value : 'definition'))

/** Where the other systems call it: its public address, or the interface's own. */
const callAt = computed(() => {
  const l = limits.value
  if (!l || !slug.value) return ''
  return `${dataApiOrigin(l.base_url ?? '', l.host ?? '', location)}/data/${slug.value}/`
})

// a new data API takes its address from its name until the address is typed
watch(name, (n) => { if (apiId.value == null && !slugTouched.value) slug.value = slugOf(n) })

// ── the datasources one may publish: those this person reads ─────────────────
const catalog = ref<SqlCatalogItem[]>([])
async function loadCatalog() {
  try { catalog.value = await sqlApi.catalog() } catch { catalog.value = [] }
}
const dsOptions = computed(() => catalog.value.map((d) => ({ value: d.id, label: d.name, hint: d.path })))

/** The columns a resource may choose from: its datasource's, as this person reads it. */
function columnsOf(r: DataApiResource): { name: string; type: string }[] {
  const mine = catalog.value.find((d) => d.id === r.source.id)
  if (mine) return mine.columns
  const saved = detail.value?.resource_list.find((x) => x.name === r.name && x.source.id === r.source.id)
  return saved?.datasource.columns ?? []
}
/** A resource whose datasource this person may not read: they leave it as it is. */
function foreign(r: DataApiResource): boolean {
  return !catalog.value.some((d) => d.id === r.source.id)
}
function dsLabel(r: DataApiResource): string {
  const saved = detail.value?.resource_list.find((x) => x.source.id === r.source.id)
  if (saved && !saved.datasource.exists) return t('dataApi.datasourceGone')
  return catalog.value.find((d) => d.id === r.source.id)?.name ?? saved?.datasource.name ?? t('dataApi.datasourceUnreadable')
}

const open = ref<number | null>(0)
function addResource(id: number) {
  const ds = catalog.value.find((d) => d.id === id)
  if (!ds) return
  resources.value.push(newResource(ds, resources.value.map((r) => r.name), limits.value?.default_page ?? 1000))
  open.value = resources.value.length - 1
  adding.value = false
}
const adding = ref(false)
function removeResource(i: number) {
  resources.value.splice(i, 1)
  open.value = null
}
function chooseDatasource(r: DataApiResource, id: number) {
  const ds = catalog.value.find((d) => d.id === id)
  if (!ds) return
  const fresh = newResource(ds, [], r.max_page)
  r.source = fresh.source
  r.columns = fresh.columns
  r.filters = fresh.filters
  r.default_sort = fresh.default_sort
}
function goesOut(r: DataApiResource, col: string): boolean { return r.columns.includes(col) }
function toggleOut(r: DataApiResource, col: string) {
  if (goesOut(r, col)) {
    r.columns = r.columns.filter((c) => c !== col)
    Object.assign(r, tidyResource(r))
  } else {
    const order = columnsOf(r).map((c) => c.name)
    r.columns = order.filter((c) => c === col || r.columns.includes(c))
  }
}
function filterOf(r: DataApiResource, col: string) { return r.filters.find((f) => f.column === col) }
function toggleFilter(r: DataApiResource, col: string, type: string) {
  if (filterOf(r, col)) {
    r.filters = r.filters.filter((f) => f.column !== col)
    Object.assign(r, tidyResource(r))
  } else {
    const ops = operatorsFor(type)
    r.filters.push({ column: col, operators: ops.filter((o) => o === 'eq' || o === 'in') })
  }
}
function toggleOp(r: DataApiResource, col: string, op: DataApiOperator) {
  const f = filterOf(r, col)
  if (!f) return
  f.operators = f.operators.includes(op) ? f.operators.filter((o) => o !== op) : [...f.operators, op]
}
const sortOptions = (r: DataApiResource) => [
  { value: '', label: t('dataApi.sortNone') },
  ...r.filters.map((f) => ({ value: f.column, label: f.column })),
]
function setSort(r: DataApiResource, col: string) {
  const desc = (r.default_sort[0] ?? '').startsWith('-')
  r.default_sort = col ? [(desc ? '-' : '') + col, ...r.default_sort.slice(1).filter((s) => s.replace(/^-/, '') !== col)] : []
}
function toggleDesc(r: DataApiResource) {
  const first = r.default_sort[0]
  if (!first) return
  r.default_sort = [first.startsWith('-') ? first.slice(1) : `-${first}`, ...r.default_sort.slice(1)]
}
const opLabel = (op: DataApiOperator) => t(`dataApi.op_${op}`)

// what is wrong before the gateway says it
const problem = computed(() => {
  if (!name.value.trim()) return t('dataApi.nameRequired')
  if (!validLabel(slug.value.trim())) return t('dataApi.slugInvalid')
  const names = new Set<string>()
  for (const r of resources.value) {
    if (!validLabel(r.name)) return t('dataApi.resourceNameInvalid', { name: r.name || '…' })
    if (names.has(r.name)) return t('dataApi.resourceNameTwice', { name: r.name })
    names.add(r.name)
    if (!r.columns.length) return t('dataApi.resourceNoColumns', { name: r.name })
  }
  return ''
})

// ── load, save ───────────────────────────────────────────────────────────────
function apply(d: DataApiDetail) {
  detail.value = d
  apiId.value = d.id
  projectId.value = d.project_id
  name.value = d.name
  slug.value = d.slug
  slugTouched.value = true
  description.value = d.description
  resources.value = d.resource_list.map((r) => ({ name: r.name, source: { kind: 'datasource', id: r.source.id },
    columns: [...r.columns], filters: r.filters.map((f) => ({ column: f.column, operators: [...f.operators] })),
    default_sort: [...r.default_sort], max_page: r.max_page }))
  runtime.value = { ...d.runtime }
  runtimeDraft.value = null
  savedAs.value = snapshot()
}

async function load() {
  loadError.value = ''
  loading.value = true
  try {
    limits.value = await api.limits()
    if (!limits.value.enabled) return
    if (apiId.value == null) {
      detail.value = null
      if (limits.value.default_runtime) runtime.value = { ...limits.value.default_runtime }
      savedAs.value = ''
      return
    }
    apply(await api.get(apiId.value))
    await Promise.all([loadVersions(), loadKeys()])
  } catch (e) {
    loadError.value = errMessage(e)
  } finally {
    loading.value = false
  }
}

async function save() {
  if (saving.value || !canEdit.value || off.value) return
  if (problem.value) {
    toast.error(problem.value)
    return
  }
  saving.value = true
  // never the runtime: an administrator may have changed it since the page opened
  const body = { name: name.value.trim(), slug: slug.value.trim(), description: description.value.trim(),
    resources: resources.value.map(tidyResource) }
  try {
    if (apiId.value != null) {
      apply(await api.update(apiId.value, body))
      await loadVersions()
      toast.success(t('dataApi.saved', { name: body.name }))
    } else if (projectId.value != null) {
      const d = await api.create(projectId.value, body)
      apply(d)
      navigateTo(`/data-api?id=${d.id}`, { replace: true })
      await Promise.all([loadVersions(), loadKeys()])
      toast.success(t('dataApi.created', { name: body.name }))
    }
  } catch (e) {
    toast.error(errMessage(e))
  } finally {
    saving.value = false
  }
}

// ── the runtime, an administrator's ─────────────────────────────────────────
const runtimeDraft = ref<DataApiRuntime | null>(null)
const savingRuntime = ref(false)
const runtimeIssue = computed(() => {
  const l = limits.value?.runtime_limits
  const p = runtimeDraft.value && l ? runtimeProblem(runtimeDraft.value, l) : null
  return p ? t(`dataApi.${p.key}`, p.values ?? {}) : ''
})
function editRuntime() {
  runtimeDraft.value = { ...runtime.value }
}
async function saveRuntime() {
  if (!detail.value || !runtimeDraft.value || savingRuntime.value || runtimeIssue.value) return
  savingRuntime.value = true
  try {
    const got = await api.adminSetRuntime(detail.value.id, tidyRuntime(runtimeDraft.value))
    runtime.value = { ...got }
    detail.value = { ...detail.value, runtime: { ...got } }
    runtimeDraft.value = null
    toast.success(t(detail.value.state === 'active' ? 'dataApi.runtimeSavedActive' : 'dataApi.runtimeSaved'))
  } catch (e) {
    toast.error(errMessage(e))
  } finally {
    savingRuntime.value = false
  }
}

// ── state ────────────────────────────────────────────────────────────────────
const switching = ref(false)
async function setState(to: 'active' | 'paused') {
  if (!detail.value || switching.value) return
  if (dirty.value) {
    toast.error(t('dataApi.saveFirst'))
    return
  }
  switching.value = true
  try {
    apply(to === 'active' ? await api.activate(detail.value.id) : await api.pause(detail.value.id))
    toast.success(t(to === 'active' ? 'dataApi.activated' : 'dataApi.paused'))
  } catch (e) {
    toast.error(errMessage(e))
  } finally {
    switching.value = false
  }
}
const stateLabel = computed(() => (detail.value ? t(`dataApi.state_${detail.value.state}`) : t('dataApi.state_new')))

// ── versions ─────────────────────────────────────────────────────────────────
const versions = ref<DataApiVersion[]>([])
async function loadVersions() {
  if (apiId.value == null) return
  try { versions.value = await api.versions(apiId.value) } catch { versions.value = [] }
}
const restoring = ref<number | null>(null)
async function restore(version: number) {
  if (apiId.value == null || restoring.value != null) return
  if (dirty.value && !confirm(t('dataApi.restoreDiscards'))) return
  restoring.value = version
  try {
    apply(await api.restore(apiId.value, version))
    await loadVersions()
    toast.success(t('dataApi.restored', { n: version }))
  } catch (e) {
    toast.error(errMessage(e))
  } finally {
    restoring.value = null
  }
}

// ── keys ─────────────────────────────────────────────────────────────────────
const keys = ref<DataApiKey[]>([])
async function loadKeys() {
  if (apiId.value == null || !detail.value?.can_manage) return
  try { keys.value = await api.keys(apiId.value) } catch { keys.value = [] }
}
const maxKeyDay = computed(() => dayAfter(limits.value?.key_max_days ?? 365))
const keyForm = ref(false)
const keyName = ref('')
const keyExpires = ref(dayAfter(90))
const keyRate = ref<number>(600)
const keyResources = ref<string[]>([])
const keyCidrs = ref('')
const keyBusy = ref(false)
const secret = ref<string | null>(null)
function startKey() {
  keyForm.value = true
  keyName.value = ''
  keyExpires.value = dayAfter(Math.min(90, limits.value?.key_max_days ?? 90))
  keyRate.value = limits.value?.key_default_rate ?? 600
  keyResources.value = []
  keyCidrs.value = ''
}
async function createKey() {
  if (apiId.value == null || keyBusy.value || !keyName.value.trim() || !keyExpires.value) return
  keyBusy.value = true
  try {
    const out = await api.createKey(apiId.value, { name: keyName.value.trim(), expires_at: endOfDay(keyExpires.value),
      rate_per_minute: Number(keyRate.value), resources: keyResources.value, cidrs: cidrsOf(keyCidrs.value) })
    secret.value = out.secret
    keyForm.value = false
    await loadKeys()
  } catch (e) {
    toast.error(errMessage(e))
  } finally {
    keyBusy.value = false
  }
}
const revoking = ref<number | null>(null)
async function revokeKey(k: DataApiKey) {
  if (apiId.value == null) return
  try {
    await api.revokeKey(apiId.value, k.id)
    revoking.value = null
    toast.success(t('dataApi.keyRevoked', { name: k.name }))
    await loadKeys()
  } catch (e) {
    toast.error(errMessage(e))
  }
}
const renewing = ref<number | null>(null)
const renewExpires = ref(dayAfter(90))
const renewGrace = ref(24)
const graceOptions = computed(() => [0, 1, 24, 72, 168, 720].filter((h) => h <= (limits.value?.max_grace_days ?? 30) * 24)
  .map((h) => ({ value: h, label: h === 0 ? t('dataApi.graceNone') : t('dataApi.graceHours', { n: h }, h) })))
function startRenew(k: DataApiKey) {
  renewing.value = k.id
  renewExpires.value = dayAfter(Math.min(90, limits.value?.key_max_days ?? 90))
  renewGrace.value = 24
}
async function renewKey(k: DataApiKey) {
  if (apiId.value == null || keyBusy.value) return
  keyBusy.value = true
  try {
    const out = await api.renewKey(apiId.value, k.id, { expires_at: endOfDay(renewExpires.value), grace_hours: renewGrace.value })
    secret.value = out.secret
    renewing.value = null
    await loadKeys()
  } catch (e) {
    toast.error(errMessage(e))
  } finally {
    keyBusy.value = false
  }
}
const keyStateLabel = (k: DataApiKey) => t(`dataApi.key_${k.state}`)

// ── leaving with unsaved changes ─────────────────────────────────────────────
const guard = () => dirty.value && canEdit.value && !off.value && !loading.value
function onBeforeUnload(e: BeforeUnloadEvent) {
  if (guard()) e.preventDefault()
}
onMounted(() => {
  window.addEventListener('beforeunload', onBeforeUnload)
  folderPaths.load()
  loadCatalog()
  load()
})
onUnmounted(() => window.removeEventListener('beforeunload', onBeforeUnload))
onBeforeRouteLeave(() => !guard() || confirm(t('dataApi.leaveConfirm')))
onBeforeRouteUpdate((to) => (Number(to.query.id) || null) === apiId.value || !guard() || confirm(t('dataApi.leaveConfirm')))
watch(() => route.query.id, (id) => {
  const next = Number(id) || null
  if (next === apiId.value) return // a data API just created took its address
  apiId.value = next
  versions.value = []
  keys.value = []
  load()
})
</script>

<template>
  <AppShell>
    <div class="da">
      <div class="page-head">
        <h1>
          <Braces :size="18" />
          <input
            v-model="name"
            class="dname"
            type="text"
            maxlength="200"
            :readonly="!canEdit || off"
            :placeholder="$t('dataApi.namePlaceholder')"
            :aria-label="$t('dataApi.nameLabel')"
          />
        </h1>
        <span v-if="folderPath" class="where muted">{{ $t('dataApi.inFolder', { path: folderPath }) }}</span>
        <div class="head-actions">
          <span v-if="detail" class="tag">v{{ detail.current_version }}</span>
          <span class="tag" :class="['st', detail?.state ?? 'new']">{{ stateLabel }}</span>
          <span v-if="dirty && canEdit && detail" class="tag unsaved">{{ $t('dataApi.unsaved') }}</span>
          <template v-if="detail && canEdit">
            <button v-if="detail.state !== 'active'" type="button" :disabled="switching || !detail.resources" @click="setState('active')">
              <LoaderCircle v-if="switching" :size="14" class="spin" /><Play v-else :size="14" /> {{ $t('dataApi.activate') }}
            </button>
            <button v-else type="button" :disabled="switching" @click="setState('paused')">
              <LoaderCircle v-if="switching" :size="14" class="spin" /><Pause v-else :size="14" /> {{ $t('dataApi.pause') }}
            </button>
          </template>
          <button v-if="canEdit && !off" class="primary" :disabled="saving || loading || (!dirty && apiId != null)" @click="save">
            <LoaderCircle v-if="saving" :size="14" class="spin" /><Save v-else :size="14" />
            {{ apiId == null ? $t('dataApi.create') : $t('dataApi.save') }}
          </button>
        </div>
      </div>

      <p v-if="loadError" class="err" role="alert">{{ loadError }}</p>
      <SkeletonRows v-else-if="loading" :rows="5" />
      <p v-else-if="off" class="note" role="status">
        <CircleAlert :size="14" /> {{ $t(limits?.reason === 'disabled' ? 'dataApi.offDisabled' : 'dataApi.offKubernetes') }}
      </p>

      <div v-else class="work">
        <section class="main">
          <p v-if="!canEdit" class="note small" role="note">{{ $t('dataApi.readOnly') }}</p>
          <p v-if="detail?.suspended" class="note warn small" role="status">
            <CircleAlert :size="14" /> {{ $t(`dataApi.suspended_${detail.suspended}`) }}
          </p>
          <div v-if="detail" class="tabs" role="tablist" :aria-label="$t('dataApi.tabs')">
            <button type="button" role="tab" :aria-selected="shownTab === 'definition'" :class="{ on: shownTab === 'definition' }"
                    data-tab="definition" @click="setTab('definition')">
              <SlidersHorizontal :size="13" /> {{ $t('dataApi.tabDefinition') }}
            </button>
            <button type="button" role="tab" :aria-selected="shownTab === 'pods'" :class="{ on: shownTab === 'pods' }"
                    data-tab="pods" @click="setTab('pods')">
              <Server :size="13" /> {{ $t('dataApi.stTitle') }}
            </button>
            <button type="button" role="tab" :aria-selected="shownTab === 'docs'" :class="{ on: shownTab === 'docs' }"
                    data-tab="docs" @click="setTab('docs')">
              <BookOpen :size="13" /> {{ $t('dataApi.docTitle') }}
            </button>
          </div>
          <DataApiStatus v-if="detail && shownTab === 'pods'" :api-id="detail.id" :state="detail.state" />
          <DataApiDocs v-if="detail && shownTab === 'docs'" :api-id="detail.id" :state="detail.state" :version="detail.current_version" />

          <template v-if="shownTab === 'definition'">
          <div class="card">
            <h2>{{ $t('dataApi.address') }}</h2>
            <label class="field">
              <span>{{ $t('dataApi.slugLabel') }}</span>
              <span class="slugline">
                <code class="muted">/data/</code>
                <input v-model="slug" type="text" maxlength="63" class="mono" :readonly="!canEdit || detail?.state === 'active'"
                       :aria-invalid="!validLabel(slug)" @input="slugTouched = true" />
                <code class="muted">/</code>
              </span>
            </label>
            <p class="muted small help">{{ $t(detail?.state === 'active' ? 'dataApi.slugActive' : 'dataApi.slugHelp') }}</p>
            <p v-if="callAt" class="muted small help">{{ $t('dataApi.callAt') }} <code class="callat">{{ callAt }}</code></p>
            <label class="field">
              <span>{{ $t('dataApi.description') }}</span>
              <textarea v-model="description" rows="2" maxlength="2000" :readonly="!canEdit" :placeholder="$t('dataApi.descriptionPlaceholder')" />
            </label>
          </div>

          <div class="card">
            <h2>{{ $t('dataApi.resources') }} <span class="muted small">{{ resources.length }}</span></h2>
            <p class="muted small help">{{ $t('dataApi.resourcesHelp') }}</p>
            <p v-if="!resources.length" class="muted small">{{ $t('dataApi.noResources') }}</p>
            <div v-for="(r, i) in resources" :key="i" class="res" :class="{ isopen: open === i }">
              <div class="res-head">
                <button type="button" class="chev" :aria-expanded="open === i" :aria-label="$t('dataApi.resourceToggle', { name: r.name })"
                        @click="open = open === i ? null : i">
                  <ChevronDown v-if="open === i" :size="14" /><ChevronRight v-else :size="14" />
                </button>
                <code class="rpath">/data/{{ slug || '…' }}/{{ r.name || '…' }}</code>
                <span class="muted small">{{ dsLabel(r) }} · {{ $t('dataApi.columnsCount', { n: r.columns.length }, r.columns.length) }}</span>
                <button v-if="canEdit" type="button" class="mini danger" :aria-label="$t('dataApi.removeResource', { name: r.name })"
                        :title="$t('dataApi.removeResource', { name: r.name })" @click="removeResource(i)">
                  <Trash2 :size="12" />
                </button>
              </div>
              <div v-if="open === i" class="res-body">
                <p v-if="foreign(r)" class="note small">{{ $t('dataApi.foreignResource') }}</p>
                <div class="row2">
                  <label class="field">
                    <span>{{ $t('dataApi.resourceName') }}</span>
                    <input v-model="r.name" type="text" maxlength="63" class="mono" :readonly="!canEdit || foreign(r)" :aria-invalid="!validLabel(r.name)" />
                  </label>
                  <label class="field">
                    <span>{{ $t('dataApi.datasource') }}</span>
                    <Select v-if="!foreign(r)" :model-value="r.source.id" :options="dsOptions" :disabled="!canEdit"
                            :aria-label="$t('dataApi.datasource')" @update:model-value="(v: number) => chooseDatasource(r, v)" />
                    <input v-else type="text" readonly :value="dsLabel(r)" />
                  </label>
                </div>
                <table class="cols">
                  <thead>
                    <tr>
                      <th>{{ $t('dataApi.column') }}</th>
                      <th>{{ $t('dataApi.goesOut') }}</th>
                      <th>{{ $t('dataApi.filterable') }}</th>
                      <th>{{ $t('dataApi.operators') }}</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr v-for="c in columnsOf(r)" :key="c.name">
                      <td><span class="cn">{{ c.name }}</span> <span class="muted ct">{{ c.type }}</span></td>
                      <td>
                        <input type="checkbox" :checked="goesOut(r, c.name)" :disabled="!canEdit || foreign(r)"
                               :aria-label="$t('dataApi.goesOutOf', { name: c.name })" @change="toggleOut(r, c.name)" />
                      </td>
                      <td>
                        <input type="checkbox" :checked="!!filterOf(r, c.name)"
                               :disabled="!canEdit || foreign(r) || !goesOut(r, c.name) || !operatorsFor(c.type).length"
                               :aria-label="$t('dataApi.filterableOf', { name: c.name })" @change="toggleFilter(r, c.name, c.type)" />
                      </td>
                      <td>
                        <div v-if="filterOf(r, c.name)" class="ops">
                          <button v-for="op in operatorsFor(c.type)" :key="op" type="button" class="op"
                                  :class="{ on: filterOf(r, c.name)!.operators.includes(op) }" :aria-pressed="filterOf(r, c.name)!.operators.includes(op)"
                                  :disabled="!canEdit || foreign(r)" :title="opLabel(op)" @click="toggleOp(r, c.name, op)">
                            {{ op }}
                          </button>
                        </div>
                      </td>
                    </tr>
                  </tbody>
                </table>
                <p v-if="!columnsOf(r).length" class="muted small">{{ $t('dataApi.noColumns') }}</p>
                <div class="row2">
                  <label class="field">
                    <span>{{ $t('dataApi.defaultSort') }}</span>
                    <span class="sortline">
                      <Select :model-value="(r.default_sort[0] ?? '').replace(/^-/, '')" :options="sortOptions(r)"
                              :disabled="!canEdit || foreign(r)" :aria-label="$t('dataApi.defaultSort')"
                              @update:model-value="(v: string) => setSort(r, v)" />
                      <button type="button" class="mini" :disabled="!canEdit || foreign(r) || !r.default_sort.length" @click="toggleDesc(r)">
                        {{ (r.default_sort[0] ?? '').startsWith('-') ? $t('dataApi.descending') : $t('dataApi.ascending') }}
                      </button>
                    </span>
                  </label>
                  <label class="field">
                    <span>{{ $t('dataApi.maxPage') }}</span>
                    <input v-model.number="r.max_page" type="number" min="1" :max="limits?.max_page ?? 10000" :readonly="!canEdit || foreign(r)" />
                  </label>
                </div>
              </div>
            </div>
            <div v-if="canEdit" class="add">
              <Select v-if="adding" :model-value="null" :options="dsOptions" :placeholder="$t('dataApi.chooseDatasource')" auto-open searchable
                      :aria-label="$t('dataApi.chooseDatasource')" @update:model-value="addResource" @close="adding = false" />
              <button v-else type="button" :disabled="!catalog.length" @click="adding = true">
                <Plus :size="14" /> {{ $t('dataApi.addResource') }}
              </button>
              <span v-if="!catalog.length" class="muted small">{{ $t('dataApi.noDatasources') }}</span>
            </div>
          </div>

          <div class="card" data-runtime>
            <h2>
              <Cpu :size="14" /> {{ $t('dataApi.runtime') }}
              <button v-if="detail?.can_set_runtime && !runtimeDraft" type="button" class="mini push" data-rt="edit" @click="editRuntime">
                {{ $t('dataApi.runtimeChange') }}
              </button>
            </h2>
            <dl v-if="!runtimeDraft" class="rtsum">
              <div><dt>{{ $t('dataApi.rtPods') }}</dt>
                <dd data-rt="pods">{{ runtime.autoscale ? $t('dataApi.rtPodsAuto', { min: runtime.min_replicas, max: runtime.max_replicas })
                  : $t('dataApi.rtPodsFixed', { n: runtime.replicas }, runtime.replicas) }}</dd></div>
              <div><dt>{{ $t('dataApi.cpu') }}</dt><dd class="mono" data-rt="cpu">{{ runtime.cpu }}</dd></div>
              <div><dt>{{ $t('dataApi.memory') }}</dt><dd class="mono" data-rt="memory">{{ runtime.memory }}</dd></div>
            </dl>
            <template v-else>
              <DataApiRuntimeForm v-model="runtimeDraft" :limits="limits?.runtime_limits" :disabled="savingRuntime" />
              <div class="formacts">
                <button type="button" class="primary" :disabled="savingRuntime || !!runtimeIssue" data-rt="save" @click="saveRuntime">
                  <LoaderCircle v-if="savingRuntime" :size="14" class="spin" /><Save v-else :size="14" /> {{ $t('dataApi.runtimeApply') }}
                </button>
                <button type="button" :disabled="savingRuntime" @click="runtimeDraft = null">{{ $t('dataApi.cancel') }}</button>
              </div>
            </template>
            <p class="muted small help" data-rt="who">
              {{ detail?.can_set_runtime ? $t('dataApi.runtimeAdminHint') : detail ? $t('dataApi.runtimeByAdmin') : $t('dataApi.runtimeNew') }}
            </p>
          </div>
          </template>
        </section>

        <aside class="side">
          <div v-if="detail && detail.can_manage" class="card" :aria-label="$t('dataApi.keys')">
            <h2><KeyRound :size="15" /> {{ $t('dataApi.keys') }}</h2>
            <p class="muted small help">{{ $t('dataApi.keysHelp') }}</p>
            <SecretOnce v-if="secret" :value="secret" :label="$t('dataApi.keyMade')" @done="secret = null" />
            <form v-if="keyForm" class="keyform" @submit.prevent="createKey">
              <label class="field">
                <span>{{ $t('dataApi.keyName') }}</span>
                <input v-model="keyName" type="text" maxlength="200" required :placeholder="$t('dataApi.keyNamePlaceholder')" />
              </label>
              <label class="field">
                <span>{{ $t('dataApi.keyExpires') }}</span>
                <input v-model="keyExpires" type="date" required :min="dayAfter(1)" :max="maxKeyDay" />
              </label>
              <label class="field">
                <span>{{ $t('dataApi.keyRate') }}</span>
                <input v-model.number="keyRate" type="number" min="1" :max="limits?.key_max_rate" />
              </label>
              <fieldset v-if="resources.length > 1" class="field">
                <legend>{{ $t('dataApi.keyResources') }}</legend>
                <label v-for="r in detail.resource_list" :key="r.name" class="check">
                  <input v-model="keyResources" type="checkbox" :value="r.name" /> <code>{{ r.name }}</code>
                </label>
              </fieldset>
              <label class="field">
                <span>{{ $t('dataApi.keyCidrs') }}</span>
                <textarea v-model="keyCidrs" rows="2" class="mono" :placeholder="$t('dataApi.keyCidrsPlaceholder')" />
              </label>
              <div class="formacts">
                <button type="submit" class="primary" :disabled="keyBusy || !keyName.trim() || !keyExpires">
                  <LoaderCircle v-if="keyBusy" :size="14" class="spin" /><KeyRound v-else :size="14" /> {{ $t('dataApi.makeKey') }}
                </button>
                <button type="button" @click="keyForm = false">{{ $t('dataApi.cancel') }}</button>
              </div>
            </form>
            <button v-else-if="!secret" type="button" @click="startKey"><Plus :size="14" /> {{ $t('dataApi.newKey') }}</button>
            <ul class="keys">
              <li v-for="k in keys" :key="k.id" :class="{ dead: !keyAnswers(k.state) }">
                <div class="ktop">
                  <strong>{{ k.name }}</strong>
                  <span class="tag" :class="`k-${k.state}`">{{ keyStateLabel(k) }}</span>
                </div>
                <code class="muted small">tdk_{{ k.hint }}…</code>
                <div class="muted small">
                  {{ $t('dataApi.keyLine', { date: k.expires_at ? fmtx.date(k.expires_at) : '', rate: fmtx.count(k.rate_per_minute) }) }}
                  <template v-if="k.state === 'grace' && k.grace_until"> · {{ $t('dataApi.keyGraceUntil', { date: fmtx.dateTime(k.grace_until) }) }}</template>
                </div>
                <div class="muted small">
                  {{ k.resources.length ? k.resources.join(', ') : $t('dataApi.keyAllResources') }}<template v-if="k.cidrs.length"> · {{ k.cidrs.join(', ') }}</template>
                </div>
                <div class="muted small">
                  {{ $t('dataApi.keyUse', { n: fmtx.count(k.requests_24h) }) }} ·
                  {{ k.last_used_at ? $t('dataApi.keyLastUsed', { date: fmtx.dateTime(k.last_used_at) }) : $t('dataApi.keyNeverUsed') }}
                </div>
                <div v-if="renewing === k.id" class="renew">
                  <label class="field">
                    <span>{{ $t('dataApi.keyExpires') }}</span>
                    <input v-model="renewExpires" type="date" :min="dayAfter(1)" :max="maxKeyDay" />
                  </label>
                  <label class="field">
                    <span>{{ $t('dataApi.grace') }}</span>
                    <Select :model-value="renewGrace" :options="graceOptions" :aria-label="$t('dataApi.grace')" @update:model-value="(v: number) => (renewGrace = v)" />
                  </label>
                  <div class="formacts">
                    <button type="button" class="primary mini" :disabled="keyBusy" @click="renewKey(k)">{{ $t('dataApi.renew') }}</button>
                    <button type="button" class="mini" @click="renewing = null">{{ $t('dataApi.cancel') }}</button>
                  </div>
                </div>
                <div v-else-if="revoking === k.id" class="renew">
                  <span class="small">{{ $t('dataApi.revokeConfirm', { name: k.name }) }}</span>
                  <div class="formacts">
                    <button type="button" class="mini danger" @click="revokeKey(k)">{{ $t('dataApi.revoke') }}</button>
                    <button type="button" class="mini" @click="revoking = null">{{ $t('dataApi.cancel') }}</button>
                  </div>
                </div>
                <div v-else-if="keyAnswers(k.state) || k.state === 'expired'" class="kacts">
                  <button v-if="k.state === 'active' || k.state === 'expired'" type="button" class="mini" @click="startRenew(k)">
                    <RefreshCw :size="12" /> {{ $t('dataApi.renew') }}
                  </button>
                  <button v-if="keyAnswers(k.state)" type="button" class="mini" @click="revoking = k.id">
                    <X :size="12" /> {{ $t('dataApi.revoke') }}
                  </button>
                </div>
              </li>
            </ul>
            <p v-if="!keys.length && !keyForm" class="muted small">{{ $t('dataApi.noKeys') }}</p>
          </div>

          <div v-if="apiId != null" class="card" :aria-label="$t('dataApi.versions')">
            <h2><History :size="15" /> {{ $t('dataApi.versions') }}</h2>
            <ol class="versions">
              <li v-for="v in versions" :key="v.version" :class="{ current: v.current }">
                <div class="vtop">
                  <strong>v{{ v.version }}</strong>
                  <span v-if="v.current" class="tag">{{ $t('dataApi.currentVersion') }}</span>
                  <span class="muted small">{{ versionNote(v.note, t) }}</span>
                </div>
                <div class="muted small">{{ v.created_at ? fmtx.dateTime(v.created_at) : '' }}<template v-if="v.created_by"> · {{ v.created_by }}</template></div>
                <div v-if="canEdit && !v.current" class="vacts">
                  <button type="button" class="mini" :disabled="restoring != null" @click="restore(v.version)">
                    <LoaderCircle v-if="restoring === v.version" :size="12" class="spin" /><RotateCcw v-else :size="12" />
                    {{ $t('dataApi.restore') }}
                  </button>
                </div>
              </li>
            </ol>
          </div>
        </aside>
      </div>
    </div>
  </AppShell>
</template>

<style scoped>
.da { display: flex; flex-direction: column; gap: 12px; min-height: 0; }
.page-head { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.page-head h1 { display: inline-flex; align-items: center; gap: 8px; margin: 0; flex: 0 1 380px; min-width: 0; }
.dname { font-size: 16px; font-weight: 600; padding: 4px 9px; min-width: 0; width: 100%; }
.dname[readonly] { border-color: transparent; background: transparent; padding-left: 0; }
.where { font-size: 12.5px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; min-width: 0; flex: 1; }
.head-actions { display: flex; align-items: center; gap: 8px; margin-left: auto; flex-shrink: 0; flex-wrap: wrap; }
.head-actions button { display: inline-flex; align-items: center; gap: 6px; white-space: nowrap; }
.tag {
  font-size: 10px; text-transform: uppercase; letter-spacing: 0.04em; padding: 1px 7px;
  border-radius: 8px; background: var(--panel-2); border: 1px solid var(--border); color: var(--muted);
}
.tag.unsaved { color: var(--warning); border-color: color-mix(in srgb, var(--warning) 45%, var(--border)); }
.tag.st.active, .tag.k-active { color: var(--accent-2); border-color: color-mix(in srgb, var(--accent-2) 45%, var(--border)); }
.tag.st.paused, .tag.k-grace { color: var(--warning); border-color: color-mix(in srgb, var(--warning) 45%, var(--border)); }
.tag.k-revoked, .tag.k-expired, .tag.k-replaced { color: var(--danger); }
.err { color: var(--danger); }
.work { display: grid; grid-template-columns: minmax(0, 1fr) 320px; gap: 16px; align-items: start; }
.main, .side { display: flex; flex-direction: column; gap: 12px; min-width: 0; }
.card { border: 1px solid var(--border-soft); border-radius: 10px; padding: 12px 14px; background: var(--panel); display: flex; flex-direction: column; gap: 10px; min-width: 0; }
.card h2 { margin: 0; font-size: 13.5px; display: flex; align-items: center; gap: 7px; flex-wrap: wrap; }
.note { margin: 0; padding: 8px 10px; border-radius: 8px; background: var(--bg-soft); border: 1px solid var(--border); display: flex; gap: 8px; align-items: flex-start; }
.note.warn { border-color: color-mix(in srgb, var(--warning) 45%, var(--border)); }
.field { display: flex; flex-direction: column; gap: 5px; font-size: 12px; color: var(--muted); min-width: 0; border: 0; padding: 0; margin: 0; }
.field input, .field textarea { font: inherit; color: var(--text); }
.field textarea { resize: vertical; }
.small { font-size: 12px; }
.help { margin: 0; line-height: 1.45; }
.mono, code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.slugline { display: flex; align-items: center; gap: 4px; }
.slugline input { flex: 1; min-width: 0; }
.res { border: 1px solid var(--border-soft); border-radius: 8px; }
.res.isopen { border-color: var(--border); }
.res-head { display: flex; align-items: center; gap: 8px; padding: 6px 8px; min-width: 0; }
.res-head .muted { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.rpath { font-size: 12.5px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 55%; }
.chev { border: 0; background: none; min-height: 0; box-shadow: none; color: var(--muted); padding: 2px; cursor: pointer; }
.res-body { padding: 4px 12px 12px; display: flex; flex-direction: column; gap: 10px; border-top: 1px solid var(--border-soft); }
.row2 { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.row3 { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 10px; }
.cols { width: 100%; border-collapse: collapse; font-size: 12.5px; }
.cols th { text-align: left; font-weight: 500; color: var(--muted); font-size: 11.5px; padding: 4px 6px; border-bottom: 1px solid var(--border-soft); }
.cols td { padding: 3px 6px; border-bottom: 1px solid var(--border-soft); vertical-align: middle; }
.cn { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.ct { font-size: 11px; }
.ops { display: flex; flex-wrap: wrap; gap: 4px; }
.op { min-height: 0; padding: 1px 7px; font-size: 11px; border-radius: 6px; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.op.on { background: var(--tint-accent); border-color: color-mix(in srgb, var(--accent) 55%, var(--border)); color: var(--text); }
.sortline { display: flex; gap: 6px; align-items: center; }
.sortline > :first-child { flex: 1; min-width: 0; }
.add { display: flex; align-items: center; gap: 10px; }
.add button, .card > button { display: inline-flex; align-items: center; gap: 6px; align-self: flex-start; }
.check { display: inline-flex; align-items: center; gap: 6px; font-size: 12.5px; color: var(--text); }
.card h2 .push { margin-left: auto; font-size: 12px; font-weight: 500; }
.rtsum { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 8px; margin: 0; }
.rtsum > div { min-width: 0; }
.rtsum dt { font-size: 12px; color: var(--muted); }
.rtsum dd { margin: 2px 0 0; font-size: 13px; overflow-wrap: anywhere; }
.mini { display: inline-flex; align-items: center; gap: 4px; }
.danger { color: var(--danger); }
.keyform, .renew { display: flex; flex-direction: column; gap: 8px; padding: 8px; border: 1px solid var(--border-soft); border-radius: 8px; }
.formacts, .kacts { display: flex; gap: 6px; flex-wrap: wrap; }
.formacts button { display: inline-flex; align-items: center; gap: 6px; }
.keys, .versions { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; }
.keys li, .versions li { border: 1px solid var(--border-soft); border-radius: 8px; padding: 7px 9px; display: flex; flex-direction: column; gap: 3px; min-width: 0; }
.keys li.dead { opacity: 0.7; }
.ktop, .vtop { display: flex; align-items: center; gap: 7px; flex-wrap: wrap; }
.versions { max-height: 40vh; overflow: auto; }
.versions li.current { border-color: var(--accent); }
.vacts { display: flex; gap: 6px; margin-top: 3px; }
.tabs { display: flex; gap: 2px; border-bottom: 1px solid var(--border-soft); overflow-x: auto; scrollbar-width: none; }
.tabs button {
  display: inline-flex; align-items: center; gap: 5px; padding: 6px 11px; font-size: 12.5px; white-space: nowrap;
  border: 0; border-bottom: 2px solid transparent; border-radius: 6px 6px 0 0; background: transparent;
  color: var(--muted); box-shadow: none; min-height: 0;
}
.tabs button:hover { color: var(--text); box-shadow: none; }
.tabs button.on { color: var(--text); border-bottom-color: var(--accent); }
.callat { overflow-wrap: anywhere; }
@media (max-width: 900px) {
  .work { grid-template-columns: minmax(0, 1fr); }
  .row2 { grid-template-columns: 1fr; }
  .head-actions { flex: 1 1 100%; flex-shrink: 1; min-width: 0; margin-left: 0; }
}
</style>
