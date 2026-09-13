<script setup lang="ts">
// Dialog di creazione/modifica di una connessione database, con test in-place.
// La password non viene mai mostrata: in modifica, campo vuoto = non cambiarla.
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { Plug, X, CheckCircle2, XCircle, LoaderCircle } from 'lucide-vue-next'
import { errMessage } from '~/composables/useApi'
import { useDialogA11y } from '~/composables/useDialogA11y'
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
const { t } = useI18n()

// fuoco iniziale sulla card, Tab confinato, Esc funzionante, fuoco restituito
const card = ref<HTMLElement | null>(null)
useDialogA11y(card, () => props.open, () => emit('cancel'))

const name = ref('')
const description = ref('')
const dbType = ref('postgresql')
const host = ref('')
const port = ref<string>('')
const username = ref('')
const password = ref('')
const database = ref('')
const dbSchema = ref('')

// SMTP: mittente, cifratura e domini ammessi non hanno una colonna propria e
// viaggiano nel campo `extra` (JSON). Qui restano quattro campi normali.
const fromAddress = ref('')
const fromName = ref('')
const tls = ref('starttls')
const allowedDomains = ref('')

const isEdit = computed(() => !!props.existing)
// object storage: stesse colonne, etichette diverse (host=endpoint, ecc.)
const isS3 = computed(() => dbType.value === 's3')
const isSmtp = computed(() => dbType.value === 'smtp')

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
    // `extra` è JSON opaco: se una connessione salvata a mano lo avesse rotto,
    // il form deve comunque aprirsi — si riparte dai default
    let opts: Record<string, any> = {}
    try {
      const parsed = JSON.parse(c?.extra || '{}')
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) opts = parsed
    } catch { /* JSON illeggibile: campi vuoti */ }
    fromAddress.value = opts.from_address ?? ''
    fromName.value = opts.from_name ?? ''
    tls.value = opts.tls ?? 'starttls'
    allowedDomains.value = Array.isArray(opts.allowed_domains)
      ? opts.allowed_domains.join(', ')
      : (opts.allowed_domains ?? '')
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
    // inviato sempre: per gli altri tipi resta un oggetto vuoto, e ometterlo in
    // modifica vorrebbe dire «non toccarlo», che qui non è mai l'intenzione
    extra: JSON.stringify(
      isSmtp.value
        ? {
            from_address: fromAddress.value.trim(),
            from_name: fromName.value.trim(),
            tls: tls.value,
            allowed_domains: allowedDomains.value
              .split(/[,;\s]+/)
              .map((d) => d.trim())
              .filter(Boolean),
          }
        : {},
    ),
  }
}

