<script setup lang="ts">
import { computed, inject } from 'vue'
import { useI18n } from 'vue-i18n'
import type { Node } from '@vue-flow/core'
import { FileText, Settings, Link2, Trash2, Download, FileSpreadsheet, Repeat, Database, HardDriveDownload, RefreshCw, PlayCircle, StickyNote, FlaskConical, Filter, Plus, X } from 'lucide-vue-next'
import type { ColumnInfo } from '~/composables/useApi'
import type { DatasourceInfo } from '~/composables/useDatasources'
import { defaultParams, isCompleteSourceFilter, getDefaultSampleRows, type SourceFilter } from '~/composables/useFlowModel'
import { opMeta } from '~/composables/useOpIcons'
import { useNodeStats } from '~/composables/useNodeStats'

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
  flows?: { id: number; name: string }[]
  currentFlowId?: number | null
}>()
const { t } = useI18n()
const nodeStats = useNodeStats(() => props.node?.id ?? '', inject, t)
const emit = defineEmits<{
  (e: 'update', patch: Record<string, any>): void
  (e: 'delete'): void
  (e: 'export', format: 'csv' | 'xlsx'): void
  (e: 'preview'): void // anteprima a comando (nodo SQL)
  (e: 'email-test'): void // prova a vuoto: manda l'allegato a sé stessi
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

// chiavi di ORDER BY dell'output datasource: il parquet esce ordinato e la copia
// materializzata del viewer le eredita → pruning a valle. L'ordine di selezione
// è l'ordine delle chiavi (prima cliccata = prima nell'ORDER BY).
function toggleSortKey(name: string) {
  const cur: string[] = [...(props.node?.data?.sortKeys ?? [])]
  const i = cur.indexOf(name)
  if (i >= 0) cur.splice(i, 1)
  else cur.push(name)
  emit('update', { sortKeys: cur })
}

// Destinatari email: il flusso li SALVA come elenco, perché è così che li
// rilegge il resolver lato server per i run schedulati. Nel form restano una
// riga separata da virgole, quindi si converte solo all'uscita dal campo
// (@change e non @input: spezzare a ogni tasto cancellerebbe la virgola appena
// digitata, che qui è il separatore).
function splitAddresses(raw: string): string[] {
  return raw
    .split(/[,;\s]+/)
    .map((s) => s.trim())
    .filter(Boolean)
}

// la prova a vuoto ignora i destinatari (li impone il server), ma senza
// connessione e senza oggetto non c'è niente da spedire
function emailTestReady(): boolean {
  const d = props.node?.data ?? {}
  return d.connectionId != null && !!(d.emailSubject ?? '').trim()
}

// nodo Refresh: sceglie una datasource database da aggiornare prima del run
function pickRefreshDatasource(id: number | null) {
  const ds = (props.datasources ?? []).find((d) => d.id === id)
  emit('update', { datasourceId: id, dsName: ds?.name ?? '' })
}
// nodo Esegui-flusso: sceglie un altro flusso da eseguire
function pickRunFlow(id: number | null) {
  const f = (props.flows ?? []).find((x) => x.id === id)
  emit('update', { flowId: id, flowName: f?.name ?? '' })
}

// filtri A MONTE del nodo sorgente (vedi sourceFilterOperations in useFlowModel):
// condizioni in AND applicate prima di tutto, in sviluppo E in produzione.
// Ogni riga ha un id stabile (chiave del form: rimuovere una riga non deve
// far "scivolare" i valori delle altre).
const sourceFilters = (): SourceFilter[] => (props.node?.data?.filters ?? []) as SourceFilter[]
function addSourceFilter() {
  const id = typeof crypto !== 'undefined' && 'randomUUID' in crypto ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`
  emit('update', { filters: [...sourceFilters(), { id, operator: 'eq' }] })
}
function updateSourceFilter(id: string, params: Record<string, any>) {
  emit('update', {
    filters: sourceFilters().map((f) =>
      f.id === id ? { id, column: params.column, operator: params.operator, value: params.value } : f,
    ),
  })
}
function removeSourceFilter(id: string) {
  const rest = sourceFilters().filter((f) => f.id !== id)
  emit('update', { filters: rest.length ? rest : null })
}

// campione di sviluppo del nodo sorgente (vedi devSampleOperation in useFlowModel).
// Con il campione automatico acceso (gateway) `sample: null` vale «automatico»
// e lo spegnimento va salvato esplicito ({ mode: 'off' }); senza, null è «off»
// come sempre e le due voci coincidono.
const autoSampleRows = computed(() => getDefaultSampleRows())
const sampleMode = computed(() => {
  const m = props.node?.data?.sample?.mode
  if (!m) return autoSampleRows.value > 0 ? 'auto' : 'off'
  return m
})
const sampleOptions = computed(() => [
  ...(autoSampleRows.value > 0
    ? [{ value: 'auto', label: t('nodePanel.sampleAuto', { n: autoSampleRows.value.toLocaleString() }) }]
    : []),
  { value: 'off', label: t('nodePanel.sampleOff') },
  { value: 'first', label: t('nodePanel.sampleFirst') },
  { value: 'random', label: t('nodePanel.sampleRandom') },
])
function setSampleMode(mode: string) {
  if (mode === 'auto') {
    emit('update', { sample: null })
    return
  }
  if (mode === 'off') {
    emit('update', { sample: autoSampleRows.value > 0 ? { mode: 'off' } : null })
    return
  }
  const cur = props.node?.data?.sample ?? {}
  emit('update', {
    sample: mode === 'first'
      ? { mode: 'first', rows: cur.rows ?? 100000 }
      : { mode: 'random', percent: cur.percent ?? 10 },
  })
}
function setSampleValue(key: 'rows' | 'percent', raw: string) {
  const cur = props.node?.data?.sample
  if (!cur) return
  const v = Number(raw)
  if (!(v > 0)) return
  emit('update', { sample: { ...cur, [key]: key === 'rows' ? Math.floor(v) : Math.min(v, 99) } })
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
    <div v-if="!node" class="muted">{{ $t('nodePanel.noNodeSelected') }}</div>

    <template v-else-if="node.type === 'comment'">
      <div class="row">
        <h3><StickyNote :size="15" /> {{ $t('nodePanel.noteTitle') }}</h3>
        <button class="del" :title="$t('nodePanel.deleteNoteTitle')" @click="emit('delete')"><Trash2 :size="14" /></button>
      </div>
      <label>{{ $t('nodePanel.noteTextLabel') }}</label>
      <textarea
        class="commenttext"
        :value="node.data.text || ''"
        rows="6"
        :placeholder="$t('nodePanel.noteTextPlaceholder')"
        @input="emit('update', { text: ($event.target as HTMLTextAreaElement).value })"
      />
      <p class="muted outhint">{{ $t('nodePanel.noteHint') }}</p>
    </template>

    <template v-else-if="isSource()">
      <div class="row">
        <h3><FileText :size="15" /> {{ $t('nodePanel.sourceTitle') }}</h3>
        <button class="del" :title="$t('nodePanel.deleteSourceTitle')" @click="emit('delete')"><Trash2 :size="14" /></button>
      </div>
      <template v-if="node.data.parquetKey">
        <p><strong>{{ node.data.filename }}</strong></p>
        <p class="muted">{{ $t('nodePanel.rowsColsSummary', { rows: node.data.rows, cols: (node.data.columns || []).length }) }}</p>
        <p class="muted mono">{{ node.data.parquetKey }}</p>
        <div class="exportbar">
          <label>{{ $t('nodePanel.downloadNodeData') }}</label>
          <div class="exportbtns">
            <button @click="emit('export', 'csv')"><Download :size="14" /> CSV</button>
            <button @click="emit('export', 'xlsx')"><FileSpreadsheet :size="14" /> Excel</button>
          </div>
        </div>
      </template>
      <p v-else class="muted">{{ $t('nodePanel.sourceEmptyHint') }}</p>

      <!-- filtri A MONTE: condizioni applicate subito dopo la lettura della
           sorgente, SEMPRE (sviluppo e produzione), prima del campione -->
      <div v-if="node.data.parquetKey" class="filtersbox">
        <label><Filter :size="12" /> {{ $t('nodePanel.sourceFiltersTitle') }}</label>
        <div v-for="(f, i) in sourceFilters()" :key="f.id" class="filterrow" :class="{ incomplete: !isCompleteSourceFilter(f) }">
          <div class="filterhead">
            <span class="muted">{{ i === 0 ? $t('nodePanel.sourceFilterWhere') : $t('nodePanel.sourceFilterAnd') }}</span>
            <span v-if="!isCompleteSourceFilter(f)" class="muted warn">{{ $t('nodePanel.sourceFilterIncomplete') }}</span>
            <button class="x" :title="$t('nodePanel.sourceFilterRemove')" :aria-label="$t('nodePanel.sourceFilterRemove')" @click="removeSourceFilter(f.id)"><X :size="12" /></button>
          </div>
          <ParamForm
            :node-id="`${node.id}:filter:${f.id}`"
            op-type="filter"
            :params="f"
            :input-columns="(node.data.columns as ColumnInfo[]) ?? []"
            :fetch-distinct="fetchDistinct"
            @update="(p: Record<string, any>) => updateSourceFilter(f.id, p)"
          />
        </div>
        <button class="addfilter" @click="addSourceFilter"><Plus :size="13" /> {{ $t('nodePanel.sourceFilterAdd') }}</button>
        <p class="muted outhint">{{ $t('nodePanel.sourceFiltersHint') }}</p>
      </div>

      <!-- campione di SVILUPPO: solo preview e run dall'editor; in produzione
           (scheduler, esegui in produzione) la sorgente è letta per intero -->
      <div v-if="node.data.parquetKey" class="samplebox">
        <label><FlaskConical :size="12" /> {{ $t('nodePanel.sampleTitle') }}</label>
        <div class="samplerow">
          <Select
            :model-value="sampleMode"
            :options="sampleOptions"
            @update:model-value="setSampleMode"
          />
          <input
            v-if="node.data.sample?.mode === 'first'"
            type="number"
            min="1"
            step="1000"
            class="samplenum"
            :value="node.data.sample?.rows ?? 100000"
            :placeholder="$t('nodePanel.sampleRowsLabel')"
            @change="setSampleValue('rows', ($event.target as HTMLInputElement).value)"
          />
          <span v-if="node.data.sample?.mode === 'first'" class="muted">{{ $t('nodePanel.sampleRowsLabel') }}</span>
          <input
            v-if="node.data.sample?.mode === 'random'"
            type="number"
            min="0.01"
            max="99"
            step="1"
            class="samplenum"
            :value="node.data.sample?.percent ?? 10"
            @change="setSampleValue('percent', ($event.target as HTMLInputElement).value)"
          />
          <span v-if="node.data.sample?.mode === 'random'" class="muted">%</span>
        </div>
        <p class="muted outhint">{{ $t('nodePanel.sampleHint') }}</p>
        <p v-if="autoSampleRows > 0" class="muted outhint">{{ $t('nodePanel.sampleAutoHint', { n: autoSampleRows.toLocaleString() }) }}</p>
      </div>

      <div class="dspick">
        <label><Database :size="12" /> {{ $t('nodePanel.orUseCatalogDatasource') }}</label>
        <Select
          searchable
          :model-value="node.data.datasourceId ?? null"
          :options="(datasources ?? [])
            .filter((d) => d.key)
            .map((d) => ({ value: d.id, label: d.rows != null ? `${d.name} (${$t('nodePanel.rowsCountLabel', { rows: d.rows })})` : d.name }))"
          :placeholder="$t('nodePanel.chooseDatasourcePlaceholder')"
          @update:model-value="pickDatasource"
        />
      </div>
    </template>

    <!-- nodo Output: dove finisce il risultato della catena -->
    <template v-else-if="node.type === 'output'">
      <div class="row">
        <h3><HardDriveDownload :size="15" /> {{ $t('nodePanel.outputTitle') }}</h3>
        <button class="del" :title="$t('nodePanel.deleteNodeTitle')" @click="emit('delete')"><Trash2 :size="14" /></button>
      </div>

      <label>{{ $t('nodePanel.destinationLabel') }}</label>
      <Select
        :model-value="node.data.destType ?? 'datasource'"
        :options="[
          { value: 'datasource', label: $t('nodePanel.destTypeDatasource') },
          { value: 'database', label: $t('nodePanel.destTypeDatabaseTable') },
          { value: 's3', label: $t('nodePanel.destTypeS3') },
          { value: 'email', label: $t('nodePanel.destTypeEmail') },
        ]"
        @update:model-value="(v: any) => emit('update', { destType: v })"
      />

      <template v-if="(node.data.destType ?? 'datasource') === 'datasource'">
        <label>{{ $t('nodePanel.datasourceNameLabel') }}</label>
        <input
          :value="node.data.name ?? ''"
          type="text"
          :placeholder="$t('nodePanel.datasourceNamePlaceholder')"
          @input="emit('update', { name: ($event.target as HTMLInputElement).value })"
        />
        <label>{{ $t('nodePanel.folderLabel') }}</label>
        <Select
          :model-value="node.data.projectId ?? null"
          :options="(projects ?? []).map((p) => ({ value: p.id, label: p.name }))"
          :placeholder="$t('nodePanel.folderPlaceholder')"
          @update:model-value="(v: any) => emit('update', { projectId: v })"
        />
        <label>{{ $t('nodePanel.descriptionOptionalLabel') }}</label>
        <input
          :value="node.data.description ?? ''"
          type="text"
          :placeholder="$t('nodePanel.descriptionPlaceholder')"
          @input="emit('update', { description: ($event.target as HTMLInputElement).value })"
        />
        <label class="chk ovw">
          <input
            type="checkbox"
            :checked="node.data.overwrite ?? false"
            @change="emit('update', { overwrite: ($event.target as HTMLInputElement).checked })"
          />
          {{ $t('nodePanel.overwriteIfExists') }}
        </label>
        <p class="muted outhint">
          {{ $t('nodePanel.overwriteHint') }}
        </p>

        <label>{{ $t('nodePanel.sortKeysLabel') }} <span class="muted">{{ $t('nodePanel.sortKeysHint') }}</span></label>
        <div v-if="inputColumns.length" class="partchecks">
          <label v-for="c in inputColumns" :key="c.name" class="chk">
            <input
              type="checkbox"
              :checked="(node.data.sortKeys ?? []).includes(c.name)"
              @change="toggleSortKey(c.name)"
            />
            {{ c.name }}
          </label>
        </div>
        <p v-else class="muted outhint">{{ $t('nodePanel.sortKeysNoColumns') }}</p>

        <!-- copia su S3 esterno IN AGGIUNTA alla datasource: sempre in
             sovrascrittura, e best-effort (se fallisce il run resta valido) -->
        <label class="chk ovw">
          <input
            type="checkbox"
            :checked="node.data.mirrorEnabled ?? false"
            @change="emit('update', { mirrorEnabled: ($event.target as HTMLInputElement).checked })"
          />
          {{ $t('nodePanel.mirrorEnable') }}
        </label>
        <template v-if="node.data.mirrorEnabled">
          <label>{{ $t('nodePanel.s3ConnectionLabel') }}</label>
          <Select
            :model-value="node.data.mirrorConnectionId ?? null"
            :options="(connections ?? []).filter((c) => c.db_type === 's3').map((c) => ({
              value: c.id,
              label: c.database ? `${c.name} (bucket ${c.database})` : c.name,
            }))"
            :placeholder="$t('nodePanel.connectionPlaceholder')"
            @update:model-value="(v: any) => emit('update', { mirrorConnectionId: v })"
          />
          <label>{{ $t('nodePanel.mirrorBucketLabel') }}</label>
          <input
            :value="node.data.mirrorBucket ?? ''"
            type="text"
            :placeholder="$t('nodePanel.mirrorBucketPlaceholder')"
            @input="emit('update', { mirrorBucket: ($event.target as HTMLInputElement).value })"
          />
          <label>{{ $t('nodePanel.mirrorKeyLabel') }}</label>
          <input
            :value="node.data.mirrorKey ?? ''"
            type="text"
            :placeholder="$t('nodePanel.mirrorKeyPlaceholder')"
            @input="emit('update', { mirrorKey: ($event.target as HTMLInputElement).value })"
          />
          <!-- nessuna scelta del formato: sempre parquet. Nome e formato
               sarebbero indipendenti, e un CSV chiamato .parquet romperebbe
               chi lo legge a valle -->
          <p class="muted outhint">{{ $t('nodePanel.mirrorHint') }}</p>
        </template>
      </template>

      <!-- destinazione S3: connessione object storage + chiave + formato + partizioni -->
      <template v-else-if="(node.data.destType ?? 'datasource') === 's3'">
        <label>{{ $t('nodePanel.s3ConnectionLabel') }}</label>
        <Select
          :model-value="node.data.connectionId ?? null"
          :options="(connections ?? []).filter((c) => c.db_type === 's3').map((c) => ({
            value: c.id,
            label: c.database ? `${c.name} (bucket ${c.database})` : c.name,
          }))"
          :placeholder="$t('nodePanel.connectionPlaceholder')"
          @update:model-value="(v: any) => emit('update', { connectionId: v })"
        />
        <label>Bucket <span class="muted">{{ $t('nodePanel.bucketHint') }}</span></label>
        <input
          :value="node.data.s3Bucket ?? ''"
          type="text"
          :placeholder="$t('nodePanel.bucketPlaceholder')"
          @input="emit('update', { s3Bucket: ($event.target as HTMLInputElement).value })"
        />
        <label>{{ $t('nodePanel.keyPathLabel') }}</label>
        <input
          :value="node.data.s3Key ?? ''"
          type="text"
          :placeholder="$t('nodePanel.s3KeyPlaceholder')"
          @input="emit('update', { s3Key: ($event.target as HTMLInputElement).value })"
        />
        <label>{{ $t('nodePanel.formatLabel') }}</label>
        <Select
          :model-value="node.data.s3Format ?? 'parquet'"
          :options="[
            { value: 'parquet', label: 'Parquet' },
            { value: 'csv', label: 'CSV' },
          ]"
          @update:model-value="(v: any) => emit('update', { s3Format: v })"
        />
        <label>{{ $t('nodePanel.partitionByLabel') }}</label>
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
        <p v-else class="muted outhint">{{ $t('nodePanel.partitionNoColumnsHint') }}</p>
        <p class="muted outhint">
          {{ $t('nodePanel.partitionHintPre') }}
          <code>{{ $t('nodePanel.partitionHintFormat') }}</code>
          {{ $t('nodePanel.partitionHintPost') }}
        </p>
      </template>

      <!-- destinazione email: l'output parte come allegato. I destinatari qui
           sono una proposta: il gateway li confronta con i domini ammessi della
           connessione, che sono il vincolo vero -->
      <template v-else-if="(node.data.destType ?? 'datasource') === 'email'">
        <label>{{ $t('nodePanel.smtpConnectionLabel') }}</label>
        <Select
          :model-value="node.data.connectionId ?? null"
          :options="(connections ?? []).filter((c) => c.db_type === 'smtp').map((c) => ({
            value: c.id,
            label: c.name,
          }))"
          :placeholder="$t('nodePanel.connectionPlaceholder')"
          @update:model-value="(v: any) => emit('update', { connectionId: v })"
        />
        <label>{{ $t('nodePanel.emailToLabel') }} <span class="muted">{{ $t('nodePanel.emailListHint') }}</span></label>
        <input
          :value="(node.data.emailTo ?? []).join(', ')"
          type="text"
          placeholder="report@azienda.it, capo@azienda.it"
          @change="emit('update', { emailTo: splitAddresses(($event.target as HTMLInputElement).value) })"
        />
        <label>{{ $t('nodePanel.emailCcLabel') }} <span class="muted">{{ $t('nodePanel.emailListHint') }}</span></label>
        <input
          :value="(node.data.emailCc ?? []).join(', ')"
          type="text"
          @change="emit('update', { emailCc: splitAddresses(($event.target as HTMLInputElement).value) })"
        />
        <label>{{ $t('nodePanel.emailSubjectLabel') }}</label>
        <input
          :value="node.data.emailSubject ?? ''"
          type="text"
          :placeholder="$t('nodePanel.emailSubjectPlaceholder')"
          @input="emit('update', { emailSubject: ($event.target as HTMLInputElement).value })"
        />
        <label>{{ $t('nodePanel.emailBodyLabel') }}</label>
        <textarea
          :value="node.data.emailBody ?? ''"
          rows="4"
          :placeholder="$t('nodePanel.emailBodyPlaceholder')"
          @input="emit('update', { emailBody: ($event.target as HTMLTextAreaElement).value })"
        />
        <label class="chk ovw">
          <input
            type="checkbox"
            :checked="node.data.emailHtml ?? false"
            @change="emit('update', { emailHtml: ($event.target as HTMLInputElement).checked })"
          />
          {{ $t('nodePanel.emailHtmlLabel') }}
        </label>
        <label>{{ $t('nodePanel.attachmentNameLabel') }}</label>
        <input
          :value="node.data.attachmentName ?? ''"
          type="text"
          :placeholder="$t('nodePanel.attachmentNamePlaceholder')"
          @input="emit('update', { attachmentName: ($event.target as HTMLInputElement).value })"
        />
        <label>{{ $t('nodePanel.formatLabel') }}</label>
        <Select
          :model-value="node.data.attachmentFormat ?? 'xlsx'"
          :options="[
            { value: 'xlsx', label: 'Excel (.xlsx)' },
            { value: 'csv', label: 'CSV' },
          ]"
          @update:model-value="(v: any) => emit('update', { attachmentFormat: v })"
        />
        <!-- scelta dell'utente: default acceso. Spento, un invio fallito resta
             fra gli errori del run e i passi successivi proseguono -->
        <label class="chk ovw">
          <input
            type="checkbox"
            :checked="node.data.stopOnFailure !== false"
            @change="emit('update', { stopOnFailure: ($event.target as HTMLInputElement).checked })"
          />
          {{ $t('nodePanel.emailStopOnFailure') }}
        </label>
        <p class="muted outhint">{{ $t('nodePanel.emailHint') }}</p>

        <button class="dryrun" :disabled="!emailTestReady()" @click="emit('email-test')">
          <FlaskConical :size="13" />
          {{ $t('nodePanel.emailDryRun') }}
        </button>
        <p class="muted outhint">{{ $t('nodePanel.emailDryRunHint') }}</p>
      </template>

      <template v-else>
        <label>{{ $t('nodePanel.connectionLabel') }}</label>
        <Select
          :model-value="node.data.connectionId ?? null"
          :options="(connections ?? []).filter((c) => !['s3', 'smtp', 'sharepoint'].includes(c.db_type)).map((c) => ({
            value: c.id,
            label: c.database ? `${c.name} (${c.db_type} · ${c.database})` : `${c.name} (${c.db_type})`,
          }))"
          :placeholder="$t('nodePanel.connectionPlaceholder')"
          @update:model-value="(v: any) => emit('update', { connectionId: v })"
        />
        <label>{{ $t('nodePanel.destTableLabel') }}</label>
        <input
          :value="node.data.table ?? ''"
          type="text"
          :placeholder="$t('nodePanel.destTablePlaceholder')"
          @input="emit('update', { table: ($event.target as HTMLInputElement).value })"
        />
        <label>{{ $t('nodePanel.writeModeLabel') }}</label>
        <Select
          :model-value="node.data.mode ?? 'append'"
          :options="[
            { value: 'append', label: $t('nodePanel.modeAppend') },
            { value: 'replace', label: $t('nodePanel.modeReplace') },
          ]"
          @update:model-value="(v: any) => emit('update', { mode: v })"
        />
        <label>{{ $t('nodePanel.postSqlLabel') }}</label>
        <textarea
          :value="node.data.postSql ?? ''"
          rows="3"
          :placeholder="$t('nodePanel.postSqlPlaceholder')"
          @input="emit('update', { postSql: ($event.target as HTMLTextAreaElement).value })"
        />
        <p class="muted outhint">
          {{ $t('nodePanel.tableAutoCreateHint') }}
        </p>
      </template>
    </template>

    <!-- nodo di controllo: Refresh di una datasource prima del run -->
    <template v-else-if="node.type === 'refresh'">
      <div class="row">
        <h3><RefreshCw :size="15" /> {{ $t('nodePanel.refreshDatasourceTitle') }}</h3>
        <button class="del" :title="$t('nodePanel.deleteNodeTitle')" @click="emit('delete')"><Trash2 :size="14" /></button>
      </div>
      <p class="muted outhint">
        {{ $t('nodePanel.refreshHint') }}
      </p>
      <label>{{ $t('nodePanel.datasourceToRefreshLabel') }}</label>
      <Select
        searchable
        :model-value="node.data.datasourceId ?? null"
        :options="(datasources ?? []).filter((d) => d.kind === 'database').map((d) => ({ value: d.id, label: d.name }))"
        :placeholder="$t('nodePanel.chooseDbDatasourcePlaceholder')"
        @update:model-value="pickRefreshDatasource"
      />
    </template>

    <!-- nodo di controllo: Esegui un altro flusso -->
    <template v-else-if="node.type === 'runflow'">
      <div class="row">
        <h3><PlayCircle :size="15" /> {{ $t('nodePanel.runFlowTitle') }}</h3>
        <button class="del" :title="$t('nodePanel.deleteNodeTitle')" @click="emit('delete')"><Trash2 :size="14" /></button>
      </div>
      <p class="muted outhint">
        {{ $t('nodePanel.runFlowHint') }}
      </p>
      <label>{{ $t('nodePanel.flowToRunLabel') }}</label>
      <Select
        :model-value="node.data.flowId ?? null"
        :options="(flows ?? []).filter((f) => f.id !== currentFlowId).map((f) => ({ value: f.id, label: f.name }))"
        :placeholder="$t('nodePanel.chooseFlowPlaceholder')"
        @update:model-value="pickRunFlow"
      />
    </template>

    <!-- container foreach -->
    <template v-else-if="node.type === 'foreach'">
      <div class="row">
        <h3><Repeat :size="15" /> {{ $t('nodePanel.foreachLoopTitle') }}</h3>
        <button class="del" :title="$t('nodePanel.deleteForeachTitle')" @click="emit('delete')"><Trash2 :size="14" /></button>
      </div>

      <div class="joinhelp">
        <strong class="joinhead"><Repeat :size="13" /> {{ $t('nodePanel.howItWorksTitle') }}</strong>
        <ol>
          <li v-html="$t('nodePanel.foreachStepTopLeft')" />
          <li v-html="$t('nodePanel.foreachStepBottomLeft')" />
          <li v-html="$t('nodePanel.foreachStepDragOps')" />
          <li v-html="$t('nodePanel.foreachStepPlaceholders')" />
          <li v-html="$t('nodePanel.foreachStepOutput')" />
        </ol>
        <p v-if="rightColumns.length" class="phlist">
          {{ $t('nodePanel.availablePlaceholders') }}
          <code v-for="c in rightColumns" :key="c.name" v-text="`{{${c.name}}}`" />
        </p>
        <p v-else class="muted phlist">{{ $t('nodePanel.noDriverConnectedHint') }}</p>
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
        <label>{{ $t('nodePanel.downloadLoopOutput') }}</label>
        <div class="exportbtns">
          <button @click="emit('export', 'csv')"><Download :size="14" /> CSV</button>
          <button @click="emit('export', 'xlsx')"><FileSpreadsheet :size="14" /> Excel</button>
        </div>
      </div>
    </template>

    <template v-else>
      <div class="row">
        <h3><Settings :size="15" /> {{ $t('nodePanel.operationTitle') }}</h3>
        <button class="del" :title="$t('nodePanel.deleteNodeTitle')" @click="emit('delete')"><Trash2 :size="14" /></button>
      </div>

      <label>{{ $t('nodePanel.typeLabel') }}</label>
      <Select
        :model-value="node.data.opType"
        :options="operations.filter((o) => o !== 'foreach').map((op) => ({ value: op, label: opMeta(op).label || op }))"
        @update:model-value="changeType"
      />
      <p v-if="nodeStats?.cacheHint" class="muted outhint cachehint"><RefreshCw :size="12" /> {{ nodeStats.cacheHint }}</p>

      <div v-if="node.data.opType === 'join'" class="joinhelp">
        <strong class="joinhead"><Link2 :size="13" /> {{ $t('nodePanel.howToConnectJoinTitle') }}</strong>
        <ol>
          <li v-html="$t('nodePanel.joinStepTopLeft')" />
          <li v-html="$t('nodePanel.joinStepBottomLeft')" />
          <li v-html="$t('nodePanel.joinStepPickType')" />
        </ol>
      </div>

      <div v-if="node.data.opType === 'union'" class="joinhelp">
        <strong class="joinhead"><Link2 :size="13" /> {{ $t('nodePanel.howToConnectUnionTitle') }}</strong>
        <ol>
          <li v-html="$t('nodePanel.unionStepTopLeft')" />
          <li v-html="$t('nodePanel.unionStepBottomLeft')" />
          <li v-html="$t('nodePanel.unionStepChain')" />
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
        @preview="emit('preview')"
      />

      <div class="exportbar">
        <label>{{ $t('nodePanel.downloadNodeData') }}</label>
        <div class="exportbtns">
          <button @click="emit('export', 'csv')"><Download :size="14" /> CSV</button>
          <button @click="emit('export', 'xlsx')"><FileSpreadsheet :size="14" /> Excel</button>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.filtersbox { display: flex; flex-direction: column; gap: 6px; margin: 10px 0; padding: 8px 10px; border: 1px solid var(--border); border-radius: 8px; }
.filtersbox > label { display: inline-flex; align-items: center; gap: 5px; font-size: 12px; color: var(--muted); }
.filterrow { padding: 6px 8px; border: 1px solid var(--border-soft); border-radius: 6px; }
.filterrow.incomplete { border-style: dashed; }
.filterhead { display: flex; align-items: center; gap: 6px; font-size: 11px; text-transform: uppercase; letter-spacing: 0.04em; }
.filterhead .warn { margin-left: auto; text-transform: none; letter-spacing: 0; color: #fbbf24; }
.filterhead .x { margin-left: auto; padding: 1px 4px; background: none; border: 0; color: var(--muted); cursor: pointer; }
.filterhead .warn + .x { margin-left: 0; }
.filterhead .x:hover { color: var(--danger, #f87171); }
.addfilter { align-self: flex-start; display: inline-flex; align-items: center; gap: 4px; padding: 3px 8px; font-size: 12px; }
.samplebox { display: flex; flex-direction: column; gap: 4px; margin: 10px 0; padding: 8px 10px; border: 1px dashed var(--border); border-radius: 8px; }
.samplebox > label { display: inline-flex; align-items: center; gap: 5px; font-size: 12px; color: var(--muted); }
.samplerow { display: flex; align-items: center; gap: 6px; }
.samplerow .samplenum { width: 110px; }
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
.commenttext { font-family: inherit; }
.partchecks {
  display: flex;
  flex-direction: column;
  gap: 2px;
  max-height: 150px;
  overflow-y: auto;
}
.partchecks .chk { display: flex; align-items: center; gap: 6px; font-size: 13px; }
.partchecks .chk input { width: auto; }
.chk.ovw { display: flex; align-items: center; gap: 6px; font-size: 13px; margin-top: 8px; }
.chk.ovw input { width: auto; }
.phlist { margin: 8px 0 0; font-size: 12px; }
.phlist code {
  background: var(--panel-2);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 0 5px;
  margin-right: 4px;
  font-size: 11px;
}
.dryrun {
  align-self: flex-start;
  display: inline-flex;
  align-items: center;
  gap: 5px;
  margin-top: 10px;
  padding: 4px 10px;
  font-size: 12px;
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
  /* --on-accent, non #fff: su Dracula e Monokai l'accento è chiaro */
  background: var(--accent); color: var(--on-accent); border-radius: 4px; padding: 0 5px; font-size: 11px;
}
.tag.right { background: var(--accent-2); color: var(--on-accent-2); }
</style>
