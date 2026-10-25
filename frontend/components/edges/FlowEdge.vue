<script setup lang="ts">
// A canvas edge with a removal affordance: a «×» in the middle, visible when the edge is
// selected or under the pointer. The stroke is Vue Flow's (BaseEdge), so the `animated`
// and `edge-seq` classes keep governing colour and dashes; the editor does the real
// removal (inject).
import { computed, inject, ref, type Ref } from 'vue'
import { BaseEdge, EdgeLabelRenderer, getBezierPath, type Position } from '@vue-flow/core'
import { X } from 'lucide-vue-next'
import { useI18n } from 'vue-i18n'

// Vue Flow passes all the EdgeProps with v-bind; here only those used are declared,
// inline: a type imported in defineProps would need TypeScript in the development
// container, which does not have it. The others stay attributes.
const props = defineProps<{
  id: string
  sourceX: number
  sourceY: number
  targetX: number
  targetY: number
  sourcePosition: string
  targetPosition: string
  markerEnd?: string
  style?: Record<string, any>
  selected?: boolean
}>()
// two roots (stroke + label): the undeclared attributes have nowhere to fall
defineOptions({ inheritAttrs: false })
const { t } = useI18n()
const remove = inject<(id: string) => void>('flowEdgeRemove', () => {})
const hoveredEdge = inject<Ref<string | null>>('flowEdgeHovered', ref(null))
const overButton = ref(false)

const path = computed(() =>
  getBezierPath({
    sourceX: props.sourceX,
    sourceY: props.sourceY,
    sourcePosition: props.sourcePosition as Position,
    targetX: props.targetX,
    targetY: props.targetY,
    targetPosition: props.targetPosition as Position,
  }),
)
const visible = computed(() => !!props.selected || hoveredEdge.value === props.id || overButton.value)
</script>

<template>
  <BaseEdge :id="id" :path="path[0]" :marker-end="markerEnd" :style="style" />
  <EdgeLabelRenderer>
    <button
      class="edge-remove nodrag nopan"
      :class="{ 'is-visible': visible, 'is-selected': selected }"
      :style="{ transform: `translate(-50%, -50%) translate(${path[1]}px, ${path[2]}px)` }"
      type="button"
      :tabindex="visible ? 0 : -1"
      :aria-label="t('flowEdge.remove')"
      :title="t('flowEdge.remove')"
      @mouseenter="overButton = true"
      @mouseleave="overButton = false"
      @click.stop="remove(id)"
    >
      <X :size="11" />
    </button>
  </EdgeLabelRenderer>
</template>

<style scoped>
.edge-remove {
  position: absolute;
  display: grid;
  place-items: center;
  width: 20px;
  height: 20px;
  padding: 0;
  border: 1px solid var(--border);
  border-radius: var(--radius-pill, 999px);
  background: var(--panel);
  color: var(--muted);
  box-shadow: var(--shadow-1);
  opacity: 0;
  pointer-events: none;
  transition: opacity 0.15s, color 0.15s, border-color 0.15s, background 0.15s;
}
.edge-remove.is-visible { opacity: 1; pointer-events: all; }
.edge-remove.is-selected { border-color: var(--accent); color: var(--text); }
.edge-remove:hover { border-color: var(--danger); color: var(--danger); background: var(--panel-2); box-shadow: var(--shadow-1); }
.edge-remove:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; opacity: 1; pointer-events: all; }
@media (prefers-reduced-motion: reduce) {
  .edge-remove { transition: none; }
}
</style>
