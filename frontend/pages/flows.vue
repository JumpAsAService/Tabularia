<script setup lang="ts">
// Flussi nelle cartelle leggibili, con ricerca server-side + paginazione. Ogni
// flusso ha un expander (come le Esecuzioni) con metriche d'esecuzione, la
// timeline (Gantt) dei run e lo storico versioni con promozione.
import { onMounted, reactive, ref } from 'vue'
import {
  Workflow, Search, Trash2, Folder, Plus, CalendarClock, ChevronRight, Pencil, ArrowUpFromLine,
} from 'lucide-vue-next'
import { errMessage } from '~/composables/useApi'
import {
  useFlows, type FlowSummary, type FlowStats, type FlowVersionInfo,
} from '~/composables/useFlows'
import { useProjects } from '~/composables/useProjects'
import { useRuns, type RunInfo } from '~/composables/useRuns'
import { usePagedList } from '~/composables/usePagedList'

const flowsApi = useFlows()
const projectsApi = useProjects()
const runsApi = useRuns()
const toast = useToast()

const { q, items, total, offset, pageSize, loading, error, load, next, prev } =
  usePagedList<FlowSummary>((p) => flowsApi.listPaged(p))

const folderName = ref<Record<number, string>>({})
onMounted(async () => {
  try {
    const projects = await projectsApi.list()
    folderName.value = Object.fromEntries(projects.map((p) => [p.id, p.name]))
  } catch {
    /* i nomi cartella sono accessori */
  }
})

// ── Expander per-flusso: metriche + Gantt + versioni (caricati alla prima apertura)
interface Detail {
  stats: FlowStats | null
  versions: FlowVersionInfo[]
  runs: RunInfo[]
  loading: boolean
  error: string
}
const expanded = ref<number | null>(null)
const detail = reactive<Record<number, Detail>>({})

async function fetchDetail(id: number) {
  detail[id].loading = true
  try {
    const [stats, versions, runs] = await Promise.all([
      flowsApi.stats(id),
      flowsApi.versions(id),
      runsApi.listByFlow(id),
    ])
    detail[id].stats = stats
    detail[id].versions = versions
    detail[id].runs = runs
    detail[id].error = ''
  } catch (e) {
    detail[id].error = errMessage(e)
  } finally {
    detail[id].loading = false
  }
}

function toggle(f: FlowSummary) {
  if (expanded.value === f.id) {
    expanded.value = null
    return
  }
  expanded.value = f.id
  if (!detail[f.id]) {
    detail[f.id] = { stats: null, versions: [], runs: [], loading: true, error: '' }
    fetchDetail(f.id)
  }
}

async function promote(id: number, version: number) {
  if (!confirm(`Promuovere la versione v${version} a corrente? Diventa la definizione del flusso.`)) return
  try {
    await flowsApi.promote(id, version)
    toast.success(`Versione v${version} promossa`)
    await fetchDetail(id)
  } catch (e) {
    toast.error(errMessage(e))
  }
}

async function deleteFlow(f: FlowSummary) {
  if (!confirm(`Eliminare il flusso "${f.name}"?`)) return
  try {
    await flowsApi.remove(f.id)
    toast.success(`Flusso "${f.name}" eliminato`)
    await load()
  } catch (e) {
    toast.error(errMessage(e))
  }
}

function fmtDate(iso: string | null): string {
  if (!iso) return '—'
  return new Date(iso).toLocaleString('it-IT', { dateStyle: 'short', timeStyle: 'short' })
}
function fmtDur(secs: number | null | undefined): string {
  if (secs == null) return '—'
  if (secs < 60) return `${secs.toFixed(1)}s`
  return `${Math.floor(secs / 60)}m ${Math.round(secs % 60)}s`
}

// ── Scheduling (dialog condiviso) ────────────────────────────────────────────
const scheduleFor = ref<FlowSummary | null>(null)
const savingSchedule = ref(false)
async function saveSchedule(cron: string) {
  if (!scheduleFor.value) return
  savingSchedule.value = true
  try {
    const updated = await flowsApi.setSchedule(scheduleFor.value.id, cron.trim())
    items.value = items.value.map((x) => (x.id === updated.id ? { ...x, ...updated } : x))
    toast.success(cron.trim() ? `Esecuzione schedulata: ${updated.run_schedule}` : 'Schedulazione disattivata')
    scheduleFor.value = null
  } catch (e) {
    toast.error(errMessage(e))
  } finally {
    savingSchedule.value = false
  }
}
</script>

