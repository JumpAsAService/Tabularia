<script setup lang="ts">
// The form to create an API token: name, level, expiry. The personal tokens and the
// service accounts' ones use it; whoever uses it makes the call. The expiries offered do
// not go past the one the gateway accepts.
import { computed, ref } from 'vue'
import { Plus } from 'lucide-vue-next'
import Select from '~/components/ui/Select.vue'
import { TOKEN_DURATIONS, type TokenRequest } from '~/composables/useIntegrations'
import { useAuth } from '~/composables/useAuth'

defineProps<{ busy?: boolean }>()
const emit = defineEmits<{ (e: 'create', body: TokenRequest): void }>()

const { user } = useAuth()
const durations = computed(() => {
  const max = user.value?.api_token_max_days ?? Math.max(...TOKEN_DURATIONS)
  const below = TOKEN_DURATIONS.filter((d) => d < max)
  return [...below, max]
})

const name = ref('')
const scope = ref<'read' | 'write'>('read')
const days = ref(Math.min(90, durations.value[durations.value.length - 1]))

function send() {
  if (!name.value.trim()) return
  emit('create', { name: name.value.trim(), scope: scope.value, expires_in_days: days.value })
  name.value = ''
}
</script>

<template>
  <form class="tf" @submit.prevent="send">
    <label class="tf-field tf-name">
      <span>{{ $t('integrations.tokenName') }}</span>
      <input v-model="name" type="text" maxlength="80" :placeholder="$t('integrations.tokenNamePlaceholder')" required />
    </label>
    <label class="tf-field">
      <span>{{ $t('integrations.scope') }}</span>
      <Select
        :model-value="scope"
        :options="[
          { value: 'read', label: $t('integrations.scopeRead') },
          { value: 'write', label: $t('integrations.scopeWrite') },
        ]"
        @update:model-value="scope = $event as 'read' | 'write'"
      />
    </label>
    <label class="tf-field">
      <span>{{ $t('integrations.expires') }}</span>
      <Select
        :model-value="days"
        :options="durations.map((d) => ({ value: d, label: $t('integrations.days', { n: d }) }))"
        @update:model-value="days = Number($event)"
      />
    </label>
    <button class="primary tf-go" type="submit" :disabled="busy || !name.trim()">
      <Plus :size="14" /> {{ $t('integrations.createToken') }}
    </button>
    <p class="tf-hint">{{ scope === 'write' ? $t('integrations.scopeWriteHint') : $t('integrations.scopeReadHint') }}</p>
  </form>
</template>

<style scoped>
.tf { display: grid; grid-template-columns: minmax(0, 1.4fr) minmax(0, 1fr) minmax(0, 0.8fr) auto; gap: 8px 10px; align-items: end; }
.tf-field { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); min-width: 0; }
.tf-go { white-space: nowrap; }
.tf-hint { grid-column: 1 / -1; margin: 0; font-size: 12px; color: var(--muted); }
@media (max-width: 640px) {
  .tf { grid-template-columns: 1fr 1fr; }
  .tf-name { grid-column: 1 / -1; }
  .tf-go { grid-column: 1 / -1; }
}
</style>
