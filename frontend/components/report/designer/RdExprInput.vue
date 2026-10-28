<script setup lang="ts">
// The field of an expression: the text coloured as the language reads it (fields, functions,
// texts, numbers), the completion of the dataset's fields (after «[»), of the functions with
// their signature and what they do, of the parameters (inside PARAM(")), the signature of
// the call the caret is in, and the mistakes — those this side sees while one types, after
// a pause, and those of the server's check at the save, at their position.
//
// A textarea over a copy of its text: the copy draws the colours and the underline of a
// mistake, the textarea (its text transparent) keeps the caret, the selection, the undo of
// the browser and the screen readers' reading. Nothing typed is ever put in the page as
// HTML: the copy is text in spans.
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  FUNCTIONS, callAt, checkExpression, completionAt, highlightTokens, type ExprError, type TokenKind,
} from '~/composables/reportExpressions'
import { fieldRef, type DField } from '~/composables/reportDesignModel'
import RdFieldIcon from '~/components/report/designer/RdFieldIcon.vue'

const props = defineProps<{
  modelValue: string | null | undefined
  /** the fields the expression can name; null: not known (none are checked) */
  fields?: DField[] | null
  params?: string[]
  /** a text item's value: plain text, or an expression when it starts with «=» */
  textMode?: boolean
  placeholder?: string
  label?: string
  id?: string
  /** the server's verdict on the saved text: shown until the text changes */
  problem?: { message: string; pos: number | null; length: number } | null
  multiline?: boolean
  disabled?: boolean
}>()
const emit = defineEmits<{ (e: 'update:modelValue', v: string): void; (e: 'edited'): void }>()
const { t, te } = useI18n()

const ta = ref<HTMLTextAreaElement | null>(null)
const text = computed(() => props.modelValue ?? '')
const isExpr = computed(() => !props.textMode || text.value.startsWith('='))
const focused = ref(false)
const caret = ref(0)
const uid = `rx-${Math.random().toString(36).slice(2, 9)}`

// ── the check done here, after a pause ──
const localError = ref<ExprError | null>(null)
let timer: ReturnType<typeof setTimeout> | null = null
function scheduleCheck() {
  if (timer) clearTimeout(timer)
  timer = setTimeout(runCheck, 300)
}
function runCheck() {
  timer = null
  if (!isExpr.value || !text.value.trim() || (props.textMode && text.value.trim() === '=')) {
    localError.value = null
    return
  }
  localError.value = checkExpression(text.value, {
    fields: props.fields ? props.fields.map((f) => f.name) : null,
    params: props.params ?? [],
  })
}
watch(() => [props.fields, props.params], runCheck, { deep: true })
onBeforeUnmount(() => { if (timer) clearTimeout(timer) })
runCheck()

const localMessage = (e: ExprError) => {
  const key = `reportDesigner.expr.${e.code}`
  return te(key) ? t(key, e.args ?? {}) : t('reportDesigner.expr.unexpected', { what: '' })
}
/** what is shown: the server's verdict first, else ours */
const shown = computed(() => {
  if (props.problem) return { message: props.problem.message, pos: props.problem.pos, length: props.problem.length, server: true }
  if (localError.value) return { message: localMessage(localError.value), pos: localError.value.pos, length: localError.value.length, server: false }
  return null
})

// ── the coloured copy ──
interface Seg { text: string; kind: TokenKind | 'plain'; bad: boolean; mark?: boolean }
const segments = computed<Seg[]>(() => {
  const v = text.value
  const toks = isExpr.value ? highlightTokens(v) : [{ kind: 'plain' as const, text: v, pos: 0, end: v.length }]
  const err = shown.value
  const from = err?.pos ?? -1
  const to = err && err.pos != null ? err.pos + Math.max(err.length, 0) : -1
  const out: Seg[] = []
  for (const tk of toks) {
    const kind = (props.textMode && tk.pos === 0 && tk.text === '=' ? 'op' : tk.kind) as Seg['kind']
    const cuts = [tk.pos, tk.end, ...[from, to].filter((c) => c > tk.pos && c < tk.end)].sort((a, b) => a - b)
    for (let i = 0; i < cuts.length - 1; i++) {
      if (cuts[i] === cuts[i + 1]) continue
      out.push({ text: v.slice(cuts[i], cuts[i + 1]), kind, bad: from >= 0 && cuts[i] >= from && cuts[i + 1] <= to && to > from })
    }
  }
  // a mistake with no width (something missing at that point): a mark there
  if (err && err.pos != null && err.length === 0) {
    let at = 0
    const idx = out.findIndex((s) => { const hit = at >= err.pos!; at += s.text.length; return hit })
    const mark: Seg = { text: '', kind: 'plain', bad: true, mark: true }
    if (idx < 0) out.push(mark)
    else out.splice(idx, 0, mark)
  }
  return out
})

