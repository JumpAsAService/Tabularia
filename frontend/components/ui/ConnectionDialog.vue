<script setup lang="ts">
// Dialog di creazione/modifica di una connessione database, con test in-place.
// La password non viene mai mostrata: in modifica, campo vuoto = non cambiarla.
import { computed, ref, watch } from 'vue'
import { Plug, X, CheckCircle2, XCircle, LoaderCircle } from 'lucide-vue-next'
import { errMessage } from '~/composables/useApi'
import {
  useConnections,
  DB_TYPES,
  type ConnectionDraft,
  type ConnectionInfo,
} from '~/composables/useConnections'

const props = defineProps<{
  open: boolean
  projectId: number
  existing?: ConnectionInfo | null // valorizzata = modifica
  error?: string
  busy?: boolean
}>()
const emit = defineEmits<{
  (e: 'confirm', draft: ConnectionDraft): void
  (e: 'cancel'): void
}>()

const connApi = useConnections()

const name = ref('')
const description = ref('')
const dbType = ref('postgresql')
const host = ref('')
const port = ref<string>('')
const username = ref('')
const password = ref('')
const database = ref('')
const dbSchema = ref('')

const isEdit = computed(() => !!props.existing)
// object storage: stesse colonne, etichette diverse (host=endpoint, ecc.)
const isS3 = computed(() => dbType.value === 's3')

watch(
  () => props.open,
  (open) => {
    if (!open) return
    testResult.value = null
    const c = props.existing
    name.value = c?.name ?? ''
    description.value = c?.description ?? ''
    dbType.value = c?.db_type ?? 'postgresql'
    host.value = c?.host ?? ''
    port.value = c?.port != null ? String(c.port) : ''
    username.value = c?.username ?? ''
    password.value = '' // mai precompilata
    database.value = c?.database ?? ''
    dbSchema.value = c?.db_schema ?? ''
  },
)

function draft(): ConnectionDraft {
  return {
    name: name.value.trim(),
    description: description.value,
    db_type: dbType.value,
    host: host.value.trim(),
    port: port.value.trim() ? Number(port.value) : null,
    username: username.value,
    password: password.value,
    database: database.value.trim(),
    db_schema: dbSchema.value.trim(),
  }
}

// per S3 l'endpoint può essere vuoto (= AWS): basta il nome
const incomplete = computed(() => !name.value.trim() || (!isS3.value && !host.value.trim()))

// ── Test connection ──────────────────────────────────────────────────────────
const testing = ref(false)
const testResult = ref<{ ok: boolean; message: string } | null>(null)

async function test() {
  testing.value = true
  testResult.value = null
  try {
    if (isEdit.value && !password.value) {
      // password invariata: si testa la connessione salvata
      await connApi.test(props.existing!.id)
    } else {
      await connApi.testDraft(props.projectId, draft())
    }
    testResult.value = { ok: true, message: 'Connection works' }
  } catch (e) {
    testResult.value = { ok: false, message: errMessage(e) }
  } finally {
    testing.value = false
  }
}

function confirm() {
  if (incomplete.value) return
  emit('confirm', draft())
}
</script>

