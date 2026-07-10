<script setup lang="ts">
// Palette dei componenti: trascina una voce sul canvas per creare il nodo.
// Il tipo viaggia nel dataTransfer (application/tabularia), letto dal drop
// handler di FlowEditor.
import { computed } from 'vue'
import { GripVertical, HardDriveDownload } from 'lucide-vue-next'
import { opMeta, SOURCE_META } from '~/composables/useOpIcons'

const props = defineProps<{ operations: string[] }>()

// foreach ha una voce dedicata nel gruppo "Controllo" (è un container, non
// un'operazione normale della catena)
const transformOps = computed(() => props.operations.filter((o) => o !== 'foreach'))
const hasForeach = computed(() => props.operations.includes('foreach'))

function onDragStart(ev: DragEvent, kind: string) {
  ev.dataTransfer?.setData('application/tabularia', kind)
  if (ev.dataTransfer) ev.dataTransfer.effectAllowed = 'move'
}
</script>

<template>
  <aside class="opsidebar">
    <div class="group-title">Sorgenti</div>
    <div
      class="item"
      draggable="true"
      :style="{ '--item-color': SOURCE_META.color }"
      @dragstart="onDragStart($event, 'source')"
    >
      <component :is="SOURCE_META.icon" :size="15" class="item-icon" />
      <span>{{ SOURCE_META.label }}</span>
      <GripVertical :size="13" class="grip" />
    </div>

    <div class="group-title">Trasformazioni</div>
    <div
      v-for="op in transformOps"
      :key="op"
      class="item"
      draggable="true"
      :style="{ '--item-color': opMeta(op).color }"
      @dragstart="onDragStart($event, `op:${op}`)"
    >
      <component :is="opMeta(op).icon" :size="15" class="item-icon" />
      <span>{{ opMeta(op).label || op }}</span>
      <GripVertical :size="13" class="grip" />
    </div>

    <template v-if="hasForeach">
      <div class="group-title">Controllo</div>
      <div
        class="item"
        draggable="true"
        :style="{ '--item-color': opMeta('foreach').color }"
        @dragstart="onDragStart($event, 'op:foreach')"
      >
        <component :is="opMeta('foreach').icon" :size="15" class="item-icon" />
        <span>{{ opMeta('foreach').label }}</span>
        <GripVertical :size="13" class="grip" />
      </div>
    </template>

    <div class="group-title">Output</div>
    <div
      class="item"
      draggable="true"
      :style="{ '--item-color': '#fbbf24' }"
      @dragstart="onDragStart($event, 'output')"
    >
      <HardDriveDownload :size="15" class="item-icon" />
      <span>Output (datasource / database)</span>
      <GripVertical :size="13" class="grip" />
    </div>

    <p v-if="!operations.length" class="muted empty">Operazioni non caricate.</p>
  </aside>
</template>

<style scoped>
.opsidebar {
  height: 100%;
  overflow-y: auto;
  padding: 10px 8px;
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.group-title {
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.12em;
  color: var(--muted);
  padding: 8px 6px 4px;
}
.item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 8px;
  border: 1px solid transparent;
  border-radius: 8px;
  font-size: 13px;
  cursor: grab;
  user-select: none;
  transition: background 0.12s, border-color 0.12s, transform 0.12s;
}
.item:hover {
  background: var(--panel-2);
  border-color: var(--border);
  transform: translateX(2px);
}
.item:active { cursor: grabbing; }
.item-icon { color: var(--item-color); flex-shrink: 0; }
.item span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.grip { margin-left: auto; color: var(--border); flex-shrink: 0; }
.item:hover .grip { color: var(--muted); }
.empty { font-size: 12px; padding: 0 6px; }
</style>
