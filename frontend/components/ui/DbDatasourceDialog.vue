<script setup lang="ts">
// Dialog di creazione di una datasource da database: connessione + tabella
// oppure SQL libero ("vista"). Alla conferma parte il primo ingest (snapshot
// parquet); il refresh successivo si fa dalla lista datasource.
// The dialog follows the chosen connection: a database (table or SQL), SharePoint
// (path + sheet), SFTP or S3 (folder + glob + reading options), an HTTP API (the
// call). The file sources share the «dynamic» part: placeholders in the names,
// variables, refresh mode — with a live preview of what the names give now.
import { computed, ref, watch } from 'vue'
import { Database, X, Table2, Code2, LoaderCircle, FileSpreadsheet, FolderSearch, FileText } from 'lucide-vue-next'
import { errMessage } from '~/composables/useApi'
import { useConnections, type ConnectionInfo } from '~/composables/useConnections'
import { cleanApiCall, emptyApiCall, type ApiCallDraft, type ApiDatasourceDraft, type DbDatasourceDraft, type S3DatasourceDraft, type SharePointDatasourceDraft, type SftpDatasourceDraft } from '~/composables/useDatasources'
import { cleanFileTemplates, emptyFileTemplates, usePlaceholderPreview, type FileTemplatesDraft, type FilesCheck, type PreviewRequest } from '~/composables/usePlaceholders'
import ApiCallForm from '~/components/ui/ApiCallForm.vue'
import DynamicSourceFields from '~/components/ui/DynamicSourceFields.vue'
import TemplatePreview from '~/components/ui/TemplatePreview.vue'
import MissingValues from '~/components/ui/MissingValues.vue'
import { useDialogA11y } from '~/composables/useDialogA11y'
import { useFolderPaths } from '~/composables/useFolderPaths'

const props = defineProps<{
  open: boolean
  connections: ConnectionInfo[] // quelle usabili dall'utente (CONNECT)
  error?: string
  busy?: boolean
}>()
const emit = defineEmits<{
  (e: 'confirm', draft: DbDatasourceDraft): void
  (e: 'confirm-sharepoint', draft: SharePointDatasourceDraft): void
  (e: 'confirm-sftp', draft: SftpDatasourceDraft): void
  (e: 'confirm-s3', draft: S3DatasourceDraft): void
  (e: 'confirm-api', draft: ApiDatasourceDraft): void
  (e: 'cancel'): void
}>()

const connApi = useConnections()
const { pathOf } = useFolderPaths() // connection names repeat across folders

// fuoco iniziale sulla card, Tab confinato, Esc funzionante, fuoco restituito
const card = ref<HTMLElement | null>(null)
useDialogA11y(card, () => props.open, () => emit('cancel'))

const name = ref('')
const description = ref('')
const connectionId = ref<number | null>(null)
const sourceType = ref<'table' | 'sql'>('table')
const tableName = ref('')
const sql = ref('')
// chiavi di ORDER BY, testo separato da virgole: pre-ingest non conosciamo le
// colonne (solo i nomi delle tabelle), quindi l'utente le digita — è la SUA
// tabella. Vuoto = nessun ordine. Un nome sbagliato dà l'errore parlante del DB.
const sortKeysText = ref('')

// Connessione SharePoint: niente tabella né SQL — un PERCORSO (anche con glob) e
// un FOGLIO. Più file corrispondenti diventano una tabella sola, e ogni riga dice
// da quale file arriva. Il file deve essere machine readable: intestazione in
// prima riga, un nome per colonna. Non ci sono opzioni per aggirarlo, apposta.
const selected = computed(() => props.connections.find((c) => c.id === connectionId.value) ?? null)
const isSp = computed(() => selected.value?.db_type === 'sharepoint')
const spPath = ref('')
const spSheet = ref('')
const spFiles = ref<FilesCheck['files'] | null>(null)
const spTotal = ref(0)
const spCheck = ref<FilesCheck | null>(null)
const spChecking = ref(false)
const spError = ref('')
let spSeq = 0 // solo l'ULTIMO controllo scrive: due Invio di fila non si sovrappongono
async function checkFiles() {
  if (connectionId.value == null || !spPath.value.trim() || spChecking.value) return
  const seq = ++spSeq
  const path = spPath.value.trim()
  spChecking.value = true
  spError.value = ''
  spFiles.value = null
  try {
    const res = await connApi.sharepointFiles(connectionId.value, { path, ...cleanFileTemplates(dyn.value) })
    if (seq !== spSeq || path !== spPath.value.trim()) return
    spFiles.value = res.files
    spTotal.value = res.total
    spCheck.value = res
  } catch (e) {
    if (seq === spSeq) spError.value = errMessage(e)
  } finally {
    if (seq === spSeq) spChecking.value = false
  }
}
watch(spPath, () => { spFiles.value = null; spError.value = '' })

