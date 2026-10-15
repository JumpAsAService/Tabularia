<script setup lang="ts">
// Who is speaking in a conversation with an assistant: the user, or an AI — named as
// such, with the model that wrote the answer. The disclosure dialog says it once;
// this says it on every message, so a page read later, or by someone who never saw
// the dialog, still cannot be mistaken for a conversation with a person (AI Act,
// art. 50: people must know they are dealing with an AI system).
import { useI18n } from 'vue-i18n'
import { Sparkles, User } from 'lucide-vue-next'

defineProps<{ who: 'ai' | 'you'; model?: string | null }>()
const { t } = useI18n()
</script>

<template>
  <div class="speaker" :class="who">
    <template v-if="who === 'ai'">
      <Sparkles :size="12" aria-hidden="true" />
      <span class="who">{{ t('aiCompliance.assistantLabel') }}</span>
      <span v-if="model" class="model">· {{ model }}</span>
    </template>
    <template v-else>
      <User :size="12" aria-hidden="true" />
      <span class="who">{{ t('aiCompliance.youLabel') }}</span>
    </template>
  </div>
</template>

<style scoped>
.speaker { display: flex; align-items: center; gap: 5px; margin: 0 0 4px; font-size: 11.5px; color: var(--muted); }
.speaker.you { justify-content: flex-end; }
.speaker.ai svg { color: var(--accent-hi); }
.who { font-weight: 600; letter-spacing: 0.01em; }
.speaker.ai .who { color: var(--text); }
.model { font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
</style>
