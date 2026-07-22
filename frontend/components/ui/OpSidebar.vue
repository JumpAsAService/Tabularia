<script setup lang="ts">
// Palette dei componenti: trascina una voce sul canvas per creare il nodo.
// Il tipo viaggia nel dataTransfer (application/tabularia), letto dal drop
// handler di FlowEditor.
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { GripVertical, HardDriveDownload, RefreshCw, PlayCircle, StickyNote, Search } from 'lucide-vue-next'
import { opMeta, SOURCE_META } from '~/composables/useOpIcons'

const { t } = useI18n()

const props = defineProps<{ operations: string[] }>()

// ── ricerca: filtra le voci della palette per etichetta (o nome operazione) ──
const query = ref('')
const match = (...texts: string[]) => {
  const q = query.value.trim().toLowerCase()
  if (!q) return true
  return texts.some((t) => (t ?? '').toLowerCase().includes(q))
}

// foreach ha una voce dedicata nel gruppo "Controllo" (è un container, non
// un'operazione normale della catena)
const transformOps = computed(() =>
  props.operations.filter((o) => o !== 'foreach' && match(opMeta(o).label || o, o)),
)
const hasForeach = computed(() => props.operations.includes('foreach'))

// visibilità delle singole voci fisse e dei rispettivi gruppi
const showSource = computed(() => match(SOURCE_META.label, 'source'))
const showForeach = computed(() => hasForeach.value && match(opMeta('foreach').label || 'foreach', 'foreach'))
const showRefresh = computed(() => match(t('opSidebar.refreshDatasourceLabel'), 'refresh'))
const showRunflow = computed(() => match(t('opSidebar.runFlowLabel'), 'runflow'))
const showOutput = computed(() => match(t('opSidebar.outputLabel'), 'output'))
const showComment = computed(() => match(t('opSidebar.noteLabel'), 'comment', 'commento'))
const showControl = computed(() => showForeach.value || showRefresh.value || showRunflow.value)
const noResults = computed(
  () =>
    !showSource.value &&
    !transformOps.value.length &&
    !showControl.value &&
    !showOutput.value &&
    !showComment.value,
)

function onDragStart(ev: DragEvent, kind: string) {
  ev.dataTransfer?.setData('application/tabularia', kind)
  if (ev.dataTransfer) ev.dataTransfer.effectAllowed = 'move'
}
</script>

