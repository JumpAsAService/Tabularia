<script setup lang="ts">
// Viewer: mini-strumento BI READ-ONLY su una Datasource Tabularia. Costruisce al
// volo filtri + campi calcolati + pivot e li esegue con l'engine scelto via la
// PREVIEW (che legge lo snapshot della datasource, NON la modifica). La tabella
// e i grafici (ChartPanel, aggregazione sull'intero dataset) girano sugli stessi
// dati trasformati. Nulla viene salvato.
import { ref, computed } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  PieChart, Table2, Filter, Sigma, Plus, X, Play, Cpu, Rows3, Download, FileSpreadsheet,
} from 'lucide-vue-next'
import { useApi, errMessage, type Operation, type ColumnInfo } from '~/composables/useApi'
import { useDatasources, type DatasourceInfo } from '~/composables/useDatasources'

const api = useApi()
const dsApi = useDatasources()
const { t } = useI18n()

// ── sorgente + motore ─────────────────────────────────────────────────────
const datasources = ref<DatasourceInfo[]>([])
const dsId = ref<number | null>(null)
const { preferredEngine, defaultEngine } = usePreferredEngine()
const engines = ref<{ id: string; label: string; available: boolean }[]>([
  { id: 'polars', label: 'Polars', available: true },
])
// default = motore preferito dell'utente (fallback Polars finché il catalogo
// non è caricato; poi onMounted valida la disponibilità)
const engine = ref(preferredEngine.value)

const selectedDs = computed(() => datasources.value.find((d) => d.id === dsId.value) || null)
const dsOptions = computed(() =>
  datasources.value.filter((d) => d.key).map((d) => ({ value: d.id, label: d.name })),
)
const engineOptions = computed(() =>
  engines.value.filter((e) => e.available).map((e) => ({ value: e.id, label: e.label })),
)

// ── trasformazioni al volo ────────────────────────────────────────────────
const FILTER_OPS = [
  { value: 'eq', label: '=' }, { value: 'ne', label: '≠' },
  { value: 'gt', label: '>' }, { value: 'ge', label: '≥' },
  { value: 'lt', label: '<' }, { value: 'le', label: '≤' },
  { value: 'between', label: t('viewer.opBetween') },
  { value: 'contains', label: t('viewer.opContains') }, { value: 'starts_with', label: t('viewer.opStartsWith') },
  { value: 'in', label: t('viewer.opIn') }, { value: 'not_in', label: t('viewer.opNotIn') },
  { value: 'is_null', label: t('viewer.opIsNull') }, { value: 'is_not_null', label: t('viewer.opIsNotNull') },
]
const NO_VALUE = new Set(['is_null', 'is_not_null'])

// colonna temporale → input calendario nativo (come nell'editor di flussi)
function temporalType(col: string): 'date' | 'datetime-local' | 'time' | 'text' {
  const dt = baseCols.value.find((c) => c.name === col)?.dtype || ''
  if (dt.startsWith('Datetime')) return 'datetime-local'
  if (dt === 'Date') return 'date'
  if (dt === 'Time') return 'time'
  return 'text'
}
const AGG_FUNCS = [
  { value: 'sum', label: t('viewer.aggSum') }, { value: 'mean', label: t('viewer.aggMean') },
  { value: 'min', label: t('viewer.aggMin') }, { value: 'max', label: t('viewer.aggMax') },
  { value: 'count', label: t('viewer.aggCount') },
]

interface FilterRow { column: string; operator: string; value: string; value2: string }
interface CompRow { name: string; expr: string }

const filters = ref<FilterRow[]>([])
const computedFields = ref<CompRow[]>([])

