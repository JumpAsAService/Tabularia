<script setup lang="ts">
// Il guscio di un dialogo modale: sfondo, scheda, intestazione con titolo e
// chiusura, fuoco e tastiera. I dialoghi esistenti se lo riscrivono ciascuno per
// conto suo (vedi docs/audit/SEMPLIFICAZIONE-2026-10-05.md, E1): questo è il
// punto unico per quelli nuovi, e quello a cui portare i vecchi.
import { ref } from 'vue'
import { X } from 'lucide-vue-next'
import { useDialogA11y } from '~/composables/useDialogA11y'

const props = withDefaults(defineProps<{ open: boolean; title: string; width?: number }>(), { width: 560 })
const emit = defineEmits<{ (e: 'close'): void }>()

const card = ref<HTMLElement | null>(null)
const titleId = `modal-${Math.random().toString(36).slice(2, 9)}`
useDialogA11y(card, () => props.open, () => emit('close'))
</script>

<template>
  <Teleport to="body">
    <div v-if="open" class="modal-backdrop" @mousedown.self="emit('close')">
      <div
        ref="card"
        class="modal-card"
        role="dialog"
        aria-modal="true"
        :aria-labelledby="titleId"
        tabindex="-1"
        :style="{ width: `min(${width}px, calc(100vw - 32px))` }"
      >
        <header class="modal-head">
          <h2 :id="titleId">{{ title }}</h2>
          <slot name="head" />
          <button class="modal-close" :aria-label="$t('contracts.close')" :title="$t('contracts.close')" @click="emit('close')">
            <X :size="16" />
          </button>
        </header>
        <div class="modal-body"><slot /></div>
        <footer v-if="$slots.footer" class="modal-foot"><slot name="footer" /></footer>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.modal-backdrop {
  position: fixed; inset: 0; z-index: 2000;
  display: grid; place-items: center;
  background: var(--scrim); backdrop-filter: blur(2px);
}
.modal-card {
  display: flex; flex-direction: column;
  max-height: calc(100vh - 48px);
  background: var(--panel); border: 1px solid var(--border); border-radius: 14px;
  box-shadow: var(--shadow-2); outline: none;
}
.modal-head { display: flex; align-items: center; gap: 10px; padding: 16px 20px 12px; border-bottom: 1px solid var(--border-soft); }
.modal-head h2 { margin: 0; font-size: 15px; font-weight: 650; flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.modal-close { display: grid; place-items: center; width: 28px; height: 28px; border: 0; border-radius: 8px; background: transparent; color: var(--muted); cursor: pointer; }
.modal-close:hover { background: var(--panel-2); color: var(--text); }
.modal-body { padding: 16px 20px; overflow-y: auto; }
.modal-foot { display: flex; align-items: center; gap: 8px; padding: 12px 20px 16px; border-top: 1px solid var(--border-soft); }
</style>
