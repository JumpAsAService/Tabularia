<script setup lang="ts">
// Dialog di creazione/modifica di una connessione database, con test in-place.
// La password non viene mai mostrata: in modifica, campo vuoto = non cambiarla.
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { Plug, X, CheckCircle2, XCircle, LoaderCircle, KeyRound, ShieldCheck, ShieldAlert, TriangleAlert } from 'lucide-vue-next'
import { errMessage } from '~/composables/useApi'
import { useDialogA11y } from '~/composables/useDialogA11y'
import {
  useConnections,
  DB_TYPES,
  type ConnectionDraft,
  type DriverInfo,
  type ConnectionInfo,
  type HostKeyResult,
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
// SQL Server: come trattare il TLS (`extra.tls`)
const sqlTls = ref('verify')
const allowedDomains = ref('')

const isEdit = computed(() => !!props.existing)
// object storage: stesse colonne, etichette diverse (host=endpoint, ecc.)
const isS3 = computed(() => dbType.value === 's3')
const isSmtp = computed(() => dbType.value === 'smtp')
// SharePoint: stesse colonne, altre etichette (host=sito, username=client id,
// password=secret, database=tenant); la raccolta documenti vive in `extra`
const isSp = computed(() => dbType.value === 'sharepoint')
const library = ref('')

// SFTP: password OR private key (`extra.auth`), and the server's host key, accepted
// in two steps and never with a credential sent first: a test SHOWS a key that is
// not accepted (the engine closes without authenticating); the user compares it
// and presses «Accept this key», a second test naming the key, which only then
// sends the credentials. In the form of a new or changed connection the accepted
// key is kept and stored on save; on a saved connection it is stored at once.
const isSftp = computed(() => dbType.value === 'sftp')
const sftpAuth = ref<'password' | 'key'>('password')
const privateKey = ref('')
const passphrase = ref('')
// the key accepted in the form's test (credentials checked against it), stored on save
const seenKey = ref<{ type: string; fingerprint: string } | null>(null)
// a key the server offers that is not accepted: shown, waiting for «Accept this key»
const pendingKey = ref<{ type: string; fingerprint: string; changed: boolean; previous: string; saved: boolean } | null>(null)
// the key accepted by a test of the saved connection, while the dialog is open
const acceptedKey = ref<{ type: string; fingerprint: string } | null>(null)
const currentKey = computed(() =>
  acceptedKey.value ??
  (props.existing?.host_key_fingerprint
    ? { type: props.existing.host_key_type ?? '', fingerprint: props.existing.host_key_fingerprint }
    : null),
)

// HTTP API: host = the base URL; the secret of the chosen authentication goes in the
// password field (API key, token, password or client secret); the user in `username`
// (basic user, OAuth2 client id); the rest in `extra`. Default headers are edited as
// text, one «Name: value» per line. The secret and the headers are sent only to the
// base URL's host (the engine enforces it), so a new host needs the secret again.
type ApiAuth = 'none' | 'api_key' | 'bearer' | 'basic' | 'oauth2'
const API_AUTHS: ApiAuth[] = ['none', 'api_key', 'bearer', 'basic', 'oauth2']
const isApi = computed(() => dbType.value === 'api')
const apiAuth = ref<ApiAuth>('api_key')
const keyName = ref('')
const keyIn = ref<'header' | 'query'>('header')
const tokenUrl = ref('')
const scope = ref('')
const clientAuth = ref<'body' | 'basic'>('body')
const apiHeaders = ref('')
const testPath = ref('')
const apiUsesUser = computed(() => apiAuth.value === 'basic' || apiAuth.value === 'oauth2')

function headersToText(h: unknown): string {
  if (!h || typeof h !== 'object' || Array.isArray(h)) return ''
  return Object.entries(h as Record<string, string>).map(([k, v]) => `${k}: ${v}`).join('\n')
}
function textToHeaders(text: string): Record<string, string> {
  const out: Record<string, string> = {}
  for (const line of text.split('\n')) {
    const i = line.indexOf(':')
    if (i > 0) out[line.slice(0, i).trim()] = line.slice(i + 1).trim()
  }
  return out
}
function originOf(url: string): string {
  try { const u = new URL(url.trim()); return `${u.protocol}//${u.host}` } catch { return url.trim() }
}

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
    library.value = opts.library ?? ''
    fromAddress.value = opts.from_address ?? ''
    fromName.value = opts.from_name ?? ''
    tls.value = opts.tls ?? 'starttls'
    sqlTls.value = c?.db_type === 'sqlserver' ? (opts.tls ?? 'verify') : 'verify'
    allowedDomains.value = Array.isArray(opts.allowed_domains)
      ? opts.allowed_domains.join(', ')
      : (opts.allowed_domains ?? '')
    sftpAuth.value = opts.auth === 'key' ? 'key' : 'password'
    apiAuth.value = c?.db_type === 'api' && API_AUTHS.includes(opts.auth) ? opts.auth : 'api_key'
    keyName.value = opts.key_name ?? ''
    keyIn.value = opts.key_in === 'query' ? 'query' : 'header'
    tokenUrl.value = opts.token_url ?? ''
    scope.value = opts.scope ?? ''
    clientAuth.value = opts.client_auth === 'basic' ? 'basic' : 'body'
    apiHeaders.value = headersToText(opts.headers)
    testPath.value = opts.test_path ?? ''
    privateKey.value = '' // never shown, like the password
    passphrase.value = ''
    seenKey.value = null
    acceptedKey.value = null
    pendingKey.value = null
  },
)

