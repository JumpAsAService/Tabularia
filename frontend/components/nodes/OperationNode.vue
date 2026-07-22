<script setup lang="ts">
import { computed } from 'vue'
import { Handle, Position } from '@vue-flow/core'
import { opMeta } from '~/composables/useOpIcons'

const props = defineProps<{ id: string; data: any }>()

const isJoin = computed(() => props.data?.opType === 'join')
const isUnion = computed(() => props.data?.opType === 'union')
// operazioni con un secondo input (ramo destro): sta a sinistra, sotto il principale
const needsRight = computed(() => isJoin.value || isUnion.value)
const meta = computed(() => opMeta(props.data?.opType ?? ''))
const paramCount = computed(() => Object.keys(props.data?.params ?? {}).length)
</script>

<template>
  <div
    class="node node-op"
    :class="{ 'node-join': needsRight }"
    :style="{ '--node-accent': meta.color }"
  >
    <!-- input principale (catena): a sinistra (in alto se c'è un secondo input) -->
    <Handle id="left" type="target" :position="Position.Left" :class="needsRight ? 'h-left-top' : ''" />
    <!-- secondo input (join: tabella da unire; union: ramo da accodare): a sinistra, sotto -->
    <template v-if="needsRight">
      <Handle id="right" type="target" :position="Position.Left" class="handle-right h-left-bottom" />
      <span class="hlabel-left">{{ $t('operationNode.tableInputLabel') }}</span>
    </template>

    <div class="node-title">
      <component :is="meta.icon" :size="13" class="node-icon" />
      {{ data.opType || $t('operationNode.operationFallback') }}
    </div>
    <div class="node-body muted">
      <template v-if="isJoin">{{ $t('operationNode.joinLabel', { how: data.params?.how || 'inner' }) }}</template>
      <template v-else-if="isUnion">
        {{ data.params?.strategy === 'strict' ? $t('operationNode.unionStrict') : $t('operationNode.unionAlign') }}
      </template>
      <template v-else>
        {{ $t('operationNode.paramCount', { n: paramCount }) }}
      </template>
    </div>

    <Handle id="out" type="source" :position="Position.Right" />
  </div>
</template>

<style scoped>
.node-join { min-width: 180px; position: relative; }
.handle-right { background: var(--accent-2) !important; }
.node-icon { color: var(--node-accent); }
/* etichetta del secondo input, appena fuori dal bordo sinistro all'altezza dell'handle */
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