<template>
  <aside class="opsidebar">
    <div class="searchbar">
      <Search :size="14" class="search-icon" />
      <input v-model="query" type="text" class="search-input" :placeholder="$t('opSidebar.searchPlaceholder')" />
      <button v-if="query" class="search-clear" :title="$t('opSidebar.clearSearch')" @click="query = ''">✕</button>
    </div>

    <template v-if="showSource">
      <div class="group-title">{{ $t('opSidebar.sourcesGroup') }}</div>
      <div
        class="item"
        draggable="true"
        :style="{ '--item-color': SOURCE_META.color }"
        @dragstart="onDragStart($event, 'source')"
      >
        <component :is="SOURCE_META.icon" :size="15" class="item-icon" />
        <span>{{ SOURCE_META.label }}</span>
        <GripVertical :size="13" class="grip" />
      </div>
    </template>

    <template v-if="transformOps.length">
      <div class="group-title">{{ $t('opSidebar.transformsGroup') }}</div>
      <div
        v-for="op in transformOps"
        :key="op"
        class="item"
        draggable="true"
        :style="{ '--item-color': opMeta(op).color }"
        @dragstart="onDragStart($event, `op:${op}`)"
      >
        <component :is="opMeta(op).icon" :size="15" class="item-icon" />
        <span>{{ opMeta(op).label || op }}</span>
        <GripVertical :size="13" class="grip" />
      </div>
    </template>

    <template v-if="showControl">
      <div class="group-title">{{ $t('opSidebar.controlGroup') }}</div>
      <div
        v-if="showForeach"
        class="item"
        draggable="true"
        :style="{ '--item-color': opMeta('foreach').color }"
        @dragstart="onDragStart($event, 'op:foreach')"
      >
        <component :is="opMeta('foreach').icon" :size="15" class="item-icon" />
        <span>{{ opMeta('foreach').label }}</span>
        <GripVertical :size="13" class="grip" />
      </div>
      <div
        v-if="showRefresh"
        class="item"
        draggable="true"
        :style="{ '--item-color': '#38bdf8' }"
        @dragstart="onDragStart($event, 'refresh')"
      >
        <RefreshCw :size="15" class="item-icon" />
        <span>{{ $t('opSidebar.refreshDatasourceLabel') }}</span>
        <GripVertical :size="13" class="grip" />
      </div>
      <div
        v-if="showRunflow"
        class="item"
        draggable="true"
        :style="{ '--item-color': '#c084fc' }"
        @dragstart="onDragStart($event, 'runflow')"
      >
        <PlayCircle :size="15" class="item-icon" />
        <span>{{ $t('opSidebar.runFlowLabel') }}</span>
        <GripVertical :size="13" class="grip" />
      </div>
    </template>

    <template v-if="showOutput">
      <div class="group-title">{{ $t('opSidebar.outputGroup') }}</div>
      <div
        class="item"
        draggable="true"
        :style="{ '--item-color': '#fbbf24' }"
        @dragstart="onDragStart($event, 'output')"
      >
        <HardDriveDownload :size="15" class="item-icon" />
        <span>{{ $t('opSidebar.outputLabel') }}</span>
        <GripVertical :size="13" class="grip" />
      </div>
    </template>

    <template v-if="showComment">
      <div class="group-title">{{ $t('opSidebar.annotationsGroup') }}</div>
      <div
        class="item"
        draggable="true"
        :style="{ '--item-color': '#fbbf24' }"
        @dragstart="onDragStart($event, 'comment')"
      >
        <StickyNote :size="15" class="item-icon" />
        <span>{{ $t('opSidebar.noteLabel') }}</span>
        <GripVertical :size="13" class="grip" />
      </div>
    </template>

    <p v-if="!operations.length" class="muted empty">{{ $t('opSidebar.noOperationsLoaded') }}</p>
    <p v-else-if="noResults" class="muted empty">{{ $t('opSidebar.noResultsFor', { query }) }}</p>
  </aside>
</template>

<style scoped>
.opsidebar {
  height: 100%;
  overflow-y: auto;
  padding: 10px 8px;
  display: flex;
  flex-direction: column;
  gap: 3px;
}
/* barra di ricerca fissa in cima mentre la lista scorre */
.searchbar {
  position: sticky;
  top: -10px; /* compensa il padding dell'aside */
  z-index: 1;
  display: flex;
  align-items: center;
  gap: 6px;
  margin: -10px -8px 4px;
  padding: 10px 10px 8px;
  background: var(--panel);
  border-bottom: 1px solid var(--border);
}
.search-icon { color: var(--muted); flex-shrink: 0; }
.search-input {
  flex: 1;
  min-width: 0;
  background: var(--bg-soft);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 6px 8px;
  font: inherit;
  font-size: 13px;
  color: var(--text);
  outline: none;
}
.search-input:focus { border-color: var(--accent); }
.search-input::placeholder { color: var(--muted); }
.search-clear {
  flex-shrink: 0;
  background: none;
  border: none;
  color: var(--muted);
  cursor: pointer;
  font-size: 12px;
  padding: 2px 4px;
  line-height: 1;
}
.search-clear:hover { color: var(--text); }
.group-title {
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.12em;
  color: var(--muted);
  padding: 8px 6px 4px;
}
.item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 8px;
  border: 1px solid transparent;
  border-radius: 8px;
  font-size: 13px;
  cursor: grab;
  user-select: none;
  transition: background 0.12s, border-color 0.12s, transform 0.12s;
}
.item:hover {
  background: var(--panel-2);
  border-color: var(--border);
  transform: translateX(2px);
}
.item:active { cursor: grabbing; }
.item-icon { color: var(--item-color); flex-shrink: 0; }
.item span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.grip { margin-left: auto; color: var(--border); flex-shrink: 0; }
.item:hover .grip { color: var(--muted); }
.empty { font-size: 12px; padding: 0 6px; }
</style>
