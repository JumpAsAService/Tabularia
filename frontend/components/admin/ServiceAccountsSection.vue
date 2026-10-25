<script setup lang="ts">
// Service accounts (Admin panel): the integrations that sign in with a token, their
// groups (from which they take the permissions, as people do) and their tokens. At the
// bottom ALL the installation's API tokens, to withdraw the one that leaked. The
// observer sees and does not touch (the commands disappear: the server would say 403).
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
const newName = ref('')
const busy = ref(false)
const isOpen = ref<number | null>(null)
const tokensOf = ref<ApiToken[]>([])
// the secret just created, with the account it belongs to: if meanwhile another account
// opens, it does not show under it with the wrong name
const secret = ref<{ account: number; value: string } | null>(null)

async function load() {
  try {
    ;[accounts.value, allTokens.value] = await Promise.all([api.serviceAccounts(), api.allTokens()])
    emit('count', accounts.value.length)
  } catch (e) {
    toast.error(errMessage(e))
  }
}
onMounted(load)

async function create() {
  if (!newName.value.trim()) return
  busy.value = true
  try {
    const acc = await api.createServiceAccount(newName.value.trim())
    newName.value = ''
    await load()
    await open(acc.id)
  } catch (e) {
    toast.error(errMessage(e))
  } finally {
    busy.value = false
  }
}

async function remove(a: ServiceAccount) {
  if (!confirm(t('integrations.confirmDeleteAccount', { name: a.name }))) return
  try {
    await api.deleteServiceAccount(a.id)
    if (isOpen.value === a.id) isOpen.value = null
    await load()
  } catch (e) {
    toast.error(errMessage(e))
  }
}

async function open(id: number) {
  secret.value = null
  if (isOpen.value === id) {
    isOpen.value = null
    return
  }
  isOpen.value = id
  try {
    tokensOf.value = await api.accountTokens(id)
  } catch (e) {
    toast.error(errMessage(e))
  }
}

async function createToken(a: ServiceAccount, body: TokenRequest) {
  busy.value = true
  try {
    secret.value = { account: a.id, value: (await api.createAccountToken(a.id, body)).token }
    if (isOpen.value === a.id) tokensOf.value = await api.accountTokens(a.id)
    await load()
  } catch (e) {
    toast.error(errMessage(e))
  } finally {
    busy.value = false
  }
}

async function revokeToken(a: ServiceAccount, tk: ApiToken) {
  if (!confirm(t('integrations.confirmRevoke', { name: tk.name }))) return
  busy.value = true
  try {
    await api.revokeAccountToken(a.id, tk.id)
    tokensOf.value = tokensOf.value.filter((x) => x.id !== tk.id)
    await load()
  } catch (e) {
    toast.error(errMessage(e))
  } finally {
    busy.value = false
  }
}

async function revokeAny(tk: AdminApiToken) {
  if (!confirm(t('integrations.confirmRevokeOf', { name: tk.name, who: tk.user_name || tk.user_email }))) return
  try {
    await api.revokeAnyToken(tk.id)
    await load()
    if (isOpen.value != null) tokensOf.value = await api.accountTokens(isOpen.value)
  } catch (e) {
    toast.error(errMessage(e))
  }
}

// groups: that is where an account takes the permissions on the folders
function groupsToAdd(a: ServiceAccount) {
  return props.groups.filter((g) => !a.groups.includes(g.name)).map((g) => ({ value: g.id, label: g.name }))
}
async function addGroup(a: ServiceAccount, gid: number) {
  try {
    await projectsApi.addToGroup(a.id, gid)
    await load()
  } catch (e) {
    toast.error(errMessage(e))
  }
}
async function removeGroup(a: ServiceAccount, name: string) {
  const g = props.groups.find((x) => x.name === name)
  if (!g) return
  try {
    await projectsApi.removeFromGroup(a.id, g.id)
    await load()
  } catch (e) {
    toast.error(errMessage(e))
  }
}

const humans = computed(() => allTokens.value.filter((tk) => !tk.is_service).length)
</script>

