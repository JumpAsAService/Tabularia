<script setup lang="ts">
// A dataset of the report, added or changed: SQL on the catalog or on a connection (the
// editor of the SQL scripts, with the catalog's completion), a datasource or a saved view
// with filters applied before, or a saved SQL script; its calculated fields; its fields as
// the data has them («Refresh fields», read from the definition being edited), with its
// rows counted (a SELECT * on a big table shows here, before a preview reads it). A
// parameter goes in the SQL as {name}, without quotes: the server writes it as a literal.
import { computed, defineAsyncComponent, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { Database, FileCode, LoaderCircle, Plus, RefreshCw, Table2, Trash2, View } from 'lucide-vue-next'
import Modal from '~/components/ui/Modal.vue'
import Select from '~/components/ui/Select.vue'
import RdExprInput from '~/components/report/designer/RdExprInput.vue'
import RdFieldIcon from '~/components/report/designer/RdFieldIcon.vue'
import RdRowCount from '~/components/report/designer/RdRowCount.vue'
import RdSeg from '~/components/report/designer/RdSeg.vue'
import { useDesign } from '~/composables/reportDesignStore'
import { useReportDesign } from '~/composables/useReportDesign'
import { DB_TYPES, useConnections, type ConnectionInfo } from '~/composables/useConnections'
import { useDatasources, type DatasourceInfo } from '~/composables/useDatasources'
import { useSavedViews, type SavedView } from '~/composables/useSavedViews'
import {
  QUERYABLE_DB_TYPES, catalogDialect, sqlIdent, sqlName, sqlTableRef, useSqlScripts, type SqlCatalogItem, type SqlScript, type SqlTable,
} from '~/composables/useSqlScripts'
import { usePreferredEngine } from '~/composables/useEngine'
import { useFolderPaths } from '~/composables/useFolderPaths'
import { errMessage } from '~/composables/useApi'
import { renameEverywhere } from '~/composables/reportExpressions'
import { countFor, rowCountOf, sourceKey, type CountedRows } from '~/composables/reportRowCount'
import {
  FILTER_OPS, NAME_RE, fieldKind, samePath, valueAt, type DCalculated, type DDataset, type DField, type DFilter, type DSource,
} from '~/composables/reportDesignModel'

const SqlEditor = defineAsyncComponent(() => import('~/components/sql/SqlEditor.vue'))

const props = defineProps<{ open: boolean; name: string | null; reportId: number | null }>()
const emit = defineEmits<{ (e: 'close'): void; (e: 'applied', name: string): void }>()
const store = useDesign()
const { t } = useI18n()
const designApi = useReportDesign()
const folderPaths = useFolderPaths()
const { engineCatalog, loadCatalog, defaultEngine } = usePreferredEngine()

type Kind = DSource['kind']
interface Draft {
  name: string
  kind: Kind
  sql: string
  connection_id: number | null
  engine: string | null
  datasource_id: number | null
  view_id: number | null
  script_id: number | null
  filters: DFilter[]
  calculated: DCalculated[]
  fields: DField[]
}
const draft = ref<Draft | null>(null)
const original = ref<string | null>(null)
/** the source the draft's fields were read from: another source, and they are read again */
const fieldsFor = ref('')
/** the rows counted with the fields, kept with the source counted (reportRowCount.ts) */
const counted = ref<CountedRows | null>(null)

function fromDataset(d: DDataset | null): Draft {
  const s = d?.source
  return {
    name: d?.name ?? freeName(),
    kind: s?.kind ?? 'sql',
    sql: s?.kind === 'sql' ? s.sql : '',
    connection_id: s?.kind === 'sql' ? s.connection_id : null,
    engine: s?.kind === 'sql' ? s.engine : null,
    datasource_id: s?.kind === 'datasource' ? s.datasource_id : null,
    view_id: s?.kind === 'view' ? s.view_id : null,
    script_id: s?.kind === 'sql_script' ? s.script_id : null,
    filters: JSON.parse(JSON.stringify((s && 'filters' in s && s.filters) || [])),
    calculated: JSON.parse(JSON.stringify(d?.calculated ?? [])),
    fields: JSON.parse(JSON.stringify(d?.fields ?? [])),
  }
}
function freeName(): string {
  const taken = new Set(store.doc.value.datasets.map((d) => d.name))
  if (!taken.has('data')) return 'data'
  for (let i = 2; ; i++) if (!taken.has(`data${i}`)) return `data${i}`
}

watch(() => props.open, (o) => {
  if (!o) return
  const d = props.name ? store.doc.value.datasets.find((x) => x.name === props.name) ?? null : null
  draft.value = fromDataset(d)
  original.value = d?.name ?? null
  fieldsFor.value = d ? JSON.stringify(d.source) : ''
  counted.value = d ? store.rowCounts.value[d.name] ?? null : null
  refreshError.value = ''
  loadLists()
}, { immediate: true })

// ── the lists to choose from, read once ──
const connections = ref<ConnectionInfo[]>([])
const catalog = ref<SqlCatalogItem[]>([])
const datasources = ref<DatasourceInfo[]>([])
const views = ref<SavedView[]>([])
const scripts = ref<SqlScript[]>([])
let loaded = false
async function loadLists() {
  if (loaded) return
  loaded = true
  folderPaths.load()
  loadCatalog()
  const sql = useSqlScripts()
  const [c, cat, ds, vw, sc] = await Promise.allSettled([
    useConnections().list(), sql.catalog(), useDatasources().list(), useSavedViews().list(), sql.list(),
  ])
  if (c.status === 'fulfilled') connections.value = c.value.filter((x) => QUERYABLE_DB_TYPES.includes(x.db_type))
  if (cat.status === 'fulfilled') catalog.value = cat.value
  if (ds.status === 'fulfilled') datasources.value = ds.value
  if (vw.status === 'fulfilled') views.value = vw.value
  if (sc.status === 'fulfilled') scripts.value = sc.value
}

const kinds = computed(() => [
  { value: 'sql', label: t('reportDesigner.dataset.kindSql'), icon: FileCode },
  { value: 'datasource', label: t('reportDesigner.dataset.kindDatasource'), icon: Database },
  { value: 'view', label: t('reportDesigner.dataset.kindView'), icon: View },
  { value: 'sql_script', label: t('reportDesigner.dataset.kindScript'), icon: Table2 },
])
const dbLabel = (dbType: string) => DB_TYPES.find((d) => d.value === dbType)?.label ?? dbType
const sourceOptions = computed(() => [
  { value: null, label: t('sqlScript.sourceCatalog'), hint: t('sqlScript.sourceCatalogHint') },
  ...connections.value.map((c) => ({ value: c.id, label: c.name, hint: dbLabel(c.db_type) })),
])
const usable = computed(() => engineCatalog.value.filter((e) => e.available && !e.disabled_by_admin))
const engineOptions = computed(() => [
  { value: null, label: t('sqlScript.engineDefault', { engine: usable.value.find((e) => e.id === defaultEngine(usable.value))?.label ?? '' }) },
  ...usable.value.map((e) => ({ value: e.id, label: e.label })),
])
const datasourceOptions = computed(() => datasources.value.map((d) => ({ value: d.id, label: d.name, hint: folderPaths.pathOf(d.project_id) ?? '' })))
const viewOptions = computed(() => views.value.map((v) => ({ value: v.id, label: v.name, hint: v.datasource_name ?? '' })))
const scriptOptions = computed(() => scripts.value.map((s) => ({ value: s.id, label: s.name, hint: folderPaths.pathOf(s.project_id) ?? '' })))

// ── the SQL's completion: the catalog, or the connection's tables ──
const connection = computed(() => connections.value.find((c) => c.id === draft.value?.connection_id) ?? null)
const dbTables = ref<string[]>([])
watch(() => connection.value?.id ?? null, async (id) => {
  dbTables.value = []
  if (id == null) return
  try { dbTables.value = (await useConnections().tables(id)).tables } catch { dbTables.value = [] }
})
const editorTables = computed<SqlTable[]>(() => {
  const d = draft.value
  if (!d) return []
  if (d.connection_id == null) {
    const dialect = catalogDialect(d.engine ?? defaultEngine(usable.value))
    return catalog.value.filter((x) => x.has_data).map((x) => ({
      name: x.name, insert: sqlName(x, catalog.value, dialect), detail: x.path,
      columns: x.columns.map((c) => ({ name: c.name, insert: sqlIdent(c.name, dialect), type: c.type })),
    }))
  }
  const type = connection.value?.db_type
  if (!type) return []
  return dbTables.value.map((table) => ({ name: table.split('.').pop() ?? table, insert: sqlTableRef(table, type), detail: connection.value?.name ?? '', columns: [] }))
})
const editorRef = ref<{ insert: (s: string) => void } | null>(null)
const sqlText = computed({
  get: () => draft.value?.sql ?? '',
  set: (v: string) => { if (draft.value) draft.value.sql = v },
})

// ── filters of a datasource or a view ──
const filterColumns = computed(() => {
  const d = draft.value
  if (!d) return []
  const dsId = d.kind === 'view' ? views.value.find((v) => v.id === d.view_id)?.datasource_id : d.datasource_id
  const ds = datasources.value.find((x) => x.id === dsId)
  return (ds?.columns ?? []).map((c) => ({ value: c.name, label: c.name, hint: c.dtype }))
})
const opOptions = FILTER_OPS.map((o) => ({ value: o, label: o }))
function filterText(f: DFilter): string {
  return Array.isArray(f.value) ? f.value.join(', ') : f.value == null ? '' : String(f.value)
}
function setFilterValue(f: DFilter, text: string) {
  const v = text.trim()
  const numeric = fieldKind(filterColumns.value.find((c) => c.value === f.column)?.hint) === 'number'
  const one = (s: string) => (numeric && /^-?\d+(\.\d+)?$/.test(s) ? Number(s) : s)
  if (/^\{[A-Za-z_]\w*\}$/.test(v)) f.value = v
  else if (f.op === 'in') f.value = v.split(',').map((s) => s.trim()).filter(Boolean).map(one)
  else f.value = one(v)
}

// ── checks ──
const nameError = computed(() => {
  const d = draft.value
  if (!d) return ''
  if (!NAME_RE.test(d.name)) return t('reportDesigner.dataset.nameRule')
  if (d.name !== original.value && store.doc.value.datasets.some((x) => x.name === d.name)) return t('reportDesigner.dataset.nameTaken', { name: d.name })
  return ''
})
const sourceError = computed(() => {
  const d = draft.value
  if (!d) return ''
  if (d.kind === 'sql' && !d.sql.trim()) return t('reportDesigner.dataset.needSql')
  if (d.kind === 'datasource' && d.datasource_id == null) return t('reportDesigner.dataset.needDatasource')
  if (d.kind === 'view' && d.view_id == null) return t('reportDesigner.dataset.needView')
  if (d.kind === 'sql_script' && d.script_id == null) return t('reportDesigner.dataset.needScript')
  return ''
})
const serverProblems = computed(() => (original.value ? store.problems.value.filter((p) => p.dataset === original.value) : []))

function built(): DDataset | null {
  const d = draft.value
  if (!d) return null
  let source: DSource
  if (d.kind === 'sql') source = { kind: 'sql', sql: d.sql, connection_id: d.connection_id, engine: d.connection_id == null ? d.engine : null }
  else if (d.kind === 'datasource') source = { kind: 'datasource', datasource_id: d.datasource_id!, filters: d.filters.filter((f) => f.column) }
  else if (d.kind === 'view') source = { kind: 'view', view_id: d.view_id!, filters: d.filters.filter((f) => f.column) }
  else source = { kind: 'sql_script', script_id: d.script_id! }
  return {
    name: d.name, source, fields: d.fields,
    calculated: d.calculated.filter((c) => c.name.trim()).map((c) => ({ name: c.name.trim(), expr: c.expr })),
  }
}

// ── fields, read from the definition as it would be with this dataset ──
const refreshing = ref(false)
const refreshError = ref('')
async function refreshFields() {
  const ds = built()
  if (!ds || props.reportId == null || nameError.value || sourceError.value) return
  refreshing.value = true
  refreshError.value = ''
  try {
    const def = JSON.parse(JSON.stringify(store.doc.value))
    const at = def.datasets.findIndex((x: DDataset) => x.name === (original.value ?? ds.name))
    if (at >= 0) def.datasets[at] = ds
    else def.datasets.push(ds)
    const got = await designApi.fields(props.reportId, ds.name, def)
    if (draft.value) draft.value.fields = got.fields
    fieldsFor.value = JSON.stringify(ds.source)
    const count = rowCountOf(got)
    counted.value = count ? { ...count, source: sourceKey(ds.source) } : null
  } catch (e) {
    refreshError.value = errMessage(e)
  } finally {
    refreshing.value = false
  }
}

function apply() {
  const ds = built()
  if (!ds || nameError.value || sourceError.value) return
  if (JSON.stringify(ds.source) !== fieldsFor.value) ds.fields = []
  const doc = store.doc.value
  const old = original.value
  const at = old ? doc.datasets.findIndex((x) => x.name === old) : -1
  if (at >= 0) doc.datasets.splice(at, 1, ds)
  else doc.datasets.push(ds)
  if (old && old !== ds.name) {
    // what read the dataset by its old name reads it by the new one, the aggregates that
    // name it (SUM([amount], "old")) included
    for (const b of ['page_header', 'body', 'page_footer'] as const) {
      for (const it of doc.sections[b].items) if ('dataset' in it && it.dataset === old) it.dataset = ds.name
    }
    for (const p of doc.parameters) if (p.choices && 'dataset' in p.choices && p.choices.dataset === old) p.choices.dataset = ds.name
    renameEverywhere(doc, { dataset: [old, ds.name] })
  }
  store.problems.value = store.problems.value.filter((p) => p.dataset !== old)
  // the count goes with the dataset while its source is the one counted
  const { [old ?? ds.name]: _, [ds.name]: __, ...others } = store.rowCounts.value
  store.rowCounts.value = countFor(counted.value, ds.source) ? { ...others, [ds.name]: counted.value! } : others
  emit('applied', ds.name)
  emit('close')
}

const params = computed(() => store.doc.value.parameters)
const braced = (name: string) => `{${name}}`
const calcProblem = (i: number) => serverProblems.value.find((p) => samePath(p.path, `calculated[${i}]`)) ?? null
/** the live check's verdict on a calculated field, while the draft still holds what it judged */
function calcLive(i: number) {
  const d = draft.value
  if (!d || !original.value) return null
  return store.liveProblems.value.find((p) => p.dataset === original.value && /^calculated\[\d+\]/.test(p.path)
    && samePath(p.path.replace(/\.expr$/, ''), `calculated[${i}]`)
    && p.seen === JSON.stringify(valueAt({ calculated: d.calculated }, p.path) ?? null)) ?? null
}
const fieldsForCalc = computed(() => (draft.value?.fields.length ? draft.value.fields : null))
/** the rows counted, while the draft's source is still the one counted */
const rowCount = computed(() => countFor(counted.value, built()?.source))
</script>

<template>
  <Modal :open="open" :title="name ? $t('reportDesigner.dataset.editTitle', { name }) : $t('reportDesigner.dataset.newTitle')" :width="780" @close="emit('close')">
    <div v-if="draft" class="rdd">
      <div v-for="(p, i) in serverProblems" :key="i" class="rdd-alert" role="alert">{{ p.path ? `${p.path}: ` : '' }}{{ p.message }}</div>
      <div class="rp-row">
        <label class="rp-f rdd-name">
          <span>{{ $t('reportDesigner.dataset.name') }}</span>
          <input v-model="draft.name" type="text" maxlength="64" spellcheck="false" class="rdd-mono" :aria-invalid="!!nameError" />
        </label>
        <div class="rp-f">
          <span>{{ $t('reportDesigner.dataset.kind') }}</span>
          <RdSeg v-model="draft.kind" :options="kinds" :label="$t('reportDesigner.dataset.kind')" />
        </div>
      </div>
      <p v-if="nameError" class="rp-err">{{ nameError }}</p>

      <!-- SQL -->
      <template v-if="draft.kind === 'sql'">
        <div class="rp-row">
          <label class="rp-f">
            <span>{{ $t('reportDesigner.dataset.runsOn') }}</span>
            <Select :model-value="draft.connection_id" :options="sourceOptions" :aria-label="$t('reportDesigner.dataset.runsOn')" @update:model-value="draft.connection_id = $event" />
          </label>
          <label v-if="draft.connection_id == null" class="rp-f">
            <span>{{ $t('reportDesigner.dataset.engine') }}</span>
            <Select :model-value="draft.engine" :options="engineOptions" :aria-label="$t('reportDesigner.dataset.engine')" @update:model-value="draft.engine = $event" />
          </label>
        </div>
        <div class="rdd-editor" data-own-escape>
          <SqlEditor ref="editorRef" v-model="sqlText" :tables="editorTables" />
        </div>
        <p class="rp-help rdd-agg">
          {{ $t('reportDesigner.dataset.aggregateHelp') }} <code>{{ $t('reportDesigner.dataset.aggregateExample') }}</code> {{ $t('reportDesigner.dataset.aggregateWhy') }}
        </p>
        <div class="rdd-params">
          <p class="rp-help">{{ $t('reportDesigner.dataset.paramsHelp') }}</p>
          <div v-if="params.length" class="rdd-chips">
            <button v-for="p in params" :key="p.name" type="button" class="mini rdd-chip" :title="$t('reportDesigner.dataset.insertParam')" @click="editorRef?.insert(braced(p.name))">
              {{ braced(p.name) }}
            </button>
          </div>
        </div>
      </template>

      <!-- a datasource or a saved view -->
      <template v-else-if="draft.kind === 'datasource' || draft.kind === 'view'">
        <label class="rp-f">
          <span>{{ draft.kind === 'view' ? $t('reportDesigner.dataset.view') : $t('reportDesigner.dataset.datasource') }}</span>
          <Select v-if="draft.kind === 'view'" :model-value="draft.view_id" :options="viewOptions" searchable :placeholder="$t('reportDesigner.dataset.choose')" :aria-label="$t('reportDesigner.dataset.view')" @update:model-value="draft.view_id = $event" />
          <Select v-else :model-value="draft.datasource_id" :options="datasourceOptions" searchable :placeholder="$t('reportDesigner.dataset.choose')" :aria-label="$t('reportDesigner.dataset.datasource')" @update:model-value="draft.datasource_id = $event" />
        </label>
        <div class="rp-list">
          <div class="rp-lhead">
            <span>{{ $t('reportDesigner.dataset.filters') }}</span>
            <button type="button" class="mini" @click="draft.filters.push({ column: '', op: '=', value: '' })"><Plus :size="12" /> {{ $t('reportDesigner.dataset.addFilter') }}</button>
          </div>
          <p class="rp-help">{{ $t('reportDesigner.dataset.filtersHelp') }}</p>
          <div v-for="(f, i) in draft.filters" :key="i" class="rdd-filter">
            <Select :model-value="f.column || null" :options="filterColumns" searchable :placeholder="$t('reportDesigner.dataset.column')" :aria-label="$t('reportDesigner.dataset.column')" @update:model-value="f.column = $event" />
            <Select class="rdd-op" :model-value="f.op" :options="opOptions" :aria-label="$t('reportDesigner.dataset.operator')" @update:model-value="f.op = $event" />
            <input type="text" class="rdd-mono" :value="filterText(f)" :placeholder="f.op === 'in' ? 'a, b, c' : '{region}'" :aria-label="$t('reportDesigner.dataset.value')" @change="setFilterValue(f, ($event.target as HTMLInputElement).value)" />
            <button type="button" class="rp-icon danger" :aria-label="$t('reportDesigner.props.remove')" :title="$t('reportDesigner.props.remove')" @click="draft.filters.splice(i, 1)"><Trash2 :size="13" /></button>
          </div>
        </div>
      </template>

      <!-- a saved SQL script -->
      <template v-else>
        <label class="rp-f">
          <span>{{ $t('reportDesigner.dataset.script') }}</span>
          <Select :model-value="draft.script_id" :options="scriptOptions" searchable :placeholder="$t('reportDesigner.dataset.choose')" :aria-label="$t('reportDesigner.dataset.script')" @update:model-value="draft.script_id = $event" />
        </label>
        <p class="rp-help">{{ $t('reportDesigner.dataset.scriptHelp') }}</p>
      </template>
      <p v-if="sourceError" class="rp-help rdd-need">{{ sourceError }}</p>

      <!-- calculated fields -->
      <div class="rp-list">
        <div class="rp-lhead">
          <span>{{ $t('reportDesigner.dataset.calculated') }}</span>
          <button type="button" class="mini" @click="draft.calculated.push({ name: '', expr: '' })"><Plus :size="12" /> {{ $t('reportDesigner.dataset.addCalculated') }}</button>
        </div>
        <div v-for="(c, i) in draft.calculated" :key="i" class="rdd-calc">
          <input v-model="c.name" type="text" class="rdd-mono rdd-cname" maxlength="256" :placeholder="$t('reportDesigner.dataset.calcName')" :aria-label="$t('reportDesigner.dataset.calcName')" />
          <RdExprInput v-model="c.expr" :fields="fieldsForCalc" :params="store.params.value" :label="$t('reportDesigner.dataset.calcExpr')" placeholder="[revenue] - [cost]"
                       :problem="calcProblem(i)" :live="calcLive(i)" />
          <button type="button" class="rp-icon danger" :aria-label="$t('reportDesigner.props.remove')" :title="$t('reportDesigner.props.remove')" @click="draft.calculated.splice(i, 1)"><Trash2 :size="13" /></button>
        </div>
      </div>

      <!-- the fields as the data has them -->
      <div class="rp-list">
        <div class="rp-lhead">
          <span>{{ $t('reportDesigner.dataset.fields', { n: draft.fields.length }) }}</span>
          <button type="button" class="mini" :disabled="refreshing || reportId == null || !!nameError || !!sourceError" @click="refreshFields">
            <LoaderCircle v-if="refreshing" :size="12" class="spin" /><RefreshCw v-else :size="12" /> {{ $t('reportDesigner.data.refreshFields') }}
          </button>
        </div>
        <p v-if="refreshError" class="rp-err" role="alert">{{ refreshError }}</p>
        <RdRowCount v-else :count="rowCount" />
        <p v-if="!refreshError && !draft.fields.length" class="rp-help">{{ $t('reportDesigner.dataset.fieldsHelp') }}</p>
        <ul v-else class="rdd-fields">
          <li v-for="f in draft.fields" :key="f.name"><RdFieldIcon :type="f.type" :size="12" /> <span class="rdd-mono">{{ f.name }}</span></li>
        </ul>
      </div>
    </div>
    <template #footer>
      <span class="rdd-grow" />
      <button type="button" @click="emit('close')">{{ $t('reportDesigner.cancel') }}</button>
      <button type="button" class="primary" :disabled="!!nameError || !!sourceError" @click="apply">{{ name ? $t('reportDesigner.apply') : $t('reportDesigner.dataset.add') }}</button>
    </template>
  </Modal>
</template>

<style scoped src="~/assets/reportprops.css" />
<style scoped>
.rdd { display: flex; flex-direction: column; gap: 12px; }
.rdd-name { flex: 0 0 180px; }
.rdd-mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12.5px; }
.rdd-editor { height: 220px; min-height: 0; }
.rdd-params { display: flex; flex-direction: column; gap: 6px; }
.rdd-chips { display: flex; flex-wrap: wrap; gap: 6px; }
.rdd-chip { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11.5px; color: var(--warning); }
.rdd-filter { display: grid; grid-template-columns: minmax(0, 1.2fr) 90px minmax(0, 1fr) auto; gap: 6px; align-items: center; }
.rdd-filter :deep(.sel-trigger) { padding: 5px 8px; }
.rdd-filter input { padding: 5px 8px; }
.rdd-calc { display: grid; grid-template-columns: 150px minmax(0, 1fr) auto; gap: 6px; align-items: start; }
.rdd-cname { padding: 5px 8px; }
.rdd-fields { list-style: none; margin: 0; padding: 0; display: flex; flex-wrap: wrap; gap: 4px 12px; font-size: 12px; max-height: 120px; overflow: auto; }
.rdd-fields li { display: inline-flex; align-items: center; gap: 4px; color: var(--muted); }
.rdd-fields li :deep(svg) { color: var(--accent-hi); }
.rdd-fields .rdd-mono { color: var(--text); font-size: 12px; }
.rdd-need { color: var(--warning); }
.rdd-agg code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11px; color: var(--text); overflow-wrap: anywhere; }
.rdd-alert {
  padding: 8px 10px; border-radius: 8px; font-size: 12px; line-height: 1.4; overflow-wrap: anywhere;
  color: var(--danger); background: color-mix(in srgb, var(--danger) 8%, transparent); border: 1px solid color-mix(in srgb, var(--danger) 40%, transparent);
}
.rdd-grow { flex: 1; }
</style>
