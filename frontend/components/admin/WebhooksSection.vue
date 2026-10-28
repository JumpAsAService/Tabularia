<script setup lang="ts">
// Webhooks (Admin panel): Tabularia calls an address when a flow or a datasource's
// refresh ends. Every delivery is signed with the webhook's secret, which is seen once
// (at creation or when it is regenerated). The log says what went out, what did not and
// why; from there one resends.
import { onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  CheckCircle2, ChevronDown, ChevronRight, Clock, KeyRound, Plus, RotateCcw, Send, Trash2, Webhook as WebhookIcon, XCircle,
} from 'lucide-vue-next'
import Select from '~/components/ui/Select.vue'
import SecretOnce from '~/components/ui/SecretOnce.vue'
import { errMessage } from '~/composables/useApi'
import { useFormat } from '~/composables/useFormat'
import type { Project } from '~/composables/useProjects'
import {
  useIntegrations, WEBHOOK_EVENTS, type Webhook, type WebhookDelivery, type WebhookEvent,
} from '~/composables/useIntegrations'

const props = defineProps<{ canWrite: boolean; projects: Project[] }>()
const emit = defineEmits<{ (e: 'count', n: number): void }>()

const { t } = useI18n()
const fmt = useFormat()
const api = useIntegrations()
const toast = useToast()

const hooks = ref<Webhook[]>([])
const busy = ref(false)
const secret = ref<{ name: string; value: string } | null>(null)
const isOpen = ref<number | null>(null)
const deliveries = ref<WebhookDelivery[]>([])
const nw = ref<{ name: string; url: string; events: WebhookEvent[]; project_id: number | null }>({
  name: '', url: '', events: ['flow.run.failed'], project_id: null,
})

async function load() {
  try {
    hooks.value = await api.webhooks()
    emit('count', hooks.value.length)
  } catch (e) {
    toast.error(errMessage(e))
  }
}
onMounted(load)

function eventLabel(e: string) {
  return t(`integrations.event_${e.replace(/\./g, '_')}`)
}

async function create() {
  busy.value = true
  try {
    const w = await api.createWebhook({ ...nw.value, name: nw.value.name.trim(), url: nw.value.url.trim() })
    secret.value = { name: w.name, value: w.secret }
    nw.value = { name: '', url: '', events: ['flow.run.failed'], project_id: null }
    await load()
  } catch (e) {
    toast.error(errMessage(e))
  } finally {
    busy.value = false
  }
}

async function activate(w: Webhook) {
  try {
    await api.updateWebhook(w.id, { active: !w.active })
    await load()
  } catch (e) {
    toast.error(errMessage(e))
  }
}

async function test(w: Webhook) {
  try {
    await api.testWebhook(w.id)
    toast.success(t('integrations.testQueued', { name: w.name }))
    // the outcome arrives in a couple of seconds: the log opens and is read again
    isOpen.value = w.id
    setTimeout(() => openLog(w.id, true), 2500)
  } catch (e) {
    toast.error(errMessage(e))
  }
}

async function rotate(w: Webhook) {
  if (!confirm(t('integrations.confirmRotate', { name: w.name }))) return
  try {
    const r = await api.rotateWebhookSecret(w.id)
    secret.value = { name: r.name, value: r.secret }
  } catch (e) {
    toast.error(errMessage(e))
  }
}

async function remove(w: Webhook) {
  if (!confirm(t('integrations.confirmDeleteWebhook', { name: w.name }))) return
  try {
    await api.deleteWebhook(w.id)
    if (isOpen.value === w.id) isOpen.value = null
    await load()
  } catch (e) {
    toast.error(errMessage(e))
  }
}

async function openLog(id: number, reload = false) {
  if (isOpen.value === id && !reload) {
    isOpen.value = null
    return
  }
  isOpen.value = id
  try {
    deliveries.value = await api.deliveries(id)
    await load()
  } catch (e) {
    toast.error(errMessage(e))
  }
}

async function resend(d: WebhookDelivery, wid: number) {
  try {
    await api.redeliver(d.id)
    setTimeout(() => openLog(wid, true), 2500)
  } catch (e) {
    toast.error(errMessage(e))
  }
}

function toggleEvent(e: WebhookEvent) {
  const s = new Set(nw.value.events)
  s.has(e) ? s.delete(e) : s.add(e)
  nw.value.events = WEBHOOK_EVENTS.filter((x) => s.has(x))
}
</script>

