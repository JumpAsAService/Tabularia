<script setup lang="ts">
// Dialog di creazione di una datasource da database: connessione + tabella
// oppure SQL libero ("vista"). Alla conferma parte il primo ingest (snapshot
// parquet); il refresh successivo si fa dalla lista datasource.
import { computed, ref, watch } from 'vue'
import { Database, X, Table2, Code2, LoaderCircle, FileSpreadsheet, FolderSearch } from 'lucide-vue-next'
import { errMessage } from '~/composables/useApi'
import { useConnections, type ConnectionInfo } from '~/composables/useConnections'
import type { DbDatasourceDraft, SharePointDatasourceDraft } from '~/composables/useDatasources'
import { useDialogA11y } from '~/composables/useDialogA11y'

const props = defineProps<{
  open: boolean
  connections: ConnectionInfo[] // quelle usabili dall'utente (CONNECT)
  error?: string
  busy?: boolean
}>()
const emit = defineEmits<{
  (e: 'confirm', draft: DbDatasourceDraft): void
  (e: 'confirm-sharepoint', draft: SharePointDatasourceDraft): void
  (e: 'cancel'): void
}>()

const connApi = useConnections()

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
const isSp = computed(() => props.connections.find((c) => c.id === connectionId.value)?.db_type === 'sharepoint')
const spPath = ref('')
const spSheet = ref('')
const spFiles = ref<{ path: string; size: number }[] | null>(null)
const spTotal = ref(0)
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
    const res = await connApi.sharepointFiles(connectionId.value, path)
    if (seq !== spSeq || path !== spPath.value.trim()) return
    spFiles.value = res.files
    spTotal.value = res.total
  } catch (e) {
    if (seq === spSeq) spError.value = errMessage(e)
  } finally {
    if (seq === spSeq) spChecking.value = false
  }
}
watch(spPath, () => { spFiles.value = null; spError.value = '' })

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
    tables.value = []
    tablesError.value = ''
    if (connectionId.value != null && !isSp.value) loadTables(connectionId.value)
  },
)

watch(connectionId, (id) => {
  tables.value = []
  tablesError.value = ''
  tableName.value = ''
  spFiles.value = null
  spError.value = ''
  if (props.open && id != null && !isSp.value) loadTables(id)
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
    ? !spPath.value.trim() || !spSheet.value.trim()
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
    })
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
        class="dd-card"
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
          :options="connections.map((c) => ({ value: c.id, label: `${c.name} (${c.db_type})` }))"
          :placeholder="$t('dbDatasourceDialog.connectionPlaceholder')"
        />
        <p v-if="!connections.length" class="muted dd-hint">
          {{ $t('dbDatasourceDialog.noConnectionHint') }}
        </p>

        <template v-if="isSp">
          <label>{{ $t('sharepoint.pathLabel') }} <span class="dd-soft">{{ $t('sharepoint.pathHint') }}</span></label>
          <div class="dd-row">
            <input v-model="spPath" type="text" spellcheck="false" placeholder="Budget/2026/*.xlsx" @keydown.enter.prevent="checkFiles" />
            <button type="button" :disabled="spChecking || !spPath.trim()" @click="checkFiles">
              <LoaderCircle v-if="spChecking" :size="13" class="spin" /><FolderSearch v-else :size="13" /> {{ $t('sharepoint.checkFiles') }}
            </button>
          </div>
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
          </div>
          <label>{{ $t('sharepoint.sheetLabel') }}</label>
          <input v-model="spSheet" type="text" :placeholder="$t('sharepoint.sheetPlaceholder')" />
          <p class="muted dd-hint">{{ $t('sharepoint.machineReadable') }}</p>
        </template>

        <div v-if="!isSp" class="dd-mode">
          <button :class="{ on: sourceType === 'table' }" @click="sourceType = 'table'">
            <Table2 :size="13" /> {{ $t('dbDatasourceDialog.modeTable') }}
          </button>
          <button :class="{ on: sourceType === 'sql' }" @click="sourceType = 'sql'">
            <Code2 :size="13" /> {{ $t('dbDatasourceDialog.modeSql') }}
          </button>
        </div>

        <template v-if="!isSp && sourceType === 'table'">
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

        <template v-else-if="!isSp">
          <label>{{ $t('dbDatasourceDialog.sqlLabel') }} <span class="dd-soft">{{ $t('dbDatasourceDialog.sqlHint') }}</span></label>
          <textarea
            v-model="sql"
            rows="6"
            spellcheck="false"
            placeholder="SELECT customer_id, SUM(amount) AS total&#10;FROM orders&#10;GROUP BY customer_id"
          />
        </template>

        <template v-if="!isSp">
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
.dd-row input { flex: 1; min-width: 0; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12.5px; }
.dd-row button { display: inline-flex; align-items: center; gap: 6px; white-space: nowrap; }
.dd-files { margin: 6px 0 2px; padding: 8px 10px; border: 1px solid var(--border-soft); border-radius: 8px; background: var(--bg-soft); font-size: 12px; }
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
