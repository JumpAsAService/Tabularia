<script setup lang="ts">
// Administration → Data APIs (docs/specs/data-apis.md, «Administration»): every data API of the
// installation with its creator, its state, its pods and its traffic of the last 24 hours; an
// administrator pauses or deletes any of them, whoever made it (the delete even with the feature
// switched off, to clean what is left). The caps are the chart's (dataApi.*), shown here.
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { Braces, LoaderCircle, Pause, RefreshCw, Trash2 } from 'lucide-vue-next'
import { errMessage } from '~/composables/useApi'
import { useFormat } from '~/composables/useFormat'
import { useFolderPaths } from '~/composables/useFolderPaths'
import { dataApiOrigin, useDataApis, type DataApiAdminList, type DataApiAdminRow, type DataApiLimits } from '~/composables/useDataApis'

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
        <div v-if="limits?.enabled"><dt>{{ $t('dataApiAdmin.capPod') }}</dt>
          <dd>{{ $t('dataApiAdmin.capPodValue', { replicas: limits.max_replicas, cpu: limits.max_cpu, memory: limits.max_memory }) }}</dd></div>
        <div v-if="limits?.enabled"><dt>{{ $t('dataApiAdmin.capKeys') }}</dt>
          <dd>{{ $t('dataApiAdmin.capKeysValue', { days: limits.key_max_days, rate: fmt.count(limits.key_default_rate ?? 0) }) }}</dd></div>
        <div><dt>{{ $t('dataApiAdmin.address') }}</dt><dd class="mono">{{ origin }}/data/…</dd></div>
      </dl>
      <p class="hint">{{ $t('dataApiAdmin.capsHint') }}</p>

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
    </template>
  </section>
</template>

<style scoped>
.das { display: flex; flex-direction: column; gap: 14px; min-width: 0; }
/* the panel's table, here too (its styles are scoped to it): it scrolls by itself, never the page */
.tablewrap { max-height: 520px; overflow: auto; }
table.rows { width: 100%; border-collapse: collapse; font-size: 13px; }
table.rows th {
  text-align: left; padding: 4px; font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em;
  color: var(--muted); border-bottom: 1px solid var(--border); position: sticky; top: 0; background: var(--panel); z-index: 1;
  white-space: nowrap;
}
table.rows td { padding: 6px 4px; border-bottom: 1px solid var(--border-soft); vertical-align: top; }
table.rows tr:last-child td { border-bottom: none; }
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
.act .small { margin-right: 6px; white-space: normal; }
.act button + button { margin-left: 4px; }
.danger { color: var(--danger); }
.muted { color: var(--muted); }
.mini { display: inline-flex; align-items: center; gap: 4px; }
.spin { animation: spin 1s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .spin { animation: none; } }
</style>
