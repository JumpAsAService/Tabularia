<script setup lang="ts">
// An AI comment's properties (docs/specs/ai-comment.md): the author's instruction, the elements it reads,
// and **Try it**, which makes the call only when it is pressed. The panel says, before anyone is surprised,
// why it will not write, and which of the elements picked the model will never read: those that only list rows.
import { computed, inject } from 'vue'
import { useI18n } from 'vue-i18n'
import { CircleAlert, LoaderCircle, Sparkles } from 'lucide-vue-next'
import RdSection from '~/components/report/designer/RdSection.vue'
import { COMMENT_TRIAL, ON_A_BOARD, useDesign } from '~/composables/reportDesignStore'
import { readableBy, showsOnlyRows } from '~/composables/dashboardComments'
import type { DComment } from '~/composables/reportDesignModel'

const MAX_INSTRUCTION = 1000 // as the gateway's (services/report_definition.py)
const MAX_READS = 10

const props = defineProps<{ item: DComment }>()
const store = useDesign()
const trial = inject(COMMENT_TRIAL, null)
const onBoard = inject(ON_A_BOARD, false)
const { t } = useI18n()

const id = computed(() => props.item.id)
const reads = computed(() => props.item.reads ?? [])
const elements = computed(() => readableBy(store.doc.value.sections.body.items as unknown as Record<string, any>[]))
const nameOf = (el: Record<string, any>) => el.title || el.label || el.id
const kindOf = (el: Record<string, any>) => t(`reportDesigner.items.${el.type === 'table' && el.matrix ? 'matrix' : el.type}`)
const full = computed(() => reads.value.length >= MAX_READS)
function setRead(name: string, on: boolean) {
  props.item.reads = on ? [...reads.value, name] : reads.value.filter((x) => x !== name)
}

const problemOf = (path: string) => [...store.problems.value, ...store.liveProblems.value].find((p) => p.item === id.value && p.path.startsWith(path)) ?? null
const onlyRows = computed(() => elements.value.filter((el) => reads.value.includes(el.id) && showsOnlyRows(el, trial?.answerOf(el.id))))
const nothingToRead = computed(() => !!reads.value.length && onlyRows.value.length === reads.value.length)

const status = computed(() => trial?.status.value ?? null)
const off = computed(() => !!status.value?.metadata_only)
const noModel = computed(() => !!status.value && !off.value && !status.value.comment_model)
const cannot = computed(() => trial?.cannotTry(id.value) ?? '')
const trying = computed(() => trial?.trying.value === id.value)
const tried = computed(() => trial?.tried(id.value) ?? null)
</script>

