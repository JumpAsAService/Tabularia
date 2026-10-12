<script setup lang="ts">
// «Token API»: le credenziali dei propri script. Valgono con i propri permessi,
// mai di più; di sola lettura non modificano niente. Il segreto si vede una volta.
import { ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { LoaderCircle, Trash2, BookOpen } from 'lucide-vue-next'
import Modal from '~/components/ui/Modal.vue'
import SecretOnce from '~/components/ui/SecretOnce.vue'
import TokenForm from '~/components/ui/TokenForm.vue'
import { errMessage } from '~/composables/useApi'
import { useFormat } from '~/composables/useFormat'
import { useIntegrations, type ApiToken, type TokenRequest } from '~/composables/useIntegrations'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ (e: 'close'): void }>()

const { t } = useI18n()
const fmt = useFormat()
const api = useIntegrations()
const apiBase = useRuntimeConfig().public.apiBase as string

const items = ref<ApiToken[]>([])
const loading = ref(false)
const busy = ref(false)
const error = ref('')
const appenaCreato = ref<string | null>(null)

async function load() {
  loading.value = true
  error.value = ''
  try {
    items.value = await api.myTokens()
  } catch (e) {
    error.value = errMessage(e)
  } finally {
    loading.value = false
  }
}
watch(() => props.open, (o) => { if (o) { appenaCreato.value = null; load() } }, { immediate: true })

async function crea(body: TokenRequest) {
  busy.value = true
  error.value = ''
  try {
    const nuovo = await api.createMyToken(body)
    appenaCreato.value = nuovo.token
    await load()
  } catch (e) {
    error.value = errMessage(e)
  } finally {
    busy.value = false
  }
}

async function revoca(tk: ApiToken) {
  if (!confirm(t('integrations.confirmRevoke', { name: tk.name }))) return
  busy.value = true
  try {
    await api.revokeMyToken(tk.id)
    items.value = items.value.filter((x) => x.id !== tk.id)
  } catch (e) {
    error.value = errMessage(e)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <Modal :open="open" :title="$t('integrations.myTokensTitle')" :width="680" @close="emit('close')">
    <p class="lead muted">{{ $t('integrations.myTokensLead') }}</p>

    <SecretOnce v-if="appenaCreato" :value="appenaCreato" :label="$t('integrations.newToken')" @done="appenaCreato = null" />
    <TokenForm v-else :busy="busy" @create="crea" />

    <p v-if="error" class="err" role="alert">{{ error }}</p>
    <p v-if="loading" class="muted"><LoaderCircle :size="13" class="spin" /> {{ $t('sessions.loading') }}</p>

    <table v-else-if="items.length" class="tk">
      <thead>
        <tr>
          <th>{{ $t('integrations.tokenName') }}</th>
          <th>{{ $t('integrations.scope') }}</th>
          <th>{{ $t('integrations.expires') }}</th>
          <th>{{ $t('integrations.lastUsed') }}</th>
          <th />
        </tr>
      </thead>
      <tbody>
        <tr v-for="tk in items" :key="tk.id">
          <td><span class="tk-name">{{ tk.name }}</span> <code class="tk-prefix">{{ tk.prefix }}…</code></td>
          <td><span class="tag" :class="tk.scope">{{ tk.scope === 'write' ? $t('integrations.scopeWrite') : $t('integrations.scopeRead') }}</span></td>
          <td class="muted nowrap">{{ fmt.date(tk.expires_at) }}</td>
          <td class="muted nowrap">{{ tk.last_used_at ? fmt.ago(tk.last_used_at) : $t('integrations.never') }}</td>
          <td class="right">
            <button class="mini danger" :disabled="busy" :aria-label="$t('integrations.revokeAria', { name: tk.name })" :title="$t('integrations.revoke')" @click="revoca(tk)">
              <Trash2 :size="13" />
            </button>
          </td>
        </tr>
      </tbody>
    </table>
    <p v-else class="muted empty">{{ $t('integrations.noTokens') }}</p>

    <template #footer>
      <!-- la documentazione la serve il gateway stesso: sempre raggiungibile, sempre della versione installata -->
      <a class="doclink" :href="`${apiBase}/docs`" target="_blank" rel="noopener">
        <BookOpen :size="14" /> {{ $t('integrations.apiGuide') }}
      </a>
    </template>
  </Modal>
</template>

<style scoped>
.lead { margin: 0 0 12px; font-size: 13px; line-height: 1.5; }
.err { color: var(--danger); font-size: 13px; margin: 8px 0 0; }
.empty { font-size: 13px; margin: 12px 0 0; }
.tk { width: 100%; border-collapse: collapse; font-size: 13px; margin-top: 14px; }
.tk th { text-align: left; font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em; color: var(--muted); padding: 4px 6px; border-bottom: 1px solid var(--border); }
.tk td { padding: 7px 6px; border-bottom: 1px solid var(--border-soft); vertical-align: middle; }
.tk tr:last-child td { border-bottom: none; }
.tk-name { font-weight: 550; }
.tk-prefix { font-size: 11.5px; color: var(--muted); }
.tag { font-size: 10px; text-transform: uppercase; letter-spacing: 0.04em; border-radius: 6px; padding: 1px 7px; border: 1px solid var(--border); background: var(--panel-2); color: var(--muted); }
.tag.write { color: var(--accent-hi); border-color: color-mix(in srgb, var(--accent) 40%, var(--border)); background: var(--tint-accent); }
.nowrap { white-space: nowrap; }
.right { text-align: right; }
.doclink { display: inline-flex; align-items: center; gap: 6px; font-size: 12.5px; color: var(--muted); text-decoration: none; }
.doclink:hover { color: var(--accent); }
</style>
