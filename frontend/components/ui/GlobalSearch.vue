<script setup lang="ts">
// General search: a box in the top bar that crosses ALL the folders, not only the open
// one. It answers the real question of whoever looks for «margine» without knowing, and
// without needing to know, whether it is a flow or a view.
//
// SECURITY — the permission filter is the GATEWAY's, not this one's. `/search` applies
// three different sets: the folders on the navigable set, the contents on the readable
// one, the connections on the ORTHOGONAL CONNECT capability. This component does not
// filter and must not filter anything: it shows what the server has decided to show
// already. If the filter lived here, opening the browser's network tab would be enough
// to see what cannot be seen.
import { computed, ref, watch, onUnmounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { Search, X, Folder, Workflow, Database, Bookmark, Plug, LoaderCircle, Rows3, Lock, FileCode2, DatabaseSearch, NotebookPen } from 'lucide-vue-next'
import { errMessage } from '~/composables/useApi'
import { useFormat } from '~/composables/useFormat'
import { useSearch, searchHitTarget, type SearchHit, type SearchKind } from '~/composables/useSearch'

const { t } = useI18n()
const api = useSearch()
const fmt = useFormat()

const MIN = 2        // under 2 characters every search returns half the catalog
const DEBOUNCE = 250 // while typing, it does not query at every key
const LIMIT = 20

const q = ref('')
const open = ref(false)
const loading = ref(false)
const error = ref('')
const hits = ref<SearchHit[]>([])
const total = ref(0)
const cursor = ref(-1) // row highlighted by the keyboard

const ICON: Record<SearchKind, any> = {
  folder: Folder, flow: Workflow, group: Rows3, datasource: Database, view: Bookmark, script: FileCode2, sql: DatabaseSearch, notebook: NotebookPen, connection: Plug,
}
const ORDER: SearchKind[] = ['folder', 'flow', 'group', 'datasource', 'view', 'script', 'sql', 'notebook', 'connection']

/**
 * Grouped by kind: five short lists read better than twenty mixed rows, and the kind is
 * the first thing one needs to know about a result.
 */
const groups = computed(() =>
  ORDER
    .map((kind) => ({ kind, items: hits.value.filter((h) => h.kind === kind) }))
    .filter((g) => g.items.length),
)
/** Flattened in the order it appears: the keyboard moves on this. */
const flat = computed(() => groups.value.flatMap((g) => g.items))

let timer: ReturnType<typeof setTimeout> | null = null
let seq = 0 // drops the answers that arrive out of order

async function run(term: string) {
  const mine = ++seq
  loading.value = true
  error.value = ''
  try {
    const res = await api.query(term, { limit: LIMIT })
    if (mine !== seq) return // a newer request has answered already
    hits.value = res.items
    total.value = res.total
    cursor.value = res.items.length ? 0 : -1
  } catch (e) {
    if (mine !== seq) return
    error.value = errMessage(e)
    hits.value = []
    total.value = 0
  } finally {
    if (mine === seq) loading.value = false
  }
}

watch(q, (val) => {
  if (timer) clearTimeout(timer)
  const term = val.trim()
  open.value = true
  if (term.length < MIN) {
    hits.value = []
    total.value = 0
    loading.value = false
    return
  }
  timer = setTimeout(() => run(term), DEBOUNCE)
})

onUnmounted(() => { if (timer) clearTimeout(timer) })

function close() {
  open.value = false
  cursor.value = -1
}

function go(hit: SearchHit) {
  if (hit.locked_reason) return // a flow this person cannot open: it stays where it is
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
    go(flat.value[cursor.value])
  }
}

const index = (hit: SearchHit) => flat.value.indexOf(hit)
const shown = computed(() => flat.value.length)
</script>

<template>
  <div class="gs">
    <!-- a <label>: clicking the lens leads into the field even when closed (phone) -->
    <label class="box" :class="{ active: open && q }">
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
    </label>

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
              :class="{ on: index(h) === cursor, blocked: !!h.locked_reason }"
              role="option"
              :aria-selected="index(h) === cursor"
              :aria-disabled="h.locked_reason ? 'true' : undefined"
              :title="h.locked_reason || undefined"
              @click="go(h)"
              @mouseenter="cursor = index(h)"
            >
              <component :is="ICON[h.kind]" :size="14" class="hicon" />
              <span class="hname">{{ h.name }}</span>
              <Lock v-if="h.locked_reason" :size="12" class="lock" :aria-label="$t('projectBrowser.lockedFlow')" />
              <!-- where it is: two results with the same name are told apart only so -->
              <span v-if="h.project_name" class="hwhere"><Folder :size="11" /> {{ h.project_name }}</span>
              <span v-if="h.rows != null" class="hdetail">{{ $t('globalSearch.rows', { n: fmt.count(h.rows) }, h.rows) }}</span>
              <span v-else-if="h.detail" class="hdetail">{{ h.detail }}</span>
            </button>
          </div>
          <p v-if="total > shown" class="note more">{{ $t('globalSearch.andMore', { n: total - shown }) }}</p>
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
.box.active, .box:focus-within { border-color: var(--accent); }
.box input {
  border: none;
  background: transparent;
  outline: none;
  color: var(--text);
  width: 190px;
  font-size: 13px;
}
/* The navigation takes precedence in the bar: under 1600px the field narrows and widens
   again while in use; on the phone the lens stays, and the field opens on click or with
   Tab. Without a transition: animating the width would recompute the whole bar's layout
   at every frame. */
@media (max-width: 1600px) {
  .box input { width: 140px; } /* 20px are padding: the placeholder wants ~115 */
  .box:focus-within input { width: 220px; }
}
@media (max-width: 760px) {
  .box { cursor: pointer; }
  .box input { width: 0; padding-inline: 0; }
  .box:focus-within input { width: 46vw; padding-inline: 4px; }
}
.box .x { padding: 1px 5px; min-height: 0; }

.backdrop { position: fixed; inset: 0; z-index: 190; }
/* `results`, not `panel`: `.panel` is defined globally in main.css already as `position:
   relative` (and FlowEditor uses it). At equal specificity the cascade order would win,
   that is this box's positioning would depend on which sheet comes later. A name of its
   own costs nothing and removes the doubt. */
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
.hit.blocked { opacity: 0.45; cursor: not-allowed; }
.hit.blocked:hover, .hit.blocked.on { background: none; }
.lock { flex: none; color: var(--muted); }
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
/* `.spin` is NOT redefined: it is global in main.css:230 already with its keyframes */

@media (max-width: 900px) {
  .results { width: 320px; }
}
</style>