// SFTP connection: a FOLDER and a glob of file names; every matching file is read
// at each refresh and stacked (`_file` says which). Files stay on the server.
// The same machine-readable contract as SharePoint, plus the reading options of
// the file sources (format, CSV separator, header, encoding; Excel sheet).
const isSftp = computed(() => selected.value?.db_type === 'sftp')
// S3 connection («S3 folder»): the same form as SFTP plus the bucket (empty = the
// connection's default, its `database`)
const isS3 = computed(() => selected.value?.db_type === 's3')
const isFolderSource = computed(() => isSftp.value || isS3.value)
const s3Bucket = ref('')
// HTTP API connection: the call is made at each refresh (ApiCallForm)
const isApi = computed(() => selected.value?.db_type === 'api')
const apiCall = ref<ApiCallDraft>(emptyApiCall())
// not a database: no table, no SQL, no sort keys
const isFileSource = computed(() => isSp.value || isFolderSource.value || isApi.value)
// the moving part of the SharePoint, SFTP and S3 sources (DynamicSourceFields)
const dyn = ref<FileTemplatesDraft>(emptyFileTemplates())
const appendWithoutKeys = computed(() => dyn.value.mode === 'append' && !dyn.value.keys.length)
// what the path, or the folder and the glob, give now
const preview = usePlaceholderPreview((): PreviewRequest | null => {
  const common = {
    kind: 'input' as const,
    timezone: dyn.value.timezone,
    variables: dyn.value.variables,
    last_refresh_initial: dyn.value.last_refresh_initial,
  }
  if (isSp.value) return { ...common, templates: { path: spPath.value.trim() } }
  if (isFolderSource.value) return { ...common, templates: { folder: ftFolder.value.trim(), pattern: ftPattern.value.trim() } }
  return null
})
const ftFolder = ref('')
const ftPattern = ref('')
const ftFormat = ref<'auto' | 'csv' | 'parquet' | 'excel' | 'ndjson'>('auto')
const ftSeparator = ref(',')
const ftHeader = ref(true)
const ftEncoding = ref<'utf8' | 'utf8-lossy' | 'windows-1252'>('utf8')
const ftSheet = ref('')
const ftFiles = ref<FilesCheck['files'] | null>(null)
const ftTotal = ref(0)
const ftCheck = ref<FilesCheck | null>(null)
const ftChecking = ref(false)
const ftError = ref('')
let ftSeq = 0
async function checkFolderFiles() {
  if (connectionId.value == null || !ftPattern.value.trim() || ftChecking.value) return
  const seq = ++ftSeq
  const folder = ftFolder.value.trim()
  const pattern = ftPattern.value.trim()
  ftChecking.value = true
  ftError.value = ''
  ftFiles.value = null
  try {
    const templates = cleanFileTemplates(dyn.value)
    const res = isS3.value
      ? await connApi.s3Files(connectionId.value, { bucket: s3Bucket.value.trim(), folder, pattern, ...templates })
      : await connApi.sftpFiles(connectionId.value, { folder, pattern, ...templates })
    if (seq !== ftSeq || folder !== ftFolder.value.trim() || pattern !== ftPattern.value.trim()) return
    ftFiles.value = res.files
    ftTotal.value = res.total
    ftCheck.value = res
  } catch (e) {
    if (seq === ftSeq) ftError.value = errMessage(e)
  } finally {
    if (seq === ftSeq) ftChecking.value = false
  }
}
watch([ftFolder, ftPattern, s3Bucket], () => { ftFiles.value = null; ftError.value = '' })
// other variables or another zone: another list of files
watch(dyn, () => { ftFiles.value = null; ftError.value = ''; spFiles.value = null; spError.value = '' }, { deep: true })
const showsCsvOptions = computed(() => ftFormat.value === 'auto' || ftFormat.value === 'csv')
const showsSheet = computed(() => ftFormat.value === 'auto' || ftFormat.value === 'excel')