// ── completion ──
interface Option { label: string; insert: string; detail: string; kind: 'field' | 'fn' | 'param' | 'kw'; desc?: string; back?: number; replaceTo?: number }
const open = ref(false)
const active = ref(0)
const listPos = ref<Record<string, string>>({})
const options = ref<Option[]>([])
const optId = (i: number) => `${uid}-o${i}`


function compute(force = false): Option[] {
  if (!isExpr.value) return []
  const v = text.value
  const c = caret.value
  const ctx = completionAt(v, c)
  const fields = props.fields ?? []
  if (ctx.kind === 'field') {
    const q = ctx.prefix.toLowerCase()
    const after = v[c] === ']' ? 1 : 0
    return fields.filter((f) => f.name.toLowerCase().includes(q))
      .sort((a, b) => Number(!a.name.toLowerCase().startsWith(q)) - Number(!b.name.toLowerCase().startsWith(q)))
      .slice(0, 60)
      .map((f) => ({ label: f.name, insert: fieldRef(f.name), detail: f.type, kind: 'field' as const, replaceTo: c + after }))
  }
  if (ctx.kind === 'param') {
    const q = ctx.prefix.toLowerCase()
    // what follows the caret of the name, its quote and its parenthesis is written again
    const rest = /^[A-Za-z0-9_]*"?\s*\)?/.exec(v.slice(c))?.[0] ?? ''
    return (props.params ?? []).filter((p) => p.toLowerCase().startsWith(q))
      .map((p) => ({ label: p, insert: `"${p}")`, detail: 'param', kind: 'param' as const, replaceTo: c + rest.length }))
  }
  if (ctx.kind === 'name' || force) {
    const q = ctx.kind === 'name' ? ctx.prefix.toUpperCase() : ''
    const fns: Option[] = FUNCTIONS.filter((f) => f.name.startsWith(q)).map((f) => {
      const empty = f.max === 0
      return { label: f.name, insert: empty ? `${f.name}()` : `${f.name}(`, detail: f.sig, kind: 'fn' as const,
               desc: t(`reportDesigner.fn.${f.name}`) }
    })
    const kws: Option[] = ['AND', 'OR', 'NOT', 'TRUE', 'FALSE', 'NULL'].filter((k) => q && k.startsWith(q) && k !== q)
      .map((k) => ({ label: k, insert: k, detail: '', kind: 'kw' as const }))
    const fieldOpts: Option[] = force && ctx.kind !== 'name'
      ? fields.slice(0, 40).map((f) => ({ label: f.name, insert: fieldRef(f.name), detail: f.type, kind: 'field' as const }))
      : []
    if (ctx.kind === 'name' && q.length < 1) return []
    return [...fieldOpts, ...fns, ...kws].slice(0, 80)
  }
  return []
}

function refresh(force = false) {
  const list = compute(force)
  options.value = list
  active.value = 0
  open.value = list.length > 0 && focused.value
  if (open.value) place()
}

function place() {
  const el = ta.value
  if (!el) return
  const r = el.getBoundingClientRect()
  const below = window.innerHeight - r.bottom
  const style: Record<string, string> = { left: `${Math.min(r.left, window.innerWidth - 340)}px`, width: `${Math.max(280, Math.min(r.width, 360))}px` }
  if (below < 240 && r.top > below) style.bottom = `${window.innerHeight - r.top + 4}px`
  else style.top = `${r.bottom + 4}px`
  listPos.value = style
}

function accept(o: Option) {
  const el = ta.value
  if (!el) return
  const v = text.value
  const c = caret.value
  const ctx = completionAt(v, c)
  const from = ctx.kind === 'none' ? c : ctx.from
  const to = o.replaceTo ?? c
  const next = v.slice(0, from) + o.insert + v.slice(to)
  const at = from + o.insert.length - (o.back ?? 0)
  emit('update:modelValue', next)
  emit('edited')
  open.value = false
  nextTick(() => {
    el.focus()
    el.setSelectionRange(at, at)
    caret.value = at
    scheduleCheck()
  })
}

