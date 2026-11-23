<script setup lang="ts">
// The glossary of the side panel (docs/specs/bayes-graph-editor.md, «Explanations for a reader who
// does not know Bayes»): the words the editor uses, each in two sentences of facts — prior,
// likelihood, posterior, hyperprior, box, non-centred, standardized — and the ones the canvas draws
// beside them (data, parameter, linear predictor, latent state, correlated effects).
import { useI18n } from 'vue-i18n'
import { Info } from 'lucide-vue-next'

const { t } = useI18n()
const TERMS = ['prior', 'likelihood', 'posterior', 'hyperprior', 'box', 'nonCentred', 'standardized'] as const
const MORE = ['data', 'parameter', 'linearPredictor', 'state', 'effects', 'lag'] as const
</script>

<template>
  <div class="gloss">
    <h2><Info :size="15" aria-hidden="true" />{{ t('bayes.editor.glossary.title') }}</h2>
    <p class="lead">{{ t('bayes.editor.glossary.lead') }}</p>
    <dl>
      <div v-for="k in TERMS" :key="k">
        <dt>{{ t(`bayes.editor.glossary.term.${k}.name`) }}</dt>
        <dd>{{ t(`bayes.editor.glossary.term.${k}.what`) }}</dd>
      </div>
    </dl>
    <h3>{{ t('bayes.editor.glossary.more') }}</h3>
    <dl>
      <div v-for="k in MORE" :key="k">
        <dt>{{ t(`bayes.editor.glossary.term.${k}.name`) }}</dt>
        <dd>{{ t(`bayes.editor.glossary.term.${k}.what`) }}</dd>
      </div>
    </dl>
  </div>
</template>

<style scoped>
.gloss { display: flex; flex-direction: column; gap: 12px; min-width: 0; }
.gloss h2 { display: flex; align-items: center; gap: 7px; margin: 0; font-size: 16px; font-weight: 700; }
.gloss h2 :deep(svg) { color: var(--muted); }
.gloss h3 { margin: 6px 0 0; padding-top: 12px; border-top: 1px solid var(--border-soft); font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); }
.lead { margin: 0; font-size: 12.5px; line-height: 1.5; color: var(--muted); max-width: 62ch; }
dl { margin: 0; display: flex; flex-direction: column; gap: 12px; }
dt { font-size: 13px; font-weight: 600; color: var(--text); }
dd { margin: 3px 0 0; font-size: 12.5px; line-height: 1.55; color: var(--muted); max-width: 62ch; }
</style>