<template>
  <div class="card">
    <h4><WebhookIcon :size="14" /> {{ $t('integrations.webhooksTitle') }} <span class="muted">{{ hooks.length }}</span></h4>
    <p class="hint muted small">{{ $t('integrations.webhooksLead') }}</p>

    <SecretOnce
      v-if="secret"
      :value="secret.value"
      :label="$t('integrations.webhookSecretFor', { name: secret.name })"
      @done="secret = null"
    />

    <form v-else-if="canWrite" class="nw" @submit.prevent="create">
      <label class="f">
        <span>{{ $t('adminPanel.colName') }}</span>
        <input v-model="nw.name" type="text" maxlength="120" :placeholder="$t('integrations.webhookNamePlaceholder')" required />
      </label>
      <label class="f url">
        <span>{{ $t('integrations.url') }}</span>
        <input v-model="nw.url" type="url" placeholder="https://" required />
      </label>
      <label class="f">
        <span>{{ $t('integrations.folder') }}</span>
        <Select
          :model-value="nw.project_id"
          :options="[{ value: null, label: $t('integrations.allFolders') }, ...projects.map((p) => ({ value: p.id, label: p.name }))]"
          @update:model-value="nw.project_id = $event as number | null"
        />
      </label>
      <fieldset class="ev">
        <legend>{{ $t('integrations.events') }}</legend>
        <label v-for="e in WEBHOOK_EVENTS" :key="e" class="evopt">
          <input type="checkbox" :checked="nw.events.includes(e)" @change="toggleEvent(e)" /> {{ eventLabel(e) }}
        </label>
      </fieldset>
      <button class="primary go" type="submit" :disabled="busy || !nw.name.trim() || !nw.url.trim() || !nw.events.length">
        <Plus :size="14" /> {{ $t('integrations.createWebhook') }}
      </button>
    </form>

    <table class="rows">
      <thead>
        <tr>
          <th>{{ $t('adminPanel.colName') }}</th>
          <th>{{ $t('integrations.events') }}</th>
          <th>{{ $t('integrations.lastDelivery') }}</th>
          <th>{{ $t('integrations.active') }}</th>
          <th />
        </tr>
      </thead>
      <tbody>
        <template v-for="w in hooks" :key="w.id">
          <tr :class="{ off: !w.active }">
            <td>
              <button type="button" class="rowpick" :aria-expanded="isOpen === w.id" @click="openLog(w.id)">
                <ChevronDown v-if="isOpen === w.id" :size="13" /><ChevronRight v-else :size="13" /> {{ w.name }}
              </button>
              <div class="muted small mono">{{ w.url }}</div>
              <div v-if="w.project_name" class="muted small">{{ $t('integrations.onlyFolder', { folder: w.project_name }) }}</div>
            </td>
            <td><span v-for="e in w.events" :key="e" class="tag">{{ eventLabel(e) }}</span></td>
            <td class="small nowrap">
              <span v-if="w.last_delivery_status" class="outcome" :class="w.last_delivery_status">
                <CheckCircle2 v-if="w.last_delivery_status === 'delivered'" :size="13" />
                <XCircle v-else-if="w.last_delivery_status === 'failed'" :size="13" />
                <Clock v-else :size="13" />
                {{ $t(`integrations.status_${w.last_delivery_status}`) }}
              </span>
              <span v-else class="muted">{{ $t('integrations.never') }}</span>
              <div v-if="w.last_delivery_at" class="muted">{{ fmt.ago(w.last_delivery_at) }}</div>
            </td>
            <td>
              <button
                v-if="canWrite"
                type="button"
                class="switch"
                role="switch"
                :aria-checked="w.active"
                :aria-label="$t('integrations.toggleAria', { name: w.name })"
                @click="activate(w)"
              ><span class="knob" /></button>
              <span v-else class="small">{{ w.active ? $t('integrations.on') : $t('integrations.off') }}</span>
            </td>
            <td class="right nowrap">
              <template v-if="canWrite">
                <button class="mini" :title="$t('integrations.test')" :aria-label="$t('integrations.testAria', { name: w.name })" :disabled="!w.active" @click="test(w)"><Send :size="13" /></button>
                <button class="mini" :title="$t('integrations.rotateSecret')" :aria-label="$t('integrations.rotateSecretAria', { name: w.name })" @click="rotate(w)"><KeyRound :size="13" /></button>
                <button class="mini danger" :title="$t('integrations.deleteWebhook')" :aria-label="$t('integrations.deleteWebhookAria', { name: w.name })" @click="remove(w)"><Trash2 :size="13" /></button>
              </template>
            </td>
          </tr>
          <tr v-if="isOpen === w.id" class="detail">
            <td colspan="5">
              <p class="small muted reg">{{ $t('integrations.deliveriesLead') }}</p>
              <table v-if="deliveries.length" class="cons">
                <tbody>
                  <tr v-for="d in deliveries" :key="d.id">
                    <td class="nowrap muted small">{{ fmt.dateTime(d.created_at, { seconds: true }) }}</td>
                    <td class="small mono">{{ d.event }}</td>
                    <td class="small nowrap">
                      <span class="outcome" :class="d.status">{{ $t(`integrations.status_${d.status}`) }}</span>
                      <span class="muted"> · {{ $t('integrations.attempts', { n: d.attempts }, d.attempts) }}</span>
                      <span v-if="d.last_status_code" class="muted"> · HTTP {{ d.last_status_code }}</span>
                    </td>
                    <td class="small err-cell">
                      <template v-if="d.status === 'pending' && d.next_attempt_at && d.attempts">{{ $t('integrations.nextAttempt', { when: fmt.ago(d.next_attempt_at) }) }}</template>
                      <span v-if="d.last_error" class="err-text" :title="d.last_error">{{ d.last_error }}</span>
                      <details v-if="d.payload" class="payload">
                        <summary>{{ $t('integrations.payload') }}</summary>
                        <pre>{{ JSON.stringify(JSON.parse(d.payload), null, 2) }}</pre>
                      </details>
                    </td>
                    <td class="right">
                      <button v-if="canWrite" class="mini" :title="$t('integrations.redeliver')" :aria-label="$t('integrations.redeliverAria', { id: d.id })" @click="resend(d, w.id)"><RotateCcw :size="12" /></button>
                    </td>
                  </tr>
                </tbody>
              </table>
              <p v-else class="muted small">{{ $t('integrations.noDeliveries') }}</p>
            </td>
          </tr>
        </template>
        <tr v-if="!hooks.length">
          <td colspan="5" class="muted">{{ $t('integrations.noWebhooks') }}</td>
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
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11.5px; overflow-wrap: anywhere; }
.nw { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1.6fr) minmax(0, 1fr); gap: 8px 10px; align-items: end; }
.f { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); min-width: 0; }
.ev { grid-column: 1 / -1; display: flex; flex-wrap: wrap; gap: 6px 16px; border: 0; padding: 0; margin: 0; }
.ev legend { font-size: 12px; color: var(--muted); padding: 0; margin-bottom: 4px; }
.evopt { display: inline-flex; align-items: center; gap: 6px; font-size: 13px; }
.go { grid-column: 1 / -1; justify-self: start; }
@media (max-width: 760px) { .nw { grid-template-columns: 1fr; } }
table.rows { width: 100%; border-collapse: collapse; font-size: 13px; }
table.rows th { text-align: left; padding: 4px; font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em; color: var(--muted); border-bottom: 1px solid var(--border); }
table.rows > tbody > tr > td { padding: 7px 4px; border-bottom: 1px solid var(--border-soft); vertical-align: middle; }
table.rows tr.detail > td { background: var(--bg-soft); padding: 12px; }
tr.off > td:first-child .rowpick { color: var(--muted); }
.rowpick { padding: 0; border: 0; background: none; font: inherit; font-weight: 550; color: inherit; cursor: pointer; display: inline-flex; align-items: center; gap: 4px; }
.rowpick:hover { color: var(--accent); box-shadow: none; }
.tag { display: inline-block; font-size: 10.5px; border-radius: 6px; padding: 1px 7px; margin: 1px 4px 1px 0; border: 1px solid var(--border); background: var(--panel-2); color: var(--muted); }
.outcome { display: inline-flex; align-items: center; gap: 4px; }
.outcome.delivered { color: var(--accent-2); }
.outcome.failed { color: var(--danger); }
.outcome.pending, .outcome.sending { color: var(--warning); }
.switch { position: relative; width: 34px; height: 20px; min-height: 0; padding: 0; border-radius: 999px; background: var(--panel-2); border: 1px solid var(--control-border); }
.switch .knob { position: absolute; top: 2px; left: 2px; width: 14px; height: 14px; border-radius: 50%; background: var(--muted); transition: transform 0.15s, background 0.15s; }
.switch[aria-checked="true"] { background: var(--tint-accent); border-color: var(--accent); }
.switch[aria-checked="true"] .knob { transform: translateX(14px); background: var(--accent); }
.reg { margin: 0 0 8px; }
.cons { width: 100%; border-collapse: collapse; }
.cons td { padding: 5px 4px; border-bottom: 1px solid var(--border-soft); vertical-align: top; }
.err-cell { max-width: 420px; }
.err-text { display: block; color: var(--danger); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.payload summary { cursor: pointer; color: var(--muted); }
.payload pre { margin: 6px 0 0; font-size: 11.5px; max-height: 220px; overflow: auto; background: var(--panel); border: 1px solid var(--border); border-radius: 7px; padding: 8px; }
.nowrap { white-space: nowrap; }
.right { text-align: right; }
button.mini { padding: 2px 8px; min-height: 24px; }
.mini.danger { border-color: var(--danger); color: var(--danger); }
.mini.danger:hover { background: var(--danger-fill); color: var(--on-danger); }
</style>
