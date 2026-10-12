<script setup lang="ts">
// Account di servizio (pannello Admin): le integrazioni che entrano con un token,
// i loro gruppi (da cui prendono i permessi, come le persone) e i loro token. In
// fondo TUTTI i token API dell'installazione, per ritirare quello che è trapelato.
// L'osservatore vede e non tocca (i comandi spariscono: il server direbbe 403).
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { Bot, ChevronDown, ChevronRight, KeyRound, Plus, Trash2, X } from 'lucide-vue-next'
import Select from '~/components/ui/Select.vue'
import SecretOnce from '~/components/ui/SecretOnce.vue'
import TokenForm from '~/components/ui/TokenForm.vue'
import { errMessage } from '~/composables/useApi'
import { useFormat } from '~/composables/useFormat'
import { useProjects, type GroupOut } from '~/composables/useProjects'
import {
  useIntegrations, type AdminApiToken, type ApiToken, type ServiceAccount, type TokenRequest,
} from '~/composables/useIntegrations'

const props = defineProps<{ canWrite: boolean; groups: GroupOut[] }>()
const emit = defineEmits<{ (e: 'count', n: number): void }>()

const { t } = useI18n()
const fmt = useFormat()
const api = useIntegrations()
const projectsApi = useProjects()
const toast = useToast()

const accounts = ref<ServiceAccount[]>([])
const allTokens = ref<AdminApiToken[]>([])
const nuovoNome = ref('')
const busy = ref(false)
const aperto = ref<number | null>(null)
const tokensDi = ref<ApiToken[]>([])
// il segreto appena creato, con l'account a cui appartiene: se intanto si apre
// un altro account non gli compare sotto col nome sbagliato
const segreto = ref<{ account: number; value: string } | null>(null)

async function load() {
  try {
    ;[accounts.value, allTokens.value] = await Promise.all([api.serviceAccounts(), api.allTokens()])
    emit('count', accounts.value.length)
  } catch (e) {
    toast.error(errMessage(e))
  }
}
onMounted(load)

async function crea() {
  if (!nuovoNome.value.trim()) return
  busy.value = true
  try {
    const acc = await api.createServiceAccount(nuovoNome.value.trim())
    nuovoNome.value = ''
    await load()
    await apri(acc.id)
  } catch (e) {
    toast.error(errMessage(e))
  } finally {
    busy.value = false
  }
}

async function elimina(a: ServiceAccount) {
  if (!confirm(t('integrations.confirmDeleteAccount', { name: a.name }))) return
  try {
    await api.deleteServiceAccount(a.id)
    if (aperto.value === a.id) aperto.value = null
    await load()
  } catch (e) {
    toast.error(errMessage(e))
  }
}

async function apri(id: number) {
  segreto.value = null
  if (aperto.value === id) {
    aperto.value = null
    return
  }
  aperto.value = id
  try {
    tokensDi.value = await api.accountTokens(id)
  } catch (e) {
    toast.error(errMessage(e))
  }
}

async function creaToken(a: ServiceAccount, body: TokenRequest) {
  busy.value = true
  try {
    segreto.value = { account: a.id, value: (await api.createAccountToken(a.id, body)).token }
    if (aperto.value === a.id) tokensDi.value = await api.accountTokens(a.id)
    await load()
  } catch (e) {
    toast.error(errMessage(e))
  } finally {
    busy.value = false
  }
}

async function revocaToken(a: ServiceAccount, tk: ApiToken) {
  if (!confirm(t('integrations.confirmRevoke', { name: tk.name }))) return
  busy.value = true
  try {
    await api.revokeAccountToken(a.id, tk.id)
    tokensDi.value = tokensDi.value.filter((x) => x.id !== tk.id)
    await load()
  } catch (e) {
    toast.error(errMessage(e))
  } finally {
    busy.value = false
  }
}

async function revocaQualsiasi(tk: AdminApiToken) {
  if (!confirm(t('integrations.confirmRevokeOf', { name: tk.name, who: tk.user_name || tk.user_email }))) return
  try {
    await api.revokeAnyToken(tk.id)
    await load()
    if (aperto.value != null) tokensDi.value = await api.accountTokens(aperto.value)
  } catch (e) {
    toast.error(errMessage(e))
  }
}

// gruppi: è da lì che un account prende i permessi sulle cartelle
function gruppiDaAggiungere(a: ServiceAccount) {
  return props.groups.filter((g) => !a.groups.includes(g.name)).map((g) => ({ value: g.id, label: g.name }))
}
async function aggiungiGruppo(a: ServiceAccount, gid: number) {
  try {
    await projectsApi.addToGroup(a.id, gid)
    await load()
  } catch (e) {
    toast.error(errMessage(e))
  }
}
async function togliGruppo(a: ServiceAccount, nome: string) {
  const g = props.groups.find((x) => x.name === nome)
  if (!g) return
  try {
    await projectsApi.removeFromGroup(a.id, g.id)
    await load()
  } catch (e) {
    toast.error(errMessage(e))
  }
}

