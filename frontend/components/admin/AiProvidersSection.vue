<script setup lang="ts">
// The AI providers and their models (Admin panel → AI). A provider is the one of the
// environment, shown and not changed here, or one added here: an OpenAI-compatible endpoint or
// Anthropic. Each provider's catalog is read on its own, so one that does not answer says
// so where its models would be and leaves the others alone. The observer reads, and touches
// nothing; the key never comes back, in any answer.
import { computed, onMounted, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  CheckCircle2, CircleAlert, LoaderCircle, Pencil, Plug, Plus, Power, RefreshCw, ShieldAlert, ShieldCheck, Sparkles, Trash2, XCircle,
} from 'lucide-vue-next'
import { errMessage } from '~/composables/useApi'
import { useAi, type AiModel, type AiProvider, type AiProviderKind, type AiProviderTestResult, type AiStatus } from '~/composables/useAi'
import Select from '~/components/ui/Select.vue'

const props = defineProps<{ canWrite: boolean; status: AiStatus | null }>()
const emit = defineEmits<{ (e: 'count', n: number): void; (e: 'settings'): void }>()

const { t } = useI18n()
const api = useAi()
const toast = useToast()

const providers = ref<AiProvider[] | null>(null)
const error = ref('')
const models = reactive<Record<string, AiModel[]>>({})
const failed = reactive<Record<string, string>>({})
const loading = reactive(new Set<string>())
const tested = reactive<Record<string, AiProviderTestResult>>({})
const testing = reactive(new Set<string>())

// the models people can choose: those of a provider that is off cannot be
function count() {
  const on = (providers.value ?? []).filter((p) => p.active).flatMap((p) => models[p.key] ?? [])
  emit('count', on.filter((m) => m.enabled).length)
}

async function loadModels(p: AiProvider) {
  loading.add(p.key)
  try {
    models[p.key] = await api.models(p.key)
    delete failed[p.key]
  } catch (e) {
    delete models[p.key]
    failed[p.key] = errMessage(e)
  } finally {
    loading.delete(p.key)
    count()
  }
}

async function load() {
  try {
    const list = await api.providers()
    providers.value = list
    error.value = ''
    await Promise.all(list.map(loadModels))
  } catch (e) {
    error.value = errMessage(e)
  }
}
onMounted(load)

async function refreshRow(p: AiProvider) {
  providers.value = (providers.value ?? []).map((x) => (x.key === p.key ? p : x))
  await loadModels(p)
}

// ── the form ───────────────────────────────────────────────────────────────
const blank = () => ({ name: '', kind: 'anthropic' as AiProviderKind, base_url: '', api_key: '' })
const form = reactive(blank())
const editing = ref<AiProvider | null>(null)
const formOpen = ref(false)
const saving = ref(false)
const formTest = ref<AiProviderTestResult | null>(null)
const formTesting = ref(false)

const kindOptions = computed(() => [
  { value: 'anthropic', label: t('adminPanel.aiKindAnthropic'), hint: t('adminPanel.aiKindAnthropicHint') },
  { value: 'openai_compatible', label: t('adminPanel.aiKindOpenai'), hint: t('adminPanel.aiKindOpenaiHint') },
])
const kindLabel = (kind: AiProviderKind) => (kind === 'anthropic' ? t('adminPanel.aiKindAnthropic') : t('adminPanel.aiKindOpenai'))

function openForm(p?: AiProvider) {
  editing.value = p ?? null
  Object.assign(form, p ? { name: p.name, kind: p.kind, base_url: p.base_url, api_key: '' } : blank())
  formTest.value = null
  formOpen.value = true
}

function closeForm() {
  formOpen.value = false
  Object.assign(form, blank())  // the key typed does not stay in the page
  editing.value = null
}

async function save() {
  if (!form.name.trim()) {
    toast.error(t('adminPanel.aiProviderNameRequired'))
    return
  }
  saving.value = true
  try {
    if (editing.value?.id != null) {
      const updated = await api.updateProvider(editing.value.id, { name: form.name, base_url: form.base_url, api_key: form.api_key })
      await refreshRow(updated)
    } else {
      const made = await api.createProvider({ ...form })
      providers.value = [...(providers.value ?? []), made]
      await loadModels(made)
    }
    toast.success(t('adminPanel.aiProviderSaved', { name: form.name }))
    closeForm()
  } catch (e) {
    toast.error(errMessage(e))
  } finally {
    saving.value = false
  }
}

