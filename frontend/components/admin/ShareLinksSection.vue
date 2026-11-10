<script setup lang="ts">
// Administration → Share links (docs/specs/share-links.md): the switch that turns the feature off
// (no link answers, none is deleted), the longest life of a link, the AI comments a link may ask
// for in an hour, and every link of the installation, revocable from here.
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { Link2, LoaderCircle, Save, Trash2 } from 'lucide-vue-next'
import { errMessage } from '~/composables/useApi'
import { useFormat } from '~/composables/useFormat'
import { useShareLinks, type ShareLink, type ShareSettings } from '~/composables/shareLinks'

const props = defineProps<{ canWrite: boolean }>()
const emit = defineEmits<{ (e: 'count', n: number): void }>()

const { t } = useI18n()
const fmt = useFormat()
const toast = useToast()
const api = useShareLinks()

const settings = ref<ShareSettings | null>(null)
const draft = ref({ enabled: true, max_days: 90, ai_comments_per_hour: 20 })
const links = ref<ShareLink[]>([])
const publicBase = ref('')
const busy = ref(false)
const filter = ref<'live' | 'all'>('live')
const revoking = ref<number | null>(null)

async function load() {
  try {
    const got = await api.all()
    settings.value = got.settings
    draft.value = { enabled: got.settings.enabled, max_days: got.settings.max_days, ai_comments_per_hour: got.settings.ai_comments_per_hour }
    links.value = got.links
    publicBase.value = (got as any).public_base_url ?? ''
    emit('count', got.links.filter((l) => l.state === 'active' || l.state === 'waiting').length)
  } catch (e) {
    toast.error(errMessage(e))
  }
}
onMounted(load)

const dirty = computed(() => !!settings.value && (draft.value.enabled !== settings.value.enabled
  || draft.value.max_days !== settings.value.max_days || draft.value.ai_comments_per_hour !== settings.value.ai_comments_per_hour))
const valid = computed(() => draft.value.max_days >= 1 && draft.value.max_days <= 365
  && draft.value.ai_comments_per_hour >= 0 && draft.value.ai_comments_per_hour <= 200)

async function save() {
  if (!dirty.value || !valid.value) return
  busy.value = true
  try {
    settings.value = await api.saveSettings(draft.value)
    toast.success(t('share.adminSaved'))
    await load()
  } catch (e) {
    toast.error(errMessage(e))
  } finally {
    busy.value = false
  }
}

async function revoke(l: ShareLink) {
  try {
    await api.revoke(l.id)
    toast.success(t('share.revoked'))
    revoking.value = null
    await load()
  } catch (e) {
    toast.error(errMessage(e))
  }
}

const shown = computed(() => (filter.value === 'all' ? links.value : links.value.filter((l) => l.state !== 'revoked' && l.state !== 'expired')))
const stateLabel = (l: ShareLink) => (l.suspended ? t(`share.suspended_${l.suspended}`) : t(`share.state_${l.state}`))
const objectHref = (l: ShareLink) => (l.kind === 'report' ? `/report?id=${l.object_id}` : `/dashboard?id=${l.object_id}`)
</script>

