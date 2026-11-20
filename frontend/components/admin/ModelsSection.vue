<script setup lang="ts">
// Statistical models (Admin panel): the groups of models Tabularia ships (regression, survival,
// time series), each with an image the cluster builds from the group's recipe and proves with a
// self-test on reference values before any run uses it. An administrator enables a group (which
// queues its build), disables it, builds it again, sets its resources and limits and says who may
// use it. Kubernetes only: on Compose the section says so. Observers read it all and change
// nothing. Every control is a native button, field or disclosure: Tab, Enter and Space reach all.
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { CheckCircle2, CircleDot, LoaderCircle, Power, RotateCcw, Sigma, TriangleAlert, XCircle } from 'lucide-vue-next'
import { errMessage } from '~/composables/useApi'
import { useFormat } from '~/composables/useFormat'
import type { GroupOut } from '~/composables/useProjects'
import {
  failureSentence, groupBusy, limitsDraftOf, limitsInputOf, useModelsAdmin,
  type LimitsDraft, type ModelGroupAdmin, type ModelGroupAnswer, type ModelsAdminState,
} from '~/composables/useModelsAdmin'

const props = defineProps<{ groups: GroupOut[] }>()
const emit = defineEmits<{ (e: 'count', n: number): void }>()

const { t, te } = useI18n()
const fmt = useFormat()
const api = useModelsAdmin()
const toast = useToast()

const state = ref<ModelsAdminState | null>(null)
const loadError = ref('')
// the groups seen with their logs (opened cards): the list leaves the logs out
const detail = reactive<Record<string, ModelGroupAdmin>>({})
const opened = reactive<Record<string, boolean>>({})
const limits = reactive<Record<string, LimitsDraft>>({})
const limitsBase = reactive<Record<string, string>>({})
const access = reactive<Record<string, { everyone: boolean; group_ids: number[] }>>({})
const accessBase = reactive<Record<string, string>>({})
const busy = reactive<Record<string, string>>({}) // the action in progress, by group

const off = computed(() => state.value?.disabled_reason ?? null)
const editable = computed(() => !!state.value?.editable)
const shown = (g: ModelGroupAdmin): ModelGroupAdmin => detail[g.id] ?? g

// the forms follow the server's numbers until the person changes them
function fillDrafts(g: ModelGroupAdmin, force = false) {
  const l = JSON.stringify(limitsDraftOf(g))
  if (force || !limits[g.id] || JSON.stringify(limits[g.id]) === limitsBase[g.id]) {
    limits[g.id] = limitsDraftOf(g)
    limitsBase[g.id] = l
  }
  const a = JSON.stringify({ everyone: g.everyone, group_ids: [...g.group_ids].sort((x, y) => x - y) })
  if (force || !access[g.id] || JSON.stringify(sorted(access[g.id])) === accessBase[g.id]) {
    access[g.id] = { everyone: g.everyone, group_ids: [...g.group_ids] }
    accessBase[g.id] = a
  }
}
const sorted = (a: { everyone: boolean; group_ids: number[] }) => JSON.stringify({ everyone: a.everyone, group_ids: [...a.group_ids].sort((x, y) => x - y) })

function take(answer: ModelGroupAnswer, force = false) {
  const g = answer.group
  if (state.value) {
    state.value.groups = state.value.groups.map((x) => (x.id === g.id ? { ...g, versions: g.versions.map(({ lock, build_log, selftest, ...v }) => v) } : x))
    state.value.editable = answer.editable
    state.value.disabled_reason = answer.disabled_reason
    state.value.storage_ready = answer.storage_ready
  }
  if (opened[g.id] || detail[g.id]) detail[g.id] = g
  fillDrafts(g, force)
  emit('count', enabledCount())
}

const enabledCount = () => (state.value?.groups ?? []).filter((g) => g.enabled).length

async function load() {
  try {
    state.value = await api.get()
    loadError.value = ''
    for (const g of state.value.groups) fillDrafts(g)
    emit('count', enabledCount())
    // the cards open keep their logs fresh
    await Promise.all(state.value.groups.filter((g) => opened[g.id]).map(refreshDetail))
  } catch (e) {
    loadError.value = errMessage(e)
  }
}

async function refreshDetail(g: ModelGroupAdmin) {
  try {
    detail[g.id] = (await api.group(g.id)).group
  } catch { /* the next round */ }
}

onMounted(load)

