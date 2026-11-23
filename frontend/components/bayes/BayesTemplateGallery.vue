<script setup lang="ts">
// The empty canvas's proposal (docs/specs/bayes-graph-editor.md, «The templates and the free model»;
// the approved mockup «Where do you start?»): the ten templates in their three families, each a card
// with a small drawing of its layout (its boxes and nodes as rectangles, its edges as the mirror reads
// them: an SVG, no library), its title, its two sentences and what opening it asks; and «Empty model»,
// a button of the same weight beside the heading, one click away (answer 1).
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { Check, Plus, Ruler } from 'lucide-vue-next'
import { bayesGraphDecode, bayesGraphModel } from '~/composables/bayesGraph'
import { BAYES_NODE_H, BAYES_NODE_W } from '~/composables/bayesCanvasModel'
import { bayesOpenTemplate, bayesTemplates, type BayesTemplate } from '~/composables/bayesTemplates'

defineProps<{ chosen?: string | null }>()
const emit = defineEmits<{ (e: 'choose', id: string): void, (e: 'empty'): void }>()
const { t } = useI18n()

interface Drawing {
  view: string
  boxes: { x: number, y: number, w: number, h: number }[]
  nodes: { x: number, y: number, w: number, h: number, kind: string }[]
  edges: string[]
}

const LOGIT = new Set(['bernoulli_logit', 'binomial_logit'])
const LOG = new Set(['poisson_log', 'neg_binomial_2_log'])

// Drawn once per template: the layout as opened with no option, the edges the mirror finds.
function drawing (tpl: BayesTemplate): Drawing {
  const g = bayesOpenTemplate(tpl.id, {})
  const rect = (id: string) => {
    const at = g.layout[id]
    return at ? { x: at.x, y: at.y, w: at.w ?? BAYES_NODE_W, h: at.h ?? BAYES_NODE_H } : null
  }
  const boxes = g.plates.filter(p => p.kind === 'rows' || p.kind === 'group').map(p => rect(p.id)).filter(r => r !== null)
  const nodes = g.nodes.map(n => ({ ...rect(n.id), kind: n.kind })).filter((n): n is Drawing['nodes'][number] => typeof n.x === 'number')
  const all = [...boxes, ...nodes]
  const x0 = Math.min(...all.map(r => r.x)) - 20
  const y0 = Math.min(...all.map(r => r.y)) - 20
  const x1 = Math.max(...all.map(r => r.x + r.w)) + 20
  const y1 = Math.max(...all.map(r => r.y + r.h)) + 20
  const decoded = bayesGraphDecode(JSON.stringify(g))
  const model = decoded.graph ? bayesGraphModel(decoded.graph) : null
  const owner = (id: string) => id.split('.')[0]!
  const pairs = new Set<string>()
  const edges: string[] = []
  for (const e of model?.edges ?? []) {
    const a = rect(owner(e.from))
    const b = rect(owner(e.to))
    const key = `${owner(e.from)}>${owner(e.to)}`
    if (!a || !b || pairs.has(key) || owner(e.from) === owner(e.to)) continue
    pairs.add(key)
    const sx = a.x + a.w; const sy = a.y + a.h / 2
    const tx = b.x; const ty = b.y + b.h / 2
    const bend = Math.max(40, Math.abs(tx - sx) / 2)
    edges.push(`M${sx},${sy} C${sx + bend},${sy} ${tx - bend},${ty} ${tx},${ty}`)
  }
  return { view: `${x0} ${y0} ${x1 - x0} ${y1 - y0}`, boxes, nodes, edges }
}

/** What opening it asks, in a few words (the dialog asks it, every field optional). */
function asks (tpl: BayesTemplate): { scale: boolean, key: string } {
  if (tpl.order) return { scale: true, key: 'bayes.editor.gallery.asksOrder' }
  if (tpl.scale === 'outcome') return { scale: true, key: 'bayes.editor.gallery.asksScale' }
  const dist = String(tpl.graph.nodes.find(n => n.kind === 'likelihood')?.dist ?? '')
  return { scale: false, key: LOGIT.has(dist) ? 'bayes.editor.gallery.asksNoneLogit' : LOG.has(dist) ? 'bayes.editor.gallery.asksNoneLog' : 'bayes.editor.gallery.asksNone' }
}

const families = computed(() => (['glm', 'hierarchical', 'series'] as const).map(f => ({
  id: f,
  items: bayesTemplates().filter(x => x.family === f).map(x => ({ id: x.id, draw: drawing(x), asks: asks(x) })),
})))
</script>

