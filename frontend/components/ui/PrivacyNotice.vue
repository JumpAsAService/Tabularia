<script setup lang="ts">
// Privacy notice, at the bottom of the sign-in page: it is read BEFORE signing in, not
// after. A bar as wide as the window, as on any site — a notice that cannot be seen
// informs nobody, and writing it small in a corner is the polite way not to say it.
//
// It does not close: it is not a warning to get out of one's way, it is what is written
// on the door. That is why it remembers nothing in the browser.
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { ShieldCheck, X, ExternalLink } from 'lucide-vue-next'
import MarkdownLite from '~/components/ui/MarkdownLite.vue'
import { usePrivacy, type PrivacyNotice } from '~/composables/usePrivacy'
import { useDialogA11y } from '~/composables/useDialogA11y'

const { t } = useI18n()
const api = usePrivacy()
const note = ref<PrivacyNotice | null>(null)
const openOne = ref(false)
const card = ref<HTMLElement | null>(null)
useDialogA11y(card, () => openOne.value, () => { openOne.value = false })

const toShow = computed(() => !!note.value?.enabled && !!note.value.summary)

onMounted(async () => {
  // a notice that does not load must not prevent signing in
  try { note.value = await api.get() } catch { note.value = null }
})
</script>

<template>
  <aside v-if="toShow" class="pv" role="region" :aria-label="t('privacy.title')">
    <div class="pv-in">
      <ShieldCheck :size="18" aria-hidden="true" />
      <p class="pv-text">{{ note!.summary }}</p>
      <div class="pv-acts">
        <!-- neutral: on the sign-in page the main action is «Sign in» -->
        <button class="pv-btn ghost" type="button" @click="openOne = true">{{ t('privacy.details') }}</button>
        <a v-if="note!.url" class="pv-btn ghost" :href="note!.url" target="_blank" rel="noopener">
          {{ t('privacy.full') }} <ExternalLink :size="12" />
        </a>
      </div>
    </div>
  </aside>

  <Teleport to="body">
    <div v-if="openOne" class="pv-backdrop" @mousedown.self="openOne = false">
      <div ref="card" class="pv-card" role="dialog" aria-modal="true" aria-labelledby="pv-title" tabindex="-1">
        <div class="pv-head">
          <h3 id="pv-title"><ShieldCheck :size="15" /> {{ t('privacy.title') }}</h3>
          <button class="pv-x" type="button" :aria-label="t('privacy.close')" @click="openOne = false">
            <X :size="14" />
          </button>
        </div>
        <MarkdownLite :text="note?.body || ''" />
        <a v-if="note?.url" class="pv-btn pv-link" :href="note.url" target="_blank" rel="noopener">
          {{ t('privacy.full') }} <ExternalLink :size="12" />
        </a>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.pv {
  /* at the bottom of the window and as wide as it, as on any site: it is the place where
     people expect to find it, and that is why they find it */
  position: fixed; left: 0; right: 0; bottom: 0; z-index: 30;
  background: var(--panel);
  border-top: 1px solid var(--border);
  box-shadow: 0 -8px 28px rgba(0, 0, 0, 0.28);
}
.pv-in {
  display: flex; align-items: center; gap: 14px;
  max-width: 1080px; margin: 0 auto; padding: 14px 22px;
}
.pv-in > svg { flex: none; color: var(--accent); }
.pv-text {
  flex: 1; min-width: 0; margin: 0;
  font-size: 14px; line-height: 1.45; color: var(--text);
}
.pv-acts { flex: none; display: flex; align-items: center; gap: 8px; }
.pv-btn {
  display: inline-flex; align-items: center; gap: 5px;
  padding: 7px 16px; min-height: 34px;
  border: 1px solid transparent; border-radius: 8px;
  background: var(--grad-accent); color: var(--on-accent); /* white on --accent: 1.65 in monokai */
  font: inherit; font-size: 13.5px; font-weight: 600;
  text-decoration: none; cursor: pointer; white-space: nowrap;
}
.pv-btn:hover { filter: brightness(0.92); }
.pv-btn.ghost { background: transparent; color: var(--text); border-color: var(--border); }
.pv-btn.ghost:hover { border-color: var(--accent); filter: none; }
/* on a narrow screen the text goes above and the buttons below, wide */
@media (max-width: 640px) {
  .pv-in { flex-wrap: wrap; padding: 12px 16px; gap: 10px; }
  .pv-text { flex: 1 1 100%; font-size: 13.5px; }
  .pv-acts { flex: 1 1 100%; }
  .pv-btn { flex: 1; justify-content: center; }
}
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
