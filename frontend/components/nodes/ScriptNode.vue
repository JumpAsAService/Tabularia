<script setup lang="ts">
// Script node: Python or R code that runs as a Kubernetes Job, outside Tabularia, in
// a scripting environment an administrator defined. Its input (data in, left top)
// and its output (data out, right top) are optional: a node with neither is a step
// of the flow that runs something elsewhere. Sequence handles below, as on the
// other control nodes. See services/script_runs.py in the gateway.
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { Handle, Position } from '@vue-flow/core'
import { FileCode2 } from 'lucide-vue-next'
import { useScriptingEditor } from '~/composables/useScripting'

const props = defineProps<{ id: string; data: any }>()
const { t } = useI18n()
const scripting = useScriptingEditor()
// an installation without Kubernetes keeps the node, greyed out: it does not run there
const off = computed(() => scripting.state.value?.disabled_reason === 'kubernetes')
const files = computed<{ path: string }[]>(() => props.data?.files ?? [])
// the environment's name as it is now (it may have been renamed), else as the node saved it
const environment = computed(() => scripting.state.value?.environments.find((e) => e.id === props.data?.environmentId))
const language = computed(() => environment.value?.language ?? props.data?.language)
const summary = computed(() => {
  if (typeof props.data?.scriptId === 'number') return t('scripting.node.summarySaved', { name: props.data?.scriptName || `#${props.data.scriptId}` })
  const env = (environment.value?.name ?? props.data?.environmentName ?? '').trim()
  if (!env) return t('scripting.node.chooseEnvironment')
  if (!files.value.length) return t('scripting.node.noCode', { env })
  return t('scripting.node.summary', { env, entry: props.data?.entrypoint || '—', n: files.value.length })
})
</script>

<template>
  <div class="node node-script" :class="{ off: off }" :title="off ? t('scripting.kubernetesOnly') : undefined">
    <Handle id="left" type="target" :position="Position.Left" class="h-left-top" />
    <Handle id="seq-in" type="target" :position="Position.Left" class="handle-seq h-left-bottom" />
    <div class="node-title">
      <FileCode2 :size="13" class="node-icon" />
      {{ data?.label || t('scripting.node.title') }}
      <span v-if="language" class="language">{{ language === 'r' ? 'R' : 'Python' }}</span>
    </div>
    <div class="node-body muted">{{ summary }}</div>
    <Handle id="out" type="source" :position="Position.Right" class="h-right-top" />
    <Handle id="seq-out" type="source" :position="Position.Right" class="handle-seq h-right-bottom" />
  </div>
</template>

<style scoped>
.off { opacity: .45; }
.node-script { --node-accent: #34d399; min-width: 180px; }
.node-icon { color: var(--node-accent); }
.node-body { max-width: 240px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.handle-seq { background: var(--node-accent) !important; }
.language { margin-left: auto; font-size: 10px; padding: 0 6px; border-radius: 999px; border: 1px solid var(--border); color: var(--muted); font-weight: 500; }
</style>