<template>
  <AppShell>
    <div class="page-head">
      <h2><Workflow :size="18" /> Flows <span class="muted count">{{ total }}</span></h2>
      <div class="head-actions">
        <span class="searchbox"><Search :size="14" /><input v-model="q" type="text" placeholder="Cerca flussi…" /></span>
        <NuxtLink to="/editor" class="btn-link"><Plus :size="14" /> Nuovo flusso</NuxtLink>
      </div>
    </div>

    <p v-if="error" class="err">{{ error }}</p>
    <SkeletonRows v-else-if="loading" :rows="5" />
    <p v-else-if="!items.length" class="muted">
      {{ q ? 'Nessun flusso per la ricerca.' : 'Nessun flusso: creane uno da una cartella o con Nuovo flusso.' }}
    </p>

    <div v-else class="flows">
      <div v-for="f in items" :key="f.id" class="flow" :class="{ open: expanded === f.id }">
        <div class="flow-row">
          <button class="flow-head" @click="toggle(f)">
            <ChevronRight :size="14" class="chev" :class="{ rot: expanded === f.id }" />
            <span class="fname"><Workflow :size="14" /> {{ f.name }}</span>
            <span class="folder muted"><Folder :size="12" /> {{ folderName[f.project_id] ?? `#${f.project_id}` }}</span>
            <span v-if="f.run_schedule" class="sched-badge" :title="f.run_schedule"><CalendarClock :size="11" /> schedulato</span>
            <span class="when muted">{{ fmtDate(f.updated_at) }}</span>
          </button>
          <div class="flow-actions">
            <button class="mini" title="Apri nell'editor" @click="navigateTo(`/editor?flow=${f.id}`)"><Pencil :size="13" /></button>
            <button class="mini" :class="{ active: !!f.run_schedule }" title="Schedula esecuzione" @click="scheduleFor = f"><CalendarClock :size="13" /></button>
            <button class="mini danger" title="Elimina flusso" @click="deleteFlow(f)"><Trash2 :size="13" /></button>
          </div>
        </div>

        <div v-if="expanded === f.id" class="flow-detail">
          <SkeletonRows v-if="detail[f.id]?.loading" :rows="2" />
          <p v-else-if="detail[f.id]?.error" class="err">{{ detail[f.id].error }}</p>
          <template v-else-if="detail[f.id]">
            <div class="metrics">
              <div class="metric"><span class="mlabel">Cartella</span><span>{{ folderName[f.project_id] ?? `#${f.project_id}` }}</span></div>
              <div class="metric"><span class="mlabel">Creato</span><span>{{ fmtDate(f.created_at) }}</span></div>
              <div class="metric"><span class="mlabel">Esecuzioni</span><span>{{ detail[f.id].stats?.run_count ?? 0 }}</span></div>
              <div class="metric"><span class="mlabel">Successi / falliti</span><span>{{ detail[f.id].stats?.success_count ?? 0 }} / {{ detail[f.id].stats?.failure_count ?? 0 }}</span></div>
              <div class="metric"><span class="mlabel">Ultima esecuzione</span><span>{{ fmtDate(detail[f.id].stats?.last_run_at ?? null) }}</span></div>
              <div class="metric"><span class="mlabel">Tempo medio</span><span>{{ fmtDur(detail[f.id].stats?.avg_duration_seconds) }}</span></div>
            </div>

            <div class="section-title">Timeline esecuzioni</div>
            <RunGantt :runs="detail[f.id].runs" />

            <div class="section-title">Versioni <span class="muted">({{ detail[f.id].versions.length }})</span></div>
            <div class="versions">
              <div v-for="v in detail[f.id].versions" :key="v.version" class="ver">
                <span class="vnum">v{{ v.version }}</span>
                <span v-if="v.is_current" class="tag">corrente</span>
                <span class="vnote muted">{{ v.note }}</span>
                <span class="vdate muted">{{ fmtDate(v.created_at) }}</span>
                <button v-if="!v.is_current" class="mini promote" title="Promuovi a corrente" @click="promote(f.id, v.version)">
                  <ArrowUpFromLine :size="12" /> Promuovi
                </button>
              </div>
            </div>
          </template>
        </div>
      </div>
    </div>

    <Pager :offset="offset" :page-size="pageSize" :total="total" :loading="loading" @prev="prev" @next="next" />

    <ScheduleDialog
      :open="!!scheduleFor"
      :title="scheduleFor?.name ?? ''"
      subtitle="Esegui automaticamente i nodi Output di"
      :current="scheduleFor?.run_schedule ?? null"
      :busy="savingSchedule"
      @save="saveSchedule"
      @cancel="scheduleFor = null"
    />
  </AppShell>
