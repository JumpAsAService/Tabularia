<script setup lang="ts">
// Ricerca generale: una casella nella barra in alto che attraversa TUTTE le
// cartelle, non solo quella aperta. Risponde alla domanda vera di chi cerca
// «margine» senza sapere, e senza dover sapere, se sia un flusso o una vista.
//
// SICUREZZA — il filtro per permessi è del GATEWAY, non di qui. `/search` applica
// tre insiemi diversi: le cartelle sull'insieme navigabile, i contenuti su quello
// leggibile, le connessioni sulla capability ORTOGONALE CONNECT. Questo
// componente non filtra e non deve filtrare nulla: mostra ciò che il server ha
// già deciso di mostrare. Se il filtro vivesse qui, basterebbe aprire la scheda
// di rete del browser per vedere ciò che non si può vedere.
import { computed, ref, watch, onUnmounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { Search, X, Folder, Workflow, Database, Bookmark, Plug, LoaderCircle } from 'lucide-vue-next'
import { errMessage } from '~/composables/useApi'
import { useSearch, searchHitTarget, type SearchHit, type SearchKind } from '~/composables/useSearch'

const { t } = useI18n()
const api = useSearch()

const MIN = 2        // sotto i 2 caratteri ogni ricerca torna mezzo catalogo
const DEBOUNCE = 250 // digitando non si interroga a ogni tasto
const LIMIT = 20

const q = ref('')
const open = ref(false)
const loading = ref(false)
const error = ref('')
const hits = ref<SearchHit[]>([])
const total = ref(0)
const cursor = ref(-1) // riga evidenziata dalla tastiera

const ICON: Record<SearchKind, any> = {
  folder: Folder, flow: Workflow, datasource: Database, view: Bookmark, connection: Plug,
}
const ORDER: SearchKind[] = ['folder', 'flow', 'datasource', 'view', 'connection']

/** Raggruppati per tipo: cinque elenchi corti si leggono meglio di venti righe
 *  mescolate, e il tipo è la prima cosa che serve sapere di un risultato. */
const groups = computed(() =>
  ORDER
    .map((kind) => ({ kind, items: hits.value.filter((h) => h.kind === kind) }))
    .filter((g) => g.items.length),
)
/** Appiattito nell'ordine in cui appare: è su questo che si muove la tastiera. */
const flat = computed(() => groups.value.flatMap((g) => g.items))

let timer: ReturnType<typeof setTimeout> | null = null
let seq = 0 // scarta le risposte arrivate fuori ordine

async function run(termine: string) {
  const mio = ++seq
  loading.value = true
  error.value = ''
  try {
    const res = await api.query(termine, { limit: LIMIT })
    if (mio !== seq) return // una richiesta più recente ha già risposto
    hits.value = res.items
    total.value = res.total
    cursor.value = res.items.length ? 0 : -1
  } catch (e) {
    if (mio !== seq) return
    error.value = errMessage(e)
    hits.value = []
    total.value = 0
  } finally {
    if (mio === seq) loading.value = false
  }
}

watch(q, (val) => {
  if (timer) clearTimeout(timer)
  const termine = val.trim()
  open.value = true
  if (termine.length < MIN) {
    hits.value = []
    total.value = 0
    loading.value = false
    return
  }
  timer = setTimeout(() => run(termine), DEBOUNCE)
})

onUnmounted(() => { if (timer) clearTimeout(timer) })

function close() {
  open.value = false
  cursor.value = -1
}

function vai(hit: SearchHit) {
  close()
  q.value = ''
  hits.value = []
  navigateTo(searchHitTarget(hit))
}

function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape') { close(); return }
  if (!flat.value.length) return
  if (e.key === 'ArrowDown') {
    e.preventDefault()
    cursor.value = (cursor.value + 1) % flat.value.length
  } else if (e.key === 'ArrowUp') {
    e.preventDefault()
    cursor.value = (cursor.value - 1 + flat.value.length) % flat.value.length
  } else if (e.key === 'Enter' && cursor.value >= 0) {
    e.preventDefault()
    vai(flat.value[cursor.value])
  }
}

const indice = (hit: SearchHit) => flat.value.indexOf(hit)
const mostrati = computed(() => flat.value.length)
</script>

