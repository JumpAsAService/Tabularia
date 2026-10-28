<script setup lang="ts">
// The steps the Assistant page's agent took in a turn (docs/specs/mcp-agent.md, «visible
// work»): each one in words, with its state, how long it took and its result; the attempts
// that failed and were tried again under the same step, each with its reason; the arguments
// on demand. A query's table — the evidence of the numbers — comes from the page, in the
// `evidence` slot.
import { computed, reactive } from 'vue'
import { Check, CircleAlert, LoaderCircle, RotateCcw } from 'lucide-vue-next'
import { groupSteps, useAgentSteps, type AgentStep } from '~/composables/useAgentSteps'

const props = defineProps<{ steps: AgentStep[]; dsName: (id: unknown) => string }>()
defineSlots<{ evidence(props: { step: AgentStep }): any }>()

const { label, result, duration } = useAgentSteps()
const groups = computed(() => groupSteps(props.steps))
const open = reactive<Record<string, boolean>>({})

function shownArgs(s: AgentStep): string {
  // the SQL is shown as such, apart
  const rest = Object.fromEntries(Object.entries(s.args ?? {}).filter(([k]) => k !== 'sql'))
  return Object.keys(rest).length ? JSON.stringify(rest, null, 2) : ''
}
</script>

<template>
  <ol class="steps">
    <li v-for="g in groups" :key="g.key" class="step" :class="g.last.state">
      <div class="step-line">
        <LoaderCircle v-if="g.last.state === 'running'" :size="13" class="step-ico spin" aria-hidden="true" />
        <CircleAlert v-else-if="g.last.state === 'error'" :size="13" class="step-ico" aria-hidden="true" />
        <RotateCcw v-else-if="g.failed.length" :size="13" class="step-ico" aria-hidden="true" />
        <Check v-else :size="13" class="step-ico" aria-hidden="true" />
        <span class="step-label">{{ label(g.last, dsName) }}</span>
        <span class="sr-only">{{ $t(`chat.step.state.${g.last.state}`) }}</span>
        <span v-if="result(g.last)" class="step-meta">{{ result(g.last) }}</span>
        <span v-if="g.failed.length" class="step-meta">{{ $t('chat.step.attempts', { n: g.failed.length + 1 }) }}</span>
        <span v-if="g.last.durationMs != null" class="step-meta num">{{ duration(g.last.durationMs) }}</span>
        <button class="linkish" type="button" :aria-expanded="!!open[g.key]" @click="open[g.key] = !open[g.key]">
          {{ open[g.key] ? $t('chat.step.hideDetails') : $t('chat.step.showDetails') }}
        </button>
      </div>
      <p v-if="g.last.state === 'error' && g.last.error" class="step-err">{{ g.last.error }}</p>
      <ul v-if="g.failed.length" class="attempts" :aria-label="$t('chat.step.failedAttempts')">
        <li v-for="(f, i) in g.failed" :key="f.id">
          {{ $t('chat.step.attempt', { n: i + 1, error: f.error || $t('chat.step.noReason') }) }}
        </li>
      </ul>
      <div v-if="open[g.key]" class="details">
        <template v-for="(a, i) in [...g.failed, g.last]" :key="a.id">
          <p v-if="g.failed.length" class="details-head">{{ $t('chat.step.attemptN', { n: i + 1 }) }}</p>
          <pre v-if="a.args?.sql" class="code"><code>{{ a.args.sql }}</code></pre>
          <pre v-if="shownArgs(a)" class="code"><code>{{ shownArgs(a) }}</code></pre>
          <p v-if="!a.args?.sql && !shownArgs(a)" class="muted small">{{ $t('chat.step.noArguments') }}</p>
        </template>
      </div>
      <slot name="evidence" :step="g.last" />
    </li>
  </ol>
</template>

<style scoped>
.steps { margin: 0 0 10px; padding: 0; list-style: none; display: flex; flex-direction: column; gap: 6px; }
.step-line { display: flex; align-items: center; flex-wrap: wrap; gap: 4px 6px; font-size: 12px; color: var(--muted); }
.step-label { color: var(--text); overflow-wrap: anywhere; }
.step-meta { color: var(--muted); }
.step-meta::before { content: '·'; margin-right: 6px; }
.num { font-variant-numeric: tabular-nums; }
.step-ico { flex: none; }
.step.ok .step-ico { color: var(--accent-2); }
.step.error .step-ico { color: var(--danger); }
.step.running .step-ico { color: var(--accent); }
.step-err { margin: 3px 0 0 19px; font-size: 12px; color: color-mix(in srgb, var(--danger) 65%, var(--text)); overflow-wrap: anywhere; }
.attempts { margin: 3px 0 0 19px; padding: 0; list-style: none; font-size: 12px; color: var(--muted); }
.attempts li { overflow-wrap: anywhere; }
.attempts li + li { margin-top: 2px; }
.linkish { padding: 0; border: 0; background: transparent; color: var(--accent-hi); font-size: 12px; box-shadow: none; text-decoration: underline; text-underline-offset: 2px; }
.linkish:hover { color: var(--text); box-shadow: none; }
.details { margin: 5px 0 0 19px; }
.details-head { margin: 6px 0 3px; font-size: 11.5px; font-weight: 600; color: var(--muted); }
.code { margin: 0 0 5px; padding: 8px 10px; border: 1px solid var(--border-soft); border-radius: 8px; background: var(--bg-soft); font-size: 12px; overflow-x: auto; max-height: 240px; }
.code code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; white-space: pre-wrap; overflow-wrap: anywhere; }
.small { font-size: 12px; margin: 0; }
</style>