// Optional drivers this installation lacks (SQL Server's is Microsoft's, not in the
// published images): the type stays selectable — a connection can be prepared before
// the driver is added — but the list and the form say it, with the guide. Asked once
// per page; an engine that does not answer marks nothing.
const drivers = ref<Record<string, DriverInfo>>({})
let driversAsked = false
watch(
  () => props.open,
  (open) => {
    if (!open || driversAsked) return
    driversAsked = true
    connApi.drivers().then((d) => { drivers.value = d ?? {} }).catch(() => { driversAsked = false })
  },
  { immediate: true },
)
const missingDriver = computed(() => (drivers.value[dbType.value]?.installed === false ? drivers.value[dbType.value] : null))
const typeOptions = computed(() =>
  DB_TYPES.map((o) => (drivers.value[o.value]?.installed === false ? { ...o, hint: t('connectionDialog.driverNotInstalledHint') } : o)),
)

// a key seen for one address says nothing about another
watch([host, port, username], () => { seenKey.value = null; pendingKey.value = null })

// la porta di default del tipo, come suggerimento (vuota = quella)
const DEFAULT_PORTS: Record<string, string> = {
  postgresql: '5432', mysql: '3306', mariadb: '3306', clickhouse: '8123', trino: '8080', oracle: '1521', sqlserver: '1433', smtp: '587',
  sftp: '22',
}
const portPlaceholder = computed(() => DEFAULT_PORTS[dbType.value] ?? '')

function draft(): ConnectionDraft {
  return {
    name: name.value.trim(),
    description: description.value,
    db_type: dbType.value,
    host: host.value.trim(),
    port: port.value.trim() && !isApi.value ? Number(port.value) : null,
    username: isApi.value && !apiUsesUser.value ? '' : username.value,
    // SFTP with the key method, or an API without authentication: the password field
    // is hidden, nothing to send
    password: (isSftp.value && sftpAuth.value === 'key') || (isApi.value && apiAuth.value === 'none') ? '' : password.value,
    database: database.value.trim(),
    db_schema: dbSchema.value.trim(),
    ...(isSftp.value && sftpAuth.value === 'key' && privateKey.value.trim() ? { private_key: privateKey.value } : {}),
    ...(isSftp.value && sftpAuth.value === 'key' && passphrase.value ? { private_key_passphrase: passphrase.value } : {}),
    ...(isSftp.value && seenKey.value ? { accept_host_key: seenKey.value.fingerprint, accept_host_key_type: seenKey.value.type } : {}),
    // inviato sempre: per gli altri tipi resta un oggetto vuoto, e ometterlo in
    // modifica vorrebbe dire «non toccarlo», che qui non è mai l'intenzione
    extra: JSON.stringify(
      isApi.value
        ? {
            auth: apiAuth.value,
            ...(apiAuth.value === 'api_key' ? { key_name: keyName.value.trim(), key_in: keyIn.value } : {}),
            ...(apiAuth.value === 'oauth2'
              ? { token_url: tokenUrl.value.trim(), scope: scope.value.trim(), client_auth: clientAuth.value }
              : {}),
            headers: textToHeaders(apiHeaders.value),
            test_path: testPath.value.trim(),
          }
        : isSftp.value
        ? { auth: sftpAuth.value }
        : isSmtp.value
        ? {
            from_address: fromAddress.value.trim(),
            from_name: fromName.value.trim(),
            tls: tls.value,
            allowed_domains: allowedDomains.value
              .split(/[,;\s]+/)
              .map((d) => d.trim())
              .filter(Boolean),
          }
        : isSp.value
          ? { library: library.value.trim() }
          : dbType.value === 'sqlserver'
            ? { tls: sqlTls.value }
            : {},
    ),
  }
}