<template>
  <div class="gs">
    <span class="box" :class="{ active: open && q }">
      <Search :size="14" />
      <input
        v-model="q"
        type="text"
        :placeholder="$t('globalSearch.placeholder')"
        :aria-label="$t('globalSearch.ariaLabel')"
        @focus="open = true"
        @keydown="onKeydown"
      />
      <button v-if="q" class="x" :aria-label="$t('globalSearch.clear')" @click="q = ''; close()">
        <X :size="12" />
      </button>
    </span>

    <template v-if="open && q.trim()">
      <div class="backdrop" @click="close" />
      <div class="results" role="listbox">
        <p v-if="q.trim().length < MIN" class="note">{{ $t('globalSearch.keepTyping', { n: MIN }) }}</p>
        <p v-else-if="loading" class="note"><LoaderCircle :size="13" class="spin" /> {{ $t('globalSearch.searching') }}</p>
        <p v-else-if="error" class="note err">{{ error }}</p>
        <p v-else-if="!flat.length" class="note">{{ $t('globalSearch.noResults', { q: q.trim() }) }}</p>

        <template v-else>
          <div v-for="g in groups" :key="g.kind" class="group">
            <div class="ghead">{{ $t(`globalSearch.kind_${g.kind}`) }} <span class="n">{{ g.items.length }}</span></div>
            <button
              v-for="h in g.items"
              :key="`${h.kind}-${h.id}`"
              class="hit"
              :class="{ on: indice(h) === cursor }"
              role="option"
              :aria-selected="indice(h) === cursor"
              @click="vai(h)"
              @mouseenter="cursor = indice(h)"
            >
              <component :is="ICON[h.kind]" :size="14" class="hicon" />
              <span class="hname">{{ h.name }}</span>
              <!-- dove si trova: due risultati omonimi si distinguono solo così -->
              <span v-if="h.project_name" class="hwhere"><Folder :size="11" /> {{ h.project_name }}</span>
              <span v-if="h.detail" class="hdetail">{{ h.detail }}</span>
            </button>
          </div>
          <p v-if="total > mostrati" class="note more">{{ $t('globalSearch.andMore', { n: total - mostrati }) }}</p>
        </template>
      </div>
    </template>
  </div>
</template>

<style scoped>
.gs { position: relative; flex-shrink: 0; }
.box {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  padding: 5px 9px;
  border: 1px solid var(--control-border);
  border-radius: 8px;
  background: var(--panel-2);
  color: var(--muted);
}
.box.active { border-color: var(--accent); }
.box input {
  border: none;
  background: transparent;
  outline: none;
  color: var(--text);
  width: 190px;
  font-size: 13px;
}
.box .x { padding: 1px 5px; min-height: 0; }

.backdrop { position: fixed; inset: 0; z-index: 190; }
/* `results`, non `panel`: `.panel` è già definita globalmente in main.css come
   `position: relative` (e la usa FlowEditor). A parità di specificità vincerebbe
   l'ordine di cascata, cioè il posizionamento di questo riquadro dipenderebbe da
   quale foglio arriva dopo. Un nome proprio costa nulla e toglie il dubbio. */
.results {
  position: absolute;
  right: 0;
  top: calc(100% + 8px);
  z-index: 191;
  width: 420px;
  max-width: 92vw;
  max-height: 60vh;
  overflow-y: auto;
  padding: 6px;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  box-shadow: var(--shadow-2);
}
.note { display: flex; align-items: center; gap: 7px; margin: 0; padding: 10px 10px; font-size: 12.5px; color: var(--muted); }
.note.err { color: var(--danger); }
.note.more { border-top: 1px solid var(--border-soft); margin-top: 4px; }
.group + .group { margin-top: 4px; }
.ghead {
  padding: 6px 10px 3px;
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--muted);
}
.ghead .n { font-variant-numeric: tabular-nums; opacity: 0.7; }
.hit {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  text-align: left;
  padding: 7px 10px;
  border: none;
  background: transparent;
  border-radius: 7px;
  font-size: 13px;
  color: var(--text);
}
.hit:hover, .hit.on { background: var(--row-hover); box-shadow: none; }
.hicon { color: var(--muted); flex-shrink: 0; }
.hname { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.hwhere {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  margin-left: auto;
  flex-shrink: 0;
  font-size: 11.5px;
  color: var(--muted);
}
.hdetail { font-size: 11.5px; color: var(--muted); flex-shrink: 0; }
/* `.spin` NON si ridefinisce: è già globale in main.css:230 con le sue keyframes */

@media (max-width: 900px) {
  .box input { width: 110px; }
  .results { width: 320px; }
}
</style>