<template>
  <Teleport to="body">
    <div v-if="open" class="cd-backdrop" @mousedown.self="emit('cancel')">
      <div class="cd-card" @keydown.esc="emit('cancel')">
        <div class="cd-head">
          <h3><Plug :size="15" /> {{ isEdit ? 'Edit connection' : 'New connection' }}</h3>
          <button class="cd-x" @click="emit('cancel')"><X :size="14" /></button>
        </div>

        <div class="cd-grid">
          <div class="cd-field cd-wide">
            <label>Name</label>
            <input v-model="name" type="text" placeholder="e.g. sales-warehouse" />
          </div>
          <div class="cd-field" :class="{ 'cd-wide': isS3 }">
            <label>Connection type</label>
            <Select v-model="dbType" :options="DB_TYPES" />
          </div>
          <div v-if="!isS3" class="cd-field">
            <label>Port <span class="cd-hint">(empty = default)</span></label>
            <input v-model="port" type="text" inputmode="numeric" placeholder="5432" />
          </div>
          <div class="cd-field cd-wide">
            <label>{{ isS3 ? 'Endpoint URL' : 'Host' }} <span v-if="isS3" class="cd-hint">(empty = AWS)</span></label>
            <input
              v-model="host"
              type="text"
              :placeholder="isS3 ? 'https://minio.example.com:9000' : 'db.internal.example.com'"
            />
          </div>
          <div class="cd-field">
            <label>{{ isS3 ? 'Access key ID' : 'Username' }}</label>
            <input v-model="username" type="text" autocomplete="off" />
          </div>
          <div class="cd-field">
            <label>
              {{ isS3 ? 'Secret access key' : 'Password' }}
              <span v-if="isEdit" class="cd-hint">(empty = unchanged)</span>
            </label>
            <input v-model="password" type="password" autocomplete="new-password" />
          </div>
          <div class="cd-field">
            <label>
              {{ isS3 ? 'Default bucket' : dbType === 'trino' ? 'Catalog' : 'Database' }}
              <span v-if="isS3" class="cd-hint">(optional)</span>
            </label>
            <input v-model="database" type="text" :placeholder="isS3 ? 'exports' : ''" />
          </div>
          <div class="cd-field">
            <label>{{ isS3 ? 'Region' : 'Schema' }} <span class="cd-hint">(optional)</span></label>
            <input
              v-model="dbSchema"
              type="text"
              :placeholder="isS3 ? 'eu-south-1' : dbType === 'postgresql' ? 'public' : ''"
            />
          </div>
          <div class="cd-field cd-wide">
            <label>Description <span class="cd-hint">(optional)</span></label>
            <input v-model="description" type="text" placeholder="what it connects to, who owns it" />
          </div>
        </div>

        <p class="muted cd-note">
          <template v-if="isS3">
            Anyone with the CONNECT permission on this folder can write to whatever
            these credentials can reach — prefer keys scoped to the target bucket.
          </template>
          <template v-else>
            Anyone with the CONNECT permission on this folder can query whatever these
            credentials can read — prefer a read-only database user.
          </template>
        </p>

        <p v-if="testResult" class="cd-test" :class="testResult.ok ? 'ok' : 'ko'">
          <CheckCircle2 v-if="testResult.ok" :size="14" />
          <XCircle v-else :size="14" />
          {{ testResult.message }}
        </p>
        <p v-if="error" class="cd-err">{{ error }}</p>

        <div class="cd-actions">
          <button :disabled="testing || incomplete" @click="test">
            <LoaderCircle v-if="testing" :size="14" class="spin" />
            <Plug v-else :size="14" />
            Test connection
          </button>
          <span class="cd-spacer" />
          <button @click="emit('cancel')">Cancel</button>
          <button class="primary" :disabled="incomplete || busy" @click="confirm">
            {{ isEdit ? 'Save' : 'Create' }}
          </button>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.cd-backdrop {
  position: fixed;
  inset: 0;
  background: rgba(5, 7, 12, 0.6);
  backdrop-filter: blur(2px);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 2000;
}
.cd-card {
  width: 520px;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 14px;
  box-shadow: var(--shadow-2);
  padding: 18px 20px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.cd-head { display: flex; align-items: center; justify-content: space-between; }
.cd-head h3 { margin: 0; display: inline-flex; align-items: center; gap: 7px; font-size: 16px; }
.cd-x { padding: 3px 7px; }
.cd-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px 10px; }
.cd-field { display: flex; flex-direction: column; gap: 3px; }
.cd-wide { grid-column: 1 / -1; }
.cd-field label { font-size: 12px; color: var(--muted); }
.cd-hint { opacity: 0.7; font-weight: 400; }
.cd-note { font-size: 11.5px; margin: 0; }
.cd-test { display: flex; align-items: center; gap: 6px; font-size: 12px; margin: 0; }
.cd-test.ok { color: var(--accent-2); }
.cd-test.ko { color: var(--danger); }
.cd-err { color: var(--danger); font-size: 12px; margin: 0; }
.cd-actions { display: flex; align-items: center; gap: 8px; margin-top: 6px; }
.cd-spacer { flex: 1; }
</style>
