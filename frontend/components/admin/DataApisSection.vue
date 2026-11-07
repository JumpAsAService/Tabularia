<script setup lang="ts">
// Administration → Data APIs (docs/specs/data-apis.md, «Administration»): every data API of the
// installation with its creator, its state, its runtime, its pods and its traffic of the last 24
// hours; an administrator sets the runtime of any of them (only an administrator does: replicas
// or autoscaling, CPU and memory per pod), pauses or deletes any of them, whoever made it (the
// delete even with the feature switched off, to clean what is left). The caps are the chart's
// (dataApi.*), shown here; an observer reads it all and changes nothing.
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { Braces, Cpu, LoaderCircle, Pause, RefreshCw, Save, Trash2 } from 'lucide-vue-next'
import { errMessage } from '~/composables/useApi'
import { useFormat } from '~/composables/useFormat'
import { useFolderPaths } from '~/composables/useFolderPaths'
import {
  dataApiOrigin, runtimePods, runtimeProblem, tidyRuntime, useDataApis, type DataApiAdminList, type DataApiAdminRow, type DataApiLimits,
  type DataApiRuntime,
} from '~/composables/useDataApis'
import Modal from '~/components/ui/Modal.vue'
import DataApiRuntimeForm from '~/components/dataapi/DataApiRuntimeForm.vue'

const props = defineProps<{ canWrite: boolean }>()
const emit = defineEmits<{ (e: 'count', n: number): void }>()

const { t } = useI18n()
const fmt = useFormat()
const toast = useToast()
const api = useDataApis()
const folderPaths = useFolderPaths()

const list = ref<DataApiAdminList | null>(null)
const limits = ref<DataApiLimits | null>(null)
const failed = ref('')
const loading = ref(false)
const busy = ref<number | null>(null)
const asking = ref<{ id: number; what: 'pause' | 'delete' } | null>(null)
const filter = ref<'all' | 'active'>('all')

async function load() {
  loading.value = true
  try {
    const [got, lim] = await Promise.all([api.adminList(), api.limits().catch(() => null)])
    list.value = got
    limits.value = lim
    failed.value = ''
    emit('count', got.apis.filter((a) => a.state === 'active').length)
  } catch (e) {
    failed.value = errMessage(e)
  } finally {
    loading.value = false
  }
}
onMounted(() => { folderPaths.load(); load() })

const rows = computed(() => (list.value?.apis ?? []).filter((a) => filter.value === 'all' || a.state === 'active'))
const pods = computed(() => (list.value?.apis ?? []).filter((a) => a.state === 'active').reduce((n, a) => n + a.wanted, 0))
const origin = computed(() => (list.value ? dataApiOrigin(list.value.base_url, list.value.host, location) : ''))

function podsOf(a: DataApiAdminRow): string {
  if (a.state !== 'active') return '—'
  if (!a.checked_at) return t('dataApiAdmin.podsWaiting')
  return t('dataApiAdmin.podsReady', { ready: a.ready, wanted: a.wanted })
}
const tone = (a: DataApiAdminRow) => (a.state !== 'active' ? '' : a.error ? 'bad' : a.checked_at && a.ready >= a.wanted && a.wanted > 0 ? 'good' : 'wait')

/** The runtime in a few characters: «2 × 500m · 1Gi», «1–3 × 250m · 256Mi». */
function runtimeOf(a: DataApiAdminRow): string {
  const rt = a.runtime
  const pods = rt.autoscale ? `${rt.min_replicas}–${rt.max_replicas}` : String(rt.replicas)
  return `${pods} × ${rt.cpu} · ${rt.memory}`
}

