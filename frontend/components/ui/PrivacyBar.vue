<script setup lang="ts">
// Informativa sulla privacy in cima all'applicazione. A differenza dei banner
// di avvertimento questa SI CHIUDE: è un'informazione da leggere una volta, non
// un allarme da tenere sotto gli occhi. La chiusura vale finché il testo non
// cambia (vedi usePrivacy).
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { ShieldCheck, X, ExternalLink } from 'lucide-vue-next'
import MarkdownLite from '~/components/ui/MarkdownLite.vue'
import { usePrivacy, type PrivacyNotice } from '~/composables/usePrivacy'
import { useDialogA11y } from '~/composables/useDialogA11y'

const { t } = useI18n()
const api = usePrivacy()
const nota = ref<PrivacyNotice | null>(null)
const aperta = ref(false)
const card = ref<HTMLElement | null>(null)
useDialogA11y(card, () => aperta.value, () => { aperta.value = false })

const daMostrare = computed(() =>
  !!nota.value?.enabled && !!nota.value.summary && api.chiusaPer.value !== versione.value)
const versione = computed(() => nota.value?.updated_at ?? null)

onMounted(async () => {
  api.leggiChiusura()
  // un'informativa che non si carica non deve rompere la pagina
  try { nota.value = await api.get() } catch { nota.value = null }
})
</script>

<template>
  <div v-if="daMostrare" class="pv" role="region" :aria-label="t('privacy.title')">
    <ShieldCheck :size="15" aria-hidden="true" />
    <span class="pv-text">{{ nota!.summary }}</span>
    <button class="pv-btn" type="button" @click="aperta = true">{{ t('privacy.details') }}</button>
    <a v-if="nota!.url" class="pv-btn" :href="nota!.url" target="_blank" rel="noopener">
      {{ t('privacy.full') }} <ExternalLink :size="11" />
    </a>
    <button class="pv-x" type="button" :aria-label="t('privacy.dismiss')"
            :title="t('privacy.dismiss')" @click="api.chiudi(versione)">
      <X :size="14" />
    </button>
  </div>

  <Teleport to="body">
    <div v-if="aperta" class="pv-backdrop" @mousedown.self="aperta = false">
      <div ref="card" class="pv-card" role="dialog" aria-modal="true" aria-labelledby="pv-title" tabindex="-1">
        <div class="pv-head">
          <h3 id="pv-title"><ShieldCheck :size="15" /> {{ t('privacy.title') }}</h3>
          <button class="pv-x" type="button" :aria-label="t('privacy.close')" @click="aperta = false">
            <X :size="14" />
          </button>
        </div>
        <MarkdownLite :text="nota?.body || ''" />
        <a v-if="nota?.url" class="pv-btn pv-link" :href="nota.url" target="_blank" rel="noopener">
          {{ t('privacy.full') }} <ExternalLink :size="12" />
        </a>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.pv {
  display: flex; align-items: center; gap: 9px;
  padding: 8px 14px; margin: 0 0 10px;
  border: 1px solid var(--border-soft); border-left: 2px solid var(--accent);
  border-radius: 8px; background: var(--panel-2);
  font-size: 12.5px; line-height: 1.4;
}
.pv svg { flex: none; color: var(--accent); }
/* una riga sola, troncata con i puntini: il testo intero sta in «Dettagli» */
.pv-text { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.pv-btn {
  flex: none; display: inline-flex; align-items: center; gap: 4px;
  padding: 2px 9px; min-height: 22px; font-size: 12px;
  border: 1px solid var(--border); border-radius: 6px;
  background: transparent; color: var(--text); text-decoration: none; cursor: pointer;
}
.pv-btn:hover { border-color: var(--accent); }
.pv-x { flex: none; padding: 2px 5px; min-height: 22px; background: transparent; border: 0; cursor: pointer; color: var(--muted); }
.pv-x:hover { color: var(--text); }
.pv-backdrop {
  position: fixed; inset: 0; background: var(--scrim); backdrop-filter: blur(2px);
  display: flex; align-items: center; justify-content: center; z-index: 2000;
}
.pv-card {
  width: min(600px, calc(100vw - 32px)); max-height: calc(100vh - 48px); overflow-y: auto;
  background: var(--panel); border: 1px solid var(--border); border-radius: 14px;
  box-shadow: var(--shadow-2); padding: 18px 22px;
}
.pv-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 4px; }
.pv-head h3 { margin: 0; display: inline-flex; align-items: center; gap: 7px; font-size: 16px; }
.pv-link { margin-top: 12px; }
</style>