// while a group is being built or checked, the page follows it
const anyBusy = computed(() => !!state.value?.groups.some(groupBusy))
let timer: ReturnType<typeof setInterval> | undefined
watch(anyBusy, (on) => {
  clearInterval(timer)
  if (on) timer = setInterval(load, 5000)
}, { immediate: true })
onBeforeUnmount(() => clearInterval(timer))

function onToggle(g: ModelGroupAdmin, ev: Event) {
  const isOpen = (ev.target as HTMLDetailsElement).open
  opened[g.id] = isOpen
  if (isOpen) refreshDetail(g)
}

async function act(g: ModelGroupAdmin, what: 'enable' | 'disable' | 'rebuild') {
  if (busy[g.id]) return
  if (what === 'disable' && !confirm(t('modelsAdmin.confirmDisable', { name: groupName(g.id) }))) return
  busy[g.id] = what
  try {
    take(await api[what](g.id), true)
    toast.success(t(`modelsAdmin.done.${what}`, { name: groupName(g.id) }))
  } catch (e) {
    toast.error(errMessage(e))
  } finally {
    delete busy[g.id]
  }
}

async function saveLimits(g: ModelGroupAdmin) {
  if (busy[g.id]) return
  busy[g.id] = 'limits'
  try {
    take(await api.saveLimits(g.id, limitsInputOf(limits[g.id])), true)
    toast.success(t('modelsAdmin.limitsSaved'))
  } catch (e) {
    toast.error(errMessage(e))
  } finally {
    delete busy[g.id]
  }
}

async function saveAccess(g: ModelGroupAdmin) {
  if (busy[g.id]) return
  busy[g.id] = 'access'
  try {
    take(await api.saveAccess(g.id, { everyone: access[g.id].everyone, group_ids: access[g.id].everyone ? [] : access[g.id].group_ids }), true)
    toast.success(t('modelsAdmin.accessSaved'))
  } catch (e) {
    toast.error(errMessage(e))
  } finally {
    delete busy[g.id]
  }
}

// the resources and the access belong to the group's environment, made when it is enabled for the first time
const canTune = (g: ModelGroupAdmin) => editable.value && shown(g).versions.length > 0
const limitsDirty = (g: ModelGroupAdmin) => !!limits[g.id] && JSON.stringify(limits[g.id]) !== limitsBase[g.id]
const accessDirty = (g: ModelGroupAdmin) => !!access[g.id] && sorted(access[g.id]) !== accessBase[g.id]

// the words of a group: its own, or its id for a group a later version ships
function groupName(id: string): string {
  return te(`modelsAdmin.group.${id}.name`) ? t(`modelsAdmin.group.${id}.name`) : id
}
function groupLead(id: string): string {
  return te(`modelsAdmin.group.${id}.description`) ? t(`modelsAdmin.group.${id}.description`) : ''
}
const languageLabel = (g: ModelGroupAdmin) => `${g.language === 'r' ? 'R' : 'Python'} ${g.base}`

const stateIcon = (s: ModelGroupAdmin['state']) =>
  ({ ready: CheckCircle2, failed: XCircle, building: LoaderCircle, checking: LoaderCircle, rebuild_needed: TriangleAlert, not_enabled: CircleDot })[s]

const duration = (v: { started_at: string | null; finished_at: string | null }) => {
  if (!v.started_at || !v.finished_at) return ''
  const s = Math.round((Date.parse(v.finished_at) - Date.parse(v.started_at)) / 1000)
  return s >= 60 ? `${Math.floor(s / 60)} min ${s % 60} s` : `${s} s`
}

// the digest of an image, short: the full reference is in the title
const shortRef = (ref: string | null) => (ref ? ref.replace(/^(.*@sha256:)([0-9a-f]{12}).*$/, '$1$2…') : '')
</script>