</template>

<!-- stili condivisi delle pagine-lista: .btn-link, .mini, .err, .tag, … -->
<style scoped src="~/assets/listpage.css" />
<style scoped>
.page-head { display: flex; align-items: center; justify-content: space-between; gap: 16px; flex-wrap: wrap; }
.page-head h2 { display: inline-flex; align-items: center; gap: 8px; }
.count { font-weight: 400; font-size: 14px; }
.head-actions { display: flex; align-items: center; gap: 10px; }
.searchbox { display: inline-flex; align-items: center; gap: 6px; border: 1px solid var(--border); border-radius: 7px; padding: 5px 9px; background: var(--panel-2); }
.searchbox input { border: none; background: transparent; outline: none; color: var(--text); width: 220px; }

.flows { display: flex; flex-direction: column; gap: 6px; margin-top: 14px; }
.flow { border: 1px solid var(--border-soft); border-radius: 8px; background: var(--panel); overflow: hidden; }
.flow.open { border-color: var(--border); }
.flow-row { display: flex; align-items: center; }
.flow-head { flex: 1; display: grid; grid-template-columns: 18px minmax(160px, 1.4fr) minmax(90px, 1fr) auto 150px; align-items: center; gap: 10px; padding: 10px 12px; background: transparent; border: none; text-align: left; cursor: pointer; }
.flow-head:hover { background: var(--panel-2); }
.chev { color: var(--muted); transition: transform 0.15s; flex: none; }
.chev.rot { transform: rotate(90deg); }
.fname { display: inline-flex; align-items: center; gap: 7px; font-weight: 550; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.folder { display: inline-flex; align-items: center; gap: 5px; font-size: 12.5px; white-space: nowrap; }
.sched-badge { display: inline-flex; align-items: center; gap: 4px; font-size: 11px; color: var(--accent-2); }
.when { text-align: right; font-size: 12.5px; white-space: nowrap; }
.flow-actions { display: flex; align-items: center; gap: 4px; padding-right: 10px; }
.mini.active { color: var(--accent-2); border-color: var(--accent-2); }

.flow-detail { border-top: 1px solid var(--border-soft); padding: 12px; background: var(--panel-2); }
.metrics { display: flex; flex-wrap: wrap; gap: 10px 22px; }
.metric { display: flex; flex-direction: column; gap: 2px; min-width: 110px; }
.mlabel { font-size: 11px; text-transform: uppercase; letter-spacing: 0.04em; color: var(--muted); }
.metric > span:last-child { font-variant-numeric: tabular-nums; }
.section-title { font-size: 12px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); margin: 16px 0 6px; }

.versions { display: flex; flex-direction: column; gap: 2px; }
.ver { display: flex; align-items: center; gap: 10px; padding: 5px 8px; border-radius: 6px; font-size: 13px; }
.ver:hover { background: var(--panel); }
.vnum { font-weight: 600; font-variant-numeric: tabular-nums; min-width: 34px; }
.vnote { flex: 1; }
.vdate { white-space: nowrap; font-size: 12px; }
.promote { display: inline-flex; align-items: center; gap: 4px; }
.small { font-size: 12.5px; }
</style>
