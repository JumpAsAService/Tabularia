<script setup lang="ts">
// Informativa sulla privacy, sotto la card di accesso: si legge PRIMA di
// entrare, non dopo. Una riga discreta e un dialogo col testo intero — sulla
// pagina di accesso c'è un'unica cosa da fare, e questa non deve competerci.
//
// Non si chiude: non è un avviso da togliersi davanti, è una riga che sta lì
// ogni volta che si entra. Per questo non ricorda nulla nel browser.
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

const daMostrare = computed(() => !!nota.value?.enabled && !!nota.value.summary)

onMounted(async () => {
  // un'informativa che non si carica non deve impedire di entrare
  try { nota.value = await api.get() } catch { nota.value = null }
})
</script>

<template>
  <p v-if="daMostrare" class="pv">
    <ShieldCheck :size="13" aria-hidden="true" />
    <span class="pv-text">{{ nota!.summary }}</span>
    <button class="pv-link" type="button" @click="aperta = true">{{ t('privacy.details') }}</button>
    <a v-if="nota!.url" class="pv-link" :href="nota!.url" target="_blank" rel="noopener">
      {{ t('privacy.full') }} <ExternalLink :size="11" />
    </a>
  </p>

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
  /* sotto la card, in tono minore: informa senza chiedere attenzione */
  display: flex; align-items: center; justify-content: center; gap: 8px;
  flex-wrap: wrap; margin: 14px auto 0; max-width: min(520px, calc(100vw - 32px));
  font-size: 12px; line-height: 1.45; color: var(--muted); text-align: center;
}
.pv > svg { flex: none; }
.pv-text { min-width: 0; }
.pv-link {
  display: inline-flex; align-items: center; gap: 4px;
  padding: 0; border: 0; background: none; cursor: pointer;
  color: var(--accent); font: inherit; text-decoration: underline;
  text-underline-offset: 2px;
}
.pv-link:hover { color: var(--text); }
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
.pv-card .pv-link { margin-top: 12px; }
</style>