// the runtime of one data API, in a dialog
const editing = ref<{ row: DataApiAdminRow; rt: DataApiRuntime } | null>(null)
const savingRuntime = ref(false)
const runtimeIssue = computed(() => {
  const e = editing.value
  const p = e && list.value ? runtimeProblem(e.rt, list.value.runtime_limits) : null
  return p ? t(`dataApi.${p.key}`, p.values ?? {}) : ''
})
/** The pods the other active data APIs take, when this one is active: what is left of the total. */
const othersPods = computed(() => {
  const e = editing.value
  if (!e || !list.value) return 0
  return list.value.apis.filter((a) => a.state === 'active' && a.id !== e.row.id).reduce((n, a) => n + runtimePods(a.runtime), 0)
})
function openRuntime(a: DataApiAdminRow) {
  editing.value = { row: a, rt: { ...a.runtime } }
}
async function saveRuntime() {
  const e = editing.value
  if (!e || savingRuntime.value || runtimeIssue.value) return
  savingRuntime.value = true
  try {
    await api.adminSetRuntime(e.row.id, tidyRuntime(e.rt))
    toast.success(t(e.row.state === 'active' ? 'dataApiAdmin.runtimeSavedActive' : 'dataApiAdmin.runtimeSaved', { name: e.row.name }))
    editing.value = null
    await load()
  } catch (err) {
    toast.error(errMessage(err))
  } finally {
    savingRuntime.value = false
  }
}

async function act(a: DataApiAdminRow, what: 'pause' | 'delete') {
  busy.value = a.id
  try {
    if (what === 'pause') await api.adminPause(a.id)
    else await api.adminRemove(a.id)
    toast.success(t(what === 'pause' ? 'dataApiAdmin.paused' : 'dataApiAdmin.deleted', { name: a.name }))
    asking.value = null
    await load()
  } catch (e) {
    toast.error(errMessage(e))
  } finally {
    busy.value = null
  }
}
</script>

