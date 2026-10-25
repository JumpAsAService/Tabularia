<script setup lang="ts">
// A datasource's data contract state icon. Without a contract it shows nothing. The
// SHAPE changes with the state, not only the colour, and the state is said in words too
// (title and accessible name): an icon alone is not enough.
import { computed } from 'vue'
import { Shield, ShieldAlert, ShieldCheck, ShieldX } from 'lucide-vue-next'
import { useI18n } from 'vue-i18n'
import type { ContractSummary } from '~/composables/useContracts'

const props = withDefaults(
  defineProps<{ contract?: ContractSummary | null; size?: number; label?: boolean }>(),
  { contract: null, size: 14, label: false },
)
const { t } = useI18n()

const ICONE = { pending: Shield, passed: ShieldCheck, warning: ShieldAlert, failed: ShieldX }
const state = computed(() => props.contract?.status ?? 'pending')
const title = computed(() => {
  const c = props.contract
  if (!c) return ''
  if (c.blocked) return t('contracts.state.blocked')
  if (c.status === 'failed') return t('contracts.state.failed', { n: c.errors })
  if (c.status === 'warning') return t('contracts.state.warning', { n: c.warnings })
  return t(`contracts.state.${c.status}`)
})
</script>

<template>
  <span v-if="contract" class="contract-badge" :class="state" role="img" :aria-label="title" :title="title">
    <component :is="ICONE[state]" :size="size" aria-hidden="true" />
    <span v-if="label" class="contract-badge-text">{{ $t(`contracts.short.${contract.blocked ? 'blocked' : state}`) }}</span>
  </span>
</template>