<template>
  <p v-if="loadError" class="page-error" role="alert">{{ loadError }}</p>

  <template v-else-if="state">
    <div class="card intro">
      <h4><Sigma :size="14" /> {{ $t('modelsAdmin.title') }}</h4>
      <p class="hint muted small">{{ $t('modelsAdmin.lead') }}</p>
      <p v-if="off === 'kubernetes'" class="notice off-note" role="status">
        <TriangleAlert :size="13" /> {{ $t('modelsAdmin.offKubernetes') }}
      </p>
      <p v-else-if="off === 'not_configured'" class="notice off-note" role="status">
        <TriangleAlert :size="13" /> {{ $t('modelsAdmin.offConfiguration') }}
      </p>
      <p v-else-if="!state.storage_ready" class="notice" role="status">
        <TriangleAlert :size="13" /> {{ $t('modelsAdmin.noBucket') }}
      </p>
    </div>

    <article
      v-for="g in state.groups"
      :key="g.id"
      class="card group"
      :class="[shown(g).state, { dim: !!off }]"
      :aria-labelledby="`mg-${g.id}-name`"
    >
      <div class="heading">
        <h4 :id="`mg-${g.id}-name`"><Sigma :size="14" /> {{ groupName(g.id) }}</h4>
        <span class="language">{{ languageLabel(g) }}</span>
        <span class="version-state" :class="shown(g).state" role="status">
          <component :is="stateIcon(shown(g).state)" :size="13" :class="{ spin: groupBusy(shown(g)) }" />
          {{ $t(`modelsAdmin.state.${shown(g).state}`) }}
          <template v-if="shown(g).state === 'ready' && shown(g).current_version"> · v{{ shown(g).current_version }}</template>
        </span>
      </div>
      <p v-if="groupLead(g.id)" class="hint muted small">{{ groupLead(g.id) }}</p>
      <p class="models muted small">
        {{ $t('modelsAdmin.modelsIn') }}
        <code v-for="m in g.models" :key="m">{{ m }}</code>
        · {{ $t('modelsAdmin.packagesPinned', { n: g.packages }) }}
      </p>

      <!-- why it failed, in a sentence; the log is below -->
      <p v-if="shown(g).state === 'failed' && shown(g).versions[0]?.error" class="version-error" role="alert">
        <code>{{ failureSentence(shown(g).versions[0].error) }}</code>
      </p>
      <p v-else-if="shown(g).state === 'rebuild_needed'" class="hint muted small">{{ $t('modelsAdmin.rebuildNeededHint') }}</p>
      <p v-else-if="shown(g).state === 'checking'" class="hint muted small">{{ $t('modelsAdmin.checkingHint') }}</p>

      <div v-if="editable" class="actions">
        <button
          v-if="!shown(g).enabled"
          type="button"
          class="primary"
          :disabled="!!busy[g.id]"
          @click="act(g, 'enable')"
        >
          <LoaderCircle v-if="busy[g.id] === 'enable'" :size="13" class="spin" /><Power v-else :size="13" /> {{ $t('modelsAdmin.enable') }}
        </button>
        <template v-else>
          <button type="button" :disabled="!!busy[g.id]" @click="act(g, 'disable')">
            <LoaderCircle v-if="busy[g.id] === 'disable'" :size="13" class="spin" /><Power v-else :size="13" /> {{ $t('modelsAdmin.disable') }}
          </button>
          <button type="button" :disabled="!!busy[g.id] || groupBusy(shown(g))" @click="act(g, 'rebuild')">
            <LoaderCircle v-if="busy[g.id] === 'rebuild'" :size="13" class="spin" /><RotateCcw v-else :size="13" /> {{ $t('modelsAdmin.rebuild') }}
          </button>
        </template>
      </div>

      <!-- what an administrator sets, and the versions with their logs -->
      <details class="more" :open="opened[g.id]" @toggle="onToggle(g, $event)">
        <summary>{{ $t('modelsAdmin.details') }}</summary>

        <template v-if="limits[g.id] && access[g.id]">
          <p v-if="editable && !shown(g).versions.length" class="hint muted small">{{ $t('modelsAdmin.enableFirst') }}</p>
          <fieldset class="field-group" :disabled="!canTune(g)">
            <legend>{{ $t('modelsAdmin.resources') }}</legend>
            <p class="hint muted small">{{ $t('modelsAdmin.resourcesHint') }}</p>
            <div class="resources">
              <label class="field">
                <span>{{ $t('modelsAdmin.cpuLimit') }}</span>
                <input v-model="limits[g.id].cpu_limit" type="text" spellcheck="false" />
                <small>{{ $t('modelsAdmin.recipeDefault', { value: g.defaults.cpu_limit }) }}</small>
              </label>
              <label class="field">
                <span>{{ $t('modelsAdmin.memoryLimit') }}</span>
                <input v-model="limits[g.id].memory_limit" type="text" spellcheck="false" />
                <small>{{ $t('modelsAdmin.recipeDefault', { value: g.defaults.memory_limit }) }}</small>
              </label>
              <label class="field">
                <span>{{ $t('modelsAdmin.maxTimeout') }}</span>
                <input v-model.number="limits[g.id].minutes" type="number" min="1" max="1440" />
                <small>{{ $t('modelsAdmin.recipeDefault', { value: Math.round(g.defaults.max_timeout_seconds / 60) }) }}</small>
              </label>
              <label class="field">
                <span>{{ $t('modelsAdmin.maxRows') }}</span>
                <input
                  v-model="limits[g.id].max_rows"
                  type="number"
                  min="1"
                  inputmode="numeric"
                  :placeholder="String(g.defaults.max_rows)"
                 
                />
                <small>{{ $t('modelsAdmin.maxRowsHint', { value: g.defaults.max_rows.toLocaleString() }) }}</small>
              </label>
            </div>
            <div v-if="canTune(g)" class="actions">
              <button type="button" class="primary mini" :disabled="!!busy[g.id] || !limitsDirty(g)" @click="saveLimits(g)">
                <LoaderCircle v-if="busy[g.id] === 'limits'" :size="13" class="spin" /> {{ $t('modelsAdmin.save') }}
              </button>
            </div>
          </fieldset>

          <fieldset class="field-group" :disabled="!canTune(g)">
            <legend>{{ $t('modelsAdmin.who') }}</legend>
            <label class="tick">
              <input v-model="access[g.id].everyone" type="radio" :name="`mg-${g.id}-who`" :value="true"  />
              <span>{{ $t('modelsAdmin.whoEveryone') }}</span>
            </label>
            <label class="tick">
              <input v-model="access[g.id].everyone" type="radio" :name="`mg-${g.id}-who`" :value="false"  />
              <span>{{ $t('modelsAdmin.whoListed') }}</span>
            </label>
            <div v-if="!access[g.id].everyone" class="groups">
              <label v-for="ug in props.groups" :key="ug.id" class="tick">
                <input v-model="access[g.id].group_ids" type="checkbox" :value="ug.id"  />
                <span>{{ ug.name }}</span>
              </label>
              <span v-if="!props.groups.length" class="muted small">{{ $t('modelsAdmin.noUserGroups') }}</span>
              <span v-else-if="!access[g.id].group_ids.length" class="muted small">{{ $t('modelsAdmin.adminsOnly') }}</span>
            </div>
            <div v-if="canTune(g)" class="actions">
              <button type="button" class="primary mini" :disabled="!!busy[g.id] || !accessDirty(g)" @click="saveAccess(g)">
                <LoaderCircle v-if="busy[g.id] === 'access'" :size="13" class="spin" /> {{ $t('modelsAdmin.save') }}
              </button>
            </div>
          </fieldset>
        </template>

        <h5>{{ $t('modelsAdmin.versionsTitle') }}</h5>
        <p v-if="!shown(g).versions.length" class="hint muted small">{{ $t('modelsAdmin.noVersions') }}</p>
        <ul v-else class="versions">
          <li v-for="v in shown(g).versions" :key="v.number" :class="v.status">
            <div class="version-row">
              <component :is="v.status === 'ready' ? CheckCircle2 : v.status === 'failed' ? XCircle : LoaderCircle" :size="14" :class="{ spin: v.status !== 'ready' && v.status !== 'failed' }" />
              <strong>v{{ v.number }}</strong>
              <span class="label">{{ $t(`modelsAdmin.status.${v.status}`) }}</span>
              <span v-if="v.current" class="label current">{{ $t('modelsAdmin.current') }}</span>
              <span v-if="v.stale" class="label stale">{{ $t('modelsAdmin.stale') }}</span>
              <span class="muted small">{{ v.created_by }} · {{ fmt.ago(v.created_at) }}<template v-if="duration(v)"> · {{ duration(v) }}</template></span>
            </div>
            <p v-if="v.error" class="version-error"><code>{{ v.error }}</code></p>
            <p v-if="v.image_ref" class="image muted small"><code :title="v.image_ref">{{ shortRef(v.image_ref) }}</code></p>
            <details v-if="v.selftest">
              <summary>{{ $t('modelsAdmin.selftest') }}</summary>
              <pre>{{ v.selftest }}</pre>
            </details>
            <details v-if="v.lock">
              <summary>{{ $t('modelsAdmin.installed', { n: v.lock.split('\n').length }) }}</summary>
              <pre>{{ v.lock }}</pre>
            </details>
            <details v-if="v.build_log" :open="v.status === 'failed' && v.number === shown(g).versions[0].number">
              <summary>{{ $t('modelsAdmin.buildLog') }}</summary>
              <pre>{{ v.build_log }}</pre>
            </details>
          </li>
        </ul>
      </details>
    </article>
  </template>

  <p v-if="state && !editable && !off" class="note">{{ $t('modelsAdmin.readOnly') }}</p>
