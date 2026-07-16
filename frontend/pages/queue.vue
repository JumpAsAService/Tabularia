<script setup lang="ts">
// Queue (solo superuser): stato near-real-time dei worker Celery e dei job —
// in esecuzione e in attesa — con la possibilità di FERMARE un job in corso.
// Il numero di worker è dinamico (in produzione può essere diverso): mostriamo
// quelli online in quel momento. Polling ogni 2s; nessun websocket.
import { computed, onMounted, onUnmounted, ref, watchEffect } from 'vue'
import { Cpu, Square, RefreshCw, CircleAlert, Loader } from 'lucide-vue-next'
import { useQueue, type QueueOverview } from '~/composables/useQueue'
import { errMessage } from '~/composables/useApi'

const { user } = useAuth()
const router = useRouter()
const toast = useToast()
const queueApi = useQueue()

// guardia UX (il gateway impone la RBAC vera: /queue è require_superuser)
watchEffect(() => {
  if (user.value && !user.value.is_superuser) router.replace('/')
})

const POLL_MS = 2000
const data = ref<QueueOverview | null>(null)
const error = ref('')
const loading = ref(true)
const stopping = ref<Record<string, boolean>>({})
let timer: ReturnType<typeof setInterval> | null = null

async function tick() {
  try {
    data.value = await queueApi.overview()
    error.value = ''
  } catch (e) {
    error.value = errMessage(e)
  } finally {
    loading.value = false
  }
}

async function stop(taskId: string) {
  if (!confirm(`Fermare il job ${taskId.slice(0, 8)}…? L'esecuzione in corso verrà interrotta.`)) return
  stopping.value[taskId] = true
  try {
    await queueApi.stopJob(taskId)
    toast.success('Job fermato')
    await tick()
  } catch (e) {
    toast.error(errMessage(e))
  } finally {
    delete stopping.value[taskId]
  }
}

onMounted(() => {
  tick()
  timer = setInterval(tick, POLL_MS)
})
onUnmounted(() => {
  if (timer) clearInterval(timer)
})

// nomi task tecnici → etichette leggibili
const LABELS: Record<string, string> = {
  'app.tasks.jobs.transform_data_task': 'Trasformazione flusso',
  'app.tasks.jobs.ingest_database_task': 'Ingest datasource',
  'app.tasks.jobs.convert_to_parquet_task': 'Conversione parquet',
  'app.tasks.jobs.process_file_task': 'Elaborazione file',
  'app.tasks.jobs.evict_cache_task': 'Pulizia cache',
  'app.tasks.jobs.storage_stats_task': 'Statistiche storage',
}
function label(name: string | null): string {
  if (!name) return '—'
  return LABELS[name] ?? name.split('.').pop() ?? name
}
function fmtRuntime(s: number | null): string {
  if (s == null) return '—'
  if (s < 60) return `${s.toFixed(0)}s`
  return `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`
}

const workers = computed(() => data.value?.workers ?? [])
const running = computed(() => data.value?.running ?? [])
const reserved = computed(() => data.value?.reserved ?? [])
</script>