// ── typing ──
function onInput(e: Event) {
  const el = e.target as HTMLTextAreaElement
  let v = el.value
  if (!props.multiline && v.includes('\n')) v = v.replace(/\n/g, ' ')
  caret.value = el.selectionStart ?? v.length
  emit('update:modelValue', v)
  emit('edited')
  scheduleCheck()
  nextTick(() => refresh())
}

function onKey(e: KeyboardEvent) {
  if (open.value) {
    if (e.key === 'ArrowDown') { e.preventDefault(); active.value = (active.value + 1) % options.value.length; scrollActive(); return }
    if (e.key === 'ArrowUp') { e.preventDefault(); active.value = (active.value - 1 + options.value.length) % options.value.length; scrollActive(); return }
    if (e.key === 'Enter' || e.key === 'Tab') {
      const o = options.value[active.value]
      if (o) { e.preventDefault(); accept(o); return }
    }
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); open.value = false; return }
  }
  if (e.key === ' ' && (e.ctrlKey || e.metaKey)) {
    e.preventDefault()
    caret.value = ta.value?.selectionStart ?? 0
    refresh(true)
    return
  }
  if (e.key === 'Enter' && !(props.multiline && e.shiftKey)) e.preventDefault()
}

function onCaret() {
  caret.value = ta.value?.selectionStart ?? 0
}

function scrollActive() {
  nextTick(() => document.getElementById(optId(active.value))?.scrollIntoView({ block: 'nearest' }))
}

function onBlur() {
  focused.value = false
  // a click on the list lands after the blur: it is kept open for that click
  setTimeout(() => { if (!focused.value) open.value = false }, 150)
  runCheck()
}

// the signature of the call the caret is in
const call = computed(() => (focused.value && isExpr.value ? callAt(text.value, caret.value) : null))
const sigParts = computed(() => {
  const c = call.value
  if (!c) return null
  const m = /^(\w+)\((.*)\)$/.exec(c.fn.sig)
  if (!m) return { name: c.fn.name, args: [] as { text: string; on: boolean }[] }
  const args = m[2] ? m[2].split(/,\s*/) : []
  const on = Math.min(c.arg, Math.max(0, args.length - 1))
  return { name: m[1], args: args.map((a, i) => ({ text: a, on: i === on && args.length > 0 })) }
})

defineExpose({ focus: () => ta.value?.focus() })
</script>

<template>
  <div class="rx" :class="{ bad: !!shown, focused, disabled }">
    <div class="rx-box">
      <div class="rx-mirror" aria-hidden="true"><template v-for="(s, i) in segments" :key="i"><span v-if="s.mark" class="rx-mark" /><span v-else :class="[`k-${s.kind}`, { 'rx-bad': s.bad }]">{{ s.text }}</span></template><span class="rx-tail">&#8203;</span><span v-if="!text && placeholder" class="rx-ph">{{ placeholder }}</span></div>
      <textarea
        :id="id"
        ref="ta"
        class="rx-input"
        rows="1"
        spellcheck="false"
        autocomplete="off"
        autocapitalize="off"
        :value="text"
        :disabled="disabled"
        :aria-label="label"
        role="combobox"
        aria-autocomplete="list"
        :aria-expanded="open"
        :aria-controls="`${uid}-list`"
        :aria-activedescendant="open ? optId(active) : undefined"
        :aria-invalid="!!shown"
        :aria-describedby="shown ? `${uid}-err` : undefined"
        @input="onInput"
        @keydown="onKey"
        @keyup="onCaret"
        @click="onCaret"
        @focus="focused = true; onCaret()"
        @blur="onBlur"
      />
    </div>
    <p v-if="sigParts" class="rx-sig">
      <span class="rx-fn">{{ sigParts.name }}</span>(<template v-for="(a, i) in sigParts.args" :key="i"><span :class="{ on: a.on }">{{ a.text }}</span><template v-if="i < sigParts.args.length - 1">, </template></template>)
      <span v-if="call" class="rx-desc">{{ $t(`reportDesigner.fn.${call.fn.name}`) }}</span>
    </p>
    <p v-if="shown" :id="`${uid}-err`" class="rx-err" :role="shown.server ? 'alert' : undefined">
      <span>{{ shown.message }}</span>
      <span v-if="shown.pos != null" class="rx-at">{{ $t('reportDesigner.expr.at', { n: shown.pos + 1 }) }}</span>
    </p>
    <Teleport to="body">
      <ul v-if="open" :id="`${uid}-list`" class="rx-list" role="listbox" :style="listPos" @mousedown.prevent>
        <li
          v-for="(o, i) in options"
          :id="optId(i)"
          :key="`${o.kind}-${o.label}`"
          role="option"
          :aria-selected="i === active"
          :class="{ on: i === active }"
          @click="accept(o)"
          @mousemove="active = i"
        >
          <span class="rx-oicon" :class="o.kind"><RdFieldIcon :type="o.kind === 'field' ? o.detail : o.kind" :size="12" /></span>
          <span class="rx-olabel">{{ o.kind === 'fn' ? o.detail : o.label }}</span>
          <span v-if="o.desc && i === active" class="rx-odesc">{{ o.desc }}</span>
        </li>
      </ul>
    </Teleport>
  </div>