// larghezza sidebar (ridimensionabile)
const sidebarW = ref(300)
function startResize(e: MouseEvent) {
  const startX = e.clientX
  const startW = sidebarW.value
  const onMove = (ev: MouseEvent) => { sidebarW.value = Math.min(600, Math.max(220, startW + ev.clientX - startX)) }
  const onUp = () => {
    document.removeEventListener('mousemove', onMove)
    document.removeEventListener('mouseup', onUp)
    document.body.style.userSelect = ''
  }
  document.body.style.userSelect = 'none'
  document.addEventListener('mousemove', onMove)
  document.addEventListener('mouseup', onUp)
}
const pivotOn = ref(false)
const outline = ref(false) // non ripetere i valori delle dimensioni di riga (vista compatta)
const pivot = ref<{ index: string[]; on: string[]; values: string; func: string }>({
  index: [''], on: [''], values: '', func: 'sum',
})

const addFilter = () => filters.value.push({ column: '', operator: 'eq', value: '', value2: '' })
const addComputed = () => computedFields.value.push({ name: '', expr: '' })

// ── schema corrente (per i picker) + risultato tabella ────────────────────
const baseCols = ref<ColumnInfo[]>([]) // colonne dopo filtri+campi (NO pivot) → chart & pivot config
const tableCols = ref<ColumnInfo[]>([]) // colonne mostrate in tabella (con eventuale pivot)
const rows = ref<Record<string, any>[]>([])
const view = ref<'table' | 'chart'>('table')
const loading = ref(false)
const error = ref('')
const ROW_LIMIT = 500

const colNames = computed(() => baseCols.value.map((c) => c.name))
const isNum = (name: string) => /Int|Float|Decimal/i.test(baseCols.value.find((c) => c.name === name)?.dtype || '')

function parseVal(v: string, col: string): any {
  if (v === '' || v == null) return v
  if (v.includes(',')) return v.split(',').map((s) => s.trim()) // liste per in/not_in
  if (isNum(col)) { const n = Number(v); return Number.isNaN(n) ? v : n }
  return v
}

// base ops = filtri + campi calcolati (condivise da tabella e grafico)
const baseOps = computed<Operation[]>(() => {
  const ops: Operation[] = []
  for (const f of filters.value) {
    if (!f.column || !f.operator) continue
    const params: any = { column: f.column, operator: f.operator }
    if (f.operator === 'between') {
      if (f.value === '' || f.value2 === '') continue // servono entrambi gli estremi
      params.value = [parseVal(f.value, f.column), parseVal(f.value2, f.column)]
    } else if (!NO_VALUE.has(f.operator)) {
      params.value = parseVal(f.value, f.column)
    }
    ops.push({ type: 'filter', params })
  }
  const comp = computedFields.value.filter((c) => c.name.trim() && c.expr.trim())
  if (comp.length) ops.push({ type: 'compute', params: { columns: comp.map((c) => ({ name: c.name.trim(), expr: c.expr.trim() })) } })
  return ops
})

// query per ChartPanel: prepende SEMPRE le base ops (il grafico aggrega i dati filtrati+calcolati)
async function chartQuery(ops: Operation[], limit?: number) {
  const ds = selectedDs.value
  if (!ds) return null
  return await api.preview({ bucket: ds.bucket, input_key: ds.key, operations: [...baseOps.value, ...ops], engine: engine.value, limit, no_cache: true })
}

async function onPickDatasource(id: number | null) {
  dsId.value = id
  rows.value = []; error.value = ''
  const ds = selectedDs.value
  baseCols.value = ds?.columns ? ds.columns.map((c) => ({ name: c.name, dtype: c.dtype })) : []
  tableCols.value = baseCols.value
  if (ds) apply()
}

// operazioni della vista corrente: filtri + campi calcolati + eventuale pivot.
// Condivise da tabella, grafico ed export.
function buildTableOps(): Operation[] {
  const ops = [...baseOps.value]
  const pIndex = pivot.value.index.filter(Boolean)
  const pOn = pivot.value.on.filter(Boolean)
  if (pivotOn.value && pOn.length && pivot.value.values && pIndex.length) {
    ops.push({ type: 'pivot', params: { index: pIndex, on: pOn, values: pivot.value.values, func: pivot.value.func } })
  }
  return ops
}