const umani = computed(() => allTokens.value.filter((tk) => !tk.is_service).length)
</script>

<template>
  <div class="card">
    <h4><Bot :size="14" /> {{ $t('integrations.serviceAccountsTitle') }} <span class="muted">{{ accounts.length }}</span></h4>
    <p class="hint muted small">{{ $t('integrations.serviceAccountsLead') }}</p>

    <form v-if="canWrite" class="newrow" @submit.prevent="crea">
      <input v-model="nuovoNome" type="text" maxlength="80" :placeholder="$t('integrations.accountNamePlaceholder')" :aria-label="$t('integrations.accountNamePlaceholder')" />
      <button class="primary" type="submit" :disabled="busy || !nuovoNome.trim()"><Plus :size="14" /> {{ $t('integrations.createAccount') }}</button>
    </form>

    <table class="rows">
      <thead>
        <tr>
          <th>{{ $t('adminPanel.colName') }}</th>
          <th>{{ $t('integrations.colGroups') }}</th>
          <th>{{ $t('integrations.colTokens') }}</th>
          <th>{{ $t('integrations.lastUsed') }}</th>
          <th />
        </tr>
      </thead>
      <tbody>
        <template v-for="a in accounts" :key="a.id">
          <tr>
            <td>
              <button type="button" class="rowpick" :aria-expanded="aperto === a.id" :disabled="busy" @click="apri(a.id)">
                <ChevronDown v-if="aperto === a.id" :size="13" /><ChevronRight v-else :size="13" /> {{ a.name }}
              </button>
              <div class="muted small mono">{{ a.email }}</div>
            </td>
            <td>
              <span v-for="g in a.groups" :key="g" class="chip">
                {{ g }}
                <button v-if="canWrite" type="button" class="chip-x" :aria-label="$t('integrations.removeGroupAria', { group: g })" @click="togliGruppo(a, g)"><X :size="10" /></button>
              </span>
              <Select
                v-if="canWrite && gruppiDaAggiungere(a).length"
                class="addgroup"
                :model-value="null"
                :options="gruppiDaAggiungere(a)"
                :placeholder="$t('integrations.addGroup')"
                @update:model-value="aggiungiGruppo(a, Number($event))"
              />
              <span v-if="!a.groups.length && !canWrite" class="muted small">—</span>
            </td>
            <td class="small">{{ a.tokens }}</td>
            <td class="muted small nowrap">{{ a.last_seen_at ? fmt.ago(a.last_seen_at) : $t('integrations.never') }}</td>
            <td class="right nowrap">
              <button class="mini" :title="$t('integrations.tokensOf', { name: a.name })" :aria-label="$t('integrations.tokensOf', { name: a.name })" :disabled="busy" @click="apri(a.id)"><KeyRound :size="13" /></button>
              <button v-if="canWrite" class="mini danger" :title="$t('integrations.deleteAccount')" :aria-label="$t('integrations.deleteAccountAria', { name: a.name })" @click="elimina(a)"><Trash2 :size="13" /></button>
            </td>
          </tr>
          <tr v-if="aperto === a.id" class="detail">
            <td colspan="5">
              <SecretOnce v-if="segreto?.account === a.id" :value="segreto.value" :label="$t('integrations.newTokenFor', { name: a.name })" @done="segreto = null" />
              <TokenForm v-else-if="canWrite" :busy="busy" @create="creaToken(a, $event)" />
              <ul v-if="tokensDi.length" class="toklist">
                <li v-for="tk in tokensDi" :key="tk.id">
                  <span class="tk-name">{{ tk.name }}</span>
                  <code class="muted">{{ tk.prefix }}…</code>
                  <span class="tag" :class="tk.scope">{{ tk.scope === 'write' ? $t('integrations.scopeWrite') : $t('integrations.scopeRead') }}</span>
                  <span class="muted small">{{ $t('integrations.expiresOn', { when: fmt.date(tk.expires_at) }) }}</span>
                  <span class="muted small">{{ tk.last_used_at ? $t('integrations.usedAgo', { when: fmt.ago(tk.last_used_at) }) : $t('integrations.never') }}</span>
                  <button v-if="canWrite" class="mini danger" :disabled="busy" :aria-label="$t('integrations.revokeAria', { name: tk.name })" :title="$t('integrations.revoke')" @click="revocaToken(a, tk)"><Trash2 :size="12" /></button>
                </li>
              </ul>
              <p v-else class="muted small">{{ $t('integrations.noTokens') }}</p>
            </td>
          </tr>
        </template>
        <tr v-if="!accounts.length">
          <td colspan="5" class="muted">{{ $t('integrations.noAccounts') }}</td>
        </tr>
      </tbody>
    </table>
  </div>

  <div class="card">
    <h4><KeyRound :size="14" /> {{ $t('integrations.allTokensTitle') }} <span class="muted">{{ allTokens.length }}</span></h4>
    <p class="hint muted small">{{ $t('integrations.allTokensLead', { personal: umani }) }}</p>
    <table class="rows">
      <thead>
        <tr>
          <th>{{ $t('integrations.colOwner') }}</th>
          <th>{{ $t('integrations.tokenName') }}</th>
          <th>{{ $t('integrations.scope') }}</th>
          <th>{{ $t('integrations.expires') }}</th>
          <th>{{ $t('integrations.lastUsed') }}</th>
          <th />
        </tr>
      </thead>
      <tbody>
        <tr v-for="tk in allTokens" :key="tk.id">
          <td>
            {{ tk.user_name || tk.user_email }}
            <span v-if="tk.is_service" class="tag">{{ $t('integrations.serviceTag') }}</span>
            <div v-if="tk.user_name" class="muted small">{{ tk.user_email }}</div>
          </td>
          <td>{{ tk.name }} <code class="muted">{{ tk.prefix }}</code></td>
          <td><span class="tag" :class="tk.scope">{{ tk.scope === 'write' ? $t('integrations.scopeWrite') : $t('integrations.scopeRead') }}</span></td>
          <td class="muted small nowrap">{{ fmt.date(tk.expires_at) }}</td>
          <td class="muted small nowrap">
            {{ tk.last_used_at ? fmt.ago(tk.last_used_at) : $t('integrations.never') }}
            <template v-if="tk.last_used_ip"> · {{ tk.last_used_ip }}</template>
          </td>
          <td class="right">
            <button v-if="canWrite" class="mini danger" :aria-label="$t('integrations.revokeAria', { name: tk.name })" :title="$t('integrations.revoke')" @click="revocaQualsiasi(tk)"><Trash2 :size="13" /></button>
          </td>
        </tr>
        <tr v-if="!allTokens.length">
          <td colspan="6" class="muted">{{ $t('integrations.noTokens') }}</td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<style scoped>