<template>
  <div class="card">
    <h4><Bot :size="14" /> {{ $t('integrations.serviceAccountsTitle') }} <span class="muted">{{ accounts.length }}</span></h4>
    <p class="hint muted small">{{ $t('integrations.serviceAccountsLead') }}</p>

    <form v-if="canWrite" class="newrow" @submit.prevent="create">
      <input v-model="newName" type="text" maxlength="80" :placeholder="$t('integrations.accountNamePlaceholder')" :aria-label="$t('integrations.accountNamePlaceholder')" />
      <button class="primary" type="submit" :disabled="busy || !newName.trim()"><Plus :size="14" /> {{ $t('integrations.createAccount') }}</button>
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
              <button type="button" class="rowpick" :aria-expanded="isOpen === a.id" :disabled="busy" @click="open(a.id)">
                <ChevronDown v-if="isOpen === a.id" :size="13" /><ChevronRight v-else :size="13" /> {{ a.name }}
              </button>
              <div class="muted small mono">{{ a.email }}</div>
            </td>
            <td>
              <span v-for="g in a.groups" :key="g" class="chip">
                {{ g }}
                <button v-if="canWrite" type="button" class="chip-x" :aria-label="$t('integrations.removeGroupAria', { group: g })" @click="removeGroup(a, g)"><X :size="10" /></button>
              </span>
              <Select
                v-if="canWrite && groupsToAdd(a).length"
                class="addgroup"
                :model-value="null"
                :options="groupsToAdd(a)"
                :placeholder="$t('integrations.addGroup')"
                @update:model-value="addGroup(a, Number($event))"
              />
              <span v-if="!a.groups.length && !canWrite" class="muted small">—</span>
            </td>
            <td class="small">{{ a.tokens }}</td>
            <td class="muted small nowrap">{{ a.last_seen_at ? fmt.ago(a.last_seen_at) : $t('integrations.never') }}</td>
            <td class="right nowrap">
              <button class="mini" :title="$t('integrations.tokensOf', { name: a.name })" :aria-label="$t('integrations.tokensOf', { name: a.name })" :disabled="busy" @click="open(a.id)"><KeyRound :size="13" /></button>
              <button v-if="canWrite" class="mini danger" :title="$t('integrations.deleteAccount')" :aria-label="$t('integrations.deleteAccountAria', { name: a.name })" @click="remove(a)"><Trash2 :size="13" /></button>
            </td>
          </tr>
          <tr v-if="isOpen === a.id" class="detail">
            <td colspan="5">
              <SecretOnce v-if="secret?.account === a.id" :value="secret.value" :label="$t('integrations.newTokenFor', { name: a.name })" @done="secret = null" />
              <TokenForm v-else-if="canWrite" :busy="busy" @create="createToken(a, $event)" />
              <ul v-if="tokensOf.length" class="toklist">
                <li v-for="tk in tokensOf" :key="tk.id">
                  <span class="tk-name">{{ tk.name }}</span>
                  <code class="muted">{{ tk.prefix }}…</code>
                  <span class="tag" :class="tk.scope">{{ tk.scope === 'write' ? $t('integrations.scopeWrite') : $t('integrations.scopeRead') }}</span>
                  <span class="muted small">{{ $t('integrations.expiresOn', { when: fmt.date(tk.expires_at) }) }}</span>
                  <span class="muted small">{{ tk.last_used_at ? $t('integrations.usedAgo', { when: fmt.ago(tk.last_used_at) }) : $t('integrations.never') }}</span>
                  <button v-if="canWrite" class="mini danger" :disabled="busy" :aria-label="$t('integrations.revokeAria', { name: tk.name })" :title="$t('integrations.revoke')" @click="revokeToken(a, tk)"><Trash2 :size="12" /></button>
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
    <p class="hint muted small">{{ $t('integrations.allTokensLead', { personal: humans }) }}</p>
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
            <button v-if="canWrite" class="mini danger" :aria-label="$t('integrations.revokeAria', { name: tk.name })" :title="$t('integrations.revoke')" @click="revokeAny(tk)"><Trash2 :size="13" /></button>
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
