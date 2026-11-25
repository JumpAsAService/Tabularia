<script setup lang="ts">
// Renders the app's confirmation questions (see composables/useConfirm.ts), one at a time.
// Mounted once, in app.vue: it covers every page, the editors and the dialogs under it.
//
// What the browser's confirm() cannot do and this does: the button says the action (not «OK»),
// a destructive one is red and Cancel has the focus, Esc and a click outside cancel, Enter
// confirms only when the focus is on the confirm button, and the focus goes back to where it was.
// The keys are caught on the window in the capture phase, ahead of the dialog underneath (a
// Modal listens to Esc too: it must not close along with the question it asked).
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { CircleHelp, TriangleAlert } from 'lucide-vue-next'
import { useConfirm } from '~/composables/useConfirm'

const { queue, answer } = useConfirm()
const q = computed(() => queue.value[0] ?? null)

const card = ref<HTMLElement | null>(null)
const cancelBtn = ref<HTMLButtonElement | null>(null)
const okBtn = ref<HTMLButtonElement | null>(null)
const uid = Math.random().toString(36).slice(2, 9)
const titleId = `confirm-title-${uid}`
const textId = `confirm-text-${uid}`

// whoever had the focus before the first question: it goes back there when the queue empties
let origin: HTMLElement | null = null
// a key pressed (not held) since the question opened: only then does a keyboard «click» count
let keyedIn = false

function onKey(e: KeyboardEvent) {
  if (!q.value) return
  if (!e.repeat) keyedIn = true
  if (e.key === 'Escape') {
    e.preventDefault()
    e.stopPropagation()
    answer(false)
    return
  }
  if (e.key === 'Tab') {
    // two buttons: Tab and Shift+Tab walk between them, and never leave the dialog
    e.preventDefault()
    e.stopPropagation()
    const els = [cancelBtn.value, okBtn.value].filter((b): b is HTMLButtonElement => !!b)
    const i = els.indexOf(document.activeElement as HTMLButtonElement)
    const next = i < 0 ? 0 : (i + (e.shiftKey ? els.length - 1 : 1)) % els.length
    els[next]?.focus()
    return
  }
  // the page's own keys (g + a letter, /, ?) are not for the page while a question is open;
  // Enter and Space keep their default action, which presses the focused button
  e.stopPropagation()
}

function onOk(e: MouseEvent) {
  // a Space that opened the question lets go over the confirm button now holding the focus:
  // that keyboard «click» comes with no key pressed in the dialog, and is not an answer
  if (e.detail === 0 && !keyedIn) return
  answer(true)
}

watch(() => q.value?.id, async (id, was) => {
  if (typeof document === 'undefined') return
  if (id != null && was == null) {
    origin = document.activeElement as HTMLElement | null
    window.addEventListener('keydown', onKey, true)
  }
  if (id != null) {
    keyedIn = false
    await nextTick()
    // the safe choice is the default for a destructive question, the action for the others
    ;(q.value?.destructive ? cancelBtn.value : okBtn.value)?.focus()
  } else if (was != null) {
    window.removeEventListener('keydown', onKey, true)
    if (origin?.isConnected) origin.focus?.()
    origin = null
  }
})

onBeforeUnmount(() => {
  if (typeof window !== 'undefined') window.removeEventListener('keydown', onKey, true)
  // nobody is left waiting on a question that can no longer be shown
  while (queue.value.length) answer(false)
})
</script>

<template>
  <Teleport to="body">
    <Transition name="dlg" appear :duration="{ enter: 180, leave: 120 }">
      <div v-if="q" :key="q.id" class="confirm-backdrop" @mousedown.self="answer(false)">
        <div
          ref="card"
          class="confirm-card"
          role="alertdialog"
          aria-modal="true"
          :aria-labelledby="titleId"
          :aria-describedby="textId"
          tabindex="-1"
        >
          <div class="confirm-body">
            <span class="confirm-icon" :class="{ bad: q.destructive }" aria-hidden="true">
              <TriangleAlert v-if="q.destructive" :size="18" />
              <CircleHelp v-else :size="18" />
            </span>
            <div class="confirm-text">
              <h2 :id="titleId">{{ $t(`confirmDialog.${q.kind}.title`) }}</h2>
              <p :id="textId">{{ q.message }}</p>
            </div>
          </div>
          <footer class="confirm-foot">
            <button ref="cancelBtn" type="button" @click="answer(false)">{{ $t('confirmDialog.cancel') }}</button>
            <button
              ref="okBtn"
              type="button"
              :class="q.destructive ? 'confirm-danger' : 'primary'"
              @click="onOk"
            >{{ $t(`confirmDialog.${q.kind}.label`) }}</button>
          </footer>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.confirm-backdrop {
  position: fixed; inset: 0; z-index: 2600;
  display: grid; place-items: center;
  background: var(--scrim); backdrop-filter: blur(2px);
}
.confirm-card {
  width: min(440px, calc(100vw - 32px)); max-height: calc(100vh - 48px);
  display: flex; flex-direction: column;
  background: var(--panel); border: 1px solid var(--border); border-radius: 14px;
  box-shadow: var(--shadow-2); outline: none;
}
.confirm-body { display: flex; gap: 12px; padding: 18px 20px 14px; overflow-y: auto; }
.confirm-icon { flex: none; display: grid; place-items: center; width: 32px; height: 32px; border-radius: 50%; background: var(--panel-2); color: var(--accent); }
.confirm-icon.bad { color: var(--danger-text); background: color-mix(in srgb, var(--danger) 14%, var(--panel)); }
.confirm-text { min-width: 0; }
.confirm-text h2 { margin: 4px 0 6px; font-size: 15px; font-weight: 650; }
.confirm-text p { margin: 0; font-size: 13px; line-height: 1.5; color: var(--muted); overflow-wrap: anywhere; white-space: pre-line; }
.confirm-foot { display: flex; justify-content: flex-end; gap: 8px; padding: 10px 20px 16px; }
.confirm-danger { background: var(--danger-fill); border-color: var(--danger-fill); color: var(--on-danger); }
.confirm-danger:hover:not(:disabled) { background: var(--danger-fill); filter: brightness(1.08); }
@media (max-width: 480px) {
  .confirm-foot { flex-direction: column-reverse; }
  .confirm-foot button { width: 100%; }
}
</style>