<template>
  <RdSection :title="$t('dashboardDesigner.commentWords')" skey="comment-words" :bad="!!problemOf('instruction')">
    <!-- a tile's title is its own, above; an item on paper has it here -->
    <label v-if="!onBoard" class="rp-f">
      <span>{{ $t('reportDesigner.commentTitle') }}</span>
      <input v-model="item.title" type="text" maxlength="200" data-field="comment-title" />
    </label>
    <label class="rp-f">
      <span>{{ $t('dashboardDesigner.commentInstruction') }}</span>
      <textarea v-model="item.instruction" class="cmp-area" data-field="comment-instruction" rows="4" :maxlength="MAX_INSTRUCTION" :placeholder="$t('dashboardDesigner.commentInstructionPh')" />
    </label>
    <p class="rp-help cmp-count">{{ (item.instruction ?? '').length }} / {{ MAX_INSTRUCTION }}</p>
    <p class="rp-help">{{ $t('dashboardDesigner.commentInstructionHelp') }}</p>
    <p v-if="problemOf('instruction')" class="rp-err" role="alert">{{ problemOf('instruction')?.message }}</p>
  </RdSection>

  <RdSection :title="$t('dashboardDesigner.commentReads')" skey="comment-reads" :count="`${reads.length} / ${MAX_READS}`" :bad="!!problemOf('reads')">
    <p v-if="!elements.length" class="rp-help">{{ $t('dashboardDesigner.commentNothingToPick') }}</p>
    <div v-else class="rp-checks" role="group" :aria-label="$t('dashboardDesigner.commentReads')">
      <label v-for="el in elements" :key="el.id" class="rp-check">
        <input type="checkbox" :data-read="el.id" :checked="reads.includes(el.id)" :disabled="full && !reads.includes(el.id)" @change="setRead(el.id, ($event.target as HTMLInputElement).checked)" />
        <span class="cmp-name" :title="nameOf(el)">{{ nameOf(el) }}</span>
        <span class="cmp-kind">{{ kindOf(el) }}</span>
      </label>
    </div>
    <p class="rp-help">{{ $t(onBoard ? 'dashboardDesigner.commentReadsHelp' : 'reportDesigner.commentReadsHelp', { n: MAX_READS }) }}</p>
    <p v-for="el in onlyRows" :key="el.id" class="cmp-note" role="note">
      <CircleAlert :size="13" aria-hidden="true" />
      <span>{{ $t('dashboardDesigner.commentOnlyRows', { name: nameOf(el) }) }}</span>
    </p>
    <p v-if="nothingToRead" class="cmp-note" role="note" data-why="nothing-to-read">
      <CircleAlert :size="13" aria-hidden="true" />
      <span>{{ $t('dashboardDesigner.commentNothingLeft') }}</span>
    </p>
    <p v-if="problemOf('reads')" class="rp-err" role="alert">{{ problemOf('reads')?.message }}</p>
  </RdSection>

  <RdSection :title="$t('dashboardDesigner.commentTry')" skey="comment-try">
    <p v-if="off" class="cmp-note" role="note" data-why="off"><CircleAlert :size="13" aria-hidden="true" /> <span>{{ $t('dashboardDesigner.commentOff') }}</span></p>
    <p v-else-if="noModel" class="cmp-note" role="note" data-why="no-model"><CircleAlert :size="13" aria-hidden="true" /> <span>{{ $t('dashboardDesigner.commentNoModel') }}</span></p>
    <p v-else-if="status?.comment_model" class="rp-help">{{ $t('dashboardDesigner.commentWith', { model: status.comment_model }) }}</p>
    <button v-if="trial" type="button" class="cmp-try" data-field="comment-try" :disabled="!!cannot || off || noModel || trying" :title="cannot" @click="trial.tryIt(id)">
      <LoaderCircle v-if="trying" :size="13" class="spin" aria-hidden="true" /><Sparkles v-else :size="13" aria-hidden="true" />
      {{ $t(trying ? 'dashboard.commentWriting' : 'dashboardDesigner.commentTry') }}
    </button>
    <p v-if="cannot && !off && !noModel" class="rp-help">{{ cannot }}</p>
    <p class="rp-help" role="status" data-tried>
      <template v-if="tried?.state === 'ready'">{{ $t(onBoard ? 'dashboardDesigner.commentTried' : 'reportDesigner.commentTried') }}</template>
      <template v-else-if="tried">{{ tried.message }}<template v-if="tried.reason"> ({{ tried.reason }})</template></template>
    </p>
  </RdSection>
</template>

<style scoped src="~/assets/reportprops.css" />
<style scoped>
.cmp-area { width: 100%; box-sizing: border-box; resize: vertical; padding: 5px 8px; font: inherit; font-size: 13px; font-weight: 400; line-height: 1.45; min-height: 74px; }
.cmp-count { text-align: right; font-variant-numeric: tabular-nums; margin-top: -6px; }
.cmp-name { flex: 1; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.cmp-kind { flex: none; font-size: 11px; color: var(--muted); }
.cmp-note {
  margin: 0; padding: 6px 8px; display: flex; gap: 6px; align-items: flex-start; font-size: 12px; line-height: 1.4; color: var(--text); overflow-wrap: anywhere;
  background: var(--panel-2); border: 1px solid color-mix(in srgb, var(--warning) 40%, var(--border)); border-radius: 6px;
}
.cmp-note svg { flex: none; margin-top: 2px; color: var(--warning); }
.cmp-try { align-self: flex-start; display: inline-flex; align-items: center; gap: 6px; }
</style>