<template>
  <section class="das">
    <header class="das-head">
      <h2><Braces :size="16" aria-hidden="true" /> {{ $t('dataApiAdmin.title') }}</h2>
      <p class="muted">{{ $t('dataApiAdmin.lead') }}</p>
    </header>
    <p v-if="failed" class="err" role="alert">{{ failed }}</p>
    <template v-if="list">
      <p v-if="!list.enabled" class="note" role="status">
        {{ $t(list.reason === 'kubernetes' ? 'dataApiAdmin.offKubernetes' : 'dataApiAdmin.offDisabled') }}
      </p>
      <dl class="caps">
        <div><dt>{{ $t('dataApiAdmin.capApis') }}</dt><dd>{{ $t('dataApiAdmin.ofMost', { n: list.apis.length, max: list.max_apis }) }}</dd></div>
        <div><dt>{{ $t('dataApiAdmin.capPods') }}</dt><dd>{{ $t('dataApiAdmin.ofMost', { n: pods, max: list.max_total_replicas }) }}</dd></div>
        <div v-if="list.runtime_limits"><dt>{{ $t('dataApiAdmin.capPod') }}</dt>
          <dd>{{ $t('dataApiAdmin.capPodValue', { replicas: list.runtime_limits.max_replicas, minCpu: list.runtime_limits.min_cpu,
                                                  maxCpu: list.runtime_limits.max_cpu, minMemory: list.runtime_limits.min_memory,
                                                  maxMemory: list.runtime_limits.max_memory }) }}</dd></div>
        <div v-if="limits?.enabled"><dt>{{ $t('dataApiAdmin.capKeys') }}</dt>
          <dd>{{ $t('dataApiAdmin.capKeysValue', { days: limits.key_max_days, rate: fmt.count(limits.key_default_rate ?? 0) }) }}</dd></div>
        <div><dt>{{ $t('dataApiAdmin.address') }}</dt><dd class="mono">{{ origin }}/data/…</dd></div>
      </dl>
      <p class="hint">{{ $t('dataApiAdmin.capsHint') }}</p>

      <div class="tablecard">
        <div class="list-head">
          <h3>{{ $t('dataApiAdmin.count', { n: rows.length }, rows.length) }}</h3>
          <div class="tools">
            <div class="seg" role="group" :aria-label="$t('dataApiAdmin.filter')">
              <button type="button" class="mini" :class="{ on: filter === 'all' }" :aria-pressed="filter === 'all'" @click="filter = 'all'">{{ $t('dataApiAdmin.all') }}</button>
              <button type="button" class="mini" :class="{ on: filter === 'active' }" :aria-pressed="filter === 'active'" @click="filter = 'active'">{{ $t('dataApiAdmin.activeOnly') }}</button>
            </div>
            <button type="button" class="mini" :disabled="loading" :aria-label="$t('dataApiAdmin.reload')" :title="$t('dataApiAdmin.reload')" @click="load">
              <LoaderCircle v-if="loading" :size="12" class="spin" /><RefreshCw v-else :size="12" />
            </button>
          </div>
        </div>
        <p v-if="!rows.length" class="muted empty">{{ $t('dataApiAdmin.none') }}</p>
        <div v-else class="tablewrap">
          <table class="rows">
            <thead>
              <tr>
                <th>{{ $t('dataApiAdmin.colApi') }}</th>
                <th>{{ $t('dataApiAdmin.colCreator') }}</th>
                <th>{{ $t('dataApiAdmin.colState') }}</th>
                <th>{{ $t('dataApiAdmin.colRuntime') }}</th>
                <th>{{ $t('dataApiAdmin.colPods') }}</th>
                <th class="n">{{ $t('dataApiAdmin.colRequests') }}</th>
                <th class="n">{{ $t('dataApiAdmin.colErrors') }}</th>
                <th class="n">{{ $t('dataApiAdmin.colKeys') }}</th>
                <th v-if="props.canWrite" />
              </tr>
            </thead>
            <tbody>
              <tr v-for="a in rows" :key="a.id" :data-api="a.id">
                <td>
                  <NuxtLink :to="`/data-api?id=${a.id}`">{{ a.name }}</NuxtLink>
                  <br><span class="mono muted">{{ a.path }}</span>
                  <span v-if="folderPaths.pathOf(a.project_id)" class="muted small"> · {{ folderPaths.pathOf(a.project_id) }}</span>
                </td>
                <td>
                  {{ a.creator?.full_name || a.creator?.email || '—' }}
                  <span v-if="a.creator && !a.creator.active" class="tag bad">{{ $t('dataApiAdmin.creatorInactive') }}</span>
                  <br v-if="a.creator?.full_name"><span v-if="a.creator?.full_name" class="muted small">{{ a.creator.email }}</span>
                </td>
                <td :class="['state', a.state]">{{ $t(`dataApi.state_${a.state}`) }}</td>
                <td class="rt">
                  <span class="mono" data-rt="summary">{{ runtimeOf(a) }}</span>
                  <template v-if="a.runtime.autoscale"><br><span class="muted small">{{ $t('dataApiAdmin.autoscaling') }}</span></template>
                </td>
                <td>
                  <span :class="['pods', tone(a)]">{{ podsOf(a) }}</span>
                  <template v-if="a.state === 'active' && a.error"><br><span class="small err mono">{{ a.error }}</span></template>
                </td>
                <td class="n">{{ fmt.count(a.requests_24h) }}<template v-if="a.last_used_at"><br><span class="muted small">{{ fmt.dateTime(a.last_used_at) }}</span></template></td>
                <td class="n" :class="{ err: a.errors_24h + a.throttled_24h > 0 }">{{ fmt.count(a.errors_24h + a.throttled_24h) }}</td>
                <td class="n">{{ a.keys }}</td>
                <td v-if="props.canWrite" class="act">
                  <template v-if="asking?.id === a.id">
                    <span class="small">{{ $t(asking.what === 'pause' ? 'dataApiAdmin.pauseAsk' : 'dataApiAdmin.deleteAsk', { name: a.name }) }}</span>
                    <button type="button" class="mini" :class="{ danger: asking.what === 'delete' }" :disabled="busy === a.id" @click="act(a, asking.what)">
                      <LoaderCircle v-if="busy === a.id" :size="12" class="spin" />
                      {{ $t(asking.what === 'pause' ? 'dataApiAdmin.pause' : 'dataApiAdmin.delete') }}
                    </button>
                    <button type="button" class="mini" @click="asking = null">{{ $t('dataApi.cancel') }}</button>
                  </template>
                  <template v-else>
                    <button v-if="list.enabled" type="button" class="mini" :aria-label="$t('dataApiAdmin.runtimeOf', { name: a.name })" data-rt="edit"
                            :title="$t('dataApiAdmin.runtimeOf', { name: a.name })" @click="openRuntime(a)"><Cpu :size="12" /> {{ $t('dataApiAdmin.runtime') }}</button>
                    <button v-if="a.state === 'active'" type="button" class="mini" :aria-label="$t('dataApiAdmin.pauseOf', { name: a.name })"
                            @click="asking = { id: a.id, what: 'pause' }"><Pause :size="12" /> {{ $t('dataApiAdmin.pause') }}</button>
                    <button type="button" class="mini danger" :aria-label="$t('dataApiAdmin.deleteOf', { name: a.name })"
                            @click="asking = { id: a.id, what: 'delete' }"><Trash2 :size="12" /> {{ $t('dataApiAdmin.delete') }}</button>
                  </template>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </template>
    <Modal :open="!!editing" :title="$t('dataApiAdmin.runtimeTitle', { name: editing?.row.name ?? '' })" :width="520"
           @close="savingRuntime || (editing = null)">
      <div v-if="editing" class="rtdlg">
        <p class="muted small">{{ $t('dataApiAdmin.runtimeLead') }}</p>
        <DataApiRuntimeForm v-model="editing.rt" :limits="list?.runtime_limits" :disabled="savingRuntime" />
        <p class="muted small" data-rt="total">
          {{ editing.row.state === 'active'
            ? $t('dataApiAdmin.runtimeTotalActive', { others: othersPods, max: list?.max_total_replicas ?? 0 })
            : $t('dataApiAdmin.runtimeTotalIdle', { max: list?.max_total_replicas ?? 0 }) }}
        </p>
      </div>
      <template #footer>
        <button type="button" :disabled="savingRuntime" @click="editing = null">{{ $t('dataApi.cancel') }}</button>
        <button type="button" class="primary" :disabled="savingRuntime || !!runtimeIssue" data-rt="save" @click="saveRuntime">
          <LoaderCircle v-if="savingRuntime" :size="14" class="spin" /><Save v-else :size="14" /> {{ $t('dataApi.runtimeApply') }}
        </button>
      </template>
    </Modal>
  </section>
