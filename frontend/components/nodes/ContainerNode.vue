<script setup lang="ts">
// Container node: an image of a container registry, run as a Kubernetes Job (an
// administrator defines the registries and who may use them). As on the Script node,
// its input (data in, left top) and output (data out, right top) are optional, and
// the sequence handles are below. See services/container_runs.py in the gateway.
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { Handle, Position } from '@vue-flow/core'
import { Container } from 'lucide-vue-next'
import { useRegistriesEditor } from '~/composables/useRegistries'

const props = defineProps<{ id: string; data: any }>()
const { t } = useI18n()
const registries = useRegistriesEditor()
// the registry's name as it is now (it may have been renamed), else as the node saved it
const registry = computed(() => registries.state.value?.registries.find((r) => r.id === props.data?.registryId))
// an installation without Kubernetes keeps the node, greyed out: it does not run there
const off = computed(() => registries.state.value?.disabled_reason === 'kubernetes')
const summary = computed(() => {
  const repository = (props.data?.repository ?? '').trim()
  if (!repository) return t('containers.node.chooseImage')
  return `${repository}:${(props.data?.tag ?? '').trim() || 'latest'}`
})
const registryName = computed(() => registry.value?.name ?? props.data?.registryName ?? '')
</script>

<template>
  <div class="node node-container" :class="{ off: off }" :title="off ? t('scripting.kubernetesOnly') : undefined">
    <Handle id="left" type="target" :position="Position.Left" class="h-left-top" />
    <Handle id="seq-in" type="target" :position="Position.Left" class="handle-seq h-left-bottom" />
    <div class="node-title">
      <Container :size="13" class="node-icon" />
      {{ data?.label || t('containers.node.title') }}
      <span v-if="registryName" class="registry">{{ registryName }}</span>
    </div>
    <div class="node-body muted"><code>{{ summary }}</code></div>
    <Handle id="out" type="source" :position="Position.Right" class="h-right-top" />
    <Handle id="seq-out" type="source" :position="Position.Right" class="handle-seq h-right-bottom" />
  </div>
</template>

<style scoped>
.off { opacity: .45; }
.node-container { --node-accent: #818cf8; min-width: 180px; }
.node-icon { color: var(--node-accent); }
.node-body { max-width: 260px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.node-body code { font-size: 11px; }
.handle-seq { background: var(--node-accent) !important; }
.registry { margin-left: auto; max-width: 110px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 10px; padding: 0 6px; border-radius: 999px; border: 1px solid var(--border); color: var(--muted); font-weight: 500; }
</style>