// per S3 l'endpoint può essere vuoto (= AWS): basta il nome. Per SMTP serve
// anche il mittente: senza, la connessione si salva e poi ogni invio fallisce.
const incomplete = computed(
  () =>
    !name.value.trim() ||
    (!isS3.value && !host.value.trim()) ||
    (isSmtp.value && !fromAddress.value.trim()),
)

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
    testResult.value = { ok: true, message: t('connectionDialog.testOk') }
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
      <div
        ref="card"
        class="cd-card"
        role="dialog"
        aria-modal="true"
        aria-labelledby="cd-title"
        tabindex="-1"
      >
        <div class="cd-head">
          <h3 id="cd-title"><Plug :size="15" /> {{ isEdit ? $t('connectionDialog.titleEdit') : $t('connectionDialog.titleNew') }}</h3>
          <button class="cd-x" @click="emit('cancel')"><X :size="14" /></button>
        </div>

        <div class="cd-grid">
          <div class="cd-field cd-wide">
            <label>{{ $t('connectionDialog.nameLabel') }}</label>
            <input v-model="name" type="text" :placeholder="$t('connectionDialog.namePlaceholder')" />
          </div>
          <div class="cd-field" :class="{ 'cd-wide': isS3 }">
            <label>{{ $t('connectionDialog.typeLabel') }}</label>
            <Select v-model="dbType" :options="DB_TYPES" />
          </div>
          <div v-if="!isS3" class="cd-field">
            <label>{{ $t('connectionDialog.portLabel') }} <span class="cd-hint">{{ $t('connectionDialog.portHintDefault') }}</span></label>
            <input v-model="port" type="text" inputmode="numeric" :placeholder="isSmtp ? '587' : '5432'" />
          </div>
          <div class="cd-field cd-wide">
            <label>{{ isS3 ? $t('connectionDialog.endpointLabel') : isSmtp ? $t('connectionDialog.smtpHostLabel') : $t('connectionDialog.hostLabel') }} <span v-if="isS3" class="cd-hint">{{ $t('connectionDialog.hostHintAws') }}</span></label>
            <input
              v-model="host"
              type="text"
              :placeholder="isS3 ? 'https://minio.example.com:9000' : isSmtp ? 'smtp.azienda.it' : 'db.internal.example.com'"
            />
          </div>
          <div class="cd-field">
            <label>{{ isS3 ? $t('connectionDialog.accessKeyLabel') : $t('connectionDialog.usernameLabel') }}</label>
            <input v-model="username" type="text" autocomplete="off" />
          </div>
          <div class="cd-field">
            <label>
              {{ isS3 ? $t('connectionDialog.secretKeyLabel') : $t('connectionDialog.passwordLabel') }}
              <span v-if="isEdit" class="cd-hint">{{ $t('connectionDialog.passwordHintUnchanged') }}</span>
            </label>
            <input v-model="password" type="password" autocomplete="new-password" />
          </div>
          <!-- SMTP non ha database né schema: al loro posto le opzioni che per
               gli altri tipi non esistono -->
          <template v-if="isSmtp">
            <div class="cd-field">
              <label>{{ $t('connectionDialog.fromAddressLabel') }}</label>
              <input v-model="fromAddress" type="text" placeholder="report@azienda.it" />
            </div>
            <div class="cd-field">
              <label>{{ $t('connectionDialog.fromNameLabel') }} <span class="cd-hint">{{ $t('connectionDialog.optionalHint') }}</span></label>
              <input v-model="fromName" type="text" :placeholder="$t('connectionDialog.fromNamePlaceholder')" />
            </div>
            <div class="cd-field">
              <label>{{ $t('connectionDialog.tlsLabel') }}</label>
              <Select
                v-model="tls"
                :options="[
                  { value: 'starttls', label: 'STARTTLS (587)' },
                  { value: 'ssl', label: 'SSL/TLS (465)' },
                  { value: 'none', label: $t('connectionDialog.tlsNone') },
                ]"
              />
            </div>
            <div class="cd-field">
              <label>{{ $t('connectionDialog.allowedDomainsLabel') }} <span class="cd-hint">{{ $t('connectionDialog.allowedDomainsHint') }}</span></label>
              <input v-model="allowedDomains" type="text" placeholder="azienda.it, clienti.it" />
            </div>
          </template>
          <template v-else>
            <div class="cd-field">
              <label>
                {{ isS3 ? $t('connectionDialog.bucketLabel') : dbType === 'trino' ? $t('connectionDialog.catalogLabel') : $t('connectionDialog.databaseLabel') }}
                <span v-if="isS3" class="cd-hint">{{ $t('connectionDialog.optionalHint') }}</span>
              </label>
              <input v-model="database" type="text" :placeholder="isS3 ? $t('connectionDialog.bucketPlaceholder') : ''" />
            </div>
            <div class="cd-field">
              <label>{{ isS3 ? $t('connectionDialog.regionLabel') : $t('connectionDialog.schemaLabel') }} <span class="cd-hint">{{ $t('connectionDialog.optionalHint') }}</span></label>
              <input
                v-model="dbSchema"
                type="text"
                :placeholder="isS3 ? 'eu-south-1' : dbType === 'postgresql' ? 'public' : ''"
              />
            </div>
          </template>
          <div class="cd-field cd-wide">
            <label>{{ $t('connectionDialog.descriptionLabel') }} <span class="cd-hint">{{ $t('connectionDialog.optionalHint') }}</span></label>
            <input v-model="description" type="text" :placeholder="$t('connectionDialog.descriptionPlaceholder')" />
          </div>
        </div>

        <p class="muted cd-note">
          <template v-if="isS3">
            {{ $t('connectionDialog.noteS3') }}
          </template>
          <template v-else-if="isSmtp">
            {{ $t('connectionDialog.noteSmtp') }}
          </template>
          <template v-else>
            {{ $t('connectionDialog.noteDb') }}
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
            {{ $t('connectionDialog.testButton') }}
          </button>
          <span class="cd-spacer" />
          <button @click="emit('cancel')">{{ $t('connectionDialog.cancelButton') }}</button>
          <button class="primary" :disabled="incomplete || busy" @click="confirm">
            {{ isEdit ? $t('connectionDialog.saveButton') : $t('connectionDialog.createButton') }}
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
  background: var(--scrim);
  backdrop-filter: blur(2px);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 2000;
}
.cd-card {
  /* larghezza fissa = traboccava sotto i 520px di viewport; il max-height serve
     perché questo dialogo ha dieci campi e su schermo basso usciva in verticale */
  width: min(520px, calc(100vw - 32px));
  max-height: calc(100vh - 32px);
  overflow-y: auto;
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