</template>

<style scoped src="~/assets/admintable.css"></style>
<style scoped>
.das { display: flex; flex-direction: column; gap: 14px; min-width: 0; }
/* the panel's table (admintable.css), a little taller, with headers on one line */
.tablewrap { max-height: 520px; }
table.rows th { white-space: nowrap; }
.das-head h2 { margin: 0; display: flex; align-items: center; gap: 8px; font-size: 16px; }
.das-head p { margin: 4px 0 0; max-width: 72ch; line-height: 1.5; }
.note { margin: 0; padding: 8px 10px; border-radius: 8px; background: var(--bg-soft); border: 1px solid var(--border); max-width: 720px; }
.caps { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 8px; margin: 0; max-width: 900px; }
.caps > div { border: 1px solid var(--border-soft); border-radius: 8px; padding: 7px 10px; background: var(--panel); min-width: 0; }
.caps dt { font-size: 11.5px; color: var(--muted); }
.caps dd { margin: 2px 0 0; font-size: 13px; overflow-wrap: anywhere; }
.hint { font-size: 12px; color: var(--muted); margin: 0; line-height: 1.45; max-width: 80ch; }
.list-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
.list-head h3 { margin: 0; font-size: 14px; }
.tools { display: flex; gap: 8px; align-items: center; }
.seg { display: flex; gap: 6px; }
.seg .on { border-color: var(--accent); background: var(--tint-accent); color: var(--accent-hi); }
.empty { margin: 0; }
.n { text-align: right; font-variant-numeric: tabular-nums; }
.small { font-size: 11.5px; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 11.5px; }
.err { color: var(--danger); }
.state.active { color: var(--accent-2); }
.state.paused { color: var(--warning); }
.state.draft { color: var(--muted); }
.pods.good { color: var(--accent-2); }
.pods.wait { color: var(--warning); }
.pods.bad { color: var(--danger); }
.tag { font-size: 10px; text-transform: uppercase; letter-spacing: 0.04em; padding: 0 6px; border-radius: 8px; border: 1px solid var(--border); margin-left: 4px; }
.tag.bad { color: var(--danger); }
.act { white-space: nowrap; text-align: right; }
.rt { white-space: nowrap; }
.rtdlg { display: flex; flex-direction: column; gap: 12px; }
.rtdlg p { margin: 0; line-height: 1.45; }
.act .small { margin-right: 6px; white-space: normal; }
.act button + button { margin-left: 4px; }
.danger { color: var(--danger); }
.muted { color: var(--muted); }
.mini { display: inline-flex; align-items: center; gap: 4px; }
.spin { animation: spin 1s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .spin { animation: none; } }
</style>
