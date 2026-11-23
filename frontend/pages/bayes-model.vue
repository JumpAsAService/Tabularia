<script setup lang="ts">
// The editor of a Bayesian model (docs/specs/bayes-graph-editor.md). Lot G0, its first screen:
// /bayes-model?template=<id> opens a template on local data — the dialog asks what opening it asks
// (the outcome's typical value and spread, the AR order), then the canvas draws its nodes, edges and
// nested boxes, and the inspector of a parameter edits its prior's numbers with the chart of that
// prior alive beside them. Nothing is saved (the model store comes with lot G3); the page is reached
// by its address only, with no link in the top bar.
//
// The graph document is the truth: every edit changes it, and the mirror (bayesGraph.ts) reads it
// again at once — the findings on the nodes, the expressions' errors at their span, the prior plan
// the chart draws from.
import { computed, ref, shallowRef, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { Info, LayoutTemplate, SlidersHorizontal } from 'lucide-vue-next'
import BrandMark from '~/components/ui/BrandMark.vue'
import GridKeys from '~/components/ui/GridKeys.vue'
import BayesCanvas from '~/components/bayes/BayesCanvas.vue'
import BayesInspector from '~/components/bayes/BayesInspector.vue'
import BayesTemplateDialog from '~/components/bayes/BayesTemplateDialog.vue'
import { useKeyGroups, useKeyHelp } from '~/composables/useKeyHelp'
import { bayesGraphDecode, bayesGraphModel, bayesPriorPlan, type BayesGraphFinding } from '~/composables/bayesGraph'
import { bayesOpenTemplate, bayesTemplate, bayesTemplates, type BayesOpenOptions, type BayesTemplateGraph } from '~/composables/bayesTemplates'

const { t, te } = useI18n()
const route = useRoute()

const templateId = computed(() => (typeof route.query.template === 'string' ? route.query.template : ''))
const template = computed(() => (templateId.value ? bayesTemplate(templateId.value) : undefined))
const families = computed(() => (['glm', 'hierarchical', 'series'] as const).map(f => ({ id: f, items: bayesTemplates().filter(x => x.family === f) })))

const doc = ref<BayesTemplateGraph | null>(null)
const dialogOpen = ref(false)
const selected = ref<string | null>(null)
watch(templateId, () => {
  doc.value = null
  selected.value = null
  dialogOpen.value = !!template.value
}, { immediate: true })

const texts = (key: string): string | undefined => (te(key) ? t(key) : undefined)
function openModel (options: BayesOpenOptions) {
  if (!template.value) return
  doc.value = bayesOpenTemplate(template.value.id, options, texts)
  selected.value = null
  dialogOpen.value = false
}

useHead({ title: computed(() => (template.value ? `${t(`bayes.templates.${template.value.id}.title`)} · ${t('bayes.editor.title')}` : t('bayes.editor.title'))) })

// ---------------------------------------------------------------- the mirror

// The document is decoded and validated again after every change: a graph is at most 200 nodes, and
// the mirror's time on a template is a few milliseconds, so every keystroke is checked at once.
const decoded = computed(() => (doc.value ? bayesGraphDecode(JSON.stringify(doc.value)) : null))
const model = computed(() => (decoded.value?.graph ? bayesGraphModel(decoded.value.graph) : null))
const findings = computed<BayesGraphFinding[]>(() => [...(decoded.value?.findings ?? []), ...(model.value?.findings ?? [])])
const plan = shallowRef(model.value ? bayesPriorPlan(model.value) : null)
watch(model, (m) => { plan.value = m ? bayesPriorPlan(m) : null }, { immediate: true })

// ---------------------------------------------------------------- the edits of this lot

function nodeOf (id: string) { return doc.value?.nodes.find(n => n.id === id) }
function setArg (id: string, arg: string, text: string) {
  const n = nodeOf(id)
  if (!n?.prior) return
  n.prior.args[arg] = { expr: text }
}
function setBound (id: string, which: 'lower' | 'upper', value: number | null) {
  const n = nodeOf(id)
  if (!n) return
  const b = { ...((n.bounds as Record<string, number> | undefined) ?? {}) }
  if (value === null) delete b[which]
  else b[which] = value
  if (Object.keys(b).length) n.bounds = b
  else delete n.bounds
}
function setForm (plate: string, nonCentred: boolean) {
  const p = doc.value?.plates.find(x => x.id === plate)
  if (p) p.form = nonCentred ? 'non_centred' : 'centred'
}

// ---------------------------------------------------------------- the panel

const selectedKind = computed(() => (selected.value ? nodeOf(selected.value)?.kind : undefined))
// the panel opens at 600 px for a node with a chart, where the canvas keeps 560 px (main.css below)
const wide = computed(() => selectedKind.value === 'parameter')
const canvas = ref<InstanceType<typeof BayesCanvas> | null>(null)
function backToCanvas () {
  if (selected.value) canvas.value?.focusNode(selected.value)
}

// ---------------------------------------------------------------- the keys

const canvasKeys = ref<InstanceType<typeof GridKeys> | null>(null)
const keyHelp = useKeyHelp()
keyHelp.host(() => canvasKeys.value?.toggle())
useKeyGroups(() => {
  const k = keyHelp.names
  return [{
    title: t('bayes.editor.keys.group'),
    keys: [
      [`Tab · ${k.enter}`, t('bayes.editor.keys.tabEnter')],
      [`${k.chord(k.alt, '←')} · ${k.chord(k.alt, '↑')} · ${k.chord(k.alt, '→')} · ${k.chord(k.alt, '↓')}`, t('bayes.editor.keys.walk')],
      ['+ · - · 0', t('bayes.editor.keys.zoom')],
      ['Esc', t('bayes.editor.keys.escape')],
    ],
  }]
})
</script>

<template>
  <div class="ed" :class="{ wide }">
    <header class="etb">
      <span class="brand"><BrandMark :size="20" /></span>
      <span class="sep" aria-hidden="true" />
      <h1 class="mname">
        <LayoutTemplate :size="15" aria-hidden="true" />
        <span>{{ template ? t(`bayes.templates.${template.id}.title`) : t('bayes.editor.title') }}</span>
      </h1>
      <span class="preview"><Info :size="13" aria-hidden="true" />{{ t('bayes.editor.preview') }}</span>
      <span class="grow" />
      <button v-if="template && doc" type="button" class="mini" @click="dialogOpen = true">
        <SlidersHorizontal :size="13" aria-hidden="true" />{{ t('bayes.editor.reopen') }}
      </button>
      <GridKeys ref="canvasKeys" :groups="keyHelp.groups.value" :title="t('appKeys.title')" :label="t('canvasKeys.button')" wide />
    </header>

    <ClientOnly>
      <template v-if="doc">
        <main class="cv">
          <BayesCanvas
            ref="canvas"
            :doc="doc"
            :model="model"
            :findings="findings"
            :selected="selected"
            @select="(id: string | null) => (selected = id)"
            @form="setForm"
          />
        </main>
        <aside class="side" :aria-label="t('bayes.editor.inspector.title')">
          <div class="tabs"><span class="tab">{{ t('bayes.editor.inspector.title') }}</span></div>
          <div class="sbody">
            <BayesInspector
              :doc="doc"
              :model="model"
              :findings="findings"
              :plan="plan"
              :selected="selected"
              @arg="setArg"
              @bound="setBound"
              @form="setForm"
              @select="(id: string) => (selected = id)"
              @escape="backToCanvas"
            />
          </div>
        </aside>
      </template>
      <main v-else class="start">
        <div class="card">
          <h2>{{ template ? t(`bayes.templates.${template.id}.title`) : t('bayes.editor.start.title') }}</h2>
          <p v-if="templateId && !template" class="m">{{ t('bayes.editor.start.unknown', { id: templateId }) }}</p>
          <p class="m">{{ t('bayes.editor.start.what') }}</p>
          <button v-if="template" type="button" class="primary" @click="dialogOpen = true">
            <LayoutTemplate :size="14" aria-hidden="true" />{{ t('bayes.editor.dialog.open') }}
          </button>
          <nav v-else class="list" :aria-label="t('bayes.editor.start.title')">
            <section v-for="f in families" :key="f.id">
              <h3>{{ t(`bayes.editor.family.${f.id}`) }}</h3>
              <ul>
                <li v-for="x in f.items" :key="x.id">
                  <NuxtLink :to="{ path: '/bayes-model', query: { template: x.id } }">{{ t(`bayes.templates.${x.id}.title`) }}</NuxtLink>
                  <span>{{ t(`bayes.templates.${x.id}.what`) }}</span>
                </li>
              </ul>
            </section>
          </nav>
        </div>
      </main>
      <BayesTemplateDialog v-if="template" :template="template" :open="dialogOpen" @open="openModel" @cancel="dialogOpen = false" />
    </ClientOnly>
  </div>
</template>

<style scoped>
.ed {
  --panel-w: 420px;
  display: grid; height: 100vh; gap: 1px; background: var(--border-soft);
  grid-template-rows: auto minmax(0, 1fr);
  grid-template-columns: minmax(0, 1fr) var(--panel-w);
  grid-template-areas: "toolbar toolbar" "canvas panel";
}
/* a node with a chart opens the panel at 600 px where the canvas keeps 560 px (a window of 1400 px or more) */
.ed.wide { --panel-w: 600px; }
@media (max-width: 1399px) { .ed.wide { --panel-w: 420px; } }
.etb {
  grid-area: toolbar; display: flex; align-items: center; gap: 10px; min-width: 0; padding: 8px 12px;
  background: var(--topbar-bg); backdrop-filter: blur(8px); border-bottom: 1px solid var(--border-soft);
}
.brand { display: inline-flex; align-items: center; }
.sep { width: 1px; align-self: stretch; background: var(--border); flex: none; }
.mname { margin: 0; display: inline-flex; align-items: center; gap: 7px; min-width: 0; font-size: 14px; font-weight: 600; }
.mname span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.mname :deep(svg) { flex: none; color: var(--muted); }
.preview {
  display: inline-flex; align-items: center; gap: 5px; flex: none; padding: 2px 9px; border-radius: 999px;
  font-size: 11.5px; color: var(--muted); border: 1px solid var(--border); background: var(--panel-2); white-space: nowrap;
}
.grow { flex: 1 1 0; min-width: 0; }
.cv { grid-area: canvas; min-width: 0; min-height: 0; position: relative; }
.side { grid-area: panel; display: flex; flex-direction: column; min-width: 0; min-height: 0; background: var(--panel); }
.tabs { display: flex; align-items: flex-end; gap: 4px; padding: 8px 10px 0; border-bottom: 1px solid var(--border-soft); flex: none; }
.tab {
  display: inline-flex; align-items: center; gap: 6px; padding: 5px 10px; font-size: 12px; color: var(--text);
  border: 1px solid var(--border); border-bottom: 2px solid var(--accent); border-radius: 6px 6px 0 0; background: var(--tint-accent);
}
.sbody { flex: 1; min-height: 0; overflow-y: auto; padding: 14px 16px 18px; }
.start { grid-column: 1 / -1; grid-row: 2; background: var(--bg); overflow-y: auto; padding: 40px 24px; }
.card { max-width: 760px; margin: 0 auto; display: flex; flex-direction: column; gap: 12px; }
.card h2 { margin: 0; font-size: 19px; font-weight: 700; }
.card .m { margin: 0; font-size: 13px; line-height: 1.5; color: var(--muted); max-width: 68ch; }
.card .primary { align-self: flex-start; }
.list { display: flex; flex-direction: column; gap: 18px; margin-top: 8px; }
.list h3 { margin: 0 0 6px; font-size: 11.5px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); }
.list ul { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; border: 1px solid var(--border); border-radius: 10px; background: var(--panel); }
.list li { display: flex; flex-direction: column; gap: 2px; padding: 10px 14px; border-top: 1px solid var(--border-soft); }
.list li:first-child { border-top: 0; }
.list a { font-weight: 500; color: var(--text); text-decoration: none; }
@media (hover: hover) and (pointer: fine) { .list a:hover { color: var(--accent); text-decoration: underline; text-underline-offset: 3px; } }
.list li span { font-size: 12px; line-height: 1.45; color: var(--muted); }
</style>
