<script setup lang="ts">
// A node of a Bayesian model on the canvas (docs/specs/bayes-graph-editor.md, «Nodes»): neutral, its
// kind said by its icon and its shape, in plate notation — data a filled rectangle («observed»),
// parameters a pill («unknown»), a transformed value dashed, the linear predictor a sharp rectangle,
// the likelihood a double border, correlated effects a rounded card with a line per component.
// Colour only for what it encodes: the selection (accent), errors (danger), warnings (warning).
// What it shows comes from the canvas (BayesCanvas.vue provides it): the node itself holds an id.
// An edge is drawn by hand from its right handle to another node (the canvas asks which slot).
import { computed, inject } from 'vue'
import { useI18n } from 'vue-i18n'
import { Handle, Position } from '@vue-flow/core'
import { TriangleAlert } from 'lucide-vue-next'
import { BAYES_CANVAS_VIEW, type BayesCanvasView } from '~/composables/bayesCanvasView'

const props = defineProps<{ id: string }>()
const { t } = useI18n()
const canvas = inject(BAYES_CANVAS_VIEW) as BayesCanvasView
const v = computed(() => canvas.node(props.id))
const problems = computed(() => (v.value ? v.value.errors + v.value.warnings : 0))
</script>

<template>
  <div
    v-if="v"
    class="bn"
    :class="[`k-${v.kind}`, { selected: v.selected, err: v.errors > 0, warn: v.errors === 0 && v.warnings > 0 }]"
  >
    <Handle id="l" type="target" :position="Position.Left" />
    <span v-if="v.role" class="role">{{ v.role }}</span>
    <span
      v-if="v.errors > 0"
      class="badge"
      role="img"
      :aria-label="t('bayes.editor.node.problems', { n: problems })"
      :title="t('bayes.editor.node.problems', { n: problems })"
    >{{ v.errors }}</span>
    <span
      v-else-if="v.warnings > 0"
      class="wmark"
      role="img"
      :aria-label="t('bayes.editor.node.problems', { n: problems })"
      :title="t('bayes.editor.node.problems', { n: problems })"
    ><TriangleAlert :size="11" /></span>
    <div class="t"><component :is="v.icon" :size="13" class="ic" aria-hidden="true" /><span>{{ v.label }}</span></div>
    <div v-if="v.name || v.what" class="l2">
      <code v-if="v.name">{{ v.name }}</code>
      <span v-if="v.what" class="x">{{ v.what }}</span>
    </div>
    <div v-for="(line, i) in v.lines" :key="i" class="pr">{{ line }}</div>
    <Handle id="r" type="source" :position="Position.Right" />
  </div>
</template>

<style scoped>
.bn {
  position: relative; width: 180px; min-height: 56px; box-sizing: border-box;
  display: flex; flex-direction: column; justify-content: center; gap: 1px;
  padding: 6px 12px 7px; background: var(--panel); color: var(--text); font-size: 12px;
  border: 1px solid var(--edge); box-shadow: var(--shadow-1);
  transition: box-shadow var(--dur-2), border-color var(--dur-2);
}
@media (hover: hover) and (pointer: fine) { .bn:hover { box-shadow: var(--shadow-2); } }
.t { display: flex; align-items: flex-start; gap: 6px; min-width: 0; font-weight: 600; font-size: 12.5px; line-height: 1.25; }
.t span { overflow: hidden; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; }
.t .ic { flex: none; margin-top: 1px; color: var(--muted); }
.l2 { display: flex; align-items: baseline; gap: 6px; min-width: 0; white-space: nowrap; font-size: 11px; color: var(--muted); }
.l2 code { font-size: 11.5px; color: var(--text); }
.l2 .x { overflow: hidden; text-overflow: ellipsis; }
.pr { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11px; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.role {
  position: absolute; top: -8px; left: 18px; padding: 0 6px; border-radius: 6px;
  font-size: 9.5px; line-height: 14px; text-transform: uppercase; letter-spacing: 0.05em;
  border: 1px solid var(--edge); background: var(--panel); color: var(--muted);
}
.badge {
  position: absolute; top: -9px; right: 14px; min-width: 18px; height: 18px; padding: 0 5px; border-radius: 999px;
  display: inline-flex; align-items: center; justify-content: center;
  font-size: 10.5px; font-weight: 700; background: var(--danger-fill); color: var(--on-danger);
}
.wmark {
  position: absolute; top: -9px; right: 14px; width: 18px; height: 18px; border-radius: 999px;
  display: grid; place-items: center; background: var(--panel); color: var(--warning);
  border: 1px solid color-mix(in srgb, var(--warning) 55%, var(--border));
}
/* the shapes of plate notation */
.k-data { background: var(--panel-2); border-radius: 4px; }
.k-parameter, .k-state { border-radius: 999px; padding-inline: 16px; }
.k-transformed { border-radius: 12px; border-style: dashed; }
.k-linear_predictor { border-radius: 3px; }
.k-likelihood { border: 3px double var(--edge); border-radius: 4px; padding: 5px 11px 6px; }
.k-correlated_effects { border-radius: 26px; padding-inline: 20px; }
.bn.selected { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent), 0 4px 20px color-mix(in srgb, var(--accent) 25%, transparent); }
.bn.err { border-color: var(--danger); }
.bn.err.selected { box-shadow: 0 0 0 1px var(--danger), 0 4px 20px color-mix(in srgb, var(--danger) 22%, transparent); }
.k-likelihood.selected { border-color: var(--accent); }
.k-likelihood.err { border-color: var(--danger); }
</style>