// SFTP: a new host, port or user needs the secret typed again — a stored password
// must not follow the connection to another server or account (the gateway refuses it too)
const sftpMoved = computed(() => {
  const c = props.existing
  if (!isEdit.value || !isSftp.value || !c) return false
  return host.value.trim() !== c.host || (port.value.trim() ? Number(port.value) : null) !== (c.port ?? null) || username.value !== c.username
})
const needsSecretAgain = computed(() => sftpMoved.value && !(sftpAuth.value === 'key' ? privateKey.value.trim() : password.value))

// API: the stored secret does not follow the connection to another host (nor the
// OAuth2 client secret to another token URL): the gateway refuses it too
const apiMoved = computed(() => {
  const c = props.existing
  if (!isEdit.value || !isApi.value || !c || !c.has_password) return false
  let before: Record<string, any> = {}
  try { before = JSON.parse(c.extra || '{}') } catch { /* defaults */ }
  return originOf(host.value) !== originOf(c.host) ||
    (apiAuth.value === 'oauth2' && !!tokenUrl.value.trim() && tokenUrl.value.trim() !== (before.token_url ?? '').trim())
})
const apiNeedsSecretAgain = computed(() => apiMoved.value && !password.value)

// per S3 l'endpoint può essere vuoto (= AWS): basta il nome. Per SMTP serve
// anche il mittente: senza, la connessione si salva e poi ogni invio fallisce.
const incomplete = computed(
  () =>
    !name.value.trim() ||
    (!isS3.value && !host.value.trim()) ||
    (isSmtp.value && !fromAddress.value.trim()) ||
    (isSp.value && (!username.value.trim() || !database.value.trim())) ||
    // una SharePoint nuova senza secret si salverebbe e fallirebbe a ogni import
    (isSp.value && !isEdit.value && !password.value) ||
    // SFTP: a user, and the secret of the chosen method (stored, or typed now)
    (isSftp.value && !username.value.trim()) ||
    (isSftp.value && sftpAuth.value === 'password' && !password.value && !(isEdit.value && props.existing?.has_password)) ||
    (isSftp.value && sftpAuth.value === 'key' && !privateKey.value.trim() && !(isEdit.value && props.existing?.has_private_key)) ||
    needsSecretAgain.value ||
    // API: what the chosen authentication needs, and its secret (stored, or typed now)
    (isApi.value && apiAuth.value === 'api_key' && !keyName.value.trim()) ||
    (isApi.value && apiUsesUser.value && !username.value.trim()) ||
    (isApi.value && apiAuth.value === 'oauth2' && !tokenUrl.value.trim()) ||
    (isApi.value && apiAuth.value !== 'none' && !password.value && !(isEdit.value && props.existing?.has_password)) ||
    apiNeedsSecretAgain.value,
)

// cambiando tipo, i campi che l'altro tipo non mostra non devono viaggiare nel
// draft (porta e schema di un Postgres dentro una SharePoint, il tenant come
// database di un Postgres). Only when the USER changes the type: as a watcher it
// also fired when the dialog opened another connection, and blanked its database
// and schema, which Save then stored.
function changeType(nuovo: string) {
  const vecchio = dbType.value
  dbType.value = nuovo
  if (nuovo !== vecchio && (['sharepoint', 'sftp', 'api'].includes(nuovo) || ['sharepoint', 'sftp', 'api'].includes(vecchio))) {
    port.value = ''
    dbSchema.value = ''
    database.value = ''
    library.value = ''
  }
}