<template>
  <section class="sls">
    <header class="sls-head">
      <h2><Link2 :size="16" aria-hidden="true" /> {{ $t('share.adminTitle') }}</h2>
      <p class="muted">{{ $t('share.adminLead') }}</p>
    </header>

    <div v-if="settings" class="settings">
      <label class="switch">
        <input v-model="draft.enabled" type="checkbox" :disabled="!props.canWrite" />
        <span>
          <strong>{{ $t('share.adminEnabled') }}</strong>
          <span class="muted">{{ draft.enabled ? $t('share.adminEnabledOn') : $t('share.adminEnabledOff') }}</span>
        </span>
      </label>
      <label class="num">
        <span class="lbl">{{ $t('share.adminMaxDays') }}</span>
        <input v-model.number="draft.max_days" type="number" min="1" max="365" :disabled="!props.canWrite" />
        <span class="hint">{{ $t('share.adminMaxDaysHint') }}</span>
      </label>
      <label class="num">
        <span class="lbl">{{ $t('share.adminAI') }}</span>
        <input v-model.number="draft.ai_comments_per_hour" type="number" min="0" max="200" :disabled="!props.canWrite" />
        <span class="hint">{{ $t('share.adminAIHint') }}</span>
      </label>
      <p class="hint">{{ publicBase ? $t('share.adminPublicBase', { url: publicBase }) : $t('share.adminPublicBaseNone') }}</p>
      <div v-if="props.canWrite" class="actions">
        <button type="button" class="primary" :disabled="!dirty || !valid || busy" @click="save">
          <LoaderCircle v-if="busy" :size="14" class="spin" /><Save v-else :size="14" /> {{ $t('share.adminSave') }}
        </button>
      </div>
    </div>

    <div class="tablecard">
      <div class="list-head">
        <h3>{{ $t('share.adminLinks', { n: shown.length }) }}</h3>
        <div v-roving class="seg" role="radiogroup" :aria-label="$t('share.adminFilter')">
          <button type="button" class="mini" role="radio" :class="{ on: filter === 'live' }" :aria-checked="filter === 'live'" @click="filter = 'live'">{{ $t('share.adminLive') }}</button>
          <button type="button" class="mini" role="radio" :class="{ on: filter === 'all' }" :aria-checked="filter === 'all'" @click="filter = 'all'">{{ $t('share.adminAll') }}</button>
        </div>
      </div>
      <p v-if="!shown.length" class="muted empty">{{ $t('share.adminNone') }}</p>
      <div v-else class="tablewrap">
        <table class="rows">
          <thead>
            <tr>
              <th>{{ $t('share.adminObject') }}</th>
              <th>{{ $t('share.label') }}</th>
              <th>{{ $t('share.mode') }}</th>
              <th>{{ $t('share.adminCreator') }}</th>
              <th>{{ $t('share.expires') }}</th>
              <th class="n">{{ $t('share.adminOpenings') }}</th>
              <th>{{ $t('share.adminState') }}</th>
              <th v-if="props.canWrite" />
            </tr>
          </thead>
          <tbody>
            <tr v-for="l in shown" :key="l.id">
              <td><NuxtLink :to="objectHref(l)">{{ l.object_name ?? `#${l.object_id}` }}</NuxtLink></td>
              <td>{{ l.label || $t('share.noLabel') }} <span class="mono muted">…{{ l.hint }}</span></td>
              <td>{{ l.embeddable ? $t('share.modeEmbed') : $t('share.modePassword') }}<template v-if="l.embeddable"><br><span class="muted small">{{ l.origins.join(', ') }}</span></template></td>
              <td>{{ l.created_by?.full_name || l.created_by?.email || '—' }}</td>
              <td>{{ fmt.date(l.expires_at) }}</td>
              <td class="n">{{ l.openings }}<template v-if="l.last_opened_at"><br><span class="muted small">{{ fmt.dateTime(l.last_opened_at) }}</span></template></td>
              <td :class="['state', l.state, { off: !!l.suspended }]">{{ stateLabel(l) }}</td>
              <td v-if="props.canWrite" class="act">
                <template v-if="l.state !== 'revoked' && l.state !== 'expired'">
                  <template v-if="revoking === l.id">
                    <button type="button" class="mini danger" @click="revoke(l)">{{ $t('share.revoke') }}</button>
                    <button type="button" class="mini" @click="revoking = null">{{ $t('share.cancel') }}</button>
                  </template>
                  <button v-else type="button" class="mini" :aria-label="$t('share.revoke')" @click="revoking = l.id"><Trash2 :size="12" /> {{ $t('share.revoke') }}</button>
                </template>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </section>
</template>

<style scoped src="~/assets/admintable.css"></style>
<style scoped>
.sls { display: flex; flex-direction: column; gap: 16px; }
.sls-head h2 { margin: 0; display: flex; align-items: center; gap: 8px; font-size: 16px; }
.sls-head p { margin: 4px 0 0; max-width: 72ch; line-height: 1.5; }
.settings { display: flex; flex-direction: column; gap: 14px; padding: 14px 16px; border: 1px solid var(--border-soft); border-radius: 10px; background: var(--panel); max-width: 720px; }
.switch { display: flex; gap: 10px; align-items: flex-start; }
.switch input { margin-top: 3px; }
.switch > span { display: flex; flex-direction: column; gap: 2px; }
.num { display: grid; grid-template-columns: 1fr 110px; gap: 4px 12px; align-items: center; }
.num .hint { grid-column: 1 / -1; }
.lbl { font-size: 13px; }
.hint { font-size: 12px; color: var(--muted); margin: 0; line-height: 1.45; }
.actions { display: flex; justify-content: flex-end; }
.list-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.list-head h3 { margin: 0; font-size: 14px; }
.seg { display: flex; gap: 6px; }
.seg .on { border-color: var(--accent); background: var(--tint-accent); color: var(--accent-hi); }
.empty { margin: 0; }
.n { text-align: right; font-variant-numeric: tabular-nums; }
.small { font-size: 11.5px; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11.5px; }
.state.active { color: var(--accent-2); }
.state.locked, .state.off { color: var(--danger); }
.state.revoked, .state.expired { color: var(--muted); }
.act { white-space: nowrap; text-align: right; }
.muted { color: var(--muted); }
.spin { animation: spin 1s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .spin { animation: none; } }
</style>
