<script setup lang="ts">
// A plan of the agent (docs/specs/mcp-agent.md), for its owner to review and confirm in
// Tabularia: what an agent proposes — over MCP, or in the chat — in the user's language,
// with the differences of a changed flow and the first rows it would write. Nothing has
// changed until «Confirm»; the gateway applies the actions in order, checking the
// permissions again, and stops at the first error. Afterwards the page links what was made.
//
// /agent-plan?id=N
import { computed, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { Bot, LoaderCircle } from 'lucide-vue-next'
import { errMessage } from '~/composables/useApi'
import { useFormat } from '~/composables/useFormat'
import { useToast } from '~/composables/useToast'
import { useAgentPlans, type AgentPlan } from '~/composables/useAgentPlans'
// explicit import: in the container new components are not scanned
import AgentPlanActions from '~/components/agent/AgentPlanActions.vue'

const { t } = useI18n()
const route = useRoute()
const api = useAgentPlans()
const fmt = useFormat()
const toast = useToast()

const planId = computed(() => Number(route.query.id) || null)
const plan = ref<AgentPlan | null>(null)
const loading = ref(true)
const loadError = ref('')
const busy = ref<'confirm' | 'cancel' | null>(null)
// read out by screen readers when the plan's state changes
const announcement = ref('')

useHead({ title: computed(() => (planId.value ? t('agentPlan.title', { id: planId.value }) : 'Tabularia')) })

async function load() {
  const id = planId.value
  loadError.value = ''
  if (id == null) {
    loading.value = false
    loadError.value = t('agentPlan.noId')
    return
  }
  loading.value = true
  try {
    plan.value = await api.get(id)
  } catch (e: any) {
    plan.value = null
    loadError.value = (e?.response?.status ?? e?.statusCode) === 404 ? t('agentPlan.notFound') : errMessage(e)
  } finally {
    loading.value = false
  }
}

onMounted(load)
watch(planId, load)

const statusLabel = computed(() => (plan.value ? t(`agentPlan.status.${plan.value.status}`) : ''))
const statusHelp = computed(() => {
  const s = plan.value?.status
  return s && s !== 'pending' && s !== 'applying' ? t(`agentPlan.statusHelp.${s}`) : ''
})
const origin = computed(() => {
  const p = plan.value
  if (!p) return ''
  if (p.origin === 'chat') return t('agentPlan.fromChat')
  return p.token_name ? t('agentPlan.fromMcp', { token: p.token_name }) : t('agentPlan.fromMcpNoToken')
})

async function act(kind: 'confirm' | 'cancel') {
  const id = planId.value
  const shown = plan.value
  if (id == null || !shown || busy.value) return
  busy.value = kind
  try {
    // confirmed as shown: a plan the agent changed meanwhile is refused, and loaded again below
    plan.value = kind === 'confirm' ? await api.confirm(shown) : await api.cancel(id)
    const s = plan.value.status
    if (s === 'applied') toast.success(t('agentPlan.confirmedToast'))
    else if (s === 'failed') toast.error(t('agentPlan.failedToast'))
    else if (s === 'cancelled') toast.info(t('agentPlan.cancelledToast'))
    announcement.value = `${statusLabel.value}. ${statusHelp.value}`
  } catch (e) {
    toast.error(errMessage(e))
    await load()
  } finally {
    busy.value = null
  }
}
</script>

<template>
  <AppShell>
    <div class="plan-page">
      <div class="page-head">
        <h1><Bot :size="18" aria-hidden="true" /> {{ planId ? $t('agentPlan.title', { id: planId }) : $t('agentPlan.titleBare') }}</h1>
        <span v-if="plan" class="state" :class="plan.status">{{ statusLabel }}</span>
      </div>
      <p class="sr-only" role="status" aria-live="polite">{{ announcement }}</p>

      <SkeletonRows v-if="loading && !plan" :rows="4" />
      <p v-else-if="loadError" class="err" role="alert">{{ loadError }}</p>

      <template v-else-if="plan">
        <p class="origin">{{ origin }}</p>
        <p v-if="plan.status === 'pending'" class="lead">
          {{ $t('agentPlan.nothingYet') }}
          <span class="muted">{{ $t('agentPlan.expiresAt', { time: fmt.dateTime(plan.expires_at) }) }}</span>
        </p>
        <p v-else-if="statusHelp" class="lead" :class="{ bad: plan.status === 'failed' }">{{ statusHelp }}</p>

        <h2 class="section">{{ $t('agentPlan.actionsTitle') }}</h2>
        <AgentPlanActions :plan="plan" />

        <div v-if="plan.can_confirm" class="buttons">
          <button class="primary" :disabled="!!busy" :aria-busy="busy === 'confirm'" @click="act('confirm')">
            <LoaderCircle v-if="busy === 'confirm'" :size="14" class="spin" aria-hidden="true" />
            {{ busy === 'confirm' ? $t('agentPlan.confirming') : $t('agentPlan.confirm') }}
          </button>
          <button :disabled="!!busy" :aria-busy="busy === 'cancel'" @click="act('cancel')">
            {{ busy === 'cancel' ? $t('agentPlan.cancelling') : $t('agentPlan.cancel') }}
          </button>
        </div>
      </template>
    </div>
  </AppShell>
</template>

<style scoped>
.plan-page { max-width: 880px; min-width: 0; }
.page-head { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.page-head h1 { display: inline-flex; align-items: center; gap: 8px; font-size: 1.5em; margin-block: 0.83em; }
.state { font-size: 12px; font-weight: 600; padding: 3px 9px; border-radius: 20px; background: var(--panel-2); color: var(--muted); border: 1px solid var(--border-soft); }
.state.pending, .state.applying { background: var(--tint-accent); color: color-mix(in srgb, var(--accent) 70%, var(--text)); }
.state.applied { background: color-mix(in srgb, var(--accent-2) 15%, transparent); color: color-mix(in srgb, var(--accent-2) 70%, var(--text)); }
.state.failed { background: color-mix(in srgb, var(--danger) 15%, transparent); color: color-mix(in srgb, var(--danger) 70%, var(--text)); }
.origin { margin: 0 0 6px; color: var(--text); }
.lead { margin: 0 0 4px; color: var(--muted); line-height: 1.5; }
.lead.bad { color: color-mix(in srgb, var(--danger) 70%, var(--text)); }
.lead .muted { display: block; margin-top: 2px; font-size: 13px; }
.err { color: color-mix(in srgb, var(--danger) 70%, var(--text)); }
.section { font-size: 15px; margin: 22px 0 10px; }

.buttons { display: flex; gap: 10px; flex-wrap: wrap; margin: 18px 0 8px; }

@media (max-width: 600px) {
  .buttons button { flex: 1 1 auto; }
}
</style>
