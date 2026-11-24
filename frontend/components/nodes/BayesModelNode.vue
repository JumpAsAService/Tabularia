<script setup lang="ts">
// Bayesian model node: a saved model of the Bayesian models' editor, pinned at one of its versions and sampled on the rows that
// reach it, as a Kubernetes Job of the group `bayes` (docs/specs/bayes-flow-node.md). Handles as the Model node's: the table it
// reads in (left top), the table of its results out (right top), sequence handles below. It names the model, the pinned version
// and the sampler's size, and carries a dot when a later version changes the model («a newer version exists»).
import { computed, onMounted, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { Handle, Position } from '@vue-flow/core'
import { Sigma } from 'lucide-vue-next'
import { modelOff } from '~/composables/modelCatalog'
import { bayesNodeNewer, bayesNodeNotice, bayesNodeSamplerOf } from '~/composables/bayesNode'
import { useBayesPins } from '~/composables/useBayesPins'
import { useFormat } from '~/composables/useFormat'
import { useModelGroups } from '~/composables/useModelGroups'

const props = defineProps<{ id: string; data: any }>()
const { t } = useI18n()
const fmt = useFormat()
const groups = useModelGroups()
const pins = useBayesPins()

const modelId = computed(() => (Number.isInteger(props.data?.bayesModelId) ? (props.data.bayesModelId as number) : null))
const pinned = computed(() => (Number.isInteger(props.data?.bayesModelVersion) ? (props.data.bayesModelVersion as number) : null))
onMounted(() => { pins.load() })
watch(modelId, (id) => { if (id != null) pins.loadVersions(id) }, { immediate: true })

const listed = computed(() => (modelId.value == null ? null : (pins.models.value?.find((m) => m.id === modelId.value) ?? null)))
// the list is read: a model it does not hold is one this person does not see (the same for «deleted»)
const unavailable = computed(() => modelId.value != null && !!pins.models.value && !listed.value)
const newer = computed(() => (listed.value && pinned.value != null ? bayesNodeNewer(listed.value.current_version, pinned.value, pins.versionsOf.value[listed.value.id] ?? []) : null))
const notice = computed(() => bayesNodeNotice(newer.value))

const off = computed(() => modelOff('bayes', groups.state.value))
const reason = computed(() => {
  switch (off.value) {
    case 'kubernetes': return t('scripting.kubernetesOnly')
    case 'not_configured': return t('models.node.notConfigured')
    case 'not_enabled': return t('models.node.groupOff', { group: t('models.groups.bayes') })
    case 'not_allowed': return t('models.node.groupNotAllowed')
    default: return ''
  }
})
const title = computed(() => (props.data?.label as string) || listed.value?.name || t('models.bayes_model.name'))
const sampler = computed(() => bayesNodeSamplerOf(props.data?.sampler))
const summary = computed(() => {
  if (modelId.value == null) return t('bayesNode.node.setIt')
  if (unavailable.value) return t('bayesNode.node.unavailable')
  const version = pinned.value != null ? `v${pinned.value}` : ''
  const size = t('bayesNode.node.size', { chains: sampler.value.chains, draws: fmt.count(sampler.value.draws) })
  return [listed.value?.name && props.data?.label ? listed.value.name : '', version, size].filter(Boolean).join(' · ')
})
</script>

<template>
  <div class="node node-bayes" :class="{ off: !!reason }" :title="reason || undefined">
    <Handle id="left" type="target" :position="Position.Left" class="h-left-top" />
    <Handle id="seq-in" type="target" :position="Position.Left" class="handle-seq h-left-bottom" />
    <div class="node-title">
      <Sigma :size="13" class="node-icon" aria-hidden="true" />
      <span class="name">{{ title }}</span>
      <span v-if="notice && newer" class="dot" role="img" :title="t('bayesNode.node.newer', { version: newer.version })" :aria-label="t('bayesNode.node.newer', { version: newer.version })" />
    </div>
    <div class="node-body muted" :class="{ bad: unavailable }">{{ summary }}</div>
    <div v-if="reason" class="node-body why">{{ reason }}</div>
    <Handle id="out" type="source" :position="Position.Right" class="h-right-top" />
    <Handle id="seq-out" type="source" :position="Position.Right" class="handle-seq h-right-bottom" />
  </div>
</template>

<style scoped>
.off { opacity: .55; }
.node-bayes { --node-accent: #2dd4bf; min-width: 180px; }
.node-icon { color: var(--node-accent); flex: none; }
.name { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 200px; }
.node-body { max-width: 240px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.node-body.bad { color: var(--warning); }
.node-body.why { white-space: normal; font-size: 11px; color: var(--warning); }
.handle-seq { background: var(--node-accent) !important; }
.dot { flex: none; width: 8px; height: 8px; margin-left: auto; border-radius: 50%; background: var(--warning); }
</style>
