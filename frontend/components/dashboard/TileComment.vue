<script setup lang="ts">
// An AI comment inside its tile (docs/specs/ai-comment.md). The text is a text node, whatever it holds, and
// wraps anywhere; the line that says it is the model's, which one and on which numbers, and that it may be
// wrong, is always there and is no setting. In its place, what the gateway said: why there is none, or
// **Comment this view** after a click. In the designer the text comes from **Try it** alone.
import { computed, nextTick, ref, watch } from 'vue'
import { Clock, LoaderCircle, Lock, Sparkles } from 'lucide-vue-next'
import type { CommentSlot } from '~/composables/dashboardComments'
import { useFormat } from '~/composables/useFormat'

const props = defineProps<{ entry?: CommentSlot | null; drawing?: boolean; label?: string }>()
const emit = defineEmits<{ (e: 'ask'): void }>()

const fmtx = useFormat()
const answer = computed(() => props.entry?.answer ?? null)
const asked = computed(() => props.entry?.asking === 'asked')
const written = computed(() => answer.value?.state === 'ready')
const time = computed(() => (answer.value?.numbers_at ? fmtx.dateTime(answer.value.numbers_at, { seconds: true }) : ''))

// what a person asked for has come: the keys go on from there, as the button they were on is gone
const root = ref<HTMLElement | null>(null)
watch(asked, (now, was) => {
  if (was && !now && !props.drawing && root.value?.contains(document.activeElement)) {
    nextTick(() => root.value?.querySelector<HTMLElement>('[data-comment-focus]')?.focus({ preventScroll: true }))
  }
})
</script>

<template>
  <div ref="root" class="tcm" :aria-busy="asked ? 'true' : 'false'">
    <!-- on its way: the shape of a text, in the place it will take -->
    <template v-if="!answer">
      <p v-if="drawing && !asked" class="cm-state cm-note"><Sparkles :size="15" aria-hidden="true" /> <span>{{ $t('dashboardDesigner.commentNotTried') }}</span></p>
      <div v-else class="cm-skel" aria-hidden="true">
        <span class="sk" style="width: 92%" /><span class="sk" style="width: 78%" /><span class="sk" style="width: 85%" /><span class="sk" style="width: 40%" />
      </div>
      <span v-if="!drawing || asked" class="sr-only">{{ $t('dashboard.commentLoading') }}</span>
    </template>

    <template v-else-if="written">
      <p v-if="answer.stale" class="cm-stale" role="status">
        <Clock :size="11" aria-hidden="true" /> <span>{{ $t('dashboard.commentStale', { time }) }}</span>
      </p>
      <!-- scrolls by itself when it is longer than its tile, so the keys can reach it -->
      <p class="cm-text" tabindex="0" data-comment-focus :aria-label="label || $t('dashboard.type_comment')">{{ answer.text }}</p>
      <p class="cm-line">
        <Sparkles :size="11" aria-hidden="true" />
        <span>{{ $t('dashboard.commentBy', { model: answer.model, time }) }}. {{ $t('dashboard.commentMayBeWrong') }}</span>
      </p>
    </template>

    <div v-else class="cm-state">
      <Lock v-if="answer.state === 'no_access'" :size="15" aria-hidden="true" />
      <p v-if="answer.message" tabindex="-1" data-comment-focus>{{ answer.message }}<template v-if="answer.reason"> ({{ answer.reason }})</template></p>
      <button
        v-if="(answer.can_ask || asked) && !drawing"
        type="button"
        class="cm-ask"
        :aria-disabled="asked ? 'true' : 'false'"
        @click="!asked && emit('ask')"
      >
        <LoaderCircle v-if="asked" :size="13" class="spin" aria-hidden="true" /><Sparkles v-else :size="13" aria-hidden="true" />
        {{ $t(asked ? 'dashboard.commentWriting' : 'dashboard.commentThisView') }}
      </button>
    </div>
  </div>
</template>

<style scoped>
.tcm { flex: 1; min-height: 0; min-width: 0; display: flex; flex-direction: column; gap: 6px; }
.cm-skel { flex: 1; display: flex; flex-direction: column; gap: 8px; min-height: 0; overflow: hidden; }
.cm-text {
  flex: 1; min-height: 0; margin: 0; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; font-size: 13px; line-height: 1.5; color: inherit; border-radius: 4px;
}
.cm-text:focus-visible, .cm-state p:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
.cm-state p:focus { outline: none; }
/* not to be missed, and not to be taken for the text: the same small line at every size of the tile */
.cm-line {
  flex: none; margin: 0; display: flex; gap: 5px; align-items: baseline; font-size: 11px; line-height: 1.35; color: var(--muted); overflow-wrap: anywhere;
}
.cm-line svg { flex: none; align-self: center; color: var(--accent); }
.cm-stale {
  flex: none; margin: 0; padding: 1px 6px; display: flex; align-items: center; gap: 4px; align-self: flex-start; max-width: 100%; box-sizing: border-box;
  font-size: 11px; line-height: 1.35; color: var(--warning); background: var(--panel-2); border: 1px solid color-mix(in srgb, var(--warning) 40%, var(--border)); border-radius: 6px;
}
.cm-stale svg { flex: none; }
.cm-state {
  flex: 1; min-height: 0; margin: 0; padding: 4px; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 8px; text-align: center;
  font-size: 12.5px; line-height: 1.4; color: var(--muted); overflow: auto; overflow-wrap: anywhere;
}
.cm-state p { margin: 0; max-width: 60ch; }
.cm-note svg { flex: none; color: var(--accent); }
.cm-ask { flex: none; display: inline-flex; align-items: center; gap: 6px; color: var(--text); }
.cm-ask[aria-disabled='true'] { cursor: progress; opacity: 0.75; }
</style>
