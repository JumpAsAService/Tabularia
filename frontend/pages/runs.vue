<script setup lang="ts">
// Esecuzioni: ricerca globale dei run nei progetti leggibili, per capire perché
// i flussi falliscono. Filtro per stato + ricerca testuale sul motivo; click su
// una riga per il traceback completo.
import { onMounted, ref, watch } from 'vue'
import {
  History, Search, RefreshCw, LoaderCircle, ChevronRight, Workflow, Database,
} from 'lucide-vue-next'
import { errMessage } from '~/composables/useApi'
import { skeletonPad } from '~/composables/useSkeleton'
import { useRuns, type RunInfo } from '~/composables/useRuns'

const runsApi = useRuns()

const q = ref('')
const status = ref<'FAILURE' | 'SUCCESS' | ''>('FAILURE') // di default gli errori
const list = ref<RunInfo[]>([])
const loading = ref(true)
const error = ref('')
const expanded = ref<number | null>(null)

async function load() {
  loading.value = true
  const t0 = performance.now()
  try {
    list.value = await runsApi.search({ status: status.value || undefined, q: q.value.trim() || undefined, limit: 100 })
    error.value = ''
  } catch (e) {
    error.value = errMessage(e)
  } finally {
    await skeletonPad(t0)
    loading.value = false
  }
}

onMounted(load)

// ricarica alla pressione di stato; per il testo aspetta una piccola pausa
watch(status, load)
let deb: ReturnType<typeof setTimeout> | null = null
watch(q, () => {
  if (deb) clearTimeout(deb)
  deb = setTimeout(load, 350)
})

function toggle(id: number) {
  expanded.value = expanded.value === id ? null : id
}

function fmtDate(s: string | null): string {
  if (!s) return '—'
  const d = new Date(s)
  return d.toLocaleString('it-IT', { dateStyle: 'short', timeStyle: 'short' })
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
      <h2><History :size="18" /> Esecuzioni <span class="muted count">{{ list.length }}</span></h2>
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
          <input v-model="q" type="text" placeholder="Cerca nel motivo dell'errore…" />
        </span>
        <button class="mini" title="Aggiorna" @click="load"><RefreshCw :size="14" /></button>
      </div>
    </div>

    <p v-if="error" class="err">{{ error }}</p>
    <SkeletonRows v-else-if="loading" :rows="5" />
    <p v-else-if="!list.length" class="muted">
      Nessuna esecuzione{{ status === 'FAILURE' ? ' fallita' : '' }}{{ q ? ' per questa ricerca' : '' }}.
    </p>

    <div v-else class="runs">
      <div v-for="r in list" :key="r.id" class="run" :class="{ open: expanded === r.id }">
        <button class="run-row" @click="toggle(r.id)">
          <ChevronRight :size="14" class="chev" :class="{ rot: expanded === r.id }" />
          <span class="stpill" :class="r.status.toLowerCase()">{{ r.status }}</span>
          <span class="who">
            <component :is="r.kind === 'ingest' ? Database : Workflow" :size="13" />
            {{ r.flow_name || r.source_name || (r.kind === 'ingest' ? 'refresh datasource' : `run #${r.id}`) }}
          </span>
          <span class="msg muted">{{ r.error || (r.status === 'SUCCESS' ? '—' : '') }}</span>
          <span class="when muted">{{ fmtDate(r.started_at) }}</span>
        </button>
        <div v-if="expanded === r.id" class="detail">
          <div v-if="r.error" class="detail-summary">{{ r.error }}</div>
          <pre v-if="r.error_detail" class="trace">{{ r.error_detail }}</pre>
          <p v-else class="muted small">Nessun dettaglio aggiuntivo registrato.</p>
        </div>
      </div>
    </div>
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
.run-row { width: 100%; display: grid; grid-template-columns: 18px 78px minmax(160px, 1fr) minmax(0, 2fr) 130px; align-items: center; gap: 10px; padding: 9px 12px; background: transparent; border: none; text-align: left; cursor: pointer; }
.run-row:hover { background: var(--panel-2); }
.chev { color: var(--muted); transition: transform 0.15s; flex: none; }
.chev.rot { transform: rotate(90deg); }
.stpill { font-size: 11px; font-weight: 600; padding: 2px 7px; border-radius: 20px; text-align: center; }
.stpill.failure { background: rgba(239, 68, 68, 0.15); color: #ef4444; }
.stpill.success { background: rgba(52, 211, 153, 0.15); color: #34d399; }
.stpill.pending, .stpill.started { background: rgba(148, 163, 184, 0.15); color: var(--muted); }
.who { display: inline-flex; align-items: center; gap: 6px; font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.msg { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 13px; }
.when { text-align: right; font-size: 12.5px; white-space: nowrap; }

.detail { border-top: 1px solid var(--border-soft); padding: 10px 12px 12px; background: var(--panel-2); }
.detail-summary { font-size: 13px; margin-bottom: 8px; color: var(--text); }
.trace { margin: 0; padding: 10px; background: var(--panel); border: 1px solid var(--border-soft); border-radius: 6px; font-family: ui-monospace, monospace; font-size: 12px; line-height: 1.5; white-space: pre-wrap; word-break: break-word; max-height: 340px; overflow: auto; }
.small { font-size: 12.5px; }
.mini { padding: 6px 8px; }
</style>
