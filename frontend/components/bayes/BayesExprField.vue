<script setup lang="ts">
// An expression of the model on one line (docs/specs/bayes-graph-editor.md, «Expressions while
// typing»): a plain input checked by the mirror at every keystroke (the page runs it and hands the
// first error here), the error's span underlined in danger, its message under the field, the
// field's type in words when it checks («a number per School»). The app's keys rest while typing
// (`nokey`); Esc gives the focus back to the canvas.
//
// The underline is drawn by a copy of the text behind the input, in the same type and padding,
// its letters transparent: only the wavy line under the span shows through.
import { computed, nextTick, ref, watch } from 'vue'
import { Check, CircleAlert } from 'lucide-vue-next'
import { bayesSpanUnits } from '~/composables/bayesEditorText'

const props = defineProps<{
  id: string
  modelValue: string
  /** The first error of the expression, its span in code points (the mirror's). */
  error?: { pos: number, len: number, message: string } | null
  /** The type in words when the expression checks. */
  okText?: string
  /** A tag at the field's end («hyperprior»). */
  tag?: string
  describedBy?: string
}>()
const emit = defineEmits<{ (e: 'update:modelValue', v: string): void, (e: 'escape'): void }>()

const input = ref<HTMLInputElement | null>(null)
const mirror = ref<HTMLElement | null>(null)
const errId = computed(() => `${props.id}-err`)

const parts = computed(() => {
  const text = props.modelValue
  if (!props.error) return null
  const { start, end } = bayesSpanUnits(text, props.error.pos, props.error.len)
  // an error with no width (a value missing at the end) underlines the place where it is missing
  return { before: text.slice(0, start), bad: text.slice(start, end) || ' ', after: text.slice(end) }
})

function sync () {
  if (input.value && mirror.value) mirror.value.scrollLeft = input.value.scrollLeft
}
watch(() => props.modelValue, () => nextTick(sync))

function onInput (e: Event) {
  emit('update:modelValue', (e.target as HTMLInputElement).value)
  sync()
}
function onKey (e: KeyboardEvent) {
  if (e.key === 'Escape') {
    e.preventDefault()
    emit('escape')
  }
}
defineExpose({ focus: () => input.value?.focus() })
</script>

<template>
  <div class="xf" :class="{ bad: !!error }">
    <div class="area">
      <div ref="mirror" class="mirror" aria-hidden="true"><template v-if="parts">{{ parts.before }}<span class="u">{{ parts.bad }}</span>{{ parts.after }}</template></div>
      <input
        :id="id"
        ref="input"
        class="nokey"
        type="text"
        :value="modelValue"
        spellcheck="false"
        autocapitalize="off"
        autocomplete="off"
        :aria-invalid="error ? 'true' : 'false'"
        :aria-describedby="[error ? errId : '', describedBy ?? ''].filter(Boolean).join(' ') || undefined"
        @input="onInput"
        @scroll="sync"
        @keyup="sync"
        @click="sync"
        @keydown="onKey"
      >
    </div>
    <span v-if="!error && okText" class="ok" :title="okText"><Check :size="12" class="ic" aria-hidden="true" /><span class="txt">{{ okText }}</span></span>
    <span v-if="tag" class="role">{{ tag }}</span>
  </div>
  <p v-if="error" :id="errId" class="errmsg"><CircleAlert :size="13" class="ic" aria-hidden="true" /><span>{{ error.message }}</span></p>
</template>

<style scoped>
.xf {
  display: flex; align-items: center; gap: 8px; min-height: 32px; padding-right: 8px;
  background: var(--bg-soft); border: 1px solid var(--control-border); border-radius: 8px;
  transition: border-color var(--dur-2), box-shadow var(--dur-2);
}
.xf:focus-within { border-color: var(--accent); box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 18%, transparent); }
.xf.bad { border-color: var(--danger); box-shadow: 0 0 0 3px color-mix(in srgb, var(--danger) 16%, transparent); }
.area { position: relative; flex: 1 1 0; min-width: 9ch; align-self: stretch; }
.mirror, .area input {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12.5px; line-height: 18px;
  padding: 6px 0 6px 10px; letter-spacing: 0; white-space: pre;
}
.mirror {
  position: absolute; inset: 0; overflow: hidden; color: transparent; pointer-events: none;
}
.mirror .u {
  text-decoration: underline wavy var(--danger); text-decoration-thickness: 1.5px;
  text-underline-offset: 4px; text-decoration-skip-ink: none;
}
.area input {
  position: relative; width: 100%; height: 100%; box-sizing: border-box; margin: 0;
  background: transparent; border: 0; border-radius: 8px; box-shadow: none; outline: none; color: var(--text);
}
.area input:focus { box-shadow: none; border: 0; }
.ok { flex: 0 1 auto; min-width: 0; display: inline-flex; align-items: center; gap: 4px; font-size: 11.5px; color: var(--muted); white-space: nowrap; overflow: hidden; }
.ok .txt { overflow: hidden; text-overflow: ellipsis; }
.ok .ic { flex: none; color: var(--accent-2); }
.role {
  flex: none; font-size: 9.5px; text-transform: uppercase; letter-spacing: 0.05em; padding: 0 5px; line-height: 15px;
  border-radius: 6px; border: 1px solid var(--border); color: var(--muted); font-weight: 500;
}
.errmsg { display: grid; grid-template-columns: 15px 1fr; gap: 6px; margin: 0; font-size: 12px; line-height: 1.45; color: var(--danger-text); }
.errmsg .ic { margin-top: 2px; }
</style>
