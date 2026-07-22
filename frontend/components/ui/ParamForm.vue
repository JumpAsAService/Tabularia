<script setup lang="ts">
import { reactive, computed, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { Info, X, Calendar, Play } from 'lucide-vue-next'
import type { ColumnInfo } from '~/composables/useApi'
import {
  OP_SPECS,
  FILTER_OPERATORS,
  AGG_FUNCS,
  DTYPES,
  JOIN_HOWS,
  NO_VALUE_OPERATORS,
  OPERATOR_LABELS,
  AGG_LABELS,
  HOW_LABELS,
  DTYPE_LABELS,
  STRATEGY_LABELS,
  type FieldSpec,
} from '~/composables/useFlowModel'

const props = defineProps<{
  nodeId: string
  opType: string
  params: Record<string, any>
  inputColumns: ColumnInfo[]
  rightColumns?: ColumnInfo[]
  // le colonne del nodo sono in risoluzione (preview a monte in corso)
  columnsLoading?: boolean
  // dentro un container foreach: placeholder {{...}} utilizzabili ovunque
  placeholders?: string[]
  // carica i valori distinti di una colonna (per il picker di in/not_in)
  fetchDistinct?: (column: string) => Promise<any[]>
}>()
const emit = defineEmits<{
  (e: 'update', params: Record<string, any>): void
  (e: 'preview'): void // anteprima MANUALE (nodo SQL): non su ogni tasto
}>()
const { t } = useI18n()

const spec = computed<FieldSpec[]>(() => OP_SPECS[props.opType] ?? [])
const leftNames = computed(() => props.inputColumns.map((c) => c.name))
const rightNames = computed(() => (props.rightColumns ?? []).map((c) => c.name))
// opzioni placeholder pronte all'uso ("{{colonna}}") per i selettori
const phOptions = computed(() => (props.placeholders ?? []).map((p) => `{{${p}}}`))

// colonne selezionabili per un campo (per il join 'on' = intersezione)
function optionsFor(field: FieldSpec): string[] {
  if (props.opType === 'join' && field.key === 'on') {
    const r = new Set(rightNames.value)
    return leftNames.value.filter((n) => r.has(n))
  }
  return leftNames.value
}

// ── Filtro su colonne temporali → input calendario ────────────────────────
// dtype (dal backend: 'Date', "Datetime(...)", 'Time') della colonna scelta
const temporalInputType = computed<'date' | 'datetime-local' | 'time' | null>(() => {
  const dt = props.inputColumns.find((c) => c.name === state.column)?.dtype ?? ''
  if (dt.startsWith('Datetime')) return 'datetime-local'
  if (dt === 'Date') return 'date'
  if (dt === 'Time') return 'time'
  return null
})
// operatori a valore singolo che hanno senso col calendario
const SINGLE_DATE_OPERATORS = new Set(['eq', 'ne', 'gt', 'ge', 'lt', 'le'])

// messaggio diagnostico quando il join non ha colonne chiave disponibili
const joinOnDiag = computed(() => {
  if (!leftNames.value.length) return t('paramForm.joinDiagNoLeft')
  if (!rightNames.value.length) return t('paramForm.joinDiagNoRight')
  return t('paramForm.joinDiagNoCommon')
})

function emptyMessage(field: FieldSpec): string {
  if (props.opType === 'join' && field.key === 'on') return joinOnDiag.value
  return t('paramForm.noColumnsAvailable')
}

// ── Stato editabile locale ────────────────────────────────────────────────
const state = reactive<Record<string, any>>({})
const castRows = reactive<{ column: string; dtype: string }[]>([])
const renameRows = reactive<{ from: string; to: string }[]>([])
const fillRows = reactive<{ column: string; value: string }[]>([])
const aggRows = reactive<{ column: string; func: string; alias: string }[]>([])
const exprRows = reactive<{ name: string; expr: string }[]>([])

function parseValue(raw: string): any {
  const t = (raw ?? '').trim()
  if (t === '') return null
  try {
    return JSON.parse(t)
  } catch {
    return raw
  }
}
function showValue(v: any): string {
  if (v === null || v === undefined) return ''
  return typeof v === 'string' ? v : JSON.stringify(v)
}

// ricostruisce lo stato locale dai params quando cambia il nodo/operazione
function rebuild() {
  for (const k of Object.keys(state)) delete state[k]
  castRows.length = renameRows.length = fillRows.length = aggRows.length = exprRows.length = 0
  const p = props.params ?? {}

  for (const f of spec.value) {
    switch (f.control) {
      case 'columns':
        state[f.key] = Array.isArray(p[f.key]) ? [...p[f.key]] : []
        break
      case 'value':
        state.__value = showValue(p.value)
        // estremi del between temporale (se il valore salvato è una coppia)
        state.__lo = Array.isArray(p.value) ? String(p.value[0] ?? '') : ''
        state.__hi = Array.isArray(p.value) ? String(p.value[1] ?? '') : ''
        // valori del picker in/not_in (lista salvata)
        state.__inValues = Array.isArray(p.value) ? [...p.value] : []
        // modalità testo libero: riattivata se il valore salvato usa placeholder
        state.__phmode = JSON.stringify(p.value ?? '').includes('{{')
        break
      case 'json':
        state[`__json_${f.key}`] = p[f.key] != null ? JSON.stringify(p[f.key]) : ''
        break
      case 'castlist':
        for (const [c, d] of Object.entries(p.columns ?? {})) castRows.push({ column: c, dtype: String(d) })
        break
      case 'renamelist':
        for (const [from, to] of Object.entries(p.mapping ?? {})) renameRows.push({ from, to: String(to) })
        break
      case 'filllist':
        for (const [c, v] of Object.entries(p.columns ?? {})) fillRows.push({ column: c, value: showValue(v) })
        break
      case 'agglist':
        for (const a of p.aggregations ?? []) aggRows.push({ column: a.column, func: a.func, alias: a.alias ?? '' })
        break
      case 'exprlist':
        for (const c of p.columns ?? []) exprRows.push({ name: c.name ?? '', expr: c.expr ?? '' })
        if (!exprRows.length) exprRows.push({ name: '', expr: '' }) // parti con una riga pronta
        break
      default:
        state[f.key] = p[f.key]
    }
  }
}
watch(() => props.nodeId, rebuild, { immediate: true })

// ── Picker dei valori distinti per in/not_in ──────────────────────────────
const IN_OPERATORS = new Set(['in', 'not_in'])
const isInOperator = computed(() => IN_OPERATORS.has(state.operator))
const distinctValues = ref<any[] | null>(null)
const distinctLoading = ref(false)
const distinctCache = new Map<string, any[]>() // per colonna, entro questo nodo

async function loadDistinct(column: string) {
  if (!props.fetchDistinct || !column) return
  if (distinctCache.has(column)) {
    distinctValues.value = distinctCache.get(column)!
    return
  }
  distinctLoading.value = true
  try {
    const vals = await props.fetchDistinct(column)
    distinctCache.set(column, vals)
    distinctValues.value = vals
  } catch {
    distinctValues.value = []
  } finally {
    distinctLoading.value = false
  }
}

watch(
  () => [state.operator, state.column],
  () => {
    if (isInOperator.value && state.column) loadDistinct(state.column)
  },
  { immediate: true },
)

function onInValues(values: any[]) {
  state.__inValues = values
  emitUpdate()
}

// ── Costruzione dei params dal form ───────────────────────────────────────
function build(): Record<string, any> {
  const out: Record<string, any> = {}
  for (const f of spec.value) {
    switch (f.control) {
      case 'columns': {
        const arr = state[f.key] ?? []
        if (arr.length) out[f.key] = arr
        break
      }
      case 'value':
        if (NO_VALUE_OPERATORS.has(state.operator)) break
        if (IN_OPERATORS.has(state.operator)) {
          // lista dal picker (i tipi restano quelli originali dei dati)
          if ((state.__inValues ?? []).length) out.value = [...state.__inValues]
        } else if (temporalInputType.value && !state.__phmode && state.operator === 'between') {
          // coppia di date dai calendari: manda solo se entrambe presenti
          if (state.__lo && state.__hi) out.value = [state.__lo, state.__hi]
        } else if (temporalInputType.value && !state.__phmode && SINGLE_DATE_OPERATORS.has(state.operator)) {
          if (state.__value) out.value = state.__value // stringa ISO dal calendario
        } else {
          out.value = parseValue(state.__value)
        }
        break
      case 'number':
        if (state[f.key] !== '' && state[f.key] != null) out[f.key] = Number(state[f.key])
        break
      case 'boolean':
        out[f.key] = !!state[f.key]
        break
      case 'json': {
        const raw = (state[`__json_${f.key}`] ?? '').trim()
        if (raw) {
          try {
            out[f.key] = JSON.parse(raw)
          } catch {
            /* JSON non valido: non inviare il campo finché non è corretto */
          }
        }
        break
      }
      case 'castlist':
        out.columns = Object.fromEntries(castRows.filter((r) => r.column && r.dtype).map((r) => [r.column, r.dtype]))
        break
      case 'renamelist':
        out.mapping = Object.fromEntries(renameRows.filter((r) => r.from && r.to).map((r) => [r.from, r.to]))
        break
      case 'filllist':
        out.columns = Object.fromEntries(fillRows.filter((r) => r.column).map((r) => [r.column, parseValue(r.value)]))
        break
      case 'agglist':
        out.aggregations = aggRows
          .filter((r) => r.column && r.func)
          .map((r) => ({ column: r.column, func: r.func, ...(r.alias ? { alias: r.alias } : {}) }))
        break
      case 'exprlist':
        out.columns = exprRows
          .filter((r) => r.name.trim() && r.expr.trim())
          .map((r) => ({ name: r.name.trim(), expr: r.expr.trim() }))
        break
      default:
        if (state[f.key] !== undefined && state[f.key] !== '') out[f.key] = state[f.key]
    }
  }
  return out
}
function emitUpdate() {
  emit('update', build())
}
// nodo SQL: l'anteprima si lancia SOLO a comando (bottone / Cmd-Ctrl+Enter),
// mai a ogni tasto. Prima salva il testo corrente nei params, poi chiede il run.
function emitPreviewNow() {
  emitUpdate()
  emit('preview')
}

// helper toggle per multi-select a checkbox
function toggleColumn(key: string, name: string) {
  const arr: string[] = state[key] ?? (state[key] = [])
  const i = arr.indexOf(name)
  if (i >= 0) arr.splice(i, 1)
  else arr.push(name)
  emitUpdate()
}
function isChecked(key: string, name: string): boolean {
  return (state[key] ?? []).includes(name)
}

// gestione righe delle liste
function addCast() { castRows.push({ column: '', dtype: 'int' }); }
function addRename() { renameRows.push({ from: '', to: '' }); }
function addFill() { fillRows.push({ column: '', value: '' }); }
function addAgg() { aggRows.push({ column: '', func: 'sum', alias: '' }); }
function addExpr() { exprRows.push({ name: '', expr: '' }); }
function removeRow(rows: any[], i: number) { rows.splice(i, 1); emitUpdate() }

// ── Opzioni per il Select custom ──────────────────────────────────────────
// colonne (+ placeholder raggruppati); withEmpty aggiunge la voce "—"
function columnSelectOptions(f?: FieldSpec, withEmpty = false) {
  const cols = (f ? optionsFor(f) : leftNames.value).map((n) => ({ value: n, label: n }))
  const ph = phOptions.value.map((p) => ({ value: p, label: p, group: 'placeholder' }))
  return [...(withEmpty ? [{ value: undefined, label: '—' }] : []), ...cols, ...ph]
}
const OPERATOR_OPTIONS = FILTER_OPERATORS.map((op) => ({ value: op, label: OPERATOR_LABELS[op] ?? op }))
const HOW_OPTIONS = JOIN_HOWS.map((h) => ({ value: h, label: HOW_LABELS[h] ?? h }))
const DTYPE_OPTIONS = DTYPES.map((d) => ({ value: d, label: DTYPE_LABELS[d] ?? d }))
const FUNC_OPTIONS = AGG_FUNCS.map((fn) => ({ value: fn, label: AGG_LABELS[fn] ?? fn }))
const STRATEGY_OPTIONS = Object.entries(STRATEGY_LABELS).map(([value, label]) => ({ value, label }))
</script>

<template>
  <div class="paramform">
    <!-- colonne in risoluzione: skeleton al posto del falso "nessuna colonna" -->
    <div v-if="columnsLoading && !inputColumns.length" class="skcols" aria-busy="true">
      <span class="sk" style="width: 68%" />
      <span class="sk" style="width: 52%" />
      <span class="sk" style="width: 61%" />
    </div>
    <div v-else-if="!inputColumns.length" class="hint">
      <Info :size="13" /> {{ $t('paramForm.noUpstreamColumns') }}
    </div>

    <div v-for="f in spec" :key="f.key" class="field">
      <label>{{ $t(f.label) }}</label>

      <!-- multi-select colonne (+ placeholder del foreach, se presenti) -->
      <div v-if="f.control === 'columns'" class="checks">
        <label v-for="name in optionsFor(f)" :key="name" class="chk">
          <input type="checkbox" :checked="isChecked(f.key, name)" @change="toggleColumn(f.key, name)" />
          {{ name }}
        </label>
        <label v-for="ph in phOptions" :key="ph" class="chk ph">
          <input type="checkbox" :checked="isChecked(f.key, ph)" @change="toggleColumn(f.key, ph)" />
          {{ ph }}
        </label>
        <template v-if="!optionsFor(f).length && !phOptions.length">
          <span v-if="columnsLoading" class="sk" style="width: 130px" aria-busy="true" />
          <span v-else class="muted">{{ emptyMessage(f) }}</span>
        </template>
      </div>

      <!-- select singola colonna (+ placeholder del foreach, se presenti) -->
      <Select
        v-else-if="f.control === 'column'"
        :model-value="state[f.key]"
        :options="columnSelectOptions(f, true)"
        placeholder="—"
        @update:model-value="(v: any) => { state[f.key] = v; emitUpdate() }"
      />

      <!-- select enumerati (etichette leggibili, valore = id backend) -->
      <Select
        v-else-if="f.control === 'operator'"
        :model-value="state.operator"
        :options="OPERATOR_OPTIONS"
        @update:model-value="(v: any) => { state.operator = v; emitUpdate() }"
      />
      <Select
        v-else-if="f.control === 'how'"
        :model-value="state[f.key]"
        :options="HOW_OPTIONS"
        @update:model-value="(v: any) => { state[f.key] = v; emitUpdate() }"
      />
      <Select
        v-else-if="f.control === 'func'"
        :model-value="state[f.key] ?? 'sum'"
        :options="FUNC_OPTIONS"
        @update:model-value="(v: any) => { state[f.key] = v; emitUpdate() }"
      />
      <Select
        v-else-if="f.control === 'strategy'"
        :model-value="state[f.key] ?? 'relaxed'"
        :options="STRATEGY_OPTIONS"
        @update:model-value="(v: any) => { state[f.key] = v; emitUpdate() }"
      />

      <!-- valore filtro -->
      <template v-else-if="f.control === 'value'">
        <span v-if="NO_VALUE_OPERATORS.has(state.operator)" class="muted">{{ $t('paramForm.noValueRequired') }}</span>

        <!-- in / not_in → multiselect con ricerca sui valori distinti reali -->
        <ValuePicker
          v-else-if="isInOperator"
          :model-value="state.__inValues ?? []"
          :options="distinctValues"
          :loading="distinctLoading"
          @update:model-value="onInValues"
        />

        <!-- colonna temporale + between → doppio calendario (da → a, inclusivi) -->
        <div v-else-if="temporalInputType && !state.__phmode && state.operator === 'between'" class="daterange">
          <input :type="temporalInputType" v-model="state.__lo" @change="emitUpdate" />
          <span class="muted">→</span>
          <input :type="temporalInputType" v-model="state.__hi" @change="emitUpdate" />
          <button v-if="phOptions.length" class="phbtn" :title="$t('paramForm.usePlaceholder')" @click="state.__phmode = true">{&#8288;{ }&#8288;}</button>
        </div>

        <!-- colonna temporale + confronto singolo → calendario -->
        <div v-else-if="temporalInputType && !state.__phmode && SINGLE_DATE_OPERATORS.has(state.operator)" class="daterange">
          <input :type="temporalInputType" v-model="state.__value" @change="emitUpdate" />
          <button v-if="phOptions.length" class="phbtn" :title="$t('paramForm.usePlaceholder')" @click="state.__phmode = true">{&#8288;{ }&#8288;}</button>
        </div>

        <!-- tutti gli altri casi → testo libero (numeri, stringhe, liste, placeholder) -->
        <div v-else class="daterange">
          <input
            type="text"
            v-model="state.__value"
            :placeholder="phOptions.length ? $t('paramForm.valuePlaceholderPh', { ph: `{{${placeholders![0]}}}` }) : $t('paramForm.valuePlaceholderPlain')"
            @change="emitUpdate"
          />
          <button
            v-if="temporalInputType && state.__phmode"
            class="phbtn"
            :title="$t('paramForm.backToCalendar')"
            @click="state.__phmode = false"
          ><Calendar :size="13" /></button>
        </div>
      </template>

      <!-- JSON libero (es. iterazioni statiche del foreach) -->
      <textarea
        v-else-if="f.control === 'json'"
        v-model="state[`__json_${f.key}`]"
        rows="3"
        :placeholder="$t('paramForm.jsonPlaceholderExample')"
        @change="emitUpdate"
      />

      <!-- Execute SQL: query intera; anteprima MANUALE (mai a ogni tasto) -->
      <div v-else-if="f.control === 'sqltext'" class="sqlblock">
        <textarea
          v-model="state[f.key]"
          rows="9"
          spellcheck="false"
          class="sqlarea"
          :placeholder="$t('paramForm.sqlQueryPlaceholder')"
          @change="emitUpdate"
          @keydown.meta.enter.prevent="emitPreviewNow"
          @keydown.ctrl.enter.prevent="emitPreviewNow"
        />
        <div class="sqlactions">
          <button class="previewbtn" type="button" @click="emitPreviewNow">
            <Play :size="12" /> {{ $t('paramForm.previewButton') }}
          </button>
          <span class="sqlhint">
            {{ $t('paramForm.sqlInputTable') }} <code>self</code> {{ $t('paramForm.sqlInputAlias') }} <code>input</code>{{ $t('paramForm.sqlDialect') }}
            <br />{{ $t('paramForm.sqlManualPreview') }} <kbd>Ctrl/Cmd</kbd>+<kbd>Enter</kbd>.
          </span>
        </div>
      </div>

      <!-- scalari semplici -->
      <input v-else-if="f.control === 'number'" type="number" v-model="state[f.key]" @change="emitUpdate" />
      <label v-else-if="f.control === 'boolean'" class="chk">
        <input type="checkbox" v-model="state[f.key]" @change="emitUpdate" /> {{ $t('paramForm.yesLabel') }}
      </label>
      <input v-else-if="f.control === 'text'" type="text" v-model="state[f.key]" @change="emitUpdate" />

      <!-- cast: righe colonna → dtype -->
      <div v-else-if="f.control === 'castlist'" class="rows">
        <div v-for="(r, i) in castRows" :key="i" class="row">
          <Select
            :model-value="r.column"
            :options="columnSelectOptions()"
            :placeholder="$t('paramForm.columnPlaceholder')"
            @update:model-value="(v: any) => { r.column = v; emitUpdate() }"
          />
          <Select
            :model-value="r.dtype"
            :options="DTYPE_OPTIONS"
            class="funcsel"
            @update:model-value="(v: any) => { r.dtype = v; emitUpdate() }"
          />
          <button class="x" @click="removeRow(castRows, i)"><X :size="13" /></button>
        </div>
        <button @click="addCast">{{ $t('paramForm.addRow') }}</button>
      </div>

      <!-- rename: righe da → a -->
      <div v-else-if="f.control === 'renamelist'" class="rows">
        <div v-for="(r, i) in renameRows" :key="i" class="row">
          <Select
            :model-value="r.from"
            :options="columnSelectOptions()"
            :placeholder="$t('paramForm.columnPlaceholder')"
            @update:model-value="(v: any) => { r.from = v; emitUpdate() }"
          />
          <input type="text" v-model="r.to" :placeholder="$t('paramForm.newNamePlaceholder')" @change="emitUpdate" />
          <button class="x" @click="removeRow(renameRows, i)"><X :size="13" /></button>
        </div>
        <button @click="addRename">{{ $t('paramForm.addRow') }}</button>
      </div>

      <!-- fill_null: righe colonna → valore -->
      <div v-else-if="f.control === 'filllist'" class="rows">
        <div v-for="(r, i) in fillRows" :key="i" class="row">
          <Select
            :model-value="r.column"
            :options="columnSelectOptions()"
            :placeholder="$t('paramForm.columnPlaceholder')"
            @update:model-value="(v: any) => { r.column = v; emitUpdate() }"
          />
          <input type="text" v-model="r.value" :placeholder="$t('paramForm.valuePlaceholder')" @change="emitUpdate" />
          <button class="x" @click="removeRow(fillRows, i)"><X :size="13" /></button>
        </div>
        <button @click="addFill">{{ $t('paramForm.addRow') }}</button>
      </div>

      <!-- compute: righe nome → espressione SQL -->
      <div v-else-if="f.control === 'exprlist'" class="rows">
        <div v-for="(r, i) in exprRows" :key="i" class="exprrow">
          <div class="exprhead">
            <input type="text" v-model="r.name" :placeholder="$t('paramForm.columnNamePlaceholder')" @change="emitUpdate" />
            <button class="x" @click="removeRow(exprRows, i)"><X :size="13" /></button>
          </div>
          <textarea
            v-model="r.expr"
            rows="2"
            spellcheck="false"
            :placeholder="$t('paramForm.exprPlaceholder')"
            @change="emitUpdate"
          />
        </div>
        <button @click="addExpr">{{ $t('paramForm.addRow') }}</button>
        <p class="sqlhint">
          {{ $t('paramForm.exprHint') }}
        </p>
      </div>

      <!-- group_by: righe colonna/funzione/alias -->
      <div v-else-if="f.control === 'agglist'" class="rows">
        <div v-for="(r, i) in aggRows" :key="i" class="row">
          <Select
            :model-value="r.column"
            :options="columnSelectOptions()"
            :placeholder="$t('paramForm.columnPlaceholder')"
            @update:model-value="(v: any) => { r.column = v; emitUpdate() }"
          />
          <Select
            :model-value="r.func"
            :options="FUNC_OPTIONS"
            class="funcsel"
            @update:model-value="(v: any) => { r.func = v; emitUpdate() }"
          />
          <input type="text" v-model="r.alias" :placeholder="$t('paramForm.aliasPlaceholder')" @change="emitUpdate" />
          <button class="x" @click="removeRow(aggRows, i)"><X :size="13" /></button>
        </div>
        <button @click="addAgg">{{ $t('paramForm.addRow') }}</button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.paramform { display: flex; flex-direction: column; gap: 10px; }
.skcols { display: flex; flex-direction: column; gap: 8px; padding: 2px 0; }
.field { display: flex; flex-direction: column; gap: 4px; }
label { font-size: 12px; color: var(--muted); }
.hint { font-size: 12px; color: var(--muted); display: flex; align-items: center; gap: 5px; }
.checks { display: flex; flex-direction: column; gap: 2px; max-height: 180px; overflow-y: auto; }
.chk { display: flex; align-items: center; gap: 6px; color: var(--text); font-size: 13px; }
.chk input { width: auto; }
.daterange { display: flex; align-items: center; gap: 6px; }
.phbtn { padding: 4px 8px; font-size: 11px; white-space: nowrap; flex-shrink: 0; }
.chk.ph { color: #f472b6; font-family: ui-monospace, monospace; font-size: 12px; }
.rows { display: flex; flex-direction: column; gap: 6px; }
.row { display: flex; gap: 4px; align-items: center; }
.row .x { padding: 2px 8px; }
.exprrow { display: flex; flex-direction: column; gap: 4px; }
.exprrow textarea { font-family: ui-monospace, monospace; font-size: 12px; resize: vertical; }
.exprhead { display: flex; gap: 4px; align-items: center; }
.exprhead input { flex: 1; }
.exprhead .x { padding: 2px 8px; }
.sqlhint { font-size: 11.5px; color: var(--muted); margin: 2px 0 0; }
.sqlblock { display: flex; flex-direction: column; gap: 6px; }
.sqlarea { font-family: ui-monospace, monospace; font-size: 12.5px; line-height: 1.5; resize: vertical; tab-size: 2; }
.sqlactions { display: flex; align-items: flex-start; gap: 10px; }
.previewbtn { display: inline-flex; align-items: center; gap: 5px; white-space: nowrap; flex-shrink: 0; }
.sqlblock kbd { font-family: ui-monospace, monospace; font-size: 10.5px; background: var(--panel-2); border: 1px solid var(--border); border-radius: 3px; padding: 0 4px; }
.sqlblock code { font-family: ui-monospace, monospace; font-size: 11.5px; }
</style>