<template>
  <AppShell>
    <template v-if="user?.is_superuser">
      <div class="page-head">
        <h2><Cpu :size="18" /> Queue</h2>
        <div class="head-actions">
          <span class="live"><span class="dot" /> live · 2s</span>
          <button class="mini" title="Aggiorna ora" @click="tick"><RefreshCw :size="14" /></button>
        </div>
      </div>

      <p v-if="error" class="err"><CircleAlert :size="14" /> {{ error }}</p>

      <!-- riepilogo -->
      <div class="stats">
        <div class="stat">
          <span class="n">{{ workers.length }}</span><span class="l">worker online</span>
        </div>
        <div class="stat">
          <span class="n">{{ data?.running_count ?? 0 }}</span><span class="l">in esecuzione</span>
        </div>
        <div class="stat">
          <span class="n">{{ data?.waiting ?? 0 }}</span><span class="l">in attesa</span>
        </div>
        <div class="stat sub">
          <span class="n">{{ data?.queued ?? 0 }}</span><span class="l">accodati nel broker</span>
        </div>
      </div>

      <!-- worker -->
      <div class="section-title">Worker</div>
      <p v-if="!workers.length && !loading" class="muted">Nessun worker online.</p>
      <div v-else class="workers">
        <div v-for="w in workers" :key="w.name" class="worker">
          <div class="wname"><Cpu :size="13" /> {{ w.name }}</div>
          <div class="wmeta">
            <span :title="'Job in esecuzione su ' + (w.concurrency ?? '?') + ' slot'">
              {{ w.running }}<template v-if="w.concurrency"> / {{ w.concurrency }}</template> in esec.
            </span>
            <span v-if="w.reserved" class="muted">· {{ w.reserved }} prefetch</span>
          </div>
          <div class="wbar">
            <span
              v-for="i in (w.concurrency || Math.max(w.running, 1))"
              :key="i"
              class="slot"
              :class="{ busy: i <= w.running }"
            />
          </div>
        </div>
      </div>

      <!-- in esecuzione -->
      <div class="section-title">In esecuzione <span class="muted">({{ running.length }})</span></div>
      <p v-if="!running.length && !loading" class="muted">Nessun job in esecuzione.</p>
      <div v-else class="jobs">
        <div v-for="j in running" :key="j.task_id" class="job">
          <span class="jspin"><Loader :size="13" /></span>
          <span class="jname">{{ label(j.task_name) }}</span>
          <code class="jid" :title="j.task_id">{{ j.task_id?.slice(0, 8) }}</code>
          <span class="jworker muted">{{ j.worker }}</span>
          <span class="jrt muted">{{ fmtRuntime(j.runtime_s) }}</span>
          <button class="mini danger" :disabled="stopping[j.task_id]" @click="stop(j.task_id)">
            <Square :size="12" /> {{ stopping[j.task_id] ? 'Fermo…' : 'Ferma' }}
          </button>
        </div>
      </div>

      <!-- in attesa -->
      <div class="section-title">
        In attesa <span class="muted">({{ data?.waiting ?? 0 }})</span>
      </div>
      <p v-if="!reserved.length && !(data?.queued)" class="muted">Nessun job in attesa.</p>
      <template v-else>
        <div v-if="reserved.length" class="jobs">
          <div v-for="j in reserved" :key="j.task_id" class="job">
            <span class="jspin waiting">•</span>
            <span class="jname">{{ label(j.task_name) }}</span>
            <code class="jid" :title="j.task_id">{{ j.task_id?.slice(0, 8) }}</code>
            <span class="jworker muted">prefetch · {{ j.worker }}</span>
            <span class="jrt muted">—</span>
            <button class="mini danger" :disabled="stopping[j.task_id]" @click="stop(j.task_id)">
              <Square :size="12" /> {{ stopping[j.task_id] ? 'Rimuovo…' : 'Rimuovi' }}
            </button>
          </div>
        </div>
        <p v-if="data?.queued" class="muted queued-note">
          + {{ data.queued }} accodati nel broker, non ancora assegnati a un worker
          <template v-if="data.queues?.length">
            ({{ data.queues.map((q) => `${q.name}: ${q.messages}`).join(', ') }})
          </template>
        </p>
      </template>
    </template>
  </AppShell>
</template>

<style scoped>
.page-head { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.page-head h2 { display: inline-flex; align-items: center; gap: 8px; }
.head-actions { display: flex; align-items: center; gap: 10px; }
.live { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: var(--muted); }
.live .dot { width: 8px; height: 8px; border-radius: 50%; background: #22c55e; animation: pulse 1.6s ease-in-out infinite; }
@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.35; } }
.mini { padding: 6px 8px; }

.stats { display: flex; gap: 12px; flex-wrap: wrap; margin: 14px 0 4px; }
.stat { display: flex; flex-direction: column; gap: 2px; padding: 12px 16px; border: 1px solid var(--border-soft); border-radius: 10px; background: var(--panel); min-width: 130px; }
.stat.sub { background: var(--panel-2); }
.stat .n { font-size: 24px; font-weight: 700; font-variant-numeric: tabular-nums; }
.stat .l { font-size: 11.5px; text-transform: uppercase; letter-spacing: 0.04em; color: var(--muted); }

.section-title { font-size: 12px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted); margin: 22px 0 8px; }

.workers { display: flex; flex-wrap: wrap; gap: 10px; }
.worker { border: 1px solid var(--border-soft); border-radius: 10px; background: var(--panel); padding: 12px 14px; min-width: 220px; }
.wname { display: inline-flex; align-items: center; gap: 7px; font-weight: 600; font-size: 13px; word-break: break-all; }
.wmeta { font-size: 12.5px; margin: 6px 0 8px; }
.wbar { display: flex; gap: 3px; flex-wrap: wrap; }
.slot { width: 14px; height: 8px; border-radius: 2px; background: var(--border); }
.slot.busy { background: #34d399; }

.jobs { display: flex; flex-direction: column; gap: 4px; }
.job { display: grid; grid-template-columns: 20px minmax(140px, 1.3fr) 70px minmax(120px, 1fr) 70px auto; align-items: center; gap: 10px; padding: 8px 12px; border: 1px solid var(--border-soft); border-radius: 8px; background: var(--panel); }
.jspin { color: #facc15; display: inline-flex; }
.jspin :deep(svg) { animation: spin 1.4s linear infinite; }
.jspin.waiting { color: var(--muted); font-size: 18px; justify-content: center; animation: none; }
@keyframes spin { to { transform: rotate(360deg); } }
.jname { font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.jid { font-family: ui-monospace, monospace; font-size: 12px; color: var(--muted); }
.jworker { font-size: 12.5px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.jrt { font-size: 12.5px; text-align: right; font-variant-numeric: tabular-nums; }
.queued-note { font-size: 12.5px; margin-top: 8px; }
</style>