// ── Test connection ──────────────────────────────────────────────────────────
const testing = ref(false)
const testResult = ref<{ ok: boolean; message: string } | null>(null)

// SFTP: has the form changed anything a saved test would not see?
function sftpChanged(): boolean {
  const c = props.existing
  if (!c) return true
  let auth = 'password'
  try { auth = JSON.parse(c.extra || '{}').auth === 'key' ? 'key' : 'password' } catch { /* default */ }
  return (
    host.value.trim() !== c.host ||
    (port.value.trim() ? Number(port.value) : null) !== (c.port ?? null) ||
    username.value !== c.username ||
    sftpAuth.value !== auth ||
    !!password.value || !!privateKey.value.trim() || !!passphrase.value
  )
}

function showKey(res: HostKeyResult, saved: boolean) {
  const k = res.host_key!
  pendingKey.value = { ...k, changed: !!res.changed, previous: res.previous ?? '', saved }
  testResult.value = null
}

async function testSftp() {
  pendingKey.value = null
  if (isEdit.value && !sftpChanged()) {
    // the saved connection as it is: its accepted key lets the credentials go;
    // any other key is only shown
    const res = await connApi.test(props.existing!.id)
    if (res.needs_acceptance) return showKey(res, true)
    testResult.value = { ok: true, message: t('sftp.hostKeyVerified', { type: res.host_key!.type, fingerprint: res.host_key!.fingerprint }) }
    return
  }
  // not saved yet, or changed: the secret typed in the form, and the key accepted
  // already when the address did not change
  const c = props.existing
  const known = c && !sftpMoved.value && c.host_key_fingerprint
    ? { expected_fingerprint: c.host_key_fingerprint, expected_type: c.host_key_type }
    : seenKey.value ? { expected_fingerprint: seenKey.value.fingerprint, expected_type: seenKey.value.type } : undefined
  const res = await connApi.testDraft(props.projectId, draft(), known)
  if (res.needs_acceptance) return showKey(res, false)
  testResult.value = { ok: true, message: t('connectionDialog.testOk') }
}

// «Accept this key»: a second test naming the key; the credentials leave only if
// the server still offers exactly that key
async function acceptKey() {
  const k = pendingKey.value
  if (!k) return
  testing.value = true
  testResult.value = null
  try {
    const key = { expected_fingerprint: k.fingerprint, expected_type: k.type }
    if (k.saved) {
      await connApi.test(props.existing!.id, key)
      acceptedKey.value = { type: k.type, fingerprint: k.fingerprint }
      testResult.value = {
        ok: true,
        message: k.changed
          ? t('sftp.hostKeyChangedAccepted', { previous: k.previous, type: k.type, fingerprint: k.fingerprint })
          : t('sftp.hostKeyNowAccepted', { type: k.type, fingerprint: k.fingerprint }),
      }
    } else {
      await connApi.testDraft(props.projectId, draft(), key)
      seenKey.value = { type: k.type, fingerprint: k.fingerprint }
      testResult.value = { ok: true, message: t('sftp.hostKeyAcceptedOnSave', { type: k.type, fingerprint: k.fingerprint }) }
    }
    pendingKey.value = null
  } catch (e) {
    testResult.value = { ok: false, message: errMessage(e) }
  } finally {
    testing.value = false
  }
}