async function apply() {
  const ds = selectedDs.value
  if (!ds) return
  loading.value = true; error.value = ''
  try {
    // 1) schema dopo filtri+campi (per i picker di grafico/pivot)
    const base = await api.preview({ bucket: ds.bucket, input_key: ds.key, operations: baseOps.value, engine: engine.value, limit: 1, no_cache: true })
    baseCols.value = base.columns
    // 2) risultato tabella (con eventuale pivot)
    const res = await api.preview({ bucket: ds.bucket, input_key: ds.key, operations: buildTableOps(), engine: engine.value, limit: ROW_LIMIT, no_cache: true })
    rows.value = res.rows
    tableCols.value = res.columns
  } catch (e) {
    error.value = errMessage(e); rows.value = []
  } finally {
    loading.value = false
  }
}

const cell = (v: any) => (v === null || v === undefined ? '' : typeof v === 'number' ? new Intl.NumberFormat('it-IT', { maximumFractionDigits: 4 }).format(v) : String(v))

// export della vista corrente (csv/xlsx). Riusa /tasks/export → gira su Polars,
// a prescindere dal motore scelto per la visualizzazione.
const exporting = ref('')
async function exportView(format: 'csv' | 'xlsx') {
  const ds = selectedDs.value
  if (!ds) return
  exporting.value = format
  error.value = ''
  try {
    const filename = `${ds.name}_viewer.${format}`.toLowerCase().replace(/[^a-z0-9._-]+/g, '_')
    const blob = await api.exportData({ bucket: ds.bucket, input_key: ds.key, operations: buildTableOps(), format, filename, engine: engine.value })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = filename
    a.click()
    URL.revokeObjectURL(url)
  } catch (e) {
    error.value = errMessage(e)
  } finally {
    exporting.value = ''
  }
}

// righe da mostrare: in vista "outline" i valori delle dimensioni di riga NON si
// ripetono — il prefisso condiviso con la riga precedente viene lasciato vuoto
// (es. la regione appare una sola volta, poi solo le province). Nessun subtotale.
const displayRows = computed<Record<string, any>[]>(() => {
  if (!outline.value || !pivotOn.value) return rows.value
  const idx = pivot.value.index.filter(Boolean)
  if (idx.length < 2) return rows.value // con una sola dimensione non c'è nulla da comprimere
  const out: Record<string, any>[] = []
  let prev: Record<string, any> | null = null
  for (const r of rows.value) {
    const d = { ...r }
    if (prev) {
      // quanti livelli iniziali coincidono con la riga precedente
      let depth = 0
      while (depth < idx.length && String(prev[idx[depth]]) === String(r[idx[depth]])) depth++
      for (let j = 0; j < depth; j++) d[idx[j]] = '' // svuota il prefisso condiviso
    }
    out.push(d)
    prev = r
  }
  return out
})

onMounted(async () => {
  try { datasources.value = await dsApi.list() } catch (e) { error.value = errMessage(e) }
  try {
    engines.value = await api.engines()
    // la preferita potrebbe non essere disponibile: applica il fallback robusto
    engine.value = defaultEngine(engines.value)
  } catch { /* fallback polars */ }
})
</script>

