<script setup lang="ts">
// Statistical models (Admin panel): the groups of models Tabularia ships (regression, survival,
// time series), each with an image the cluster builds from the group's recipe and proves with a
// self-test on reference values before any run uses it. An administrator enables a group (which
// queues its build), disables it, builds it again, sets its resources and limits and says who may
// use it. Kubernetes only: on Compose the section says so. Observers read it all and change
// nothing. Every control is a native button, field or disclosure: Tab, Enter and Space reach all.
// A group with weights (docs/specs/foundation-forecast.md) shows the licence of its weights before any
// build an administrator queues: the clause, the sizes to download and the tick that accepts it.
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { CheckCircle2, CircleDot, Info, LoaderCircle, Power, RotateCcw, ShieldCheck, Sigma, TriangleAlert, XCircle } from 'lucide-vue-next'
import BayesCardFields from '~/components/admin/BayesCardFields.vue'
import { errMessage } from '~/composables/useApi'
import { useFormat } from '~/composables/useFormat'
import type { GroupOut } from '~/composables/useProjects'
import {
  failureSentence, groupBusy, groupFollowed, licenceClause, licenceDraftOf, licenceHref, licenceInputOf, licenceReady, licenceSizeLabel,
  licenceSizesText, limitsDraftOf, limitsInputOf, recheckOf, systemLibraryLabel, useModelsAdmin,
  type LicenceDraft, type LimitsDraft, type ModelGroupAdmin, type ModelGroupAnswer, type ModelsAdminState,
} from '~/composables/useModelsAdmin'

const props = defineProps<{ groups: GroupOut[] }>()
const emit = defineEmits<{ (e: 'count', n: number): void }>()

const { t, te } = useI18n()
const { confirmAction } = useConfirm()
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
// the licence box of the groups with weights: the tick (never kept: every build asks again), the sizes, whether Build again is being asked
const licence = reactive<Record<string, LicenceDraft>>({})

const off = computed(() => state.value?.disabled_reason ?? null)
const editable = computed(() => !!state.value?.editable)
const shown = (g: ModelGroupAdmin): ModelGroupAdmin => detail[g.id] ?? g

// the forms follow the server's numbers until the person changes them
function fillDrafts(g: ModelGroupAdmin, force = false) {
  if (g.licence && !licence[g.id]) licence[g.id] = licenceDraftOf(g.licence)
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

// while a group is being built or checked, or its version in use self-tested again, the page follows it
const anyBusy = computed(() => !!state.value?.groups.some(groupFollowed))
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
  if (what === 'disable' && !(await confirmAction(t('modelsAdmin.confirmDisable', { name: groupName(g.id) }), 'disable'))) return
  // a click that queues a build of a group with weights carries the acceptance the box holds
  const asks = what !== 'disable' && boxShown(g)
  if (asks && !(licence[g.id] && licenceReady(licence[g.id]))) return
  busy[g.id] = what
  try {
    if (what === 'disable') take(await api.disable(g.id), true)
    else take(await api[what](g.id, asks ? licenceInputOf(g.licence!, licence[g.id]) : undefined), true)
    toast.success(t(`modelsAdmin.done.${what}`, { name: groupName(g.id) }))
    if (g.licence) licence[g.id] = licenceDraftOf(g.licence)
    if (what === 'rebuild') focusSoon(`mg-${g.id}-rebuild`)
  } catch (e) {
    toast.error(errMessage(e))
  } finally {
    delete busy[g.id]
  }
}

// ── the licence of the weights ──────────────────────────────────────────────

