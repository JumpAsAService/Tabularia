<script setup lang="ts">
import type { Node } from '@vue-flow/core'
import { FileText, Settings, Link2, Trash2, Download, FileSpreadsheet, Repeat, Database } from 'lucide-vue-next'
import type { ColumnInfo } from '~/composables/useApi'
import type { DatasourceInfo } from '~/composables/useDatasources'
import { defaultParams } from '~/composables/useFlowModel'
import { opMeta } from '~/composables/useOpIcons'

const props = defineProps<{
  node: Node | null
  operations: string[]
  inputColumns: ColumnInfo[]
  rightColumns: ColumnInfo[]
  placeholders?: string[]
  fetchDistinct?: (column: string) => Promise<any[]>
  datasources?: DatasourceInfo[]
}>()
const emit = defineEmits<{
  (e: 'update', patch: Record<string, any>): void
  (e: 'delete'): void
  (e: 'export', format: 'csv' | 'xlsx'): void
}>()

const isSource = () => props.node?.type === 'source'

function changeType(opType: string) {
  // cambiando operazione, riparte da parametri di default puliti
  emit('update', { opType, params: defaultParams(opType) })
}

function onParams(params: Record<string, any>) {
  emit('update', { params })
}

// il nodo sorgente può caricare una datasource del catalogo al posto del file
function pickDatasource(id: number | null) {
  const ds = (props.datasources ?? []).find((d) => d.id === id)
  if (!ds) return
  emit('update', {
    datasourceId: ds.id,
    datasetId: null,
    bucket: ds.bucket,
    parquetKey: ds.key,
    filename: ds.name,
    rows: ds.rows,
    columns: ds.columns,
  })
}
</script>

