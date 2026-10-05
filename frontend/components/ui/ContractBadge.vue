<script setup lang="ts">
// L'icona di stato del data contract di una datasource. Senza contratto non
// mostra niente. La FORMA cambia con lo stato, non solo il colore, e lo stato è
// detto anche a parole (title e nome accessibile): un'icona da sola non basta.
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
const stato = computed(() => props.contract?.status ?? 'pending')
const titolo = computed(() => {
  const c = props.contract
  if (!c) return ''
  if (c.blocked) return t('contracts.state.blocked')
  if (c.status === 'failed') return t('contracts.state.failed', { n: c.errors })
  if (c.status === 'warning') return t('contracts.state.warning', { n: c.warnings })
  return t(`contracts.state.${c.status}`)
})
</script>

<template>
  <span v-if="contract" class="contract-badge" :class="stato" role="img" :aria-label="titolo" :title="titolo">
    <component :is="ICONE[stato]" :size="size" aria-hidden="true" />
    <span v-if="label" class="contract-badge-text">{{ $t(`contracts.short.${contract.blocked ? 'blocked' : stato}`) }}</span>
  </span>
</template>