// tabelle della connessione scelta (best-effort: se fallisce si digita a mano)
const tables = ref<string[]>([])
const tablesLoading = ref(false)
const tablesError = ref('')

watch(
  () => props.open,
  (open) => {
    if (!open) return
    name.value = ''
    description.value = ''
    connectionId.value = props.connections.length === 1 ? props.connections[0].id : null
    sourceType.value = 'table'
    tableName.value = ''
    sql.value = ''
    sortKeysText.value = ''
    spPath.value = ''
    spSheet.value = ''
    spFiles.value = null
    spError.value = ''
    spChecking.value = false
    spSeq++
    ftFolder.value = ''
    ftPattern.value = ''
    s3Bucket.value = ''
    dyn.value = emptyFileTemplates()
    ftFormat.value = 'auto'
    ftSeparator.value = ','
    ftHeader.value = true
    ftEncoding.value = 'utf8'
    ftSheet.value = ''
    ftFiles.value = null
    ftError.value = ''
    ftChecking.value = false
    ftSeq++
    apiCall.value = emptyApiCall()
    tables.value = []
    tablesError.value = ''
    if (connectionId.value != null && !isFileSource.value) loadTables(connectionId.value)
  },
)

watch(connectionId, (id) => {
  tables.value = []
  tablesError.value = ''
  tableName.value = ''
  spFiles.value = null
  spError.value = ''
  ftFiles.value = null
  ftError.value = ''
  if (props.open && id != null && !isFileSource.value) loadTables(id)
})

async function loadTables(id: number) {
  tablesLoading.value = true
  try {
    tables.value = (await connApi.tables(id)).tables
  } catch (e) {
    tablesError.value = errMessage(e)
  } finally {
    tablesLoading.value = false
  }
}

const incomplete = () =>
  !name.value.trim() ||
  connectionId.value === null ||
  (isSp.value
    ? !spPath.value.trim() || !spSheet.value.trim() || appendWithoutKeys.value
    : isFolderSource.value
      ? !ftPattern.value.trim() || ftSeparator.value.length !== 1 || appendWithoutKeys.value
        || (isS3.value && !s3Bucket.value.trim() && !(selected.value?.database ?? '').trim())
      : isApi.value
        ? apiCall.value.mode === 'append' && !apiCall.value.keys.length
        : sourceType.value === 'table' ? !tableName.value.trim() : !sql.value.trim())

function confirm() {
  if (incomplete()) return
  if (isSp.value) {
    emit('confirm-sharepoint', {
      name: name.value.trim(),
      description: description.value,
      connection_id: connectionId.value!,
      path: spPath.value.trim(),
      sheet: spSheet.value.trim(),
      ...cleanFileTemplates(dyn.value),
    })
    return
  }
  if (isApi.value) {
    emit('confirm-api', {
      ...cleanApiCall(apiCall.value),
      name: name.value.trim(),
      description: description.value,
      connection_id: connectionId.value!,
    })
    return
  }
  if (isFolderSource.value) {
    const files = {
      name: name.value.trim(),
      description: description.value,
      connection_id: connectionId.value!,
      folder: ftFolder.value.trim(),
      pattern: ftPattern.value.trim(),
      format: ftFormat.value,
      sheet: ftSheet.value.trim(),
      csv: { separator: ftSeparator.value, has_header: ftHeader.value, encoding: ftEncoding.value },
      ...cleanFileTemplates(dyn.value),
    }
    if (isS3.value) emit('confirm-s3', { ...files, bucket: s3Bucket.value.trim() })
    else emit('confirm-sftp', files)
    return
  }
  emit('confirm', {
    name: name.value.trim(),
    description: description.value,
    connection_id: connectionId.value!,
    source_type: sourceType.value,
    source_ref: sourceType.value === 'table' ? tableName.value.trim() : sql.value,
    sort_keys: sortKeysText.value.split(/[,;\s]+/).map((k) => k.trim()).filter(Boolean),
  })
}
</script>