<template>
  <div class="nodepanel">
    <div v-if="!node" class="muted">Nessun nodo selezionato.</div>

    <template v-else-if="isSource()">
      <div class="row">
        <h3><FileText :size="15" /> Sorgente</h3>
        <button class="del" title="Elimina sorgente (Canc)" @click="emit('delete')"><Trash2 :size="14" /></button>
      </div>
      <template v-if="node.data.parquetKey">
        <p><strong>{{ node.data.filename }}</strong></p>
        <p class="muted">{{ node.data.rows }} righe · {{ (node.data.columns || []).length }} colonne</p>
        <p class="muted mono">{{ node.data.parquetKey }}</p>
        <div class="exportbar">
          <label>Scarica i dati di questo nodo</label>
          <div class="exportbtns">
            <button @click="emit('export', 'csv')"><Download :size="14" /> CSV</button>
            <button @click="emit('export', 'xlsx')"><FileSpreadsheet :size="14" /> Excel</button>
          </div>
        </div>
      </template>
      <p v-else class="muted">Carica un file dalla toolbar per popolare la sorgente.</p>

      <div class="dspick">
        <label><Database :size="12" /> oppure usa una datasource del catalogo</label>
        <Select
          :model-value="node.data.datasourceId ?? null"
          :options="(datasources ?? [])
            .filter((d) => d.key)
            .map((d) => ({ value: d.id, label: d.rows != null ? `${d.name} (${d.rows} righe)` : d.name }))"
          placeholder="scegli una datasource…"
          @update:model-value="pickDatasource"
        />
      </div>
    </template>

    <!-- container foreach -->
    <template v-else-if="node.type === 'foreach'">
      <div class="row">
        <h3><Repeat :size="15" /> Ciclo foreach</h3>
        <button class="del" title="Elimina container e corpo (Canc)" @click="emit('delete')"><Trash2 :size="14" /></button>
      </div>

      <div class="joinhelp">
        <strong class="joinhead"><Repeat :size="13" /> Come funziona</strong>
        <ol>
          <li>input a <span class="tag">sinistra</span> = i dati da trasformare</li>
          <li>input in <span class="tag right">alto</span> = il <strong>driver</strong>: una riga = un'iterazione</li>
          <li><strong>trascina le operazioni dentro</strong> il riquadro: sono il corpo del ciclo</li>
          <li>nei valori usa i placeholder <code v-pre>{{colonna}}</code> del driver</li>
          <li>l'output è l'<strong>append</strong> dei risultati di tutte le iterazioni</li>
        </ol>
        <p v-if="rightColumns.length" class="phlist">
          Placeholder disponibili:
          <code v-for="c in rightColumns" :key="c.name" v-text="`{{${c.name}}}`" />
        </p>
        <p v-else class="muted phlist">Nessun driver collegato: definisci le iterazioni statiche qui sotto.</p>
      </div>

      <ParamForm
        :node-id="node.id"
        op-type="foreach"
        :params="node.data.params || {}"
        :input-columns="inputColumns"
        :right-columns="rightColumns"
        @update="onParams"
      />

      <div class="exportbar">
        <label>Scarica l'output del ciclo</label>
        <div class="exportbtns">
          <button @click="emit('export', 'csv')"><Download :size="14" /> CSV</button>
          <button @click="emit('export', 'xlsx')"><FileSpreadsheet :size="14" /> Excel</button>
        </div>
      </div>
    </template>

    <template v-else>
      <div class="row">
        <h3><Settings :size="15" /> Operazione</h3>
        <button class="del" title="Elimina nodo (Canc)" @click="emit('delete')"><Trash2 :size="14" /></button>
      </div>

      <label>Tipo</label>
      <Select
        :model-value="node.data.opType"
        :options="operations.filter((o) => o !== 'foreach').map((op) => ({ value: op, label: opMeta(op).label || op }))"
        @update:model-value="changeType"
      />

      <div v-if="node.data.opType === 'join'" class="joinhelp">
        <strong class="joinhead"><Link2 :size="13" /> Come collegare il join</strong>
        <ol>
          <li>input a <span class="tag">sinistra</span> = lo step precedente della catena</li>
          <li>input in <span class="tag right">alto</span> = trascina qui il <strong>ramo/sorgente</strong> (con dati) da unire</li>
          <li>poi scegli il tipo di join e le colonne chiave qui sotto</li>
        </ol>
      </div>

      <ParamForm
        :node-id="node.id"
        :op-type="node.data.opType"
        :params="node.data.params || {}"
        :input-columns="inputColumns"
        :right-columns="rightColumns"
        :placeholders="placeholders"
        :fetch-distinct="fetchDistinct"
        @update="onParams"
      />

      <div class="exportbar">
        <label>Scarica i dati di questo nodo</label>
        <div class="exportbtns">
          <button @click="emit('export', 'csv')"><Download :size="14" /> CSV</button>
          <button @click="emit('export', 'xlsx')"><FileSpreadsheet :size="14" /> Excel</button>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.nodepanel { padding: 12px; overflow-y: auto; height: 100%; display: flex; flex-direction: column; gap: 8px; }
h3 { margin: 0; display: inline-flex; align-items: center; gap: 6px; }
.joinhead { display: inline-flex; align-items: center; gap: 5px; }
label { font-size: 12px; color: var(--muted); }
.row { display: flex; align-items: center; justify-content: space-between; }
.del { padding: 2px 8px; }
.mono { font-family: ui-monospace, monospace; font-size: 11px; word-break: break-all; }
.exportbar { margin-top: 10px; padding-top: 10px; border-top: 1px solid var(--border-soft); }
.dspick { margin-top: 10px; padding-top: 10px; border-top: 1px solid var(--border-soft); display: flex; flex-direction: column; gap: 5px; }
.dspick > label { display: inline-flex; align-items: center; gap: 5px; }
.phlist { margin: 8px 0 0; font-size: 12px; }
.phlist code {
  background: var(--panel-2);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 0 5px;
  margin-right: 4px;
  font-size: 11px;
}
.exportbtns { display: flex; gap: 6px; margin-top: 6px; }
.exportbtns button { flex: 1; }
.joinhelp {
  font-size: 12px;
  background: rgba(79, 140, 255, 0.08);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 8px 10px;
}
.joinhelp ol { margin: 6px 0 0; padding-left: 18px; }
.joinhelp li { margin: 3px 0; color: var(--muted); }
.tag {
  background: var(--accent); color: #fff; border-radius: 4px; padding: 0 5px; font-size: 11px;
}
.tag.right { background: var(--accent-2); color: #0f1117; }
</style>
