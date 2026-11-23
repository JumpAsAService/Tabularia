<script setup lang="ts">
// A box of a Bayesian model (docs/specs/bayes-graph-editor.md, «Plates»): a Vue Flow parent node whose
// children are the nodes on its plate; a group's box nests in its parent's. Its header holds the
// box icon, its label, «one per value of `school`», and the switch «Non-centred» (on by default,
// answer 4); the rows' box says whether its rows are a time series (changed in a later lot).
import { computed, inject } from 'vue'
import { useI18n } from 'vue-i18n'
import { Boxes, Rows3 } from 'lucide-vue-next'
import { BAYES_CANVAS_VIEW, type BayesCanvasView } from '~/composables/bayesCanvasView'

const props = defineProps<{ id: string }>()
const { t } = useI18n()
const canvas = inject(BAYES_CANVAS_VIEW) as BayesCanvasView
const v = computed(() => canvas.plate(props.id))
const switchId = `bp-${props.id}-${Math.random().toString(36).slice(2, 7)}`
function toggle (e: Event) {
  if (!v.value) return
  canvas.setForm(v.value.id, (e.target as HTMLInputElement).checked)
}
</script>

<template>
  <div v-if="v" class="plate" :class="{ selected: v.selected, inner: v.nested, err: v.errors > 0 }">
    <div class="ph">
      <component :is="v.kind === 'rows' ? Rows3 : Boxes" :size="15" class="ic" aria-hidden="true" />
      <b>{{ v.label }}</b>
      <span v-if="v.kind === 'group' && v.keyName" class="key">
        · {{ t('bayes.editor.plate.onePer') }} <code>{{ v.keyName }}</code>
      </span>
      <span v-else-if="v.kind === 'rows'" class="key">· {{ t('bayes.editor.plate.rows') }}</span>
      <label v-if="v.kind === 'group'" class="sw nodrag nopan" :for="switchId" @mousedown.stop @click.stop>
        <input :id="switchId" type="checkbox" role="switch" :checked="v.nonCentred" @change="toggle">
        {{ t('bayes.editor.plate.nonCentred') }}
      </label>
      <label v-else class="sw off nodrag nopan" :for="switchId" :title="t('bayes.editor.plate.seriesLater')" @mousedown.stop @click.stop>
        <input :id="switchId" type="checkbox" role="switch" :checked="v.series" disabled>
        {{ t('bayes.editor.plate.series') }}
      </label>
    </div>
  </div>
</template>

<style scoped>
.plate {
  width: 100%; height: 100%; box-sizing: border-box; border-radius: 10px;
  border: 1px solid var(--edge); background: color-mix(in srgb, var(--panel) 38%, transparent);
}
.plate.inner { background: color-mix(in srgb, var(--panel) 55%, transparent); }
.plate.selected { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent), 0 4px 20px color-mix(in srgb, var(--accent) 22%, transparent); }
.plate.err { border-color: var(--danger); }
.ph {
  height: 34px; display: flex; align-items: center; gap: 7px; min-width: 0; padding: 0 10px 0 11px;
  border-bottom: 1px solid var(--border-soft); border-radius: 9px 9px 0 0;
  background: color-mix(in srgb, var(--panel-2) 70%, transparent); font-size: 12px; white-space: nowrap;
}
.ph .ic { flex: none; color: var(--muted); }
.ph b { font-weight: 600; font-size: 12.5px; }
.key { min-width: 0; overflow: hidden; text-overflow: ellipsis; color: var(--muted); }
.key code { color: var(--text); font-size: 11.5px; }
/* the switch at the right end of the header */
.sw { margin-left: auto; flex: none; display: inline-flex; align-items: center; gap: 6px; font-size: 11.5px; color: var(--text); cursor: pointer; }
.sw.off { color: var(--muted); cursor: default; }
</style>