<template>
  <AppShell fluid>
   <div class="viewer">
    <div class="page-head">
      <h2><PieChart :size="18" /> {{ $t('viewer.pageTitle') }}</h2>
      <div class="head-actions">
        <label class="hl"><Cpu :size="13" /> {{ $t('viewer.engineLabel') }}</label>
        <Select v-model="engine" :options="engineOptions" class="engsel" />
        <Select
          :model-value="dsId"
          :options="dsOptions"
          searchable
          :placeholder="$t('viewer.datasourcePlaceholder')"
          class="dssel"
          @update:model-value="onPickDatasource"
        />
      </div>
    </div>

    <p v-if="!selectedDs" class="muted empty">{{ $t('viewer.emptyStateHint') }}</p>

    <div v-else class="body" :style="{ gridTemplateColumns: `${sidebarW}px 6px 1fr` }">
      <!-- pannello trasformazioni al volo -->
      <aside class="cfg">
        <div class="cfg-sec">
          <div class="cfg-head"><Filter :size="13" /> {{ $t('viewer.filtersTitle') }} <button class="add" @click="addFilter"><Plus :size="13" /></button></div>
          <p v-if="!filters.length" class="muted small">{{ $t('viewer.noFilters') }}</p>
          <div v-for="(f, i) in filters" :key="i" class="frow">
            <Select v-model="f.column" :options="colNames" :placeholder="$t('viewer.columnPlaceholder')" class="fc" />
            <Select v-model="f.operator" :options="FILTER_OPS" class="fo" />
            <template v-if="f.operator === 'between'">
              <input v-model="f.value" :type="temporalType(f.column)" class="fv" :placeholder="$t('viewer.fromPlaceholder')" />
              <input v-model="f.value2" :type="temporalType(f.column)" class="fv" :placeholder="$t('viewer.toPlaceholder')" />
            </template>
            <input
              v-else-if="!NO_VALUE.has(f.operator)"
              v-model="f.value"
              :type="temporalType(f.column)"
              class="fv"
              :placeholder="$t('viewer.valuePlaceholder')"
            />
            <button class="rm" @click="filters.splice(i, 1)"><X :size="13" /></button>
          </div>
        </div>

        <div class="cfg-sec">
          <div class="cfg-head"><Sigma :size="13" /> {{ $t('viewer.computedFieldsTitle') }} <button class="add" @click="addComputed"><Plus :size="13" /></button></div>
          <p class="muted small">{{ $t('viewer.computedFieldsHint', { engine }) }}</p>
          <div v-for="(c, i) in computedFields" :key="i" class="crow">
            <div class="crow-top">
              <input v-model="c.name" type="text" class="cn" :placeholder="$t('viewer.fieldNamePlaceholder')" />
              <button class="rm" @click="computedFields.splice(i, 1)"><X :size="13" /></button>
            </div>
            <textarea v-model="c.expr" class="ce" rows="2" :placeholder="$t('viewer.exprPlaceholder')" />
          </div>
        </div>

        <div class="cfg-sec">
          <div class="cfg-head">
            <Rows3 :size="13" /> {{ $t('viewer.pivotTitle') }}
            <label class="toggle"><input v-model="pivotOn" type="checkbox" /> {{ $t('viewer.pivotEnable') }}</label>
          </div>
          <template v-if="pivotOn">
            <label class="lbl">{{ $t('viewer.pivotRowsLabel') }}</label>
            <div v-for="(_, i) in pivot.index" :key="'ix' + i" class="dimrow">
              <Select v-model="pivot.index[i]" :options="colNames" :placeholder="$t('viewer.columnPlaceholder')" class="full" />
              <button class="rm" :disabled="pivot.index.length === 1" @click="pivot.index.splice(i, 1)"><X :size="13" /></button>
            </div>
            <button class="add-dim" @click="pivot.index.push('')"><Plus :size="12" /> {{ $t('viewer.addRow') }}</button>

            <label class="lbl">{{ $t('viewer.pivotColumnsLabel') }}</label>
            <div v-for="(_, i) in pivot.on" :key="'on' + i" class="dimrow">
              <Select v-model="pivot.on[i]" :options="colNames" :placeholder="$t('viewer.columnPlaceholder')" class="full" />
              <button class="rm" :disabled="pivot.on.length === 1" @click="pivot.on.splice(i, 1)"><X :size="13" /></button>
            </div>
            <button class="add-dim" @click="pivot.on.push('')"><Plus :size="12" /> {{ $t('viewer.addColumn') }}</button>

            <label class="lbl">{{ $t('viewer.valuesLabel') }}</label>
            <Select v-model="pivot.values" :options="colNames" :placeholder="$t('viewer.valueColumnPlaceholder')" class="full" />
            <label class="lbl">{{ $t('viewer.functionLabel') }}</label>
            <Select v-model="pivot.func" :options="AGG_FUNCS" class="full" />
            <label class="checkline" :title="$t('viewer.outlineHint')">
              <input v-model="outline" type="checkbox" /> {{ $t('viewer.outlineToggle') }}
            </label>
          </template>
        </div>
      </aside>

      <!-- maniglia di ridimensionamento della sidebar -->
      <div class="resizer" :title="$t('viewer.resizeHint')" @mousedown="startResize" />

      <!-- area risultato: tabella o grafico -->
      <section class="result">
        <div class="rtabs">
          <button :class="{ active: view === 'table' }" @click="view = 'table'"><Table2 :size="14" /> {{ $t('viewer.tableTab') }}</button>
          <button :class="{ active: view === 'chart' }" @click="view = 'chart'"><PieChart :size="14" /> {{ $t('viewer.chartTab') }}</button>
          <button class="primary apply" :disabled="!selectedDs || loading" @click="apply"><Play :size="14" /> {{ $t('viewer.applyButton') }}</button>
          <span v-if="loading" class="muted">{{ $t('viewer.computing') }}</span>
          <span v-else-if="error" class="err">{{ error }}</span>
          <span class="rmeta muted">{{ $t('viewer.rowsColsMeta', { n: rows.length, m: tableCols.length }) }}</span>
          <div class="expbtns">
            <button :disabled="!rows.length || !!exporting" :title="$t('viewer.exportCsvTitle')" @click="exportView('csv')">
              <Download :size="13" /> {{ exporting === 'csv' ? '…' : 'CSV' }}
            </button>
            <button :disabled="!rows.length || !!exporting" :title="$t('viewer.exportExcelTitle')" @click="exportView('xlsx')">
              <FileSpreadsheet :size="13" /> {{ exporting === 'xlsx' ? '…' : 'Excel' }}
            </button>
          </div>
        </div>

        <div v-show="view === 'table'" class="tablewrap">
          <div v-if="loading" class="skeleton">
            <div class="sk-row sk-head"><span v-for="m in 7" :key="m" class="sk-cell" /></div>
            <div v-for="n in 14" :key="n" class="sk-row"><span v-for="m in 7" :key="m" class="sk-cell" /></div>
          </div>
          <table v-else-if="rows.length" class="dt">
            <thead>
              <tr>
                <th v-for="c in tableCols" :key="c.name" :class="{ num: /Int|Float|Decimal/i.test(c.dtype) }">
                  {{ c.name }}<span class="dtype">{{ c.dtype }}</span>
                </th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(row, i) in displayRows" :key="i">
                <td v-for="c in tableCols" :key="c.name" :class="{ num: /Int|Float|Decimal/i.test(c.dtype) }">{{ cell(row[c.name]) }}</td>
              </tr>
            </tbody>
          </table>
          <p v-else-if="!loading" class="muted empty">{{ $t('viewer.noDataHint') }}</p>
        </div>

        <div v-show="view === 'chart'" class="chartwrap">
          <ChartPanel :columns="baseCols" :query="chartQuery" />
        </div>
      </section>
    </div>
   </div>
  </AppShell>
