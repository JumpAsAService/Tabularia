<script setup lang="ts">
// An expression of the model on one line (docs/specs/bayes-graph-editor.md, «Expressions while
// typing»): a plain input checked by the mirror at every keystroke (the page runs it and hands the
// first error here), the error's span underlined in danger, its message under the field, the
// field's type in words when it checks («a number per School»). The app's keys rest while typing
// (`nokey`).
//
// The names and functions that fit at the caret open under the field as an ARIA combobox: by typing
// a name, or with ↓; ↑ ↓ walk them, Enter (or Tab) writes the one chosen, Esc closes the list, and a
// second Esc gives the focus back to the canvas.
//
// The underline is drawn by a copy of the text behind the input, in the same type and padding,
// its letters transparent: only the wavy line under the span shows through.
import { computed, nextTick, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { Check, CircleAlert, TriangleAlert } from 'lucide-vue-next'
import { bayesSpanUnits } from '~/composables/bayesEditorText'
import { bayesFilterNames, bayesInsertName, bayesWordAt, type BayesNameChoice } from '~/composables/bayesInspector'

const props = defineProps<{
  id: string
  modelValue: string
  /** The first error of the expression, its span in code points (the mirror's). */
  error?: { pos: number, len: number, message: string } | null
  /** A warning of the mirror on it (when it has no error). */
  warning?: string
  /** The type in words when the expression checks. */
  okText?: string
  /** A tag at the field's end («hyperprior»). */
  tag?: string
  describedBy?: string
  /** The names and functions offered at the caret, and how each is said («a number per School»). */
  names?: readonly BayesNameChoice[]
  nameHint?: (c: BayesNameChoice) => string
  placeholder?: string
}>()
const emit = defineEmits<{ (e: 'update:modelValue', v: string): void, (e: 'escape'): void }>()
const { t } = useI18n()

const input = ref<HTMLInputElement | null>(null)
const mirror = ref<HTMLElement | null>(null)
const errId = computed(() => `${props.id}-err`)
const listId = computed(() => `${props.id}-names`)

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

// ---------------------------------------------------------------- the names at the caret

const open = ref(false)
const active = ref(0)
const caret = ref(0)
const choices = computed(() => (props.names?.length ? bayesFilterNames(props.names, bayesWordAt(props.modelValue, caret.value).word) : []))
const shown = computed(() => open.value && choices.value.length > 0)
watch(choices, () => { active.value = 0 })
const optId = (i: number) => `${props.id}-name-${i}`

function readCaret () { caret.value = input.value?.selectionStart ?? props.modelValue.length }
function close () { open.value = false }

function onInput (e: Event) {
  const el = e.target as HTMLInputElement
  emit('update:modelValue', el.value)
  caret.value = el.selectionStart ?? el.value.length
  // a name being typed opens the list; anything else closes it
  open.value = bayesWordAt(el.value, caret.value).word.length > 0
  sync()
}
async function choose (c: BayesNameChoice) {
  const out = bayesInsertName(props.modelValue, caret.value, c)
  emit('update:modelValue', out.text)
  close()
  await nextTick()
  input.value?.focus()
  input.value?.setSelectionRange(out.caret, out.caret)
  caret.value = out.caret
  sync()
}
function onKey (e: KeyboardEvent) {
  if (e.key === 'ArrowDown' && props.names?.length) {
    e.preventDefault()
    readCaret()
    if (!shown.value) { open.value = true; active.value = 0 } else active.value = (active.value + 1) % choices.value.length
    return
  }
  if (e.key === 'ArrowUp' && shown.value) {
    e.preventDefault()
    active.value = (active.value - 1 + choices.value.length) % choices.value.length
    return
  }
  if ((e.key === 'Enter' || e.key === 'Tab') && shown.value && !e.shiftKey) {
    e.preventDefault()
    choose(choices.value[active.value]!)
    return
  }
  if (e.key === 'Escape') {
    e.preventDefault()
    if (shown.value) { close(); return }
    emit('escape')
  }
}
defineExpose({ focus: () => input.value?.focus() })
</script>

<template>
  <div class="xwrap">
    <div class="xf" :class="{ bad: !!error, warn: !error && !!warning }">
      <div class="area">
        <div ref="mirror" class="mirror" aria-hidden="true"><template v-if="parts">{{ parts.before }}<span class="u">{{ parts.bad }}</span>{{ parts.after }}</template></div>
        <input
          :id="id"
          ref="input"
          class="nokey"
          type="text"
          role="combobox"
          aria-autocomplete="list"
          :aria-expanded="shown ? 'true' : 'false'"
          :aria-controls="listId"
          :aria-activedescendant="shown ? optId(active) : undefined"
          :value="modelValue"
          :placeholder="placeholder"
          spellcheck="false"
          autocapitalize="off"
          autocomplete="off"
          :aria-invalid="error ? 'true' : 'false'"
          :aria-describedby="[error || warning ? errId : '', describedBy ?? ''].filter(Boolean).join(' ') || undefined"
          @input="onInput"
          @scroll="sync"
          @keyup="sync"
          @click="() => { readCaret(); sync() }"
          @keydown="onKey"
          @blur="close"
        >
      </div>
      <span v-if="!error && okText" class="ok" :title="okText"><Check :size="12" class="ic" aria-hidden="true" /><span class="txt">{{ okText }}</span></span>
      <span v-if="tag" class="role">{{ tag }}</span>
    </div>
    <ul v-show="shown" :id="listId" class="names" role="listbox" :aria-label="t('bayes.editor.names.label')">
      <li
        v-for="(c, i) in choices"
        :id="optId(i)"
        :key="c.text"
        role="option"
        :aria-selected="i === active ? 'true' : 'false'"
        :class="{ on: i === active }"
        @mousedown.prevent="choose(c)"
        @mousemove="active = i"
      >
        <code>{{ c.text }}<span v-if="c.kind === 'function'" class="paren">()</span></code>
        <span class="h">{{ nameHint ? nameHint(c) : '' }}</span>
      </li>
      <li class="foot" aria-hidden="true">{{ t('bayes.editor.names.keys') }}</li>
    </ul>
  </div>
  <p v-if="error" :id="errId" class="errmsg"><CircleAlert :size="13" class="ic" aria-hidden="true" /><span>{{ error.message }}</span></p>
  <p v-else-if="warning" :id="errId" class="warnmsg"><TriangleAlert :size="13" class="ic" aria-hidden="true" /><span>{{ warning }}</span></p>
</template>

<style scoped>
.xwrap { position: relative; }
.xf {
  display: flex; align-items: center; gap: 8px; min-height: 32px; padding-right: 8px;
  background: var(--bg-soft); border: 1px solid var(--control-border); border-radius: var(--radius-control);
  transition: border-color var(--dur-2), box-shadow var(--dur-2);
}
.xf:focus-within { border-color: var(--accent); box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 18%, transparent); }
.xf.bad { border-color: var(--danger); box-shadow: 0 0 0 3px color-mix(in srgb, var(--danger) 16%, transparent); }
.xf.warn:not(:focus-within) { border-color: color-mix(in srgb, var(--warning) 70%, var(--control-border)); }
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
  background: transparent; border: 0; border-radius: var(--radius-control); box-shadow: none; outline: none; color: var(--text);
}
.area input:focus { box-shadow: none; border: 0; }
.area input::placeholder { color: var(--muted); opacity: 0.8; }
.ok { flex: 0 1 auto; min-width: 0; display: inline-flex; align-items: center; gap: 4px; font-size: 11.5px; color: var(--muted); white-space: nowrap; overflow: hidden; }
.ok .txt { overflow: hidden; text-overflow: ellipsis; }
.ok .ic { flex: none; color: var(--accent-2); }
.role {
  flex: none; font-size: 9.5px; text-transform: uppercase; letter-spacing: 0.05em; padding: 0 5px; line-height: 15px;
  border-radius: var(--radius-small); border: 1px solid var(--border); color: var(--muted); font-weight: 500;
}
.errmsg, .warnmsg { display: grid; grid-template-columns: 15px 1fr; gap: 6px; margin: 0; font-size: 12px; line-height: 1.45; }
.errmsg { color: var(--danger-text); }
.warnmsg { color: var(--text); }
.warnmsg .ic { color: var(--warning); }
.errmsg .ic, .warnmsg .ic { margin-top: 2px; }
/* the names at the caret: the Select's panel language, under the field */
.names {
  position: absolute; z-index: 30; top: calc(100% + 4px); left: 0; right: 0; margin: 0; padding: 4px; list-style: none;
  max-height: 260px; overflow-y: auto; background: var(--panel); border: 1px solid var(--border); border-radius: var(--radius);
  box-shadow: var(--shadow-2);
}
.names li[role="option"] {
  display: flex; align-items: baseline; gap: 10px; padding: 5px 8px; border-radius: var(--radius-small); cursor: pointer; min-width: 0;
}
.names li.on { background: var(--panel-2); }
.names code { flex: none; font-size: 12.5px; color: var(--text); }
.names li.on code { color: var(--accent-hi); }
.names .paren { color: var(--muted); }
.names .h { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 11.5px; color: var(--muted); margin-left: auto; }
.names .foot { padding: 5px 8px 3px; margin-top: 2px; border-top: 1px solid var(--border-soft); font-size: 11px; color: var(--muted); }
</style>
