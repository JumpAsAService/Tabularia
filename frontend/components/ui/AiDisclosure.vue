<script setup lang="ts">
// The gate in front of the assistant: it says in plain words that one is about to write
// to an automated system, and one enters only after having read it.
//
// It is not a convenience dialog: it does not close with Esc nor by clicking outside,
// and has no X. A consent that can be skipped by mistake is not a consent.
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { Sparkles, Check } from 'lucide-vue-next'
import { useDialogA11y } from '~/composables/useDialogA11y'

const props = defineProps<{ open: boolean; metadataOnly?: boolean }>()
const emit = defineEmits<{ (e: 'accept'): void }>()
const { t } = useI18n()

const card = ref<HTMLElement | null>(null)
// focus on the card and Tab confined; closing from the keyboard does NOTHING: the only
// way out is the button
useDialogA11y(card, () => props.open, () => {})

// in metadata-only mode «the numbers come from your data» and «your data goes to the provider»
// would be false: these say what happens then, and what still goes
const POINTS = computed(() => (props.metadataOnly
  ? ['metaGenerated', 'metaCanBeWrong', 'readsOnly', 'metaConstants', 'noPersonalData']
  : ['generated', 'canBeWrong', 'readsOnly', 'noPersonalData']))
</script>

<template>
  <Teleport to="body">
    <div v-if="open" class="ad-backdrop">
      <div
        ref="card"
        class="ad-card"
        role="dialog"
        aria-modal="true"
        aria-labelledby="ad-title"
        tabindex="-1"
      >
        <h3 id="ad-title"><Sparkles :size="16" /> {{ t('aiDisclosure.title') }}</h3>
        <p class="ad-lead">{{ t('aiDisclosure.lead') }}</p>
        <ul class="ad-points">
          <li v-for="p in POINTS" :key="p">{{ t(`aiDisclosure.${p}`) }}</li>
        </ul>
        <button class="primary ad-ok" type="button" @click="emit('accept')">
          <Check :size="14" /> {{ t('aiDisclosure.accept') }}
        </button>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.ad-backdrop {
  position: fixed;
  inset: 0;
  background: var(--scrim);
  backdrop-filter: blur(2px);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 2000;
}
.ad-card {
  width: min(460px, calc(100vw - 32px));
  max-height: calc(100vh - 32px);
  overflow-y: auto;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 14px;
  box-shadow: var(--shadow-2);
  padding: 22px 24px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.ad-card h3 {
  margin: 0;
  display: inline-flex;
  align-items: center;
  gap: 8px;
  font-size: 17px;
}
.ad-lead { margin: 0; font-size: 14px; line-height: 1.5; }
.ad-points {
  margin: 0;
  padding-left: 18px;
  display: flex;
  flex-direction: column;
  gap: 7px;
  font-size: 13px;
  line-height: 1.45;
  color: var(--muted);
}
.ad-ok { align-self: flex-end; margin-top: 4px; }
</style>