.card { display: flex; flex-direction: column; gap: 10px; padding: 16px; background: var(--panel); border: 1px solid var(--border); border-radius: var(--radius); }
.card h4 { margin: 0; display: inline-flex; align-items: center; gap: 7px; }
.hint { margin: 0; line-height: 1.5; }
.small { font-size: 12px; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11.5px; }
.newrow { display: flex; gap: 8px; max-width: 520px; }
.newrow input { flex: 1; }
table.rows { width: 100%; border-collapse: collapse; font-size: 13px; }
table.rows th { text-align: left; padding: 4px; font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em; color: var(--muted); border-bottom: 1px solid var(--border); }
table.rows td { padding: 7px 4px; border-bottom: 1px solid var(--border-soft); vertical-align: middle; }
table.rows tr.detail td { background: var(--bg-soft); padding: 12px; }
.rowpick { padding: 0; border: 0; background: none; font: inherit; font-weight: 550; color: inherit; cursor: pointer; display: inline-flex; align-items: center; gap: 4px; }
.rowpick:hover { color: var(--accent); box-shadow: none; }
.chip { display: inline-flex; align-items: center; gap: 3px; font-size: 11.5px; padding: 1px 4px 1px 8px; margin: 1px 4px 1px 0; border-radius: 999px; background: var(--panel-2); border: 1px solid var(--border); }
.chip-x { border: 0; background: none; padding: 1px; min-height: 0; color: var(--muted); }
.chip-x:hover { color: var(--danger); box-shadow: none; }
.addgroup { display: inline-block; width: 160px; vertical-align: middle; }
.toklist { list-style: none; margin: 10px 0 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
.toklist li { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; font-size: 12.5px; }
.tk-name { font-weight: 550; }
.tag { font-size: 10px; text-transform: uppercase; letter-spacing: 0.04em; border-radius: 6px; padding: 1px 7px; border: 1px solid var(--border); background: var(--panel-2); color: var(--muted); }
.tag.write { color: var(--accent-hi); border-color: color-mix(in srgb, var(--accent) 40%, var(--border)); background: var(--tint-accent); }
.nowrap { white-space: nowrap; }
.right { text-align: right; }
button.mini { padding: 2px 8px; min-height: 24px; }
.mini.danger { border-color: var(--danger); color: var(--danger); }
.mini.danger:hover { background: var(--danger-fill); color: var(--on-danger); }
</style>
