<script setup lang="ts">
// Container del ciclo: i nodi trascinati DENTRO diventano il corpo (figli Vue
// Flow con parentNode). Input sinistro = dati; input in alto = driver (una
// riga = un'iterazione, le colonne sono i placeholder {{colonna}}).
import { Handle, Position } from '@vue-flow/core'
import { NodeResizer } from '@vue-flow/node-resizer'
import { Repeat, ArrowUp } from 'lucide-vue-next'

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
    <Handle id="left" type="target" :position="Position.Left" />
    <Handle id="right" type="target" :position="Position.Top" class="handle-driver" />
    <span class="hlabel-top"><ArrowUp :size="9" /> driver</span>

    <div class="foreach-title">
      <Repeat :size="13" class="ico" />
      ciclo foreach
      <span class="muted hint">trascina qui le operazioni del corpo</span>
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
.hlabel-top {
  position: absolute;
  top: -14px;
  left: 50%;
  transform: translateX(14px);
  font-size: 9px;
  line-height: 1;
  color: #f472b6;
  pointer-events: none;
  display: inline-flex;
  align-items: center;
  gap: 2px;
}
</style>
