<script setup lang="ts">
// The AI agents' instructions (Admin panel → AI). Each agent is a row that opens on its
// text, which the administrators may rewrite or put back to the default, and under it the
// context Tabularia adds at every request (date, engine, files, the interface's language),
// shown with example values and not editable. The observer reads, and touches nothing.
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { ChevronRight, LoaderCircle, Lock, MessageSquareText, RotateCcw } from 'lucide-vue-next'
import { errMessage } from '~/composables/useApi'
import { useAi, type AiAgent, type AiPrompt } from '~/composables/useAi'
import { useFormat } from '~/composables/useFormat'

const props = defineProps<{ canWrite: boolean }>()

const MAX_CHARS = 20_000

const { t, locale } = useI18n()
const fmt = useFormat()
const api = useAi()
const toast = useToast()

const prompts = ref<AiPrompt[] | null>(null)
const error = ref('')
const open = reactive(new Set<AiAgent>())
// what is being written, apart from what is saved
const drafts = reactive<Record<string, string>>({})
const busy = ref<AiAgent | null>(null)

function take(p: AiPrompt) {
  drafts[p.agent] = p.text
  prompts.value = (prompts.value ?? []).map((x) => (x.agent === p.agent ? p : x))
}

async function load() {
  try {
    const list = await api.prompts(locale.value)
    // a language change reloads the context: the drafts being written stay
    for (const p of list) if (!(p.agent in drafts) || !isDirty(p.agent)) drafts[p.agent] = p.text
    prompts.value = list
    error.value = ''
  } catch (e) {
    error.value = errMessage(e)
  }
}
onMounted(load)
watch(locale, load)

function isDirty(agent: AiAgent): boolean {
  const p = prompts.value?.find((x) => x.agent === agent)
  return !!p && (drafts[agent] ?? '').trim() !== p.text.trim()
}

const dirtyCount = computed(() => (prompts.value ?? []).filter((p) => isDirty(p.agent)).length)

function toggle(agent: AiAgent) {
  if (open.has(agent)) open.delete(agent)
  else open.add(agent)
}

async function save(p: AiPrompt) {
  busy.value = p.agent
  try {
    take(await api.setPrompt(p.agent, drafts[p.agent] ?? '', locale.value))
    toast.success(t('aiPrompts.saved', { agent: t(`aiPrompts.agents.${p.agent}.name`) }))
  } catch (e) {
    toast.error(errMessage(e))
  } finally {
    busy.value = null
  }
}

async function restore(p: AiPrompt) {
  if (!confirm(t('aiPrompts.restoreConfirm', { agent: t(`aiPrompts.agents.${p.agent}.name`) }))) return
  busy.value = p.agent
  try {
    take(await api.resetPrompt(p.agent, locale.value))
    toast.success(t('aiPrompts.restored', { agent: t(`aiPrompts.agents.${p.agent}.name`) }))
  } catch (e) {
    toast.error(errMessage(e))
  } finally {
    busy.value = null
  }
}

function tooLong(agent: AiAgent): boolean {
  return (drafts[agent] ?? '').length > MAX_CHARS
}
</script>

<template>
  <div class="card">
    <h4><MessageSquareText :size="14" /> {{ $t('aiPrompts.title') }}</h4>
    <p class="muted small hint">{{ $t('aiPrompts.lead') }}</p>
    <p v-if="error" class="page-error" role="alert">{{ error }}</p>
    <p v-if="dirtyCount" class="unsaved small" role="status">{{ $t('aiPrompts.unsaved', { n: dirtyCount }) }}</p>

    <ul v-if="prompts" class="agents">
      <li v-for="p in prompts" :key="p.agent" class="agent" :class="{ open: open.has(p.agent) }">
        <button
          type="button"
          class="agent-head"
          :aria-expanded="open.has(p.agent)"
          :aria-controls="`ai-prompt-${p.agent}`"
          @click="toggle(p.agent)"
        >
          <ChevronRight :size="14" class="chev" aria-hidden="true" />
          <span class="agent-name">
            <strong>{{ $t(`aiPrompts.agents.${p.agent}.name`) }}</strong>
            <span class="muted small">{{ $t(`aiPrompts.agents.${p.agent}.purpose`) }}</span>
          </span>
          <span v-if="isDirty(p.agent)" class="tag warn">{{ $t('aiPrompts.tagUnsaved') }}</span>
          <span v-else-if="p.custom" class="tag on">{{ $t('aiPrompts.tagCustom') }}</span>
          <span v-else class="tag off">{{ $t('aiPrompts.tagDefault') }}</span>
        </button>

        <div v-if="open.has(p.agent)" :id="`ai-prompt-${p.agent}`" class="agent-body">
          <label class="field">
            <span class="field-head">
              <span>{{ $t('aiPrompts.text') }}</span>
              <span class="count" :class="{ over: tooLong(p.agent) }">
                {{ $t('aiPrompts.chars', { n: fmt.count((drafts[p.agent] ?? '').length), max: fmt.count(MAX_CHARS) }) }}
              </span>
            </span>
            <textarea
              v-model="drafts[p.agent]"
              rows="14"
              spellcheck="false"
              :readonly="!props.canWrite"
              :disabled="busy === p.agent"
            />
          </label>
          <p v-if="p.custom && p.updated_by" class="muted small meta">
            {{ $t('aiPrompts.changedBy', { who: p.updated_by, when: fmt.ago(p.updated_at) }) }}
          </p>
          <p v-if="p.agent.startsWith('dbt_')" class="muted small meta">{{ $t('aiPrompts.dbtNote') }}</p>

          <div v-if="props.canWrite" class="actions">
            <button
              type="button"
              class="primary"
              :disabled="busy !== null || !isDirty(p.agent) || !(drafts[p.agent] ?? '').trim() || tooLong(p.agent)"
              @click="save(p)"
            >
              <LoaderCircle v-if="busy === p.agent" :size="13" class="spin" /> {{ $t('aiPrompts.save') }}
            </button>
            <button v-if="isDirty(p.agent)" type="button" :disabled="busy !== null" @click="drafts[p.agent] = p.text">
              {{ $t('aiPrompts.discard') }}
            </button>
            <button v-if="p.custom" type="button" :disabled="busy !== null" @click="restore(p)">
              <RotateCcw :size="13" /> {{ $t('aiPrompts.restore') }}
            </button>
          </div>
          <p v-else class="note small">{{ $t('aiPrompts.readOnly') }}</p>

          <section class="context" :aria-label="$t('aiPrompts.contextTitle')">
            <h5><Lock :size="12" /> {{ $t('aiPrompts.contextTitle') }}</h5>
            <p class="muted small">{{ p.context ? $t('aiPrompts.contextHint') : $t('aiPrompts.noContext') }}</p>
            <pre v-if="p.context">{{ p.context }}</pre>
          </section>
        </div>
      </li>
    </ul>
    <p v-else-if="!error" class="muted small"><LoaderCircle :size="13" class="spin" /> {{ $t('aiPrompts.loading') }}</p>
  </div>
