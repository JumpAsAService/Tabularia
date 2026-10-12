<script setup lang="ts">
// «Le tue sessioni»: dove si è collegati adesso (questo browser, il portatile,
// uno script), e la possibilità di chiuderne una o tutte le altre. Chiudere è
// vero: il gateway cancella la sessione e il suo token smette di valere subito,
// ovunque sia stato copiato.
import { ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { LoaderCircle, LogOut, MonitorSmartphone, Terminal } from 'lucide-vue-next'
import Modal from '~/components/ui/Modal.vue'
import { useApi, errMessage, type MySession } from '~/composables/useApi'
import { useFormat } from '~/composables/useFormat'
import { deviceLabel } from '~/composables/sessionDevice'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ (e: 'close'): void }>()

const { t } = useI18n()
const fmt = useFormat()
const api = useApi()

const items = ref<MySession[]>([])
const loading = ref(false)
const busy = ref(false)
const error = ref('')

async function load() {
  loading.value = true
  error.value = ''
  try {
    items.value = await api.mySessions()
  } catch (e) {
    error.value = errMessage(e)
  } finally {
    loading.value = false
  }
}
watch(() => props.open, (o) => { if (o) load() }, { immediate: true })

const others = () => items.value.filter((s) => !s.current)

async function close(s: MySession) {
  busy.value = true
  error.value = ''
  try {
    await api.closeMySession(s.id)
    items.value = items.value.filter((x) => x.id !== s.id)
  } catch (e) {
    error.value = errMessage(e)
  } finally {
    busy.value = false
  }
}

async function closeOthers() {
  const n = others().length
  if (!confirm(t('sessions.confirmCloseOthers', { n }, n))) return
  busy.value = true
  error.value = ''
  try {
    await api.closeOtherSessions()
    items.value = items.value.filter((s) => s.current)
  } catch (e) {
    error.value = errMessage(e)
  } finally {
    busy.value = false
  }
}

function device(s: MySession): string {
  return deviceLabel(s.user_agent) ?? t('sessions.unknownDevice')
}
function isScript(s: MySession): boolean {
  return !!s.user_agent && /^(curl|python|aiohttp|httpx)/i.test(s.user_agent)
}
</script>

<template>
  <Modal :open="open" :title="$t('sessions.title')" :width="560" @close="emit('close')">
    <p class="sd-lead muted">{{ $t('sessions.lead') }}</p>

    <p v-if="loading" class="muted"><LoaderCircle :size="13" class="spin" /> {{ $t('sessions.loading') }}</p>
    <p v-else-if="error" class="sd-error" role="alert">{{ error }}</p>

    <ul v-if="!loading" class="sd-list">
      <li v-for="s in items" :key="s.id" class="sd-row" :class="{ current: s.current }">
        <component :is="isScript(s) ? Terminal : MonitorSmartphone" :size="18" class="sd-icon" aria-hidden="true" />
        <div class="sd-what">
          <span class="sd-device">
            {{ device(s) }}
            <span v-if="s.current" class="sd-here">{{ $t('sessions.thisDevice') }}</span>
          </span>
          <span class="sd-meta muted">
            {{ s.ip || '—' }} · {{ $t('sessions.lastActive', { when: fmt.ago(s.last_seen_at) }) }}
          </span>
          <span class="sd-meta muted">
            {{ $t(s.method === 'sso' ? 'sessions.signedInSso' : 'sessions.signedInPassword', { when: fmt.dateTime(s.created_at) }) }}
          </span>
        </div>
        <button
          v-if="!s.current"
          class="sd-close"
          :disabled="busy"
          :aria-label="$t('sessions.closeOneAria', { device: device(s) })"
          @click="close(s)"
        >
          <LogOut :size="13" /> {{ $t('sessions.closeOne') }}
        </button>
      </li>
    </ul>

    <template #footer>
      <span class="muted sd-count">{{ $t('sessions.count', { n: items.length }, items.length) }}</span>
      <button :disabled="busy || loading || !others().length" @click="closeOthers">
        <LogOut :size="14" /> {{ $t('sessions.closeOthers') }}
      </button>
    </template>
  </Modal>
</template>

<style scoped>
.sd-lead { margin: 0 0 12px; font-size: 13px; line-height: 1.5; }
.sd-error { color: var(--danger); font-size: 13px; }
.sd-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; }
.sd-row {
  display: flex; align-items: center; gap: 12px;
  padding: 10px 12px; border: 1px solid var(--border); border-radius: 10px; background: var(--panel-2);
}
.sd-row.current { border-color: color-mix(in srgb, var(--accent) 45%, var(--border)); }
.sd-icon { color: var(--muted); flex: none; }
.sd-what { display: flex; flex-direction: column; gap: 2px; flex: 1; min-width: 0; }
.sd-device { font-size: 13.5px; font-weight: 600; display: inline-flex; align-items: center; gap: 8px; }
.sd-here {
  font-size: 10.5px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.03em;
  color: var(--accent-hi); background: var(--tint-accent); border-radius: 4px; padding: 1px 6px;
}
.sd-meta { font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sd-close { flex: none; font-size: 12.5px; padding: 5px 10px; }
.sd-count { font-size: 12.5px; margin-right: auto; }
</style>