// the box is shown at every build an administrator queues: Enable when the group has no ready version to serve
// (enabling one that has serves it at once, no build), Build again once asked for
const boxShown = (g: ModelGroupAdmin): boolean => {
  const x = shown(g)
  return editable.value && !!x.licence && (x.enabled ? !!licence[g.id]?.open : !x.ready)
}
const canGo = (g: ModelGroupAdmin): boolean => !boxShown(g) || (!!licence[g.id] && licenceReady(licence[g.id]))
const focusSoon = (id: string) => nextTick(() => document.getElementById(id)?.focus())
function askRebuild(g: ModelGroupAdmin) {
  // a fresh draft from what the server says now: the sizes of the version in use ticked, the box unticked
  const l = shown(g).licence
  if (l) licence[g.id] = licenceDraftOf(l)
  if (!licence[g.id]) return
  licence[g.id].open = true
  focusSoon(`mg-${g.id}-accept`)
}
function cancelRebuild(g: ModelGroupAdmin) {
  if (g.licence) licence[g.id] = licenceDraftOf(g.licence)
  focusSoon(`mg-${g.id}-rebuild`)
}
const clauseOf = (g: ModelGroupAdmin) => licenceClause(shown(g).licence?.text ?? '')
// the line under the card: who accepted the licence the version in use was built under, and when
const lineOf = (g: ModelGroupAdmin) => shown(g).licence?.line ?? null

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
  ({ ready: CheckCircle2, failed: XCircle, building: LoaderCircle, checking: LoaderCircle, rebuild_needed: TriangleAlert, not_enabled: CircleDot,
    rechecking: LoaderCircle, recheck_retrying: TriangleAlert, licence_pending: TriangleAlert })[s]
