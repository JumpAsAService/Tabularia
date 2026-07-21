<script setup lang="ts">
// Esecuzioni: ricerca globale dei run nei progetti leggibili, per capire perché
// i flussi falliscono. Filtro per stato + ricerca testuale sul motivo (server-side,
// su tutto il dataset) + paginazione; click su una riga per il traceback completo.
import { ref, watch } from 'vue'
import { History, Search, RefreshCw, ChevronRight, Workflow, Database, CalendarClock, User } from 'lucide-vue-next'
import { useRuns, type RunInfo } from '~/composables/useRuns'
import { usePagedList } from '~/composables/usePagedList'

const runsApi = useRuns()
const status = ref<'FAILURE' | 'SUCCESS' | ''>('FAILURE') // di default gli errori
const expanded = ref<number | null>(null)

const { q, items, total, offset, pageSize, loading, error, load, next, prev } = usePagedList<RunInfo>(
  (p) => runsApi.search({ ...p, status: status.value || undefined }),
)
// lo stato è un filtro extra: al cambio torna alla prima pagina e ricarica
watch(status, () => {
  offset.value = 0
  load()
})

function toggle(id: number) {
  expanded.value = expanded.value === id ? null : id
}

function fmtDate(s: string | null): string {
  if (!s) return '—'
  return new Date(s).toLocaleString('it-IT', { dateStyle: 'short', timeStyle: 'short' })
}

// chi ha avviato il run: se schedulato mostriamo "schedule", altrimenti il nome
function isScheduled(r: RunInfo): boolean {
  return r.trigger_type === 'schedule'
}
function triggeredBy(r: RunInfo): string {
  return isScheduled(r) ? 'schedule' : (r.launched_by_name || '—')
}

const STATUSES: { value: 'FAILURE' | 'SUCCESS' | ''; label: string }[] = [
  { value: 'FAILURE', label: 'Falliti' },
  { value: 'SUCCESS', label: 'Riusciti' },
  { value: '', label: 'Tutti' },
]
</script>

<template>
  <AppShell>
    <div class="page-head">
      <h2><History :size="18" /> Esecuzioni <span class="muted count">{{ total }}</span></h2>
      <div class="head-actions">
        <div class="segmented">
          <button
            v-for="s in STATUSES"
            :key="s.value || 'all'"
            :class="{ on: status === s.value }"
            @click="status = s.value"
          >{{ s.label }}</button>
        </div>
        <span class="searchbox">
          <Search :size="14" />
          <input v-model="q" type="text" placeholder="Cerca flusso, sorgente, autore o errore…" />
        </span>
        <button class="mini" title="Aggiorna" @click="load"><RefreshCw :size="14" /></button>
      </div>
    </div>

    <p v-if="error" class="err">{{ error }}</p>
    <SkeletonRows v-else-if="loading" :rows="5" />
    <p v-else-if="!items.length" class="muted">
      Nessuna esecuzione{{ status === 'FAILURE' ? ' fallita' : '' }}{{ q ? ' per questa ricerca' : '' }}.
    </p>

    <div v-else class="runs">
      <div v-for="r in items" :key="r.id" class="run" :class="{ open: expanded === r.id }">
        <button class="run-row" @click="toggle(r.id)">
          <ChevronRight :size="14" class="chev" :class="{ rot: expanded === r.id }" />
          <span class="stpill" :class="r.status.toLowerCase()">{{ r.status }}</span>
          <span class="who">
            <component :is="r.kind === 'ingest' ? Database : Workflow" :size="13" />
            {{ r.flow_name || r.source_name || (r.kind === 'ingest' ? 'refresh datasource' : `run #${r.id}`) }}
          </span>
          <span class="msg muted">{{ r.error || (r.status === 'SUCCESS' ? '—' : '') }}</span>
          <span class="by muted" :class="{ sched: isScheduled(r) }" :title="isScheduled(r) ? 'Avviato dallo scheduler' : 'Avviato da ' + triggeredBy(r)">
            <component :is="isScheduled(r) ? CalendarClock : User" :size="12" />
            {{ triggeredBy(r) }}
          </span>
          <span class="when muted">{{ fmtDate(r.started_at) }}</span>
        </button>
        <div v-if="expanded === r.id" class="detail">
          <div v-if="r.error" class="detail-summary">{{ r.error }}</div>
          <pre v-if="r.error_detail" class="trace">{{ r.error_detail }}</pre>
          <p v-else class="muted small">Nessun dettaglio aggiuntivo registrato.</p>
        </div>
      </div>
    </div>

    <Pager :offset="offset" :page-size="pageSize" :total="total" :loading="loading" @prev="prev" @next="next" />
  </AppShell>
</template>

<style scoped>
.page-head { display: flex; align-items: center; justify-content: space-between; gap: 16px; flex-wrap: wrap; }
.page-head h2 { display: inline-flex; align-items: center; gap: 8px; }
.count { font-weight: 400; font-size: 14px; }
.head-actions { display: flex; align-items: center; gap: 10px; }
.searchbox { display: inline-flex; align-items: center; gap: 6px; border: 1px solid var(--border); border-radius: 7px; padding: 5px 9px; background: var(--panel-2); }
.searchbox input { border: none; background: transparent; outline: none; color: var(--text); width: 220px; }
.segmented { display: inline-flex; border: 1px solid var(--border); border-radius: 7px; overflow: hidden; }
.segmented button { padding: 5px 11px; background: var(--panel-2); color: var(--muted); border: none; border-right: 1px solid var(--border); font-size: 13px; }
.segmented button:last-child { border-right: none; }
.segmented button.on { background: var(--accent); color: #fff; }

.runs { display: flex; flex-direction: column; gap: 6px; margin-top: 14px; }
.run { border: 1px solid var(--border-soft); border-radius: 8px; background: var(--panel); overflow: hidden; }
.run.open { border-color: var(--border); }
.run-row { width: 100%; display: grid; grid-template-columns: 18px 78px minmax(150px, 1fr) minmax(0, 2fr) 120px 130px; align-items: center; gap: 10px; padding: 9px 12px; background: transparent; border: none; text-align: left; cursor: pointer; }
.run-row:hover { background: var(--panel-2); }
.chev { color: var(--muted); transition: transform 0.15s; flex: none; }
.chev.rot { transform: rotate(90deg); }
.stpill { font-size: 11px; font-weight: 600; padding: 2px 7px; border-radius: 20px; text-align: center; }
.stpill.failure { background: rgba(239, 68, 68, 0.15); color: #ef4444; }
.stpill.success { background: rgba(52, 211, 153, 0.15); color: #34d399; }
.stpill.pending, .stpill.started { background: rgba(148, 163, 184, 0.15); color: var(--muted); }
.who { display: inline-flex; align-items: center; gap: 6px; font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.msg { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13px; }
.by { display: inline-flex; align-items: center; gap: 5px; font-size: 12.5px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.by.sched { color: var(--accent-2); }
.when { text-align: right; font-size: 12.5px; white-space: nowrap; }

.detail { border-top: 1px solid var(--border-soft); padding: 10px 12px 12px; background: var(--panel-2); }
.detail-summary { font-size: 13px; margin-bottom: 8px; color: var(--text); }
.trace { margin: 0; padding: 10px; background: var(--panel); border: 1px solid var(--border-soft); border-radius: 6px; font-family: ui-monospace, monospace; font-size: 12px; line-height: 1.5; white-space: pre-wrap; word-break: break-word; max-height: 340px; overflow: auto; }
.small { font-size: 12.5px; }
.mini { padding: 6px 8px; }
</style>