<template>
  <Teleport to="body">
    <div v-if="open" class="dd-backdrop" @mousedown.self="emit('cancel')">
      <div
        ref="card"
        v-label-fields
        class="dd-card"
        :class="{ wide: isFileSource }"
        role="dialog"
        aria-modal="true"
        aria-labelledby="dd-title"
        tabindex="-1"
      >
        <div class="dd-head">
          <h3 id="dd-title"><Database :size="15" /> {{ $t('dbDatasourceDialog.title') }}</h3>
          <button class="dd-x" @click="emit('cancel')"><X :size="14" /></button>
        </div>

        <label>{{ $t('dbDatasourceDialog.nameLabel') }}</label>
        <input v-model="name" type="text" :placeholder="$t('dbDatasourceDialog.namePlaceholder')" />

        <label>{{ $t('dbDatasourceDialog.connectionLabel') }}</label>
        <Select
          v-model="connectionId"
          :options="connections.map((c) => ({ value: c.id, label: `${c.name} (${c.db_type})`, hint: pathOf(c.project_id) }))"
          :placeholder="$t('dbDatasourceDialog.connectionPlaceholder')"
        />
        <p v-if="!connections.length" class="muted dd-hint">
          {{ $t('dbDatasourceDialog.noConnectionHint') }}
        </p>

        <template v-if="isSp">
          <label>{{ $t('sharepoint.pathLabel') }} <span class="dd-soft">{{ $t('sharepoint.pathHint') }}</span></label>
          <div class="dd-row">
            <input v-model="spPath" type="text" spellcheck="false" class="dd-sp-path" aria-describedby="tp-sp-path" placeholder="Budget/2026/*.xlsx" @keydown.enter.prevent="checkFiles" />
            <button type="button" :disabled="spChecking || !spPath.trim()" @click="checkFiles">
              <LoaderCircle v-if="spChecking" :size="13" class="spin" /><FolderSearch v-else :size="13" /> {{ $t('sharepoint.checkFiles') }}
            </button>
          </div>
          <TemplatePreview id="tp-sp-path" :preview="preview" name="path" />
          <p v-if="spError" class="dd-err">{{ spError }}</p>
          <div v-else-if="spFiles" class="dd-files" role="status">
            <p v-if="!spTotal" class="muted">{{ $t('sharepoint.noFiles') }}</p>
            <template v-else>
              <p class="muted">{{ $t('sharepoint.filesFound', { n: spTotal }) }}</p>
              <ul>
                <li v-for="f in spFiles.slice(0, 8)" :key="f.path"><FileSpreadsheet :size="12" /> {{ f.path }}</li>
              </ul>
              <p v-if="spTotal > 8" class="muted">{{ $t('sharepoint.andMore', { n: spTotal - 8 }) }}</p>
            </template>
            <MissingValues v-if="spCheck?.missing?.length" :missing="spCheck.missing" :count="spCheck.missing_total" :expansions="spCheck.expansions" />
          </div>
          <label>{{ $t('sharepoint.sheetLabel') }}</label>
          <input v-model="spSheet" type="text" :placeholder="$t('sharepoint.sheetPlaceholder')" />
          <p class="muted dd-hint">{{ $t('sharepoint.machineReadable') }}</p>
          <DynamicSourceFields v-model="dyn" :preview="preview" keys-placeholder="_param_day, _file" />
        </template>

        <template v-if="isFolderSource">
          <template v-if="isS3">
            <label>{{ $t('s3Source.bucketLabel') }} <span class="dd-soft">{{ $t('s3Source.bucketHint') }}</span></label>
            <input
              v-model="s3Bucket"
              type="text"
              spellcheck="false"
              class="dd-mono dd-s3-bucket"
              :placeholder="selected?.database || $t('s3Source.bucketNone')"
            />
          </template>
          <label>{{ isS3 ? $t('s3Source.folderLabel') : $t('sftp.folderLabel') }}</label>
          <input
            v-model="ftFolder"
            type="text"
            spellcheck="false"
            class="dd-mono dd-folder"
            aria-describedby="tp-folder"
            :placeholder="isS3 ? $t('s3Source.folderPlaceholder') : $t('sftp.folderPlaceholder')"
          />
          <TemplatePreview id="tp-folder" :preview="preview" name="folder" />
          <label>{{ $t('sftp.patternLabel') }} <span class="dd-soft">{{ $t('sftp.patternHint') }}</span></label>
          <div class="dd-row">
            <input v-model="ftPattern" type="text" spellcheck="false" class="dd-pattern" aria-describedby="tp-pattern" placeholder="vendite_*.csv" @keydown.enter.prevent="checkFolderFiles" />
            <button type="button" class="dd-check-files" :disabled="ftChecking || !ftPattern.trim()" @click="checkFolderFiles">
              <LoaderCircle v-if="ftChecking" :size="13" class="spin" /><FolderSearch v-else :size="13" /> {{ $t('sftp.checkFiles') }}
            </button>
          </div>
          <TemplatePreview id="tp-pattern" :preview="preview" name="pattern" />
          <p v-if="ftError" class="dd-err">{{ ftError }}</p>
          <div v-else-if="ftFiles" class="dd-files" role="status">
            <p v-if="!ftTotal" class="muted">{{ $t('sftp.noFiles') }}</p>
            <template v-else>
              <p class="muted">{{ $t('sftp.filesFound', { n: ftTotal }) }}</p>
              <ul>
                <li v-for="f in ftFiles.slice(0, 8)" :key="f.path"><FileText :size="12" /> {{ f.path }}</li>
              </ul>
              <p v-if="ftTotal > 8" class="muted">{{ $t('sftp.andMore', { n: ftTotal - 8 }) }}</p>
            </template>
            <MissingValues v-if="ftCheck?.missing?.length" :missing="ftCheck.missing" :count="ftCheck.missing_total" :expansions="ftCheck.expansions" />
          </div>
          <label>{{ $t('sftp.formatLabel') }}</label>
          <Select
            v-model="ftFormat"
            :options="[
              { value: 'auto', label: $t('sftp.formatAuto') },
              { value: 'csv', label: 'CSV' },
              { value: 'parquet', label: 'Parquet' },
              { value: 'excel', label: 'Excel' },
              { value: 'ndjson', label: 'NDJSON' },
            ]"
          />
          <div v-if="showsCsvOptions" class="dd-csv">
            <div>
              <label>{{ $t('sftp.separatorLabel') }}</label>
              <input v-model="ftSeparator" type="text" maxlength="1" class="dd-mono dd-sep" />
            </div>
            <div>
              <label>{{ $t('sftp.encodingLabel') }}</label>
              <Select
                v-model="ftEncoding"
                :options="[
                  { value: 'utf8', label: 'UTF-8' },
                  { value: 'utf8-lossy', label: $t('sftp.encodingLossy') },
                  { value: 'windows-1252', label: 'Windows-1252 (Latin-1)' },
                ]"
              />
            </div>
            <label class="dd-check"><input v-model="ftHeader" type="checkbox" /> {{ $t('sftp.headerLabel') }}</label>
          </div>
          <template v-if="showsSheet">
            <label>{{ $t('sftp.sheetLabel') }}</label>
            <input v-model="ftSheet" type="text" :placeholder="$t('sftp.sheetPlaceholder')" />
          </template>
          <p class="muted dd-hint">{{ isS3 ? $t('s3Source.machineReadable') : $t('sftp.machineReadable') }}</p>
          <DynamicSourceFields v-model="dyn" :preview="preview" />
        </template>

        <ApiCallForm v-if="isApi" v-model="apiCall" :connection-id="connectionId" />

        <div v-if="!isFileSource" class="dd-mode">
          <button :class="{ on: sourceType === 'table' }" @click="sourceType = 'table'">
            <Table2 :size="13" /> {{ $t('dbDatasourceDialog.modeTable') }}
          </button>
          <button :class="{ on: sourceType === 'sql' }" @click="sourceType = 'sql'">
            <Code2 :size="13" /> {{ $t('dbDatasourceDialog.modeSql') }}
          </button>
        </div>

        <template v-if="!isFileSource && sourceType === 'table'">
          <label>{{ $t('dbDatasourceDialog.tableLabel') }}</label>
          <Select
            v-if="tables.length"
            v-model="tableName"
            :options="tables.map((t) => ({ value: t, label: t }))"
            :placeholder="$t('dbDatasourceDialog.tablePlaceholder')"
          />
          <input
            v-else
            v-model="tableName"
            type="text"
            :placeholder="$t('dbDatasourceDialog.schemaTablePlaceholder')"
          />
          <p v-if="tablesLoading" class="muted dd-hint">
            <LoaderCircle :size="12" class="spin" /> {{ $t('dbDatasourceDialog.loadingTables') }}
          </p>
          <p v-else-if="tablesError" class="muted dd-hint">
            {{ $t('dbDatasourceDialog.tablesListError', { error: tablesError }) }}
          </p>
        </template>

        <template v-else-if="!isFileSource">
          <label>{{ $t('dbDatasourceDialog.sqlLabel') }} <span class="dd-soft">{{ $t('dbDatasourceDialog.sqlHint') }}</span></label>
          <textarea
            v-model="sql"
            rows="6"
            spellcheck="false"
            placeholder="SELECT customer_id, SUM(amount) AS total&#10;FROM orders&#10;GROUP BY customer_id"
          />
        </template>

        <template v-if="!isFileSource">
          <label>{{ $t('dbDatasourceDialog.sortKeysLabel') }} <span class="dd-soft">{{ $t('dbDatasourceDialog.sortKeysHint') }}</span></label>
          <input v-model="sortKeysText" type="text" placeholder="id, data_ordine" />
        </template>

        <label>{{ $t('dbDatasourceDialog.descriptionLabel') }} <span class="dd-soft">{{ $t('dbDatasourceDialog.optionalHint') }}</span></label>
        <input v-model="description" type="text" :placeholder="$t('dbDatasourceDialog.descriptionPlaceholder')" />

        <p v-if="error" class="dd-err">{{ error }}</p>

        <div class="dd-actions">
          <button @click="emit('cancel')">{{ $t('dbDatasourceDialog.cancel') }}</button>
          <button class="primary" :disabled="incomplete() || busy" @click="confirm">
            <LoaderCircle v-if="busy" :size="14" class="spin" />
            <Database v-else :size="14" />
            {{ $t('dbDatasourceDialog.confirm') }}
          </button>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.dd-row { display: flex; gap: 8px; }
