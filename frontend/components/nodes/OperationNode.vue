<script setup lang="ts">
import { computed, inject } from 'vue'
import { DatabaseZap, RefreshCw } from 'lucide-vue-next'
import { Handle, Position } from '@vue-flow/core'
import { useI18n } from 'vue-i18n'
import { opMeta } from '~/composables/useOpIcons'
import { opSummary } from '~/composables/opSummary'
import { useNodeStats } from '~/composables/useNodeStats'

const props = defineProps<{ id: string; data: any }>()
const { t } = useI18n()
const stats = useNodeStats(() => props.id, inject, t)
// the result of this node is in the step cache: the nodes after it start from here
const cachedNodes = inject<Set<string>>('nodeCached', new Set())
const cached = computed(() => cachedNodes.has(props.id))

const isJoin = computed(() => props.data?.opType === 'join')
const isUnion = computed(() => props.data?.opType === 'union')
// operations with a second input (right branch): it sits on the left, under the main one
const needsRight = computed(() => isJoin.value || isUnion.value)
const meta = computed(() => opMeta(props.data?.opType ?? ''))
const paramCount = computed(() => Object.keys(props.data?.params ?? {}).length)
// the operation's name as in the palette (translated), not the internal id; and in one
// line what it does with its parameters, instead of «1 parameters»
const title = computed(() => (meta.value.label.startsWith('ops.') ? t(meta.value.label) : props.data?.opType) || t('operationNode.operationFallback'))
const summary = computed(() => opSummary(props.data?.opType ?? '', props.data?.params))
</script>

<template>
  <div
    class="node node-op"
    :class="{ 'node-join': needsRight, 'node-cached': cached }"
    :style="{ '--node-accent': meta.color }"
  >
    <!-- main input (chain): on the left (at the top when there is a second input) -->
    <Handle id="left" type="target" :position="Position.Left" :class="needsRight ? 'h-left-top' : ''" />
    <!-- second input (join: table to join; union: branch to append): on the left, below -->
    <template v-if="needsRight">
      <Handle id="right" type="target" :position="Position.Left" class="handle-right h-left-bottom" />
      <span class="hlabel-left">{{ $t('operationNode.tableInputLabel') }}</span>
    </template>

    <div class="node-title">
      <component :is="meta.icon" :size="13" class="node-icon" />
      {{ title }}
    </div>
    <div class="node-body muted">
      <template v-if="isJoin">{{ $t('operationNode.joinLabel', { how: data.params?.how || 'inner' }) }}</template>
      <template v-else-if="isUnion">
        {{ data.params?.strategy === 'strict' ? $t('operationNode.unionStrict') : $t('operationNode.unionAlign') }}
      </template>
      <span v-else-if="summary" class="summary" :title="summary">{{ summary }}</span>
      <template v-else>
        {{ $t('operationNode.paramCount', { n: paramCount }, paramCount) }}
      </template>
    </div>

    <div v-if="stats || cached" class="node-stats" :title="stats?.title">
      <template v-if="stats">
        <span v-if="stats.rows">{{ stats.rows }}</span>
        <span v-if="stats.rows" class="dot" aria-hidden="true">·</span>
        <span>{{ stats.cols }}</span>
        <span v-if="stats.noCache" class="nocache" :title="stats.noCache" role="img" :aria-label="stats.noCache"><RefreshCw :size="11" /></span>
      </template>
      <span v-if="cached" class="incache" :title="$t('nodeStats.cached')" role="img" :aria-label="$t('nodeStats.cached')"><DatabaseZap :size="11" /></span>
    </div>

    <Handle id="out" type="source" :position="Position.Right" />
  </div>
</template>

<style scoped>
/* the summary is made of column names and expressions: data, so monospace */
.summary {
  display: block; max-width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11.5px;
}
.node-join { min-width: 180px; position: relative; }
.handle-right { background: var(--accent-2) !important; }
.node-icon { color: var(--node-accent); }
/* the second input's label, just outside the left border at the handle's height */
.hlabel-left {
  position: absolute;
  left: -6px;
  top: 68%;
  transform: translate(-100%, -50%);
  font-size: 9px;
  line-height: 1;
  color: var(--accent-2);
  pointer-events: none;
  white-space: nowrap;
}
</style>
