<script setup lang="ts">
// The loop's container: the nodes dragged INSIDE become the body (Vue Flow children with
// parentNode). Inputs on the left: top = data, bottom = driver (a row = an iteration,
// the columns are the {{column}} placeholders).
import { Handle, Position } from '@vue-flow/core'
import { NodeResizer } from '@vue-flow/node-resizer'
import { Repeat } from 'lucide-vue-next'

defineProps<{ id: string; data: any }>()
</script>

<template>
  <div class="foreach-box">
    <!-- resize handles on edges and corners -->
    <NodeResizer
      :min-width="280"
      :min-height="160"
      color="#f472b6"
      :line-style="{ borderWidth: '1px', borderColor: 'rgba(244, 114, 182, 0.35)' }"
    />
    <Handle id="left" type="target" :position="Position.Left" class="h-left-top" />
    <Handle id="right" type="target" :position="Position.Left" class="handle-driver h-left-bottom" />
    <span class="hlabel-left">{{ $t('foreachNode.driverLabel') }}</span>

    <div class="foreach-title">
      <Repeat :size="13" class="ico" />
      {{ $t('foreachNode.title') }}
      <span class="muted hint">{{ $t('foreachNode.hint') }}</span>
    </div>

    <Handle id="out" type="source" :position="Position.Right" />
  </div>
</template>

<style scoped>
.foreach-box {
  width: 100%;
  height: 100%;
  border: 1.5px dashed #f472b6;
  border-radius: 12px;
  background: rgba(244, 114, 182, 0.045);
  position: relative;
}
.foreach-title {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 10px;
  font-size: 12px;
  font-weight: 600;
  border-bottom: 1px dashed rgba(244, 114, 182, 0.35);
  border-radius: 12px 12px 0 0;
  background: rgba(244, 114, 182, 0.08);
}
.ico { color: #f472b6; }
.hint { font-weight: 400; font-size: 10px; margin-left: auto; }
.handle-driver { background: #f472b6 !important; }
/* the driver's label, just outside the left border at the handle's height */
.hlabel-left {
  position: absolute;
  left: -6px;
  top: 68%;
  transform: translate(-100%, -50%);
  font-size: 9px;
  line-height: 1;
  color: color-mix(in srgb, #f472b6 var(--enc-text-mix), var(--text));
  pointer-events: none;
  white-space: nowrap;
}
</style>
