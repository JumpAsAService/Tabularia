<script setup lang="ts">
import { computed } from 'vue'
import { Handle, Position } from '@vue-flow/core'
import { ArrowUp } from 'lucide-vue-next'
import { opMeta } from '~/composables/useOpIcons'

const props = defineProps<{ id: string; data: any }>()

const isJoin = computed(() => props.data?.opType === 'join')
const meta = computed(() => opMeta(props.data?.opType ?? ''))
const paramCount = computed(() => Object.keys(props.data?.params ?? {}).length)
</script>

<template>
  <div
    class="node node-op"
    :class="{ 'node-join': isJoin }"
    :style="{ '--node-accent': meta.color }"
  >
    <!-- input principale (catena): sempre a sinistra -->
    <Handle id="left" type="target" :position="Position.Left" />
    <!-- input "tabella da unire" del join: in ALTO, ben separato dal principale -->
    <template v-if="isJoin">
      <Handle id="right" type="target" :position="Position.Top" class="handle-right" />
      <span class="hlabel-top"><ArrowUp :size="9" /> tabella</span>
    </template>

    <div class="node-title">
      <component :is="meta.icon" :size="13" class="node-icon" />
      {{ data.opType || 'operazione' }}
    </div>
    <div class="node-body muted">
      <template v-if="isJoin">join {{ data.params?.how || 'inner' }}</template>
      <template v-else>
        {{ paramCount }} {{ paramCount === 1 ? 'parametro' : 'parametri' }}
      </template>
    </div>

    <Handle id="out" type="source" :position="Position.Right" />
  </div>
</template>

<style scoped>
.node-join { min-width: 180px; position: relative; }
.handle-right { background: var(--accent-2) !important; }
.node-icon { color: var(--node-accent); }
.hlabel-top {
  position: absolute;
  top: 3px;
  right: 8px;
  font-size: 9px;
  line-height: 1;
  color: var(--accent-2);
  pointer-events: none;
  display: inline-flex;
  align-items: center;
  gap: 2px;
}
</style>
