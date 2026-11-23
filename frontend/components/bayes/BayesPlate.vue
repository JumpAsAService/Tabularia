<script setup lang="ts">
// A box of a Bayesian model (docs/specs/bayes-graph-editor.md, «Plates»): a Vue Flow parent node whose
// children are the nodes on its plate; a group's box nests in its parent's. Its header holds the
// box icon, its label, «one per value of `school`», and the switch «Non-centred» (on by default,
// answer 4); the rows' box holds the switch «Time series». Selected, it is resized by its edges and
// corners (NodeResizer): what it holds stays where it is on the canvas. While a node or a box is
// dragged over it, it says it would take it.
import { computed, inject } from 'vue'
import { useI18n } from 'vue-i18n'
import { NodeResizer } from '@vue-flow/node-resizer'
import { Boxes, Rows3 } from 'lucide-vue-next'
import { BAYES_CANVAS_VIEW, type BayesCanvasView, type BayesResizeParams } from '~/composables/bayesCanvasView'

const props = defineProps<{ id: string }>()
const { t } = useI18n()
const canvas = inject(BAYES_CANVAS_VIEW) as BayesCanvasView
const v = computed(() => canvas.plate(props.id))
const switchId = `bp-${props.id}-${Math.random().toString(36).slice(2, 7)}`
function toggle (e: Event) {
  if (!v.value) return
  const on = (e.target as HTMLInputElement).checked
  if (v.value.kind === 'group') canvas.setForm(v.value.id, on)
  else canvas.setSeries(on)
}
const params = (e: { params: BayesResizeParams }) => ({ x: e.params.x, y: e.params.y, width: e.params.width, height: e.params.height })
</script>

<template>
  <div v-if="v" class="plate" :class="{ selected: v.selected, inner: v.nested, err: v.errors > 0, target: v.target }">
    <NodeResizer
      :node-id="id"
      :is-visible="v.selected && !canvas.readonly()"
      :min-width="200"
      :min-height="80"
      line-class-name="bp-line"
      handle-class-name="bp-handle"
      @resize="(e: any) => canvas.resizing(id, params(e))"
      @resize-end="(e: any) => canvas.resized(id, params(e))"
    />
    <div class="ph">
      <component :is="v.kind === 'rows' ? Rows3 : Boxes" :size="15" class="ic" aria-hidden="true" />
      <b>{{ v.label || '…' }}</b>
      <span v-if="v.kind === 'group' && v.keyName" class="key">
        · {{ t('bayes.editor.plate.onePer') }} <code>{{ v.keyName }}</code>
      </span>
      <span v-else-if="v.kind === 'rows'" class="key">· {{ t('bayes.editor.plate.rows') }}</span>
      <label class="sw nodrag nopan" :for="switchId" @mousedown.stop @click.stop>
        <input :id="switchId" type="checkbox" role="switch" :disabled="canvas.readonly()" :checked="v.kind === 'group' ? v.nonCentred : v.series" @change="toggle">
        {{ t(v.kind === 'group' ? 'bayes.editor.plate.nonCentred' : 'bayes.editor.plate.series') }}
      </label>
    </div>
  </div>
</template>

<style scoped>
.plate {
  width: 100%; height: 100%; box-sizing: border-box; border-radius: 10px;
  border: 1px solid var(--edge); background: color-mix(in srgb, var(--panel) 38%, transparent);
  transition: border-color var(--dur-2), background-color var(--dur-2);
}
.plate.inner { background: color-mix(in srgb, var(--panel) 55%, transparent); }
.plate.selected { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent), 0 4px 20px color-mix(in srgb, var(--accent) 22%, transparent); }
.plate.err { border-color: var(--danger); }
/* a drop target speaks the designer's language: a dashed accent outline inside, a 10% accent fill */
.plate.target { background: color-mix(in srgb, var(--accent) 12%, transparent); outline: 2px dashed var(--accent); outline-offset: -6px; }
.ph {
  height: 34px; display: flex; align-items: center; gap: 7px; min-width: 0; padding: 0 10px 0 11px;
  border-bottom: 1px solid var(--border-soft); border-radius: 9px 9px 0 0; cursor: grab;
  background: color-mix(in srgb, var(--panel-2) 70%, transparent); font-size: 12px; white-space: nowrap;
}
.ph:active { cursor: grabbing; }
.ph .ic { flex: none; color: var(--muted); }
/* the label keeps its room; «one per value of…» gives way first */
.ph b { flex: 0 1 auto; max-width: 60%; font-weight: 600; font-size: 12.5px; min-width: 0; overflow: hidden; text-overflow: ellipsis; }
.key { flex: 1 1 0; min-width: 0; overflow: hidden; text-overflow: ellipsis; color: var(--muted); }
.key code { color: var(--text); font-size: 11.5px; }
/* the switch at the right end of the header */
.sw { margin-left: auto; flex: none; display: inline-flex; align-items: center; gap: 6px; font-size: 11.5px; color: var(--text); cursor: pointer; }
/* the resizer in the app's tokens: lines invisible until pointed at, handles as the canvas' handles */
.plate :deep(.bp-line) { border-color: transparent; }
.plate :deep(.bp-line.left), .plate :deep(.bp-line.right) { width: 8px; }
.plate :deep(.bp-line.top), .plate :deep(.bp-line.bottom) { height: 8px; }
@media (hover: hover) and (pointer: fine) { .plate :deep(.bp-line:hover) { border-color: color-mix(in srgb, var(--accent) 55%, transparent); } }
.plate :deep(.bp-handle) { width: 9px; height: 9px; border-radius: 2px; background: var(--panel); border: 1.5px solid var(--accent); }
@media (prefers-reduced-motion: reduce) { .plate { transition: none; } }
</style>