// a version in use with a new runner (a recheck) shows its version number too: it serves
const serving = (g: ModelGroupAdmin) => ['ready', 'rechecking', 'recheck_retrying'].includes(g.state) && !!g.current_version

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
          <component :is="stateIcon(shown(g).state)" :size="13" :class="{ spin: groupBusy(shown(g)) || shown(g).state === 'rechecking' }" />
          {{ $t(`modelsAdmin.state.${shown(g).state}`) }}
          <template v-if="serving(shown(g))"> · v{{ shown(g).current_version }}</template>
        </span>
      </div>
      <p v-if="groupLead(g.id)" class="hint muted small">{{ groupLead(g.id) }}</p>
      <p class="models muted small">
        {{ $t('modelsAdmin.modelsIn') }}
        <code v-for="m in g.models" :key="m">{{ m }}</code>
        · {{ $t('modelsAdmin.packagesPinned', { n: g.packages }) }}
        <template v-for="lib in g.system_libraries ?? []" :key="lib.package">
          · <span class="system-library">{{ $t('modelsAdmin.systemLibrary', { library: systemLibraryLabel(lib) }) }}</span>
          <!-- the package's whole version (which distribution built it is in it), as secondary text: read by everyone, no tooltip to reach -->
          <span v-if="lib.package_version" class="system-library-package">({{ $t('modelsAdmin.systemLibraryPackage', { package: lib.package, version: lib.package_version }) }})</span>
        </template>
      </p>

      <!-- why it failed, in a sentence; the log is below -->
      <p v-if="shown(g).state === 'failed' && shown(g).versions[0]?.error" class="version-error" role="alert">
        <code>{{ failureSentence(shown(g).versions[0].error) }}</code>
      </p>
      <p v-else-if="shown(g).state === 'building' && shown(g).versions[0]?.status === 'queued'" class="hint muted small">{{ $t('modelsAdmin.queuedHint') }}</p>
      <p v-else-if="shown(g).state === 'rebuild_needed'" class="hint muted small">{{ $t('modelsAdmin.rebuildNeededHint') }}</p>
      <p v-else-if="shown(g).state === 'licence_pending'" class="notice" role="status">
        <TriangleAlert :size="13" /> <span>{{ $t('modelsAdmin.licence.pendingHint') }}</span>
      </p>
      <p v-else-if="shown(g).state === 'checking'" class="hint muted small">{{ $t('modelsAdmin.checkingHint') }}</p>
      <p v-else-if="shown(g).state === 'rechecking'" class="hint muted small">{{ $t('modelsAdmin.recheckingHint') }}</p>
      <!-- a recheck the cluster stopped: the version serves on; when it is tried again, and why -->
      <p v-else-if="shown(g).state === 'recheck_retrying' && recheckOf(shown(g))" class="notice" role="status">
        <TriangleAlert :size="13" />
        <span>{{ $t('modelsAdmin.recheckRetryingHint', {
          why: recheckOf(shown(g))!.recheck_error ?? '',
          at: fmt.dateTime(recheckOf(shown(g))!.recheck_after),
          n: recheckOf(shown(g))!.recheck_attempts ?? 0,
          max: recheckOf(shown(g))!.recheck_max_attempts ?? 0,
        }) }}</span>
      </p>
      <p v-else-if="shown(g).state === 'ready' && recheckOf(shown(g))?.recheck === 'recheck_stopped'" class="notice" role="status">
        <TriangleAlert :size="13" />
        <span>{{ $t('modelsAdmin.recheckStoppedHint', { why: recheckOf(shown(g))!.recheck_error ?? '', n: recheckOf(shown(g))!.recheck_attempts ?? 0 }) }}</span>
      </p>

      <!-- the licence the version in use was built under: from the version's own lines, so it stays when the audit's retention removes the row -->
      <p v-if="lineOf(g)" class="licence-line small" :class="{ earlier: !lineOf(g)!.current }">
        <ShieldCheck :size="13" />
        <span>
          {{ $t('modelsAdmin.licence.line', {
            licence: shown(g).licence!.name,
            by: lineOf(g)!.accepted_by,
            date: fmt.date(lineOf(g)!.accepted_at),
            n: lineOf(g)!.version,
            sizes: licenceSizesText(lineOf(g)!.sizes),
          }) }}
          <a :href="licenceHref(shown(g).licence!.url)" target="_blank" rel="noopener noreferrer">{{ $t('modelsAdmin.licence.readIt') }}</a>
          <template v-if="!lineOf(g)!.current"> {{ $t('modelsAdmin.licence.earlier') }}</template>
        </span>
      </p>
      <p v-if="lineOf(g) && shown(g).licence!.audit_expires_at" class="notice" role="status">
        <TriangleAlert :size="13" />
        <span>{{ $t('modelsAdmin.licence.retention', { date: fmt.date(lineOf(g)!.accepted_at), until: fmt.dateTime(shown(g).licence!.audit_expires_at) }) }}</span>
      </p>

      <!-- before a build an administrator queues: the clause, the sizes, the tick -->
      <ExpandBody><section v-if="boxShown(g) && licence[g.id]" :id="`mg-${g.id}-licence`" class="licence-box" :aria-labelledby="`mg-${g.id}-licence-title`">
        <h5 :id="`mg-${g.id}-licence-title`"><Info :size="14" /> {{ $t('modelsAdmin.licence.title') }}</h5>
        <p class="small">{{ $t('modelsAdmin.licence.intro') }}</p>
        <blockquote v-if="clauseOf(g)" class="clause" lang="en">{{ clauseOf(g) }}</blockquote>
        <p class="small">{{ $t('modelsAdmin.licence.neutral') }}</p>
        <p class="small links">
          <a :href="licenceHref(shown(g).licence!.url)" target="_blank" rel="noopener noreferrer">{{ $t('modelsAdmin.licence.officialText') }}</a>
        </p>
        <details class="shipped">
          <summary>{{ $t('modelsAdmin.licence.shippedText') }}</summary>
          <pre lang="en">{{ shown(g).licence!.text }}</pre>
        </details>
        <fieldset class="field-group sizes">
          <legend>{{ $t('modelsAdmin.licence.sizesLegend') }}</legend>
          <p class="hint muted small">{{ $t('modelsAdmin.licence.sizesHint') }}</p>
          <div class="size-list">
            <label v-for="z in shown(g).licence!.sizes" :key="z.id" class="tick">
              <input v-model="licence[g.id].sizes" type="checkbox" :value="z.id" />
              <span>{{ licenceSizeLabel(z) }}</span>
            </label>
          </div>
          <p v-if="!licence[g.id].sizes.length" class="muted small" role="status">{{ $t('modelsAdmin.licence.sizesNone') }}</p>
        </fieldset>
        <label class="tick accept">
          <input :id="`mg-${g.id}-accept`" v-model="licence[g.id].accept" type="checkbox" :aria-describedby="`mg-${g.id}-needs`" />
          <span>{{ $t('modelsAdmin.licence.accept') }}</span>
        </label>
        <p :id="`mg-${g.id}-needs`" class="muted small">{{ licenceReady(licence[g.id]) ? '' : $t('modelsAdmin.licence.needsTick') }}</p>
      </section></ExpandBody>

      <div v-if="editable" class="actions">
        <button
          v-if="!shown(g).enabled"
          type="button"
          class="primary"
          :disabled="!!busy[g.id] || !canGo(g)"
          :title="canGo(g) ? undefined : $t('modelsAdmin.licence.needsTick')"
          @click="act(g, 'enable')"
        >
          <LoaderCircle v-if="busy[g.id] === 'enable'" :size="13" class="spin" /><Power v-else :size="13" /> {{ $t('modelsAdmin.enable') }}
        </button>
        <template v-else>
          <button type="button" :disabled="!!busy[g.id]" @click="act(g, 'disable')">
            <LoaderCircle v-if="busy[g.id] === 'disable'" :size="13" class="spin" /><Power v-else :size="13" /> {{ $t('modelsAdmin.disable') }}
          </button>
          <!-- a group with weights asks for the licence first: the button opens the box, the box's own button builds -->
          <button
            v-if="shown(g).licence && !licence[g.id]?.open"
            :id="`mg-${g.id}-rebuild`"
            type="button"
            aria-haspopup="true"
            :aria-controls="`mg-${g.id}-licence`"
            aria-expanded="false"
            :disabled="!!busy[g.id] || groupBusy(shown(g))"
            @click="askRebuild(g)"
          >
            <RotateCcw :size="13" /> {{ $t('modelsAdmin.rebuild') }}
          </button>
          <template v-else-if="shown(g).licence">
            <button
              :id="`mg-${g.id}-rebuild-go`"
              type="button"
              class="primary"
              aria-expanded="true"
              :aria-controls="`mg-${g.id}-licence`"
              :disabled="!!busy[g.id] || groupBusy(shown(g)) || !canGo(g)"
              :title="canGo(g) ? undefined : $t('modelsAdmin.licence.needsTick')"
              @click="act(g, 'rebuild')"
            >
              <LoaderCircle v-if="busy[g.id] === 'rebuild'" :size="13" class="spin" /><RotateCcw v-else :size="13" /> {{ $t('modelsAdmin.rebuild') }}
            </button>
            <button type="button" :disabled="!!busy[g.id]" @click="cancelRebuild(g)">{{ $t('modelsAdmin.licence.cancel') }}</button>
          </template>
          <button v-else type="button" :disabled="!!busy[g.id] || groupBusy(shown(g))" @click="act(g, 'rebuild')">
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

        <!-- the group bayes: its compile limits, its cache of binaries, its sampling /tmp and its last compiles (read when the card is opened) -->
        <BayesCardFields v-if="g.id === 'bayes' && opened[g.id]" :editable="editable" />

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
              <span v-if="v.recheck" class="label recheck">{{ $t(`modelsAdmin.recheck.${v.recheck}`) }}</span>
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
.note { margin: 0; display: flex; gap: 6px; align-items: flex-start; font-size: 12.5px; padding: 8px 10px; border-radius: var(--radius-control); background: var(--bg-soft); border: 1px solid var(--border); }
.notice { margin: 0; display: flex; gap: 6px; align-items: flex-start; font-size: 12.5px; color: var(--warning); }
.heading { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.heading h4 { flex: 1; min-width: 0; }
.language { font-size: 12px; color: var(--muted); }
.models { margin: 0; display: flex; flex-wrap: wrap; gap: 4px 6px; align-items: center; }
.models code { font-size: 11.5px; padding: 0 5px; border-radius: 4px; background: var(--bg-soft); }
.version-state { display: inline-flex; align-items: center; gap: 5px; font-size: 12px; }
.version-state.ready svg, .versions li.ready > .version-row svg { color: var(--accent-2); }
.version-state.failed, .version-state.failed svg, .versions li.failed > .version-row svg { color: var(--danger); }
.version-state.rebuild_needed svg, .version-state.recheck_retrying svg, .version-state.licence_pending svg { color: var(--warning); }
.version-state.rechecking svg { color: var(--accent-2); }
.actions { display: flex; gap: 8px; flex-wrap: wrap; }
button.mini { padding: 2px 10px; min-height: 26px; font-size: 12px; display: inline-flex; align-items: center; gap: 5px; }
.actions button:not(.mini) { display: inline-flex; align-items: center; gap: 6px; }

.more > summary { cursor: pointer; font-size: 12.5px; color: var(--muted); padding: 2px 0; }
.more[open] { display: flex; flex-direction: column; gap: 10px; }
.more > summary:focus-visible, details summary:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 4px; }
.field-group { border: 1px solid var(--border-soft); border-radius: var(--radius-control); padding: 10px 12px; margin: 0; display: flex; flex-direction: column; gap: 8px; min-width: 0; }
.field-group legend { font-size: 12px; color: var(--muted); padding: 0 4px; }
.resources { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px 12px; }
.field { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); min-width: 0; }
.field small { font-size: 11.5px; line-height: 1.4; }
.groups { display: flex; flex-wrap: wrap; gap: 6px 16px; padding-left: 22px; }
.tick { display: inline-flex; align-items: center; gap: 6px; font-size: 13px; }

