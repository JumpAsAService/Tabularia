<script setup lang="ts">
// Rendering della coda dei toast (vedi composables/useToast.ts).
// Montato una volta sola in app.vue: copre tutte le pagine, editor incluso.
import { CheckCircle2, XCircle, Info, X } from 'lucide-vue-next'
import { useToast } from '~/composables/useToast'

const { toasts, dismiss } = useToast()

const icons = { success: CheckCircle2, error: XCircle, info: Info }
</script>

<template>
  <Teleport to="body">
    <div class="toasts" aria-live="polite">
      <TransitionGroup name="toast">
        <div v-for="t in toasts" :key="t.id" class="toast" :class="t.kind" @click="dismiss(t.id)">
          <component :is="icons[t.kind]" :size="16" class="ticon" />
          <span class="tmsg">{{ t.message }}</span>
          <button class="tx" :title="$t('toastHost.dismiss')" @click.stop="dismiss(t.id)"><X :size="13" /></button>
        </div>
      </TransitionGroup>
    </div>
  </Teleport>
</template>

<style scoped>
.toasts {
  position: fixed;
  bottom: 18px;
  right: 18px;
  z-index: 3000;
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-width: 380px;
}
.toast {
  display: flex;
  align-items: flex-start;
  gap: 9px;
  padding: 10px 12px;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 10px;
  box-shadow: var(--shadow-2);
  cursor: pointer;
  font-size: 13px;
}
.toast.success { border-color: rgba(110, 231, 183, 0.45); }
.toast.success .ticon { color: var(--accent-2); }
.toast.error { border-color: rgba(255, 107, 107, 0.5); }
.toast.error .ticon { color: var(--danger); }
.toast.info .ticon { color: var(--accent); }
.ticon { flex: none; margin-top: 1px; }
.tmsg { flex: 1; line-height: 1.35; overflow-wrap: anywhere; }
.tx {
  flex: none;
  padding: 1px 5px;
  background: transparent;
  border: none;
  color: var(--muted);
}
.tx:hover { color: var(--text); box-shadow: none; }

.toast-enter-active, .toast-leave-active { transition: all 0.22s ease; }
.toast-enter-from { opacity: 0; transform: translateY(8px); }
.toast-leave-to { opacity: 0; transform: translateX(12px); }
</style>