</template>

<style scoped>
.rx { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.rx-box { position: relative; min-width: 0; }
.rx-mirror, .rx-input {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; line-height: 18px;
  padding: 5px 8px; border-width: 1px; border-style: solid; border-radius: 8px;
  white-space: pre-wrap; overflow-wrap: anywhere; word-break: normal; letter-spacing: 0; tab-size: 2;
  box-sizing: border-box; width: 100%; margin: 0;
}
.rx-mirror { min-height: 30px; color: var(--text); border-color: transparent; background: var(--bg-soft); }
.rx-input {
  position: absolute; inset: 0; height: 100%; resize: none; overflow: hidden; background: transparent;
  color: transparent; caret-color: var(--text); border-color: var(--control-border);
}
.rx-input::selection { background: color-mix(in srgb, var(--accent) 34%, transparent); color: transparent; }
.rx.bad .rx-input { border-color: var(--danger); }
.rx.bad .rx-input:focus { box-shadow: 0 0 0 3px color-mix(in srgb, var(--danger) 18%, transparent); }
.rx.disabled { opacity: 0.6; }
.rx-tail { display: inline; }
.rx-ph { position: absolute; left: 9px; top: 6px; color: var(--muted); pointer-events: none; font-family: 'IBM Plex Sans', system-ui, sans-serif; font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: calc(100% - 18px); }
.k-field { color: var(--accent-hi); }
.k-fn { color: var(--violet); font-weight: 600; }
.k-str { color: var(--accent-2); }
.k-num { color: var(--warning); }
.k-ph { color: var(--warning); }
.k-kw { color: var(--violet); }
.k-op, .k-punct { color: var(--muted); }
.k-bad { color: var(--danger); }
.rx-bad { text-decoration: underline wavy var(--danger); text-decoration-skip-ink: none; text-underline-offset: 3px; }
.rx-mark { display: inline-block; width: 0; height: 14px; border-left: 2px solid var(--danger); vertical-align: -3px; margin: 0 -1px; }
.rx-sig { margin: 0; font-size: 11.5px; color: var(--muted); font-family: ui-monospace, SFMono-Regular, Menlo, monospace; line-height: 1.4; overflow-wrap: anywhere; }
.rx-sig .on { color: var(--text); font-weight: 600; text-decoration: underline; text-underline-offset: 2px; }
.rx-fn { color: var(--violet); font-weight: 600; }
.rx-desc { display: block; font-family: 'IBM Plex Sans', system-ui, sans-serif; color: var(--muted); }
.rx-err { margin: 0; display: flex; gap: 6px; align-items: baseline; flex-wrap: wrap; font-size: 12px; color: var(--danger); line-height: 1.35; overflow-wrap: anywhere; }
.rx-at { color: var(--muted); font-size: 11px; white-space: nowrap; }

.rx-list {
  position: fixed; z-index: 2600; margin: 0; padding: 4px; list-style: none; max-height: 260px; overflow: auto;
  background: var(--panel); border: 1px solid var(--border); border-radius: 10px; box-shadow: var(--shadow-2);
}
.rx-list li {
  display: grid; grid-template-columns: 22px minmax(0, 1fr); align-items: center; column-gap: 6px; row-gap: 2px;
  padding: 5px 7px; border-radius: 6px; cursor: pointer; font-size: 12.5px;
}
.rx-list li.on { background: var(--panel-2); }
.rx-oicon {
  display: grid; place-items: center; height: 18px; border-radius: 5px; font-size: 10.5px; font-weight: 600;
  color: var(--muted); background: var(--bg-soft); font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}
.rx-oicon.fn, .rx-oicon.kw { color: var(--violet); }
.rx-oicon.field { color: var(--accent-hi); }
.rx-olabel { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.rx-odesc { grid-column: 2; font-size: 11.5px; color: var(--muted); line-height: 1.35; }
</style>