</template>

<style scoped src="~/assets/listpage.css"></style>
<style scoped>
.viewer { display: flex; flex-direction: column; flex: 1; min-height: 0; }
.head-actions { display: flex; align-items: center; gap: 8px; }
.hl { display: inline-flex; align-items: center; gap: 4px; font-size: 12px; color: var(--muted); }
.engsel { width: 140px; }
.dssel { width: 260px; }
.empty { padding: 24px; }
.body { display: grid; grid-template-columns: 300px 6px 1fr; gap: 0; flex: 1; min-height: 0; }
.resizer { cursor: col-resize; background: transparent; position: relative; }
.resizer::before { content: ''; position: absolute; inset: 0 2px; border-radius: 2px; background: var(--border); transition: background 0.15s; }
.resizer:hover::before { background: var(--accent); }

.cfg { overflow-y: auto; display: flex; flex-direction: column; gap: 14px; padding: 0 10px 0 2px; }
.cfg-sec { border: 1px solid var(--border); border-radius: 10px; padding: 10px; }
.cfg-head { display: flex; align-items: center; gap: 6px; font-weight: 600; font-size: 13px; margin-bottom: 8px; }
.cfg-head .add { margin-left: auto; padding: 2px 6px; }
.toggle { margin-left: auto; font-size: 11px; color: var(--muted); display: inline-flex; align-items: center; gap: 4px; font-weight: 400; }
.small { font-size: 11px; }
.lbl { display: block; font-size: 11px; color: var(--muted); margin: 6px 0 3px; }
.full { width: 100%; margin-bottom: 2px; }
.frow { display: flex; align-items: center; gap: 4px; margin-bottom: 6px; flex-wrap: wrap; }
.crow { display: flex; flex-direction: column; gap: 4px; margin-bottom: 12px; }
.crow-top { display: flex; align-items: center; gap: 5px; }
.fc { flex: 1 1 100%; min-width: 0; } .fo { width: 104px; flex-shrink: 0; } .fv { flex: 1; min-width: 0; }
.crow .cn { flex: 1; min-width: 0; }
.crow .ce {
  width: 100%;
  min-height: 52px;
  resize: vertical;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
  line-height: 1.4;
}
.rm { padding: 5px 6px; flex-shrink: 0; }
.dimrow { display: flex; align-items: center; gap: 4px; margin-bottom: 4px; }
.dimrow .full { flex: 1; min-width: 0; }
.add-dim { font-size: 11px; padding: 3px 8px; margin: 2px 0 4px; }
.checkline { display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--text); margin-top: 8px; cursor: pointer; }

