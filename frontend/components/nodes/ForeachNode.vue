<script setup lang="ts">
// Container del ciclo: i nodi trascinati DENTRO diventano il corpo (figli Vue
// Flow con parentNode). Input a sinistra: sopra = dati, sotto = driver (una
// riga = un'iterazione, le colonne sono i placeholder {{colonna}}).
import { Handle, Position } from '@vue-flow/core'
import { NodeResizer } from '@vue-flow/node-resizer'
import { Repeat } from 'lucide-vue-next'

defineProps<{ id: string; data: any }>()
</script>

<template>
  <div class="foreach-box">
    <!-- maniglie di ridimensionamento su bordi e angoli -->
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
/* etichetta del driver, appena fuori dal bordo sinistro all'altezza dell'handle */
.hlabel-left {
  position: absolute;
  left: -6px;
  top: 68%;
  transform: translate(-100%, -50%);
  font-size: 9px;
  line-height: 1;
  color: #f472b6;
  pointer-events: none;
  white-space: nowrap;
}
</style>
