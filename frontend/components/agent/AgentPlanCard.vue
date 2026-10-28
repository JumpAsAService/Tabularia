<script setup lang="ts">
// The plan the Assistant page's agent proposed (docs/specs/mcp-agent.md), under its answer:
// what it does, where, when and to whom, the first rows, the differences of a changed flow
// — and three buttons. «Confirm» applies it (the gateway checks the permissions again, in
// order, and stops at the first error), «Cancel» drops it, «Change» hands the word back to
// the person, to tell the assistant what to change. Only this click applies a chat's plan:
// the agent has no tool to confirm. Afterwards the card says how it went, with the links.
//
// A plan the conversation changed later is shown in full only under its latest answer;
// the earlier cards say so in one line.
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { Bot, ExternalLink, LoaderCircle, PencilLine } from 'lucide-vue-next'
import { errMessage } from '~/composables/useApi'
import { useFormat } from '~/composables/useFormat'
import { useToast } from '~/composables/useToast'
import { useAgentPlans, type AgentPlan } from '~/composables/useAgentPlans'
import AgentPlanActions from '~/components/agent/AgentPlanActions.vue'

const props = defineProps<{ plan: AgentPlan; superseded?: boolean }>()
const emit = defineEmits<{ (e: 'update', plan: AgentPlan): void; (e: 'change'): void }>()

const { t } = useI18n()
const api = useAgentPlans()
const fmt = useFormat()
const toast = useToast()
const busy = ref<'confirm' | 'cancel' | null>(null)
const announcement = ref('')

const helpOf = (s: AgentPlan['status']) => (s !== 'pending' && s !== 'applying' ? t(`agentPlan.statusHelp.${s}`) : '')
const statusLabel = computed(() => t(`agentPlan.status.${props.plan.status}`))
const statusHelp = computed(() => helpOf(props.plan.status))

async function act(kind: 'confirm' | 'cancel') {
  if (busy.value) return
  busy.value = kind
  try {
    const plan = kind === 'confirm' ? await api.confirm(props.plan) : await api.cancel(props.plan.id)
    emit('update', plan)
    if (plan.status === 'applied') toast.success(t('agentPlan.confirmedToast'))
    else if (plan.status === 'failed') toast.error(t('agentPlan.failedToast'))
    else if (plan.status === 'cancelled') toast.info(t('agentPlan.cancelledToast'))
    announcement.value = `${t(`agentPlan.status.${plan.status}`)}. ${helpOf(plan.status)}`
  } catch (e) {
    toast.error(errMessage(e))
    // what it is now (expired, applied elsewhere, changed by the agent since it was shown…): the
    // card shows it as it is, and does not keep offering the buttons on what it was
    try { emit('update', await api.get(props.plan.id)) } catch { /* the card stays as it was */ }
  } finally {
    busy.value = null
  }
}
</script>

<template>
  <section class="plan-card" :class="[plan.status, { superseded }]" :aria-labelledby="`plan-${plan.id}-title`">
    <header class="head">
      <Bot :size="15" class="head-ico" aria-hidden="true" />
      <h3 :id="`plan-${plan.id}-title`">{{ $t('chat.plan.title', { id: plan.id }) }}</h3>
      <span class="state" :class="plan.status">{{ statusLabel }}</span>
      <NuxtLink :to="`/agent-plan?id=${plan.id}`" class="page-link">
        <ExternalLink :size="12" aria-hidden="true" /> {{ $t('chat.plan.openPage') }}
      </NuxtLink>
    </header>
    <p v-if="superseded" class="lead">{{ $t('chat.plan.updatedBelow') }}</p>
    <template v-else>
      <p v-if="plan.status === 'pending'" class="lead">
        {{ $t('chat.plan.lead') }}
        <span class="muted">{{ $t('agentPlan.expiresAt', { time: fmt.dateTime(plan.expires_at) }) }}</span>
      </p>
      <p v-else-if="statusHelp" class="lead" :class="{ bad: plan.status === 'failed' }">{{ statusHelp }}</p>
      <AgentPlanActions :plan="plan" compact />
      <div v-if="plan.can_confirm" class="buttons">
        <button
          class="primary"
          type="button"
          :disabled="!!busy"
          :aria-busy="busy === 'confirm'"
          :aria-label="$t('chat.plan.confirmLabel', { id: plan.id })"
          @click="act('confirm')"
        >
          <LoaderCircle v-if="busy === 'confirm'" :size="14" class="spin" aria-hidden="true" />
          {{ busy === 'confirm' ? $t('agentPlan.confirming') : $t('chat.plan.confirm') }}
        </button>
        <button type="button" :disabled="!!busy" @click="emit('change')">
          <PencilLine :size="13" aria-hidden="true" /> {{ $t('chat.plan.change') }}
        </button>
        <button type="button" :disabled="!!busy" :aria-busy="busy === 'cancel'" @click="act('cancel')">
          {{ busy === 'cancel' ? $t('agentPlan.cancelling') : $t('chat.plan.cancel') }}
        </button>
      </div>
    </template>
    <p class="sr-only" role="status" aria-live="polite">{{ announcement }}</p>
  </section>
</template>

<style scoped>
.plan-card { margin: 10px 0 2px; padding: 12px 14px; border: 1px solid color-mix(in srgb, var(--accent) 40%, var(--border)); border-radius: 12px; background: var(--panel); min-width: 0; }
.plan-card:not(.pending) { border-color: var(--border-soft); }
.plan-card.superseded { padding: 8px 12px; }
.head { display: flex; align-items: center; flex-wrap: wrap; gap: 6px 9px; }
.head-ico { color: var(--accent-hi); flex: none; }
.head h3 { margin: 0; font-size: 13.5px; font-weight: 600; }
.state { font-size: 11.5px; font-weight: 600; padding: 2px 8px; border-radius: 20px; background: var(--panel-2); color: var(--muted); border: 1px solid var(--border-soft); }
.state.pending, .state.applying { background: var(--tint-accent); color: color-mix(in srgb, var(--accent) 70%, var(--text)); }
.state.applied { background: color-mix(in srgb, var(--accent-2) 15%, transparent); color: color-mix(in srgb, var(--accent-2) 70%, var(--text)); }
.state.failed { background: color-mix(in srgb, var(--danger) 15%, transparent); color: color-mix(in srgb, var(--danger) 70%, var(--text)); }
.page-link { display: inline-flex; align-items: center; gap: 4px; margin-left: auto; font-size: 12px; color: var(--accent-hi); }
.lead { margin: 8px 0 10px; font-size: 13px; color: var(--muted); line-height: 1.5; }
.superseded .lead { margin: 4px 0 0; font-size: 12px; }
.lead.bad { color: color-mix(in srgb, var(--danger) 70%, var(--text)); }
.lead .muted { display: block; margin-top: 2px; font-size: 12px; }
.buttons { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 12px; }
@media (max-width: 600px) {
  .buttons button { flex: 1 1 auto; }
  .page-link { margin-left: 0; }
}
</style>