</template>

<style scoped>
.card { display: flex; flex-direction: column; gap: 10px; padding: 16px; background: var(--panel); border: 1px solid var(--border); border-radius: var(--radius); }
.card h4 { margin: 0; display: inline-flex; align-items: center; gap: 7px; }
.hint { margin: 0; line-height: 1.5; }
.small { font-size: 12px; }
.page-error { color: var(--danger); font-size: 13px; margin: 0; }
.unsaved { margin: 0; color: var(--warning); }

.agents { list-style: none; margin: 0; padding: 0; border: 1px solid var(--border); border-radius: 8px; }
.agent + .agent { border-top: 1px solid var(--border-soft); }
.agent-head {
  display: flex; align-items: center; gap: 10px; width: 100%; padding: 10px 12px; min-height: 0;
  border: 0; border-radius: 0; background: none; text-align: left; cursor: pointer; color: var(--text);
}
.agent-head:hover { background: var(--bg-soft); box-shadow: none; }
.agent-head:active { transform: none; }
.agent-head:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
.chev { flex: none; color: var(--muted); transition: transform 0.15s ease; }
.agent.open .chev { transform: rotate(90deg); }
.agent-name { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 2px; }
.agent-name strong { font-size: 13px; font-weight: 600; }
.tag { flex: none; font-size: 11px; padding: 1px 8px; border-radius: 999px; border: 1px solid var(--border); white-space: nowrap; }
.tag.on { color: var(--accent); border-color: color-mix(in srgb, var(--accent) 45%, var(--border)); }
.tag.warn { color: var(--warning); border-color: color-mix(in srgb, var(--warning) 45%, var(--border)); }
.tag.off { color: var(--muted); }

.agent-body { display: flex; flex-direction: column; gap: 10px; padding: 4px 12px 14px 36px; }
.field { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); }
.field-head { display: flex; justify-content: space-between; gap: 8px; }
.count { font-variant-numeric: tabular-nums; }
.count.over { color: var(--danger); }
textarea {
  width: 100%; min-height: 220px;
  font-size: 12.5px; line-height: 1.55; color: var(--text);
}
.meta { margin: 0; }
.actions { display: flex; gap: 8px; flex-wrap: wrap; }
.actions button { display: inline-flex; align-items: center; gap: 6px; }
.note { margin: 0; padding: 8px 10px; border-radius: 8px; background: var(--bg-soft); border: 1px solid var(--border); }

.context { display: flex; flex-direction: column; gap: 4px; padding-top: 10px; border-top: 1px dashed var(--border); }
.context h5 { margin: 0; display: inline-flex; align-items: center; gap: 6px; font-size: 12px; font-weight: 600; color: var(--muted); }
.context p { margin: 0; line-height: 1.5; }
.context pre {
  margin: 4px 0 0; max-height: 240px; overflow: auto; padding: 10px 12px; border-radius: 8px;
  background: var(--bg-soft); border: 1px solid var(--border-soft); font-size: 12px; line-height: 1.5;
  white-space: pre-wrap; overflow-wrap: anywhere; color: var(--text);
}

@media (max-width: 720px) {
  .agent-body { padding-left: 12px; }
}
</style>