.dd-mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12.5px; }
.dd-csv { display: grid; grid-template-columns: auto 1fr; gap: 6px 10px; align-items: end; }
.dd-csv > div { display: flex; flex-direction: column; gap: 3px; }
.dd-sep { width: 4.5em; text-align: center; }
.dd-check { grid-column: 1 / -1; display: inline-flex; align-items: center; gap: 6px; }
.dd-row input { flex: 1; min-width: 0; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12.5px; }
.dd-row button { display: inline-flex; align-items: center; gap: 6px; white-space: nowrap; }
.dd-files { margin: 6px 0 2px; padding: 8px 10px; border: 1px solid var(--border-soft); border-radius: 8px; background: var(--bg-soft); font-size: 12px; }
.dd-files { display: flex; flex-direction: column; gap: 6px; }
.dd-files p { margin: 0; }
.dd-files ul { list-style: none; margin: 6px 0 0; padding: 0; display: grid; gap: 3px; }
.dd-files li { display: flex; align-items: center; gap: 6px; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.dd-files li svg { flex: none; color: var(--accent-2); }
.dd-backdrop {
  position: fixed;
  inset: 0;
  background: var(--scrim);
  backdrop-filter: blur(2px);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 2000;
}
.dd-card {
  width: min(460px, calc(100vw - 32px));
  max-height: calc(100vh - 32px);
  overflow-y: auto;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 14px;
  box-shadow: var(--shadow-2);
  padding: 18px 20px;
  display: flex;
  flex-direction: column;
  gap: 7px;
}
.dd-card.wide { width: min(680px, calc(100vw - 32px)); }
.dd-head { display: flex; align-items: center; justify-content: space-between; }
.dd-head h3 { margin: 0; display: inline-flex; align-items: center; gap: 7px; font-size: 16px; }
.dd-x { padding: 3px 7px; }
label { font-size: 12px; color: var(--muted); }
.dd-soft { opacity: 0.7; }
.dd-hint { display: flex; align-items: center; gap: 5px; font-size: 12px; margin: 0; }
.dd-mode { display: flex; gap: 6px; margin: 6px 0 2px; }
.dd-mode button {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 12px;
  padding: 4px 12px;
}
.dd-mode button.on { border-color: var(--accent); color: var(--accent); }
textarea {
  resize: vertical;
  font-family: var(--mono, ui-monospace, monospace);
  font-size: 12.5px;
}
.dd-err { color: var(--danger); font-size: 12px; margin: 4px 0 0; }
.dd-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 8px; }
</style>