async function testForm() {
  formTesting.value = true
  formTest.value = null
  try {
    formTest.value = await api.testProvider({
      key: editing.value?.key, name: form.name, kind: form.kind, base_url: form.base_url, api_key: form.api_key,
    })
  } catch (e) {
    formTest.value = { ok: false, models: 0, code: null, message: errMessage(e) }
  } finally {
    formTesting.value = false
  }
}

// ── a provider's row ───────────────────────────────────────────────────────
async function testRow(p: AiProvider) {
  testing.add(p.key)
  delete tested[p.key]
  try {
    tested[p.key] = await api.testProvider({ key: p.key })
  } catch (e) {
    tested[p.key] = { ok: false, models: 0, code: null, message: errMessage(e) }
  } finally {
    testing.delete(p.key)
  }
}

const inUse = (p: AiProvider) => p.chats > 0 || p.enabled_models > 0

async function setActive(p: AiProvider, active: boolean) {
  if (!active && inUse(p) && !confirm(t('adminPanel.aiProviderOffConfirm', { name: p.name, chats: p.chats, models: p.enabled_models }))) return
  try {
    await refreshRow(await api.updateProvider(p.id!, { active }))
    toast.success(t(active ? 'adminPanel.aiProviderOn' : 'adminPanel.aiProviderOff', { name: p.name }))
  } catch (e) {
    toast.error(errMessage(e))
  }
}

async function remove(p: AiProvider) {
  const question = inUse(p)
    ? t('adminPanel.aiProviderDeleteInUse', { name: p.name, chats: p.chats, models: p.enabled_models })
    : t('adminPanel.aiProviderDeleteConfirm', { name: p.name })
  if (!confirm(question)) return
  try {
    await api.removeProvider(p.id!, inUse(p))
    providers.value = (providers.value ?? []).filter((x) => x.key !== p.key)
    delete models[p.key]
    delete failed[p.key]
    count()
    toast.success(t('adminPanel.aiProviderDeleted', { name: p.name }))
  } catch (e) {
    toast.error(errMessage(e))
  }
}

async function toggleModel(p: AiProvider, m: AiModel) {
  try {
    const updated = await api.setModel(m.id, !m.enabled)
    models[p.key] = (models[p.key] ?? []).map((x) => (x.id === updated.id ? updated : x))
    p.enabled_models += updated.enabled ? 1 : -1
    count()
    toast.success(t(updated.enabled ? 'adminPanel.aiModelEnabled' : 'adminPanel.aiModelDisabled', { model: m.model_id }))
  } catch (e) {
    toast.error(errMessage(e))
  }
}
</script>

