<script setup lang="ts">
// L'avviso dell'ultimo minuto prima dell'uscita per inattività (vedi
// plugins/idle-logout.client.ts). Qualunque gesto sulla pagina lo toglie; il
// bottone è il gesto ovvio.
import { Clock } from 'lucide-vue-next'
import { useIdleWarning } from '~/composables/useAuth'

const secondsLeft = useIdleWarning()
</script>

<template>
  <Teleport to="body">
    <div v-if="secondsLeft != null" class="idle" role="alertdialog" aria-labelledby="idle-text">
      <Clock :size="18" class="idle-icon" aria-hidden="true" />
      <p id="idle-text" class="idle-text">
        {{ $t('sessions.idleWarning') }}
        <span class="idle-left" aria-hidden="true">{{ $t('sessions.idleSeconds', { n: secondsLeft }) }}</span>
      </p>
      <button class="primary" @click="secondsLeft = null">{{ $t('sessions.stay') }}</button>
    </div>
  </Teleport>
</template>

<style scoped>
.idle {
  position: fixed; left: 50%; bottom: 24px; transform: translateX(-50%); z-index: 3000;
  display: flex; align-items: center; gap: 12px;
  width: min(560px, calc(100vw - 32px));
  padding: 12px 14px; border: 1px solid var(--border); border-radius: 12px;
  background: var(--panel); box-shadow: var(--shadow-2);
}
.idle-icon { color: var(--warning, var(--accent)); flex: none; }
.idle-text { margin: 0; flex: 1; font-size: 13px; line-height: 1.45; }
.idle-left { display: block; color: var(--muted); font-variant-numeric: tabular-nums; }
.idle button { flex: none; white-space: nowrap; }
</style>