.result { display: flex; flex-direction: column; min-height: 0; border: 1px solid var(--border); border-radius: 10px; overflow: hidden; }
.rtabs { display: flex; align-items: center; gap: 6px; padding: 8px 10px; border-bottom: 1px solid var(--border); }
.rtabs button { padding: 5px 10px; }
.rtabs button.active { border-color: var(--accent); background: rgba(79, 140, 255, 0.12); }
.rtabs .apply { margin-left: 8px; }
.rmeta { margin-left: auto; font-size: 12px; }
.expbtns { display: flex; gap: 4px; }
.expbtns button { padding: 5px 9px; }
.err { color: var(--danger); font-size: 12px; }
.tablewrap { flex: 1; overflow: auto; min-height: 0; }
.chartwrap { flex: 1; min-height: 0; padding: 12px; }
.dt { border-collapse: collapse; font-size: 12px; width: max-content; min-width: 100%; }
.dt th, .dt td {
  border-bottom: 1px solid var(--border-soft);
  border-right: 1px solid var(--border-soft); /* separatore di colonna */
  padding: 5px 12px;
  text-align: center;
  white-space: nowrap;
  vertical-align: top;
}
.dt th:last-child, .dt td:last-child { border-right: none; }
.dt th { position: sticky; top: 0; background: var(--panel); z-index: 1; font-weight: 600; vertical-align: bottom; }
.dt .dtype { display: block; font-size: 9px; color: var(--muted); font-weight: 400; text-transform: lowercase; }
.dt td.num { font-variant-numeric: tabular-nums; }
.dt tbody tr:hover { background: var(--panel-2); }

/* skeleton di caricamento (shimmer) */
.skeleton { padding: 12px 14px; }
.sk-row { display: flex; gap: 12px; margin-bottom: 11px; }
.sk-row.sk-head .sk-cell { height: 20px; opacity: 0.85; }
.sk-cell {
  flex: 1;
  height: 13px;
  border-radius: 4px;
  background: linear-gradient(90deg, var(--panel-2) 25%, var(--border) 37%, var(--panel-2) 63%);
  background-size: 400% 100%;
  animation: sk-shimmer 1.4s ease infinite;
}
@keyframes sk-shimmer { 0% { background-position: 100% 50%; } 100% { background-position: 0 50%; } }
</style>