<template>
  <div class="card">
    <h4><Plug :size="14" /> {{ $t('adminPanel.aiProvidersTitle') }}</h4>
    <p class="muted small hint">{{ $t('adminPanel.aiProvidersHint') }}</p>
    <p v-if="error" class="page-error">{{ error }}</p>
    <div v-else-if="providers" class="tablewrap">
      <table class="rows">
        <thead>
          <tr>
            <th>{{ $t('adminPanel.colProvider') }}</th>
            <th>{{ $t('adminPanel.colProviderKind') }}</th>
            <th v-if="canWrite">{{ $t('adminPanel.colProviderAddress') }}</th>
            <th>{{ $t('adminPanel.colModelState') }}</th>
            <th />
          </tr>
        </thead>
        <tbody>
          <template v-for="p in providers" :key="p.key">
            <tr>
              <td>
                {{ p.name }}
                <span v-if="p.read_only" class="tag off" :title="$t('adminPanel.aiFromEnvironmentTitle')">{{ $t('adminPanel.aiFromEnvironment') }}</span>
              </td>
              <td class="small">{{ kindLabel(p.kind) }}</td>
              <td v-if="canWrite" class="small address" :title="p.base_url">{{ p.base_url }}</td>
              <td class="small" :class="p.active ? '' : 'muted'">{{ p.active ? $t('adminPanel.aiProviderStateOn') : $t('adminPanel.aiProviderStateOff') }}</td>
              <td class="right actions">
                <template v-if="canWrite">
                  <button class="mini" type="button" :disabled="testing.has(p.key)" :title="$t('adminPanel.aiProviderTest')" :aria-label="$t('adminPanel.aiProviderTest')" @click="testRow(p)">
                    <LoaderCircle v-if="testing.has(p.key)" :size="13" class="spin" />
                    <Plug v-else :size="13" />
                  </button>
                  <template v-if="!p.read_only">
                    <button class="mini" type="button" :title="$t('adminPanel.aiProviderEdit')" :aria-label="$t('adminPanel.aiProviderEdit')" @click="openForm(p)"><Pencil :size="13" /></button>
                    <button class="mini" type="button" :title="p.active ? $t('adminPanel.aiProviderTurnOff') : $t('adminPanel.aiProviderTurnOn')" :aria-label="p.active ? $t('adminPanel.aiProviderTurnOff') : $t('adminPanel.aiProviderTurnOn')" @click="setActive(p, !p.active)"><Power :size="13" /></button>
                    <button class="mini danger" type="button" :title="$t('adminPanel.aiProviderDelete')" :aria-label="$t('adminPanel.aiProviderDelete')" @click="remove(p)"><Trash2 :size="13" /></button>
                  </template>
                </template>
              </td>
            </tr>
            <tr v-if="tested[p.key]" class="outcome">
              <td :colspan="canWrite ? 5 : 4">
                <span v-if="tested[p.key]!.ok" class="good"><CheckCircle2 :size="13" aria-hidden="true" /> {{ $t('adminPanel.aiProviderTestOk', { n: tested[p.key]!.models }) }}</span>
                <span v-else class="bad"><CircleAlert :size="13" aria-hidden="true" /> {{ tested[p.key]!.message }}</span>
              </td>
            </tr>
          </template>
          <tr v-if="!providers.length"><td :colspan="canWrite ? 5 : 4" class="muted">{{ $t('adminPanel.aiNoProviders') }}</td></tr>
        </tbody>
      </table>
    </div>

    <button v-if="canWrite && !formOpen" class="add" type="button" @click="openForm()"><Plus :size="13" /> {{ $t('adminPanel.aiProviderAdd') }}</button>

    <form v-if="canWrite && formOpen" class="provider-form" autocomplete="off" @submit.prevent="save">
      <h5>{{ editing ? $t('adminPanel.aiProviderEditTitle', { name: editing.name }) : $t('adminPanel.aiProviderAdd') }}</h5>
      <label>
        <span>{{ $t('adminPanel.aiProviderName') }}</span>
        <input v-model="form.name" type="text" maxlength="80" required :placeholder="$t('adminPanel.aiProviderNamePlaceholder')" />
        <small class="muted">{{ $t('adminPanel.aiProviderNameHint') }}</small>
      </label>
      <label>
        <span>{{ $t('adminPanel.colProviderKind') }}</span>
        <Select v-model="form.kind" :options="kindOptions" :disabled="!!editing" :aria-label="$t('adminPanel.colProviderKind')" />
      </label>
      <label>
        <span>{{ $t('adminPanel.colProviderAddress') }}</span>
        <input v-model="form.base_url" type="url" maxlength="500" :required="form.kind !== 'anthropic'"
               :placeholder="form.kind === 'anthropic' ? 'https://api.anthropic.com' : 'https://…/v1'" />
        <small class="muted">{{ form.kind === 'anthropic' ? $t('adminPanel.aiProviderAddressAnthropic') : $t('adminPanel.aiProviderAddressOpenai') }}</small>
      </label>
      <label>
        <span>{{ $t('adminPanel.aiProviderKey') }}</span>
        <input v-model="form.api_key" type="password" maxlength="2000" autocomplete="new-password" spellcheck="false"
               :required="!editing" :placeholder="editing ? $t('adminPanel.aiProviderKeyKeep') : ''" />
        <small class="muted">{{ editing ? $t('adminPanel.aiProviderKeyHintEdit') : $t('adminPanel.aiProviderKeyHint') }}</small>
      </label>
      <p v-if="formTest" class="form-outcome">
        <span v-if="formTest.ok" class="good"><CheckCircle2 :size="13" aria-hidden="true" /> {{ $t('adminPanel.aiProviderTestOk', { n: formTest.models }) }}</span>
        <span v-else class="bad"><CircleAlert :size="13" aria-hidden="true" /> {{ formTest.message }}</span>
      </p>
      <div class="form-actions">
        <button class="primary" type="submit" :disabled="saving">{{ $t('adminPanel.aiProviderSave') }}</button>
        <button type="button" :disabled="formTesting" @click="testForm">
          <LoaderCircle v-if="formTesting" :size="13" class="spin" /> {{ $t('adminPanel.aiProviderTest') }}
        </button>
        <button type="button" @click="closeForm">{{ $t('adminPanel.aiProviderCancel') }}</button>
      </div>
    </form>
  </div>

  <div class="card">
    <h4><Sparkles :size="14" /> {{ $t('adminPanel.aiTitle') }}</h4>
    <p class="muted small hint">{{ $t('adminPanel.aiHint') }}</p>
    <div v-if="status" class="data-mode">
      <p :class="{ on: status.metadata_only }">
        <component :is="status.metadata_only ? ShieldCheck : ShieldAlert" :size="14" aria-hidden="true" />
        <span>{{ status.metadata_only ? $t('adminPanel.aiMetadataOnlyOn') : $t('adminPanel.aiMetadataOnlyOff') }}</span>
      </p>
      <p :class="{ on: status.mcp_metadata_only }">
        <component :is="status.mcp_metadata_only ? ShieldCheck : ShieldAlert" :size="14" aria-hidden="true" />
        <span>{{ status.mcp_metadata_only ? $t('adminPanel.mcpMetadataOnlyOn') : $t('adminPanel.mcpMetadataOnlyOff') }}</span>
      </p>
      <button class="link-like" type="button" @click="emit('settings')">{{ $t('adminPanel.aiMetadataOnlyChange') }}</button>
    </div>
    <p v-if="providers && !providers.length" class="muted small hint">{{ $t('adminPanel.aiUnavailable') }}</p>
    <template v-for="p in providers ?? []" :key="p.key">
      <h5 class="provider-head">
        {{ p.name }}
        <span v-if="!p.active" class="tag off">{{ $t('adminPanel.aiProviderStateOff') }}</span>
        <LoaderCircle v-if="loading.has(p.key)" :size="12" class="spin muted" />
      </h5>
      <p v-if="failed[p.key]" class="catalog-error">
        <CircleAlert :size="13" aria-hidden="true" /> <span>{{ failed[p.key] }}</span>
        <button class="link-like" type="button" @click="loadModels(p)"><RefreshCw :size="11" aria-hidden="true" /> {{ $t('adminPanel.aiCatalogRetry') }}</button>
      </p>
      <p v-else-if="!p.active" class="muted small hint">{{ $t('adminPanel.aiProviderOffHint') }}</p>
      <div v-if="models[p.key]?.length" class="tablewrap">
        <table class="rows">
          <thead>
            <tr>
              <th>{{ $t('adminPanel.colModel') }}</th>
              <th>{{ $t('adminPanel.colModelState') }}</th>
              <th />
            </tr>
          </thead>
          <tbody>
            <tr v-for="m in models[p.key]" :key="m.id">
              <td>
                {{ m.model_id }}
                <span v-if="!m.chat" class="tag off">{{ $t('adminPanel.aiNotChatTag') }}</span>
                <span v-else-if="!m.available && p.active" class="tag off">{{ $t('adminPanel.aiGoneTag') }}</span>
                <span v-if="m.chat && !m.priced" class="tag warn" :title="$t('adminPanel.aiNoPriceTitle')">{{ $t('adminPanel.aiNoPriceTag') }}</span>
              </td>
              <td class="small" :class="m.enabled ? '' : 'muted'">{{ m.enabled ? $t('adminPanel.aiEnabled') : $t('adminPanel.aiDisabled') }}</td>
              <td class="right">
                <button
                  v-if="m.chat && canWrite && p.active"
                  class="mini"
                  type="button"
                  :title="m.enabled ? $t('adminPanel.aiDisableTitle') : $t('adminPanel.aiEnableTitle')"
                  :aria-label="m.enabled ? $t('adminPanel.aiDisableTitle') : $t('adminPanel.aiEnableTitle')"
                  @click="toggleModel(p, m)"
                >
                  <component :is="m.enabled ? CheckCircle2 : XCircle" :size="13" />
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>
  </div>