</template>

<style scoped>
.card { display: flex; flex-direction: column; gap: 10px; padding: 16px; background: var(--panel); border: 1px solid var(--border); border-radius: var(--radius); }
.card h4 { margin: 0; display: inline-flex; align-items: center; gap: 7px; }
.card h5 { margin: 6px 0 0; font-size: 12.5px; color: var(--muted); font-weight: 600; }
.card.dim { opacity: 0.7; }
.hint { margin: 0; line-height: 1.5; }
.small { font-size: 12px; }
.page-error { color: var(--danger); font-size: 13px; margin: 0; }
.note { margin: 0; display: flex; gap: 6px; align-items: flex-start; font-size: 12.5px; padding: 8px 10px; border-radius: 8px; background: var(--bg-soft); border: 1px solid var(--border); }
.notice { margin: 0; display: flex; gap: 6px; align-items: flex-start; font-size: 12.5px; color: var(--warning); }
.heading { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.heading h4 { flex: 1; min-width: 0; }
.language { font-size: 12px; color: var(--muted); }
.models { margin: 0; display: flex; flex-wrap: wrap; gap: 4px 6px; align-items: center; }
.models code { font-size: 11.5px; padding: 0 5px; border-radius: 4px; background: var(--bg-soft); }
.version-state { display: inline-flex; align-items: center; gap: 5px; font-size: 12px; }
.version-state.ready svg, .versions li.ready > .version-row svg { color: var(--accent-2); }
.version-state.failed, .version-state.failed svg, .versions li.failed > .version-row svg { color: var(--danger); }
.version-state.rebuild_needed svg { color: var(--warning); }
.actions { display: flex; gap: 8px; flex-wrap: wrap; }
button.mini { padding: 2px 10px; min-height: 26px; font-size: 12px; display: inline-flex; align-items: center; gap: 5px; }
.actions button:not(.mini) { display: inline-flex; align-items: center; gap: 6px; }

.more > summary { cursor: pointer; font-size: 12.5px; color: var(--muted); padding: 2px 0; }
.more[open] { display: flex; flex-direction: column; gap: 10px; }
.more > summary:focus-visible, details summary:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 4px; }
.field-group { border: 1px solid var(--border-soft); border-radius: 8px; padding: 10px 12px; margin: 0; display: flex; flex-direction: column; gap: 8px; min-width: 0; }
.field-group legend { font-size: 12px; color: var(--muted); padding: 0 4px; }
.resources { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px 12px; }
.field { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); min-width: 0; }
.field small { font-size: 11.5px; line-height: 1.4; }
.groups { display: flex; flex-wrap: wrap; gap: 6px 16px; padding-left: 22px; }
.tick { display: inline-flex; align-items: center; gap: 6px; font-size: 13px; }

.versions { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; }
.versions li { border: 1px solid var(--border-soft); border-radius: 8px; padding: 8px 10px; display: flex; flex-direction: column; gap: 6px; }
.version-row { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; font-size: 13px; }
.label { font-size: 11px; padding: 1px 7px; border-radius: 999px; border: 1px solid var(--border); }
.label.current { border-color: color-mix(in srgb, var(--accent-2) 55%, var(--border)); color: var(--accent-2); }
.label.stale { border-color: color-mix(in srgb, var(--warning) 55%, var(--border)); color: var(--warning); }
.version-error { margin: 0; }
.version-error code { display: block; font-size: 11.5px; white-space: pre-wrap; overflow-wrap: anywhere; color: var(--danger); }
details summary { cursor: pointer; font-size: 12px; color: var(--muted); }
details pre { margin: 6px 0 0; max-height: 280px; overflow: auto; font-size: 11.5px; padding: 8px; border-radius: 6px; background: var(--bg-soft); white-space: pre-wrap; overflow-wrap: anywhere; }
.image { margin: 0; overflow-wrap: anywhere; }

@media (max-width: 720px) {
  .resources { grid-template-columns: 1fr; }
}
</style>
