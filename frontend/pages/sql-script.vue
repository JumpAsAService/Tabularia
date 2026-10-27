<script setup lang="ts">
// A SQL script: SQL kept in a folder, with its versions, run on the datasources the
// person may read (docs/specs/sql-scripts.md). The SQL names a datasource by its name in
// double quotes, with its folder's in front when two share it; «Run» (Ctrl/Cmd+Enter) runs
// the selection or the whole SQL and shows the first rows. Whoever may only read the folder
// runs it and tries changes, and saves nothing.
//
// /sql-script?id=N opens a script; /sql-script?project=P starts a new one in folder P.
import { computed, defineAsyncComponent, onMounted, onUnmounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { onBeforeRouteLeave, onBeforeRouteUpdate } from 'vue-router'
import { ChevronRight, DatabaseSearch, History, LoaderCircle, Play, RotateCcw, Save, Search, Square, Table2, X } from 'lucide-vue-next'
import { errMessage } from '~/composables/useApi'
import { useFormat } from '~/composables/useFormat'
import { useFolderPaths } from '~/composables/useFolderPaths'
import { usePreferredEngine } from '~/composables/useEngine'
import { versionNote } from '~/composables/useSavedScripts'
import {
  sqlName, useSqlScripts,
  type SqlCatalogItem, type SqlRunResult, type SqlScriptDetail, type SqlScriptVersion,
} from '~/composables/useSqlScripts'
import Select from '~/components/ui/Select.vue'
import DataGrid from '~/components/ui/DataGrid.vue'

// Monaco weighs a few MB: loaded with the editor, not with the page
const SqlEditor = defineAsyncComponent(() => import('~/components/sql/SqlEditor.vue'))

const { t } = useI18n()
const fmtx = useFormat()
const toast = useToast()
const route = useRoute()
const api = useSqlScripts()
const folderPaths = useFolderPaths()
const { engineCatalog, loadCatalog, defaultEngine } = usePreferredEngine()

const scriptId = ref<number | null>(Number(route.query.id) || null)
const projectId = ref<number | null>(Number(route.query.project) || null)
const detail = ref<SqlScriptDetail | null>(null)
const loading = ref(true)
const loadError = ref('')
const saving = ref(false)

// what is on screen; `savedAs` is the same thing as last saved, to tell unsaved changes
const name = ref('')
const description = ref('')
const sql = ref('')
const engine = ref<string | null>(null)
const savedAs = ref('')
const snapshot = () => JSON.stringify([name.value.trim(), description.value.trim(), sql.value, engine.value])
const dirty = computed(() => snapshot() !== savedAs.value)

const canEdit = computed(() => (detail.value ? detail.value.editable : true))
const folderPath = computed(() => folderPaths.pathOf(projectId.value) ?? '')

// the engines the administrator lets one choose; «default» is the person's favourite among them
const usable = computed(() => engineCatalog.value.filter((e) => e.available && !e.disabled_by_admin))
const fallbackEngine = computed(() => defaultEngine(usable.value))
const engineOptions = computed(() => [
  { value: null, label: t('sqlScript.engineDefault', { engine: labelOf(fallbackEngine.value) }) },
  ...usable.value.map((e) => ({ value: e.id, label: e.label })),
])
function labelOf(id: string): string {
  return engineCatalog.value.find((e) => e.id === id)?.label ?? id
}

function apply(d: SqlScriptDetail) {
  detail.value = d
  scriptId.value = d.id
  projectId.value = d.project_id
  name.value = d.name
  description.value = d.description
  sql.value = d.content.sql
  engine.value = d.content.engine
  savedAs.value = snapshot()
}

async function load() {
  loadError.value = ''
  viewing.value = null
  result.value = null
  runError.value = ''
  if (scriptId.value == null) {
    detail.value = null
    sql.value = `${t('sqlScript.starter')}\nSELECT *\nFROM `
    savedAs.value = ''
    loading.value = false
    return
  }
  loading.value = true
  try {
    apply(await api.get(scriptId.value))
    await loadVersions()
  } catch (e) {
    loadError.value = errMessage(e)
  } finally {
    loading.value = false
  }
}

async function save() {
  if (saving.value || !canEdit.value || viewing.value) return
  const n = name.value.trim()
  if (!n) {
    toast.error(t('sqlScript.nameRequired'))
    return
  }
  saving.value = true
  const content = { sql: sql.value, engine: engine.value, connection_id: null }
  try {
    if (scriptId.value != null) {
      apply(await api.update(scriptId.value, { name: n, description: description.value.trim(), content }))
      await loadVersions()
      toast.success(t('sqlScript.saved', { name: n }))
    } else if (projectId.value != null) {
      const d = await api.create(projectId.value, { name: n, description: description.value.trim(), content })
      apply(d)
      navigateTo(`/sql-script?id=${d.id}`, { replace: true })
      await loadVersions()
      toast.success(t('sqlScript.created', { name: n }))
    }
  } catch (e) {
    toast.error(errMessage(e))
  } finally {
    saving.value = false
  }
}

// ── running ──────────────────────────────────────────────────────────────────
const editorRef = ref<{ runText: () => string; insert: (s: string) => void; focus: () => void } | null>(null)
const running = ref(false)
const result = ref<SqlRunResult | null>(null)
const runError = ref('')
let controller: AbortController | null = null

async function run(text?: string) {
  const query = (text ?? editorRef.value?.runText() ?? sql.value).trim()
  if (!query) return
  controller?.abort()
  controller = new AbortController()
  const mine = controller
  running.value = true
  runError.value = ''
  try {
    result.value = await api.run({ sql: query, engine: engine.value ?? fallbackEngine.value, script_id: scriptId.value }, mine.signal)
  } catch (e: any) {
    if (e?.name === 'AbortError' || mine.signal.aborted) return
    result.value = null
    runError.value = errMessage(e)
  } finally {
    if (controller === mine) {
      running.value = false
      controller = null
    }
  }
}

function stop() {
  controller?.abort()
  controller = null
  running.value = false
}

const resultLine = computed(() => {
  const r = result.value
  if (!r) return ''
  return t('sqlScript.resultLine', {
    rows: fmtx.count(r.row_count), more: r.truncated ? '+' : '', ms: fmtx.count(r.elapsed_ms), engine: labelOf(r.engine),
    read: r.datasources.map((d) => d.name).join(', '),
  })
})

// ── the catalog beside the editor ────────────────────────────────────────────
const catalog = ref<SqlCatalogItem[]>([])
const catalogQuery = ref('')
const openItem = ref<number | null>(null)
async function loadCatalogList() {
  try { catalog.value = await api.catalog() } catch { catalog.value = [] }
}
const shownCatalog = computed(() => {
  const q = catalogQuery.value.trim().toLowerCase()
  return q ? catalog.value.filter((d) => `${d.name} ${d.path}`.toLowerCase().includes(q)) : catalog.value
})
function insertName(d: SqlCatalogItem) {
  editorRef.value?.insert(sqlName(d, catalog.value))
}
function insertColumn(c: string) {
  editorRef.value?.insert(/^[A-Za-z_][A-Za-z0-9_]*$/.test(c) ? c : `"${c.replace(/"/g, '""')}"`)
}

// ── versions ─────────────────────────────────────────────────────────────────
const versions = ref<SqlScriptVersion[]>([])
const restoring = ref<number | null>(null)
// a version looked at in the editor, read-only; null = the SQL being edited
const viewing = ref<{ version: number; sql: string } | null>(null)

async function loadVersions() {
  if (scriptId.value == null) return
  try { versions.value = await api.versions(scriptId.value) } catch { versions.value = [] }
}

async function viewVersion(v: SqlScriptVersion) {
  if (scriptId.value == null) return
  if (v.current) {
    viewing.value = null
    return
  }
  try {
    viewing.value = { version: v.version, sql: (await api.version(scriptId.value, v.version)).sql }
  } catch (e) {
    toast.error(errMessage(e))
  }
}

async function restore(version: number) {
  if (scriptId.value == null || restoring.value != null) return
  if (dirty.value && !confirm(t('sqlScript.restoreDiscards'))) return
  if (!confirm(t('sqlScript.restoreConfirm', { n: version }))) return
  restoring.value = version
  try {
    apply(await api.restore(scriptId.value, version))
    viewing.value = null
    await loadVersions()
    toast.success(t('sqlScript.restored', { n: version }))
  } catch (e) {
    toast.error(errMessage(e))
  } finally {
    restoring.value = null
  }
}

// what the editor shows: the SQL being edited, or the version looked at
const editorText = computed({
  get: () => (viewing.value ? viewing.value.sql : sql.value),
  set: (v: string) => { if (!viewing.value) sql.value = v },
})

// ── keys and leaving ─────────────────────────────────────────────────────────
function onKey(e: KeyboardEvent) {
  if ((e.ctrlKey || e.metaKey) && !e.altKey && e.key.toLowerCase() === 's') {
    e.preventDefault()
    save()
  }
}
const guard = () => dirty.value && canEdit.value && !(scriptId.value == null && !sql.value.trim())
function onBeforeUnload(e: BeforeUnloadEvent) {
  if (!guard()) return
  e.preventDefault()
  e.returnValue = ''
}
onMounted(() => {
  document.addEventListener('keydown', onKey)
  window.addEventListener('beforeunload', onBeforeUnload)
  folderPaths.load()
  loadCatalog()
  loadCatalogList()
  load()
})
onUnmounted(() => {
  controller?.abort()
  document.removeEventListener('keydown', onKey)
  window.removeEventListener('beforeunload', onBeforeUnload)
})
onBeforeRouteLeave(() => !guard() || confirm(t('sqlScript.leaveConfirm')))
onBeforeRouteUpdate((to) => (Number(to.query.id) || null) === scriptId.value || !guard() || confirm(t('sqlScript.leaveConfirm')))
watch(() => route.query.id, (id) => {
  const next = Number(id) || null
  if (next === scriptId.value) return // a script just created took its address
  scriptId.value = next
  versions.value = []
  load()
})
</script>

<template>
  <AppShell>
    <div class="qs">
      <div class="page-head">
        <h1>
          <DatabaseSearch :size="18" />
          <input
            v-model="name"
            class="qname"
            type="text"
            maxlength="200"
            :readonly="!canEdit"
            :placeholder="$t('sqlScript.namePlaceholder')"
            :aria-label="$t('sqlScript.nameLabel')"
          />
        </h1>
        <span v-if="folderPath" class="where muted">{{ $t('sqlScript.inFolder', { path: folderPath }) }}</span>
        <div class="head-actions">
          <span v-if="detail" class="tag">v{{ detail.current_version }}</span>
          <span v-if="dirty && canEdit && detail" class="tag unsaved">{{ $t('sqlScript.unsaved') }}</span>
          <Select
            class="engine"
            :model-value="engine"
            :options="engineOptions"
            :disabled="!!viewing"
            :aria-label="$t('sqlScript.engine')"
            @update:model-value="(v: string | null) => (engine = v)"
          />
          <button v-if="running" type="button" @click="stop">
            <Square :size="13" /> {{ $t('sqlScript.stop') }}
          </button>
          <button v-else type="button" :disabled="loading || !editorText.trim()" :title="$t('sqlScript.runTitle')" @click="run()">
            <Play :size="14" /> {{ $t('sqlScript.run') }}
          </button>
          <button v-if="canEdit" class="primary" :disabled="saving || loading || !!viewing || (!dirty && scriptId != null)" @click="save">
            <LoaderCircle v-if="saving" :size="14" class="spin" />
            <Save v-else :size="14" />
            {{ scriptId == null ? $t('sqlScript.create') : $t('sqlScript.save') }}
          </button>
        </div>
      </div>

      <p v-if="loadError" class="err" role="alert">{{ loadError }}</p>
      <SkeletonRows v-else-if="loading" :rows="5" />

      <div v-else class="work">
        <section class="main">
          <p v-if="!canEdit" class="note small" role="note">{{ $t('sqlScript.readOnly') }}</p>
          <div v-if="viewing" class="viewing" role="status">
            <History :size="14" />
            <span>{{ $t('sqlScript.viewingVersion', { n: viewing.version }) }}</span>
            <button v-if="canEdit" type="button" class="mini" :disabled="restoring != null" @click="restore(viewing.version)">
              <RotateCcw :size="12" /> {{ $t('sqlScript.restore') }}
            </button>
            <button type="button" class="mini" @click="viewing = null"><X :size="12" /> {{ $t('sqlScript.backToCurrent') }}</button>
          </div>
          <div class="card editor">
            <SqlEditor
              ref="editorRef"
              v-model="editorText"
              :catalog="catalog"
              :read-only="!!viewing"
              @run="run"
            />
          </div>
          <p class="muted small keys">{{ $t('sqlScript.keys') }}</p>

          <div class="card result" aria-live="polite">
            <h2><Table2 :size="15" /> {{ $t('sqlScript.result') }}
              <span v-if="resultLine && !running && !runError" class="muted small rline">{{ resultLine }}</span>
            </h2>
            <DataGrid
              v-if="running || result || runError"
              :result="result"
              :loading="running"
              :error="runError"
            />
            <p v-else class="muted small">{{ $t('sqlScript.noResult') }}</p>
          </div>
        </section>

        <aside class="side">
          <div class="card">
            <label class="field">
              <span>{{ $t('sqlScript.description') }}</span>
              <textarea v-model="description" rows="2" maxlength="2000" :readonly="!canEdit"
                        :placeholder="$t('sqlScript.descriptionPlaceholder')" />
            </label>
          </div>

          <div class="card catalog" :aria-label="$t('sqlScript.catalog')">
            <h2>{{ $t('sqlScript.catalog') }} <span class="muted small">{{ catalog.length }}</span></h2>
            <p class="muted small help">{{ $t('sqlScript.catalogHelp') }}</p>
            <label class="search">
              <Search :size="13" aria-hidden="true" />
              <input v-model="catalogQuery" type="search" :placeholder="$t('sqlScript.catalogSearch')" :aria-label="$t('sqlScript.catalogSearch')" />
            </label>
            <ul class="items">
              <li v-for="d in shownCatalog" :key="d.id" :class="{ off: !d.has_data }">
                <div class="item-head">
                  <button
                    type="button"
                    class="chev"
                    :aria-expanded="openItem === d.id"
                    :aria-label="$t('sqlScript.columnsOf', { name: d.name })"
                    @click="openItem = openItem === d.id ? null : d.id"
                  >
                    <ChevronRight :size="13" />
                  </button>
                  <button type="button" class="dsname" :disabled="!d.has_data || !!viewing" :title="$t('sqlScript.insertName')" @click="insertName(d)">
                    <span class="nm">{{ d.name }}</span>
                    <span class="muted path">{{ d.path }}</span>
                  </button>
                </div>
                <p v-if="!d.has_data" class="muted small nodata">{{ $t('sqlScript.noData') }}</p>
                <ul v-if="openItem === d.id" class="cols">
                  <li v-for="c in d.columns" :key="c.name">
                    <button type="button" :disabled="!!viewing" @click="insertColumn(c.name)">
                      <span class="cn">{{ c.name }}</span><span class="muted ct">{{ c.type }}</span>
                    </button>
                  </li>
                </ul>
              </li>
            </ul>
            <p v-if="!shownCatalog.length" class="muted small">{{ $t('sqlScript.catalogEmpty') }}</p>
          </div>

          <div v-if="scriptId != null" class="card" :aria-label="$t('sqlScript.versions')">
            <h2><History :size="15" /> {{ $t('sqlScript.versions') }}</h2>
            <ol class="versions">
              <li v-for="v in versions" :key="v.version" :class="{ current: v.current, seen: viewing?.version === v.version }">
                <div class="vtop">
                  <strong>v{{ v.version }}</strong>
                  <span v-if="v.current" class="tag">{{ $t('sqlScript.currentVersion') }}</span>
                  <span class="muted small">{{ versionNote(v.note, t) }}</span>
                </div>
                <div class="muted small">{{ fmtx.dateTime(v.created_at) }}<template v-if="v.created_by"> · {{ v.created_by }}</template></div>
                <div class="vacts">
                  <button v-if="!v.current" type="button" class="mini" @click="viewVersion(v)">{{ $t('sqlScript.viewVersion') }}</button>
                  <button v-if="canEdit && !v.current" type="button" class="mini" :disabled="restoring != null" @click="restore(v.version)">
                    <LoaderCircle v-if="restoring === v.version" :size="12" class="spin" /><RotateCcw v-else :size="12" />
                    {{ $t('sqlScript.restore') }}
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
.qs { display: flex; flex-direction: column; gap: 12px; min-height: 0; }
.page-head { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.page-head h1 { display: inline-flex; align-items: center; gap: 8px; margin: 0; flex: 0 1 380px; min-width: 0; }
.qname { font-size: 16px; font-weight: 600; padding: 4px 9px; min-width: 0; width: 100%; }
.qname[readonly] { border-color: transparent; background: transparent; padding-left: 0; }
.where { font-size: 12.5px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; min-width: 0; flex: 1; }
.head-actions { display: flex; align-items: center; gap: 8px; margin-left: auto; flex-shrink: 0; flex-wrap: wrap; }
.head-actions button { display: inline-flex; align-items: center; gap: 6px; white-space: nowrap; }
.engine { width: 190px; }
.tag {
  font-size: 10px; text-transform: uppercase; letter-spacing: 0.04em; padding: 1px 7px;
  border-radius: 8px; background: var(--panel-2); border: 1px solid var(--border); color: var(--muted);
}
.tag.unsaved { color: var(--warning); border-color: color-mix(in srgb, var(--warning) 45%, var(--border)); }
.err { color: var(--danger); }
.work { display: grid; grid-template-columns: minmax(0, 1fr) 300px; gap: 16px; align-items: start; }
.main { display: flex; flex-direction: column; gap: 10px; min-width: 0; }
.side { display: flex; flex-direction: column; gap: 12px; min-width: 0; }
.card { border: 1px solid var(--border-soft); border-radius: 10px; padding: 12px 14px; background: var(--panel); display: flex; flex-direction: column; gap: 10px; min-width: 0; }
.card h2 { margin: 0; font-size: 13.5px; display: flex; align-items: center; gap: 7px; flex-wrap: wrap; }
.editor { padding: 0; height: clamp(220px, 40vh, 520px); resize: vertical; overflow: hidden; }
.keys { margin: -4px 0 0; }
.rline { font-weight: 400; }
.result { min-height: 120px; }
.result :deep(.datagrid) { max-height: 52vh; overflow: auto; }
.note { margin: 0; padding: 8px 10px; border-radius: 8px; background: var(--bg-soft); border: 1px solid var(--border); }
.viewing { display: flex; align-items: center; gap: 8px; padding: 7px 10px; border-radius: 8px; font-size: 12.5px;
  border: 1px solid color-mix(in srgb, var(--accent) 45%, var(--border)); background: var(--bg-soft); }
.viewing span { flex: 1; }
.viewing .mini, .vacts .mini { display: inline-flex; align-items: center; gap: 4px; }
.field { display: flex; flex-direction: column; gap: 5px; font-size: 12px; color: var(--muted); }
.field textarea { resize: vertical; font: inherit; color: var(--text); }
.small { font-size: 12px; }
.help { margin: 0; line-height: 1.45; }
.search { display: flex; align-items: center; gap: 6px; color: var(--muted); }
.search input { flex: 1; min-width: 0; }
.items { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 2px; max-height: 46vh; overflow: auto; }
.item-head { display: flex; align-items: flex-start; gap: 2px; }
.chev, .dsname, .cols button {
  border: 0; background: none; min-height: 0; box-shadow: none; color: var(--text); cursor: pointer; text-align: left;
}
.chev { padding: 4px 2px; color: var(--muted); }
.chev[aria-expanded='true'] svg { transform: rotate(90deg); }
.chev svg { transition: transform 0.15s ease; }
.dsname { flex: 1; min-width: 0; display: flex; flex-direction: column; align-items: flex-start; justify-content: flex-start; padding: 3px 6px; border-radius: 6px; }
.dsname:hover:not(:disabled), .cols button:hover:not(:disabled) { background: var(--bg-soft); }
.dsname:active, .cols button:active { transform: none; }
.nm, .path { max-width: 100%; }
.nm { font-size: 12.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.path { font-size: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.off .nm { color: var(--muted); }
.nodata { margin: 0 0 0 26px; font-size: 11px; }
.cols { list-style: none; margin: 0 0 4px 22px; padding: 0 0 0 8px; border-left: 1px solid var(--border-soft); }
.cols button { width: 100%; display: flex; justify-content: space-between; gap: 8px; padding: 2px 6px; border-radius: 5px; font-size: 12px; }
.cn { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.ct { font-size: 11px; white-space: nowrap; }
.versions { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; max-height: 40vh; overflow: auto; }
.versions li { border: 1px solid var(--border-soft); border-radius: 8px; padding: 7px 9px; display: flex; flex-direction: column; gap: 3px; }
.versions li.current { border-color: var(--accent); }
.versions li.seen { background: var(--bg-soft); }
.vtop { display: flex; align-items: center; gap: 7px; flex-wrap: wrap; }
.vacts { display: flex; gap: 6px; margin-top: 3px; }
@media (max-width: 900px) {
  .work { grid-template-columns: minmax(0, 1fr); }
  .engine { width: 160px; }
}
</style>
