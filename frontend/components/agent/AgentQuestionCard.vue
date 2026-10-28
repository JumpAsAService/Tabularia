<script setup lang="ts">
// The questions of the Assistant page's agent (`ask_user`, docs/specs/mcp-agent.md): each
// with its choices — the recommended one first — and a free answer, one choice per
// question, all sent together. The answers become the person's next message, a line per
// question («question → answer»). Once answered, or once the conversation went on, the card
// stays as it was, locked, with the choices made.
import { computed, nextTick, onMounted, ref, useId, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { MessageCircleQuestionMark, Send } from 'lucide-vue-next'
import { answersMessage, type AgentQuestion } from '~/composables/useAi'

const props = defineProps<{
  questions: AgentQuestion[]
  // the answers given, one per question (null: not answered in the next message); missing while none
  answers?: (string | null)[] | null
  locked: boolean
  autofocus?: boolean
}>()
const emit = defineEmits<{ (e: 'send', message: string, answers: string[]): void }>()

const { t } = useI18n()
const uid = useId()
const root = ref<HTMLFormElement | null>(null)
// per question: the index of the choice, 'other', or nothing yet
const choice = ref<(number | 'other' | null)[]>([])
const other = ref<string[]>([])
const MAX_OTHER = 300

function fromAnswers() {
  choice.value = props.questions.map((q, i) => {
    const given = props.answers?.[i]
    if (given == null) return null
    const at = q.options.findIndex((o) => o.label.toLowerCase() === given.toLowerCase())
    return at >= 0 ? at : 'other'
  })
  other.value = props.questions.map((q, i) => {
    const given = props.answers?.[i]
    return given != null && choice.value[i] === 'other' ? given : ''
  })
}
fromAnswers()
watch(() => [props.questions, props.answers], fromAnswers)

const answerOf = (i: number): string => {
  const c = choice.value[i]
  if (c === 'other') return other.value[i].replace(/\s+/g, ' ').trim()
  return c == null ? '' : props.questions[i].options[c]?.label ?? ''
}
const complete = computed(() => props.questions.every((_, i) => answerOf(i) !== ''))
// locked without an answer: the person wrote something else, and the conversation went on
const unanswered = computed(() => props.locked && !(props.answers ?? []).some((a) => a != null))

function submit() {
  if (props.locked || !complete.value) return
  const answers = props.questions.map((_, i) => answerOf(i))
  emit('send', answersMessage(props.questions, answers), answers)
}

function pickOther(i: number) {
  if (!props.locked) choice.value[i] = 'other'
}

// the first choice takes the focus once the card can be answered: a card born at the end of a
// turn is locked until the turn has closed
let focused = false
async function focusFirst() {
  if (focused || !props.autofocus || props.locked) return
  focused = true
  await nextTick()
  root.value?.querySelector<HTMLInputElement>('input[type="radio"]')?.focus()
}
onMounted(focusFirst)
watch(() => props.locked, focusFirst)
</script>

<template>
  <form ref="root" class="ask" :class="{ locked }" :aria-labelledby="`${uid}-title`" @submit.prevent="submit">
    <p :id="`${uid}-title`" class="ask-title">
      <MessageCircleQuestionMark :size="15" aria-hidden="true" />
      {{ locked ? (unanswered ? t('chat.ask.titleSkipped') : t('chat.ask.titleAnswered')) : t('chat.ask.title') }}
    </p>
    <fieldset v-for="(q, qi) in questions" :key="qi" class="q" :disabled="locked">
      <legend>{{ q.question }}</legend>
      <label v-for="(o, oi) in q.options" :key="oi" class="opt" :class="{ on: choice[qi] === oi }">
        <input v-model="choice[qi]" type="radio" :name="`${uid}-${qi}`" :value="oi">
        <span class="opt-text">
          <span class="opt-label">
            {{ o.label }}
            <span v-if="o.recommended" class="tag">{{ t('chat.ask.recommended') }}</span>
          </span>
          <span v-if="o.description" class="opt-desc">{{ o.description }}</span>
        </span>
      </label>
      <div class="opt other" :class="{ on: choice[qi] === 'other' }">
        <label class="other-pick">
          <input v-model="choice[qi]" type="radio" :name="`${uid}-${qi}`" value="other">
          <span class="opt-label">{{ t('chat.ask.other') }}</span>
        </label>
        <input
          v-model="other[qi]"
          type="text"
          class="other-text"
          :maxlength="MAX_OTHER"
          :placeholder="t('chat.ask.otherPlaceholder')"
          :aria-label="t('chat.ask.otherLabel', { question: q.question })"
          @focus="pickOther(qi)"
          @input="pickOther(qi)"
        >
      </div>
    </fieldset>
    <div v-if="!locked" class="ask-foot">
      <button class="primary" type="submit" :disabled="!complete">
        <Send :size="13" aria-hidden="true" /> {{ t('chat.ask.send') }}
      </button>
      <span v-if="!complete" class="muted small">{{ t('chat.ask.answerAll') }}</span>
    </div>
  </form>
</template>

<style scoped>
.ask { margin: 10px 0 2px; padding: 12px 14px; border: 1px solid color-mix(in srgb, var(--accent) 40%, var(--border)); border-radius: 12px; background: var(--panel); }
.ask.locked { border-color: var(--border-soft); }
.ask-title { display: flex; align-items: center; gap: 7px; margin: 0 0 8px; font-size: 13px; font-weight: 600; }
.ask-title svg { color: var(--accent-hi); flex: none; }
.q { margin: 0; padding: 0; border: 0; min-width: 0; }
.q + .q { margin-top: 12px; }
.q legend { padding: 0; margin: 0 0 6px; font-size: 13.5px; font-weight: 500; overflow-wrap: anywhere; }
.opt { display: flex; align-items: flex-start; gap: 9px; margin-top: 5px; padding: 7px 10px; border: 1px solid var(--border-soft); border-radius: 9px; background: var(--bg-soft); cursor: pointer; transition: border-color 0.15s, background 0.15s; }
.opt:hover { border-color: color-mix(in srgb, var(--accent) 45%, var(--border)); }
.opt.on { border-color: var(--accent); background: var(--tint-accent); }
.opt input[type="radio"] { flex: none; margin: 3px 0 0; }
.opt input[type="radio"]:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.opt-text { display: flex; flex-direction: column; gap: 1px; min-width: 0; }
.opt-label { display: inline-flex; align-items: center; flex-wrap: wrap; gap: 6px; font-size: 13px; color: var(--text); overflow-wrap: anywhere; }
.opt-desc { font-size: 12px; color: var(--muted); line-height: 1.4; overflow-wrap: anywhere; }
.tag { font-size: 11px; font-weight: 600; padding: 1px 7px; border-radius: 20px; background: var(--tint-accent); color: color-mix(in srgb, var(--accent) 75%, var(--text)); }
.other { flex-wrap: wrap; align-items: center; cursor: default; }
.other-pick { display: inline-flex; align-items: center; gap: 9px; cursor: pointer; }
.other-pick input { margin: 0; }
.other-text { flex: 1 1 220px; min-width: 0; padding: 5px 8px; font-size: 13px; }
.q:disabled .opt { cursor: default; }
.q:disabled .opt:not(.on) { opacity: 0.6; }
.q:disabled .opt:hover { border-color: var(--border-soft); }
.q:disabled .opt.on:hover { border-color: var(--accent); }
.ask-foot { display: flex; align-items: center; flex-wrap: wrap; gap: 8px 12px; margin-top: 12px; }
.small { font-size: 12px; }
@media (prefers-reduced-motion: reduce) { .opt { transition: none; } }
</style>