async function test() {
  testing.value = true
  testResult.value = null
  try {
    if (isSftp.value) {
      await testSftp()
      return
    }
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
        v-label-fields
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
          <div class="cd-field" :class="{ 'cd-wide': isS3 || isSp || isApi }">
            <label>{{ $t('connectionDialog.typeLabel') }}</label>
            <Select :model-value="dbType" :options="typeOptions" @update:model-value="changeType" />
          </div>
          <div v-if="!isS3 && !isSp && !isApi" class="cd-field">
            <label>{{ $t('connectionDialog.portLabel') }} <span class="cd-hint">{{ $t('connectionDialog.portHintDefault') }}</span></label>
            <input v-model="port" type="text" inputmode="numeric" :placeholder="portPlaceholder" />
          </div>
          <div v-if="missingDriver" class="cd-field cd-wide cd-pending" role="status">
            <p class="cd-pending-title"><TriangleAlert :size="14" /> {{ $t('connectionDialog.driverNotInstalledTitle') }}</p>
            <p>{{ $t('connectionDialog.driverNotInstalledBody') }}</p>
            <p v-if="missingDriver.docs">
              <a :href="missingDriver.docs" target="_blank" rel="noopener noreferrer">{{ $t('connectionDialog.driverNotInstalledLink') }}</a>
            </p>
          </div>
          <div class="cd-field cd-wide">
            <label>{{ isSp ? $t('sharepoint.siteLabel') : isS3 ? $t('connectionDialog.endpointLabel') : isSmtp ? $t('connectionDialog.smtpHostLabel') : isSftp ? $t('sftp.hostLabel') : isApi ? $t('apiSource.baseUrlLabel') : $t('connectionDialog.hostLabel') }} <span v-if="isS3" class="cd-hint">{{ $t('connectionDialog.hostHintAws') }}</span></label>
            <input
              v-model="host"
              type="text"
              :placeholder="isSp ? 'https://contoso.sharepoint.com/sites/Finance' : isS3 ? 'https://minio.example.com:9000' : isSmtp ? 'smtp.example.com' : isSftp ? 'sftp.example.com' : isApi ? 'https://api.example.com/v1' : 'db.internal.example.com'"
            />
          </div>
          <div v-if="isApi" class="cd-field">
            <label>{{ $t('apiSource.authLabel') }}</label>
            <Select
              v-model="apiAuth"
              :options="[
                { value: 'none', label: $t('apiSource.authNone') },
                { value: 'api_key', label: $t('apiSource.authApiKey') },
                { value: 'bearer', label: $t('apiSource.authBearer') },
                { value: 'basic', label: $t('apiSource.authBasic') },
                { value: 'oauth2', label: $t('apiSource.authOauth2') },
              ]"
            />
          </div>
          <div v-if="!isApi || apiUsesUser" class="cd-field">
            <label>{{ isSp ? $t('sharepoint.clientIdLabel') : isS3 ? $t('connectionDialog.accessKeyLabel') : isApi && apiAuth === 'oauth2' ? $t('apiSource.clientIdLabel') : $t('connectionDialog.usernameLabel') }}</label>
            <input v-model="username" type="text" autocomplete="off" />
          </div>
          <div v-if="isSftp" class="cd-field">
            <label>{{ $t('sftp.authLabel') }}</label>
            <Select
              v-model="sftpAuth"
              :options="[
                { value: 'password', label: $t('sftp.authPassword') },
                { value: 'key', label: $t('sftp.authKey') },
              ]"
            />
          </div>
          <div v-if="(!isSftp || sftpAuth === 'password') && !(isApi && apiAuth === 'none')" class="cd-field">
            <label>
              {{ isSp ? $t('sharepoint.secretLabel') : isS3 ? $t('connectionDialog.secretKeyLabel') : isApi ? $t(`apiSource.secret_${apiAuth}`) : $t('connectionDialog.passwordLabel') }}
              <span v-if="isEdit" class="cd-hint">{{ $t('connectionDialog.passwordHintUnchanged') }}</span>
            </label>
            <input v-model="password" type="password" autocomplete="new-password" />
          </div>
          <!-- SMTP non ha database né schema: al loro posto le opzioni che per
               gli altri tipi non esistono -->
          <template v-if="isSmtp">
            <div class="cd-field">
              <label>{{ $t('connectionDialog.fromAddressLabel') }}</label>
              <input v-model="fromAddress" type="text" placeholder="reports@example.com" />
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
              <input v-model="allowedDomains" type="text" placeholder="example.com, customers.example.com" />
            </div>
          </template>
          <template v-else-if="isApi">
            <template v-if="apiAuth === 'api_key'">
              <div class="cd-field">
                <label>{{ $t('apiSource.keyNameLabel') }}</label>
                <input v-model="keyName" type="text" spellcheck="false" autocomplete="off" :placeholder="keyIn === 'query' ? 'apikey' : 'X-API-Key'" />
              </div>
              <div class="cd-field">
                <label>{{ $t('apiSource.keyInLabel') }}</label>
                <Select
                  v-model="keyIn"
                  :options="[
                    { value: 'header', label: $t('apiSource.keyInHeader') },
                    { value: 'query', label: $t('apiSource.keyInQuery') },
                  ]"
                />
              </div>
            </template>
            <template v-if="apiAuth === 'oauth2'">
              <div class="cd-field cd-wide">
                <label>{{ $t('apiSource.tokenUrlLabel') }}</label>
                <input v-model="tokenUrl" type="text" spellcheck="false" placeholder="https://login.example.com/oauth2/token" />
              </div>
              <div class="cd-field">
                <label>{{ $t('apiSource.scopeLabel') }} <span class="cd-hint">{{ $t('connectionDialog.optionalHint') }}</span></label>
                <input v-model="scope" type="text" spellcheck="false" />
              </div>
              <div class="cd-field">
                <label>{{ $t('apiSource.clientAuthLabel') }}</label>
                <Select
                  v-model="clientAuth"
                  :options="[
                    { value: 'body', label: $t('apiSource.clientAuthBody') },
                    { value: 'basic', label: $t('apiSource.clientAuthBasic') },
                  ]"
                />
              </div>
            </template>
            <div class="cd-field cd-wide">
              <label>{{ $t('apiSource.defaultHeadersLabel') }} <span class="cd-hint">{{ $t('apiSource.defaultHeadersHint') }}</span></label>
              <textarea v-model="apiHeaders" rows="2" spellcheck="false" class="cd-key" placeholder="Accept: application/json" />
            </div>
            <div class="cd-field cd-wide">
              <label>{{ $t('apiSource.testPathLabel') }} <span class="cd-hint">{{ $t('apiSource.testPathHint') }}</span></label>
              <input v-model="testPath" type="text" spellcheck="false" placeholder="/status" />
            </div>
            <p v-if="apiNeedsSecretAgain" class="cd-hostkey none cd-wide">{{ $t('apiSource.retypeSecret') }}</p>
          </template>
          <template v-else-if="isSftp">
            <template v-if="sftpAuth === 'key'">
              <div class="cd-field cd-wide">
                <label>
                  {{ $t('sftp.privateKeyLabel') }}
                  <span class="cd-hint">{{ isEdit && existing?.has_private_key ? $t('sftp.privateKeyKept') : $t('sftp.privateKeyHint') }}</span>
                </label>
                <textarea
                  v-model="privateKey"
                  rows="4"
                  spellcheck="false"
                  autocomplete="off"
                  class="cd-key"
                  placeholder="-----BEGIN OPENSSH PRIVATE KEY-----"
                />
              </div>
              <div class="cd-field cd-wide">
                <label>{{ $t('sftp.passphraseLabel') }} <span class="cd-hint">{{ $t('sftp.passphraseHint') }}</span></label>
                <input v-model="passphrase" type="password" autocomplete="new-password" />
              </div>
            </template>
            <div class="cd-field cd-wide">
              <label>{{ $t('sftp.hostKeyLabel') }}</label>
              <p v-if="currentKey" class="cd-hostkey ok">
                <ShieldCheck :size="13" />
                <span>{{ $t('sftp.hostKeyAccepted') }} <code>{{ currentKey.type }} {{ currentKey.fingerprint }}</code></span>
              </p>
              <p v-else class="cd-hostkey none">
                <ShieldAlert :size="13" />
                <span>{{ $t('sftp.hostKeyNone') }}</span>
              </p>
              <p v-if="needsSecretAgain" class="cd-hostkey none">{{ $t('sftp.retypeSecret') }}</p>
            </div>
            <div v-if="pendingKey" class="cd-field cd-wide cd-pending" :class="{ changed: pendingKey.changed }" role="alert">
              <p v-if="pendingKey.changed" class="cd-pending-title">
                <ShieldAlert :size="14" /> {{ $t('sftp.hostKeyChangedWarning', { previous: pendingKey.previous }) }}
              </p>
              <p>{{ $t('sftp.hostKeyOffered') }} <code>{{ pendingKey.type }} {{ pendingKey.fingerprint }}</code></p>
              <p class="muted">{{ $t('sftp.hostKeyCompare') }}</p>
              <button type="button" class="cd-accept" :disabled="testing" @click="acceptKey">
                <KeyRound :size="13" /> {{ $t('sftp.acceptKey') }}
              </button>
            </div>
          </template>
          <template v-else-if="isSp">
            <div class="cd-field">
              <label>{{ $t('sharepoint.tenantLabel') }}</label>
              <input v-model="database" type="text" autocomplete="off" placeholder="00000000-0000-0000-0000-000000000000" />
            </div>
            <div class="cd-field">
              <label>{{ $t('sharepoint.libraryLabel') }} <span class="cd-hint">{{ $t('connectionDialog.optionalHint') }}</span></label>
              <input v-model="library" type="text" :placeholder="$t('sharepoint.libraryPlaceholder')" />
            </div>
          </template>
          <template v-else>
            <div class="cd-field">
              <label>
                {{ isS3 ? $t('connectionDialog.bucketLabel') : dbType === 'trino' ? $t('connectionDialog.catalogLabel') : dbType === 'oracle' ? $t('connectionDialog.serviceNameLabel') : $t('connectionDialog.databaseLabel') }}
                <span v-if="isS3" class="cd-hint">{{ $t('connectionDialog.optionalHint') }}</span>
              </label>
              <input v-model="database" type="text" :placeholder="isS3 ? $t('connectionDialog.bucketPlaceholder') : dbType === 'oracle' ? 'ORCLPDB1' : ''" />
            </div>
            <div class="cd-field">
              <label>{{ isS3 ? $t('connectionDialog.regionLabel') : $t('connectionDialog.schemaLabel') }} <span class="cd-hint">{{ $t('connectionDialog.optionalHint') }}</span></label>
              <input
                v-model="dbSchema"
                type="text"
                :placeholder="isS3 ? 'eu-south-1' : dbType === 'postgresql' ? 'public' : dbType === 'oracle' ? 'HR' : dbType === 'sqlserver' ? 'dbo' : ''"
              />
            </div>
            <div v-if="dbType === 'sqlserver'" class="cd-field cd-wide">
              <label>{{ $t('connectionDialog.sqlTlsLabel') }}</label>
              <Select
                v-model="sqlTls"
                :options="[
                  { value: 'verify', label: $t('connectionDialog.sqlTlsVerify') },
                  { value: 'trust', label: $t('connectionDialog.sqlTlsTrust') },
                  { value: 'none', label: $t('connectionDialog.sqlTlsNone') },
                ]"
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
          <template v-else-if="isSp">
            {{ $t('sharepoint.note') }}
          </template>
          <template v-else-if="isSftp">
            {{ $t('sftp.note') }}
          </template>
          <template v-else-if="isApi">
            {{ $t('apiSource.note') }}
          </template>
          <template v-else>
            {{ $t('connectionDialog.noteDb') }}
          </template>
        </p>

        <p v-if="testResult" class="cd-test" :class="testResult.ok ? 'ok' : 'ko'">
          <KeyRound v-if="testResult.ok && isSftp" :size="14" />
          <CheckCircle2 v-else-if="testResult.ok" :size="14" />
          <XCircle v-else :size="14" />
          <span>{{ testResult.message }}</span>
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
.cd-test { display: flex; align-items: flex-start; gap: 6px; font-size: 12px; margin: 0; overflow-wrap: anywhere; }
.cd-test svg { flex: none; margin-top: 1px; }
.cd-key { resize: vertical; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11.5px; }
.cd-hostkey { display: flex; align-items: flex-start; gap: 6px; font-size: 12px; margin: 0; overflow-wrap: anywhere; }
.cd-hostkey svg { flex: none; margin-top: 1px; }
.cd-hostkey.ok { color: var(--accent-2); }
.cd-hostkey.none { color: var(--muted); }
.cd-hostkey code { font-size: 11.5px; }
.cd-pending { border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px; font-size: 12px; gap: 6px; }
.cd-pending.changed { border-color: var(--danger); }
.cd-pending p { margin: 0; overflow-wrap: anywhere; }
.cd-pending code { font-size: 11.5px; }
.cd-pending-title { display: flex; align-items: flex-start; gap: 6px; color: var(--danger); font-weight: 600; }
.cd-pending-title svg { flex: none; margin-top: 1px; }
.cd-accept { align-self: flex-start; display: inline-flex; align-items: center; gap: 6px; }
.cd-test.ok { color: var(--accent-2); }
.cd-test.ko { color: var(--danger); }
.cd-err { color: var(--danger); font-size: 12px; margin: 0; }
.cd-actions { display: flex; align-items: center; gap: 8px; margin-top: 6px; }
.cd-spacer { flex: 1; }
</style>
