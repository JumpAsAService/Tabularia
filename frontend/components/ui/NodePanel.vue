<script setup lang="ts">
import type { Node } from '@vue-flow/core'
import {
  FileText, Settings, Link2, Trash2, Download, FileSpreadsheet, Repeat, Database,
  HardDriveDownload,
} from 'lucide-vue-next'
import type { ColumnInfo } from '~/composables/useApi'
import type { DatasourceInfo } from '~/composables/useDatasources'
import { defaultParams } from '~/composables/useFlowModel'
import { opMeta } from '~/composables/useOpIcons'

const props = defineProps<{
  node: Node | null
  operations: string[]
  inputColumns: ColumnInfo[]
  rightColumns: ColumnInfo[]
  columnsLoading?: boolean
  placeholders?: string[]
  fetchDistinct?: (column: string) => Promise<any[]>
  datasources?: DatasourceInfo[]
  projects?: { id: number; name: string }[]
  connections?: { id: number; name: string; db_type: string; database?: string }[]
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

// toggle di una colonna di partizione dell'output S3
function togglePartition(name: string) {
  const cur: string[] = [...(props.node?.data?.partitionBy ?? [])]
  const i = cur.indexOf(name)
  if (i >= 0) cur.splice(i, 1)
  else cur.push(name)
  emit('update', { partitionBy: cur })
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

    <!-- nodo Output: dove finisce il risultato della catena -->
    <template v-else-if="node.type === 'output'">
      <div class="row">
        <h3><HardDriveDownload :size="15" /> Output</h3>
        <button class="del" title="Elimina nodo (Canc)" @click="emit('delete')"><Trash2 :size="14" /></button>
      </div>

      <label>Destinazione</label>
      <Select
        :model-value="node.data.destType ?? 'datasource'"
        :options="[
          { value: 'datasource', label: 'Datasource Tabularia' },
          { value: 'database', label: 'Tabella di database' },
          { value: 's3', label: 'File su S3 / object storage' },
        ]"
        @update:model-value="(v: any) => emit('update', { destType: v })"
      />

      <template v-if="(node.data.destType ?? 'datasource') === 'datasource'">
        <label>Nome della datasource</label>
        <input
          :value="node.data.name ?? ''"
          type="text"
          placeholder="es. vendite_pulite_2024"
          @input="emit('update', { name: ($event.target as HTMLInputElement).value })"
        />
        <label>Cartella</label>
        <Select
          :model-value="node.data.projectId ?? null"
          :options="(projects ?? []).map((p) => ({ value: p.id, label: p.name }))"
          placeholder="cartella…"
          @update:model-value="(v: any) => emit('update', { projectId: v })"
        />
        <label>Descrizione (opzionale)</label>
        <input
          :value="node.data.description ?? ''"
          type="text"
          placeholder="cosa contiene, per chi"
          @input="emit('update', { description: ($event.target as HTMLInputElement).value })"
        />
      </template>

      <!-- destinazione S3: connessione object storage + chiave + formato + partizioni -->
      <template v-else-if="(node.data.destType ?? 'datasource') === 's3'">
        <label>Connessione S3</label>
        <Select
          :model-value="node.data.connectionId ?? null"
          :options="(connections ?? []).filter((c) => c.db_type === 's3').map((c) => ({
            value: c.id,
            label: c.database ? `${c.name} (bucket ${c.database})` : c.name,
          }))"
          placeholder="connessione…"
          @update:model-value="(v: any) => emit('update', { connectionId: v })"
        />
        <label>Bucket <span class="muted">(vuoto = quello della connessione)</span></label>
        <input
          :value="node.data.s3Bucket ?? ''"
          type="text"
          placeholder="es. exports-cliente"
          @input="emit('update', { s3Bucket: ($event.target as HTMLInputElement).value })"
        />
        <label>Chiave / percorso</label>
        <input
          :value="node.data.s3Key ?? ''"
          type="text"
          placeholder="es. exports/vendite.parquet — o un prefisso se partizioni"
          @input="emit('update', { s3Key: ($event.target as HTMLInputElement).value })"
        />
        <label>Formato</label>
        <Select
          :model-value="node.data.s3Format ?? 'parquet'"
          :options="[
            { value: 'parquet', label: 'Parquet' },
            { value: 'csv', label: 'CSV' },
          ]"
          @update:model-value="(v: any) => emit('update', { s3Format: v })"
        />
        <label>Partiziona per (hive: colonna=valore/…)</label>
        <div v-if="inputColumns.length" class="partchecks">
          <label v-for="c in inputColumns" :key="c.name" class="chk">
            <input
              type="checkbox"
              :checked="(node.data.partitionBy ?? []).includes(c.name)"
              @change="togglePartition(c.name)"
            />
            {{ c.name }}
          </label>
        </div>
        <p v-else class="muted outhint">Collega l'output a una catena con dati per scegliere le colonne.</p>
        <p class="muted outhint">
          Senza partizioni scrive un singolo oggetto alla chiave indicata; con le
          partizioni scrive un dataset <code>colonna=valore/…</code> sotto il prefisso
          (le run successive sovrascrivono gli stessi percorsi).
        </p>
      </template>

      <template v-else>
        <label>Connessione</label>
        <Select
          :model-value="node.data.connectionId ?? null"
          :options="(connections ?? []).filter((c) => c.db_type !== 's3').map((c) => ({
            value: c.id,
            label: c.database ? `${c.name} (${c.db_type} · ${c.database})` : `${c.name} (${c.db_type})`,
          }))"
          placeholder="connessione…"
          @update:model-value="(v: any) => emit('update', { connectionId: v })"
        />
        <label>Tabella di destinazione (anche schema.tabella)</label>
        <input
          :value="node.data.table ?? ''"
          type="text"
          placeholder="es. analytics.vendite_pulite"
          @input="emit('update', { table: ($event.target as HTMLInputElement).value })"
        />
        <label>Modalità di scrittura</label>
        <Select
          :model-value="node.data.mode ?? 'append'"
          :options="[
            { value: 'append', label: 'Accoda (INSERT)' },
            { value: 'replace', label: 'Sostituisci (TRUNCATE + INSERT)' },
          ]"
          @update:model-value="(v: any) => emit('update', { mode: v })"
        />
        <label>SQL post-insert (opzionale, statement separati da ;)</label>
        <textarea
          :value="node.data.postSql ?? ''"
          rows="3"
          placeholder="es. ANALYZE analytics.vendite_pulite"
          @input="emit('update', { postSql: ($event.target as HTMLTextAreaElement).value })"
        />
        <p class="muted outhint">
          La tabella viene creata se non esiste, con i tipi dello schema dell'output.
        </p>
      </template>
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
        :columns-loading="columnsLoading"
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

      <div v-if="node.data.opType === 'union'" class="joinhelp">
        <strong class="joinhead"><Link2 :size="13" /> Come collegare la union</strong>
        <ol>
          <li>input a <span class="tag">sinistra</span> = lo step precedente della catena</li>
          <li>input in <span class="tag right">alto</span> = il <strong>ramo/sorgente</strong> le cui righe vengono accodate sotto</li>
          <li>per accodare più sorgenti, metti più nodi union in catena</li>
        </ol>
      </div>

      <ParamForm
        :node-id="node.id"
        :op-type="node.data.opType"
        :params="node.data.params || {}"
        :input-columns="inputColumns"
        :right-columns="rightColumns"
        :columns-loading="columnsLoading"
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
.outhint { font-size: 12px; margin: 2px 0 0; }
.outhint code { font-size: 11px; }
.partchecks {
  display: flex;
  flex-direction: column;
  gap: 2px;
  max-height: 150px;
  overflow-y: auto;
}
.partchecks .chk { display: flex; align-items: center; gap: 6px; font-size: 13px; }
.partchecks .chk input { width: auto; }
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