</template>

<style scoped src="~/assets/admintable.css"></style>
<style scoped>
.card { display: flex; flex-direction: column; gap: 8px; padding: 16px; background: var(--panel); border: 1px solid var(--border); border-radius: var(--radius); }
.card h4 { margin: 0; display: inline-flex; align-items: center; gap: 7px; }
.hint { margin: 0; line-height: 1.5; }
.small { font-size: 12px; }
.page-error { color: var(--danger); font-size: 13px; margin: 0; }
/* metadata-only mode: what a model gets */
.data-mode { display: flex; flex-direction: column; align-items: flex-start; gap: 6px; padding: 10px 12px; border: 1px solid var(--border); border-radius: 8px; }
.data-mode p { display: flex; align-items: flex-start; gap: 7px; margin: 0; font-size: 12.5px; color: var(--muted); }
.data-mode p svg { flex: none; margin-top: 2px; }
.data-mode p.on { color: var(--text); }
.data-mode p.on svg { color: var(--accent-hi); }
.link-like { display: inline-flex; align-items: center; gap: 4px; padding: 0; min-height: 0; border: 0; background: none; color: var(--accent-hi); font-size: 12.5px; cursor: pointer; text-decoration: underline; }
.link-like:hover { box-shadow: none; }

td.actions { white-space: nowrap; width: 1%; }
td.address { max-width: 260px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-variant-numeric: tabular-nums; }
.tag {
  font-size: 10px; text-transform: uppercase; letter-spacing: 0.04em; padding: 1px 7px; border-radius: 8px;
  background: var(--panel-2); border: 1px solid var(--border); color: var(--accent-hi); margin-left: 6px; white-space: nowrap;
}
.tag.off { color: var(--muted); }
.tag.warn { color: var(--warning, #d08700); }
button.mini { padding: 3px 8px; min-height: 24px; }
button.mini + button.mini { margin-left: 4px; }
.mini.danger { border-color: var(--danger); color: var(--danger-text); }
.mini.danger:hover:not(:disabled) { background: var(--danger-fill); color: var(--on-danger); }
.outcome td { padding-top: 0; font-size: 12px; }
.good, .bad { display: inline-flex; align-items: flex-start; gap: 5px; font-size: 12px; }
.good svg, .bad svg { flex: none; margin-top: 2px; }
.good { color: var(--accent-hi); }
.bad { color: var(--danger); }
.add { align-self: flex-start; display: inline-flex; align-items: center; gap: 5px; }

.provider-form { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 12px 16px; margin-top: 6px; padding-top: 12px; border-top: 1px solid var(--border-soft); }
.provider-form h5, .provider-form .form-actions, .provider-form .form-outcome { grid-column: 1 / -1; margin: 0; }
.provider-form h5 { font-size: 13px; }
.provider-form label { display: flex; flex-direction: column; gap: 4px; min-width: 0; font-size: 12px; color: var(--muted); }
.provider-form small { font-size: 11.5px; line-height: 1.4; }
.form-actions { display: flex; flex-wrap: wrap; gap: 8px; }
.form-actions button { display: inline-flex; align-items: center; gap: 5px; }

.provider-head { display: flex; align-items: center; gap: 6px; margin: 10px 0 0; font-size: 13px; font-weight: 600; }
.catalog-error { display: flex; flex-wrap: wrap; align-items: flex-start; gap: 6px; margin: 0; font-size: 12.5px; color: var(--danger); }
.catalog-error svg { flex: none; margin-top: 2px; }
.spin { animation: spin 0.9s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .spin { animation: none; } }
</style>
