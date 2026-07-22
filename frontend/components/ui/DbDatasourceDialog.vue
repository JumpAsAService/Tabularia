<script setup lang="ts">
// Dialog di creazione di una datasource da database: connessione + tabella
// oppure SQL libero ("vista"). Alla conferma parte il primo ingest (snapshot
// parquet); il refresh successivo si fa dalla lista datasource.
import { ref, watch } from 'vue'
import { Database, X, Table2, Code2, LoaderCircle } from 'lucide-vue-next'
import { errMessage } from '~/composables/useApi'
import { useConnections, type ConnectionInfo } from '~/composables/useConnections'
import type { DbDatasourceDraft } from '~/composables/useDatasources'

const props = defineProps<{
  open: boolean
  connections: ConnectionInfo[] // quelle usabili dall'utente (CONNECT)
  error?: string
  busy?: boolean
}>()
const emit = defineEmits<{
  (e: 'confirm', draft: DbDatasourceDraft): void
  (e: 'cancel'): void
}>()

const connApi = useConnections()

const name = ref('')
const description = ref('')
const connectionId = ref<number | null>(null)
const sourceType = ref<'table' | 'sql'>('table')
const tableName = ref('')
const sql = ref('')

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
    tables.value = []
    tablesError.value = ''
    if (connectionId.value != null) loadTables(connectionId.value)
  },
)

watch(connectionId, (id) => {
  tables.value = []
  tablesError.value = ''
  tableName.value = ''
  if (props.open && id != null) loadTables(id)
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
  (sourceType.value === 'table' ? !tableName.value.trim() : !sql.value.trim())

function confirm() {
  if (incomplete()) return
  emit('confirm', {
    name: name.value.trim(),
    description: description.value,
    connection_id: connectionId.value!,
    source_type: sourceType.value,
    source_ref: sourceType.value === 'table' ? tableName.value.trim() : sql.value,
  })
}
</script>

<template>
  <Teleport to="body">
    <div v-if="open" class="dd-backdrop" @mousedown.self="emit('cancel')">
      <div class="dd-card" @keydown.esc="emit('cancel')">
        <div class="dd-head">
          <h3><Database :size="15" /> {{ $t('dbDatasourceDialog.title') }}</h3>
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

        <div class="dd-mode">
          <button :class="{ on: sourceType === 'table' }" @click="sourceType = 'table'">
            <Table2 :size="13" /> {{ $t('dbDatasourceDialog.modeTable') }}
          </button>
          <button :class="{ on: sourceType === 'sql' }" @click="sourceType = 'sql'">
            <Code2 :size="13" /> {{ $t('dbDatasourceDialog.modeSql') }}
          </button>
        </div>

        <template v-if="sourceType === 'table'">
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

        <template v-else>
          <label>{{ $t('dbDatasourceDialog.sqlLabel') }} <span class="dd-soft">{{ $t('dbDatasourceDialog.sqlHint') }}</span></label>
          <textarea
            v-model="sql"
            rows="6"
            spellcheck="false"
            placeholder="SELECT customer_id, SUM(amount) AS total&#10;FROM orders&#10;GROUP BY customer_id"
          />
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
  width: 460px;
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