<template>
  <section class="gallery" :aria-labelledby="'bg-title'">
    <div class="gh">
      <div class="ghr">
        <h2 id="bg-title">{{ t('bayes.editor.gallery.title') }}</h2>
        <button type="button" class="blank" @click="emit('empty')">
          <Plus :size="15" aria-hidden="true" />
          <span class="bl">{{ t('bayes.editor.gallery.empty') }}</span>
          <span class="bs">{{ t('bayes.editor.gallery.emptyWhat') }}</span>
        </button>
      </div>
      <p>{{ t('bayes.editor.gallery.intro') }}</p>
    </div>
    <section v-for="f in families" :key="f.id" class="grp" :aria-labelledby="`bg-${f.id}`">
      <div class="gtt">
        <h3 :id="`bg-${f.id}`">{{ t(`bayes.editor.family.${f.id}`) }}</h3>
        <span>{{ t(`bayes.editor.gallery.familyWhat.${f.id}`) }}</span>
      </div>
      <div class="cards">
        <button
          v-for="x in f.items"
          :key="x.id"
          type="button"
          class="tcard"
          :class="{ sel: chosen === x.id }"
          :aria-describedby="`bg-w-${x.id} bg-a-${x.id}`"
          @click="emit('choose', x.id)"
        >
          <svg class="draw" :viewBox="x.draw.view" preserveAspectRatio="xMidYMid meet" aria-hidden="true">
            <rect v-for="(b, i) in x.draw.boxes" :key="`b${i}`" class="db" :x="b.x" :y="b.y" :width="b.w" :height="b.h" rx="12" />
            <path v-for="(d, i) in x.draw.edges" :key="`e${i}`" class="de" :d="d" />
            <template v-for="(n, i) in x.draw.nodes" :key="`n${i}`">
              <rect :class="`dn k-${n.kind}`" :x="n.x" :y="n.y + 10" :width="n.w" :height="n.h - 20" :rx="n.kind === 'parameter' || n.kind === 'state' ? (n.h - 20) / 2 : n.kind === 'linear_predictor' || n.kind === 'likelihood' || n.kind === 'data' ? 3 : 10" />
              <rect v-if="n.kind === 'likelihood'" class="dn k-inner" :x="n.x + 8" :y="n.y + 18" :width="n.w - 16" :height="n.h - 36" rx="2" />
            </template>
          </svg>
          <span class="tt">{{ t(`bayes.templates.${x.id}.title`) }}</span>
          <span :id="`bg-w-${x.id}`" class="tw">{{ t(`bayes.templates.${x.id}.what`) }}</span>
          <span :id="`bg-a-${x.id}`" class="asks">
            <component :is="x.asks.scale ? Ruler : Check" :size="12" aria-hidden="true" />{{ t(x.asks.key) }}
          </span>
        </button>
      </div>
    </section>
  </section>
</template>

<style scoped>
.gallery { display: flex; flex-direction: column; gap: 18px; padding: 22px 24px 28px; max-width: 1080px; }
.ghr { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; }
.gh h2 { margin: 0; font-size: 16px; font-weight: 700; }
.gh p { margin: 6px 0 0; font-size: 12.5px; line-height: 1.5; color: var(--muted); max-width: 70ch; }
/* «Empty model»: the same weight as a card, dashed as a place still to fill */
.blank {
  margin-left: auto; display: inline-flex; align-items: baseline; gap: 7px; padding: 8px 14px; font-weight: 600;
  border-style: dashed; border-color: var(--control-border); background: color-mix(in srgb, var(--panel) 70%, transparent);
}
.blank :deep(svg) { align-self: center; }
.blank .bs { font-weight: 400; font-size: 12px; color: var(--muted); }
.grp { display: flex; flex-direction: column; gap: 8px; }
.gtt { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }
.gtt h3 { margin: 0; font-size: 11.5px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); }
.gtt span { font-size: 12px; color: var(--muted); }
.cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(176px, 1fr)); gap: 10px; }
.tcard {
  display: flex; flex-direction: column; align-items: stretch; justify-content: flex-start; gap: 6px; padding: 10px 11px;
  min-height: 0; white-space: normal; text-align: left; font: inherit; color: var(--text);
  border: 1px solid var(--border); border-radius: 10px; background: var(--panel); box-shadow: none;
  transition: border-color var(--dur-hover), box-shadow var(--dur-hover);
}
/* a card is a choice, not an action button: no sweep, no press scale */
.tcard::before { display: none; }
.tcard:active { transform: none; }
@media (hover: hover) and (pointer: fine) { .tcard:hover { border-color: var(--accent); box-shadow: var(--shadow-1); } }
.tcard:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.tcard.sel { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent); }
.draw { display: block; width: 100%; height: 56px; margin-bottom: 2px; }
/* drawn in canvas units (a model is about 1,000 wide, a card about 200): 5 units are a pixel */
.draw .db { fill: none; stroke: var(--muted); stroke-width: 4.5; opacity: 0.7; }
.draw .de { fill: none; stroke: var(--edge); stroke-width: 4.5; }
.draw .dn { fill: var(--panel); stroke: var(--edge); stroke-width: 5.5; }
.draw .dn.k-data { fill: var(--panel-2); }
.draw .dn.k-transformed { fill: none; stroke-dasharray: 14 10; }
.draw .dn.k-inner { fill: none; stroke-width: 4.5; }
.tt { font-size: 13px; font-weight: 600; line-height: 1.3; }
.tw { font-size: 11.5px; line-height: 1.42; color: var(--muted); }
.asks { margin-top: auto; padding-top: 2px; display: flex; align-items: center; gap: 5px; font-size: 11px; color: var(--muted); }
.asks :deep(svg) { flex: none; }
</style>