.versions { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; }
.versions li { border: 1px solid var(--border-soft); border-radius: var(--radius-control); padding: 8px 10px; display: flex; flex-direction: column; gap: 6px; }
.version-row { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; font-size: 13px; }
.label { font-size: 11px; padding: 1px 7px; border-radius: 999px; border: 1px solid var(--border); }
.label.current { border-color: color-mix(in srgb, var(--accent-2) 55%, var(--border)); color: var(--accent-2); }
.label.stale { border-color: color-mix(in srgb, var(--warning) 55%, var(--border)); color: var(--warning); }
.label.recheck { border-color: color-mix(in srgb, var(--warning) 55%, var(--border)); color: var(--warning); }
.licence-line { margin: 0; display: flex; gap: 6px; align-items: flex-start; color: var(--muted); line-height: 1.5; }
.licence-line svg { flex: none; margin-top: 2px; color: var(--accent-2); }
.licence-line.earlier svg { color: var(--warning); }
.licence-line a, .licence-box a { color: var(--accent); }
.licence-box { display: flex; flex-direction: column; gap: 8px; padding: 12px 14px; border-radius: var(--radius-control); background: color-mix(in srgb, var(--accent) 7%, var(--panel)); border: 1px solid color-mix(in srgb, var(--accent) 35%, var(--border)); }
.licence-box h5 { margin: 0; display: inline-flex; align-items: center; gap: 6px; font-size: 13px; color: var(--text); }
.licence-box p { margin: 0; line-height: 1.5; }
.clause { margin: 0; padding: 6px 10px; border-left: 3px solid color-mix(in srgb, var(--accent) 55%, var(--border)); font-size: 12.5px; line-height: 1.55; overflow-wrap: anywhere; background: var(--panel); border-radius: 0 6px 6px 0; }
.size-list { display: flex; flex-wrap: wrap; gap: 6px 18px; }
.licence-box .accept { align-items: flex-start; font-size: 13px; line-height: 1.5; }
.licence-box .accept input { margin-top: 3px; flex: none; }
.licence-box input:focus-visible, .actions button:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.version-error { margin: 0; }
.version-error code { display: block; font-size: 11.5px; white-space: pre-wrap; overflow-wrap: anywhere; color: var(--danger); }
details summary { cursor: pointer; font-size: 12px; color: var(--muted); }
details pre { margin: 6px 0 0; max-height: 280px; overflow: auto; font-size: 11.5px; padding: 8px; border-radius: var(--radius-small); background: var(--bg-soft); white-space: pre-wrap; overflow-wrap: anywhere; }
.image { margin: 0; overflow-wrap: anywhere; }

@media (max-width: 720px) {
  .resources { grid-template-columns: 1fr; }
}
</style>
