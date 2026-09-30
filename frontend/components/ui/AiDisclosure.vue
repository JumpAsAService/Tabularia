<script setup lang="ts">
// Il cancello davanti all'assistente: si dice a chiare lettere che si sta per
// scrivere a un sistema automatico, e si entra solo dopo averlo letto.
//
// Non è un dialogo di comodo: non si chiude con Esc né cliccando fuori, e non
// ha una X. Un consenso che si può scavalcare per sbaglio non è un consenso.
import { ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { Sparkles, Check } from 'lucide-vue-next'
import { useDialogA11y } from '~/composables/useDialogA11y'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ (e: 'accept'): void }>()
const { t } = useI18n()

const card = ref<HTMLElement | null>(null)
// fuoco sulla card e Tab confinato; la chiusura da tastiera NON fa niente:
// l'unica uscita è il bottone
useDialogA11y(card, () => props.open, () => {})

const PUNTI = ['generated', 'canBeWrong', 'readsOnly', 'noPersonalData'] as const
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
          <li v-for="p in PUNTI" :key="p">{{ t(`aiDisclosure.${p}`) }}</li>
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
