<script setup lang="ts">
// The components' palette: drag an item onto the canvas to create the node. The type
// travels in the dataTransfer (application/tabularia), read by FlowEditor's drop handler.
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { Container, FileCode2, GripVertical, HardDriveDownload, StickyNote, Search } from 'lucide-vue-next'
import { opMeta, SOURCE_META } from '~/composables/useOpIcons'

const { t } = useI18n()

// readOnly: observer — transformations only, no sources, controls, outputs
// scripting: the person may write Script nodes (an environment is there for them)
// containers: the person may use Container nodes (a registry is granted to them)
// scriptingOff / containersOff: an installation without Kubernetes (Docker Compose): the
// two nodes are shown greyed out, not dragged, with the reason
const props = defineProps<{
  operations: string[]; readOnly?: boolean; scripting?: boolean; containers?: boolean
  scriptingOff?: boolean; containersOff?: boolean
}>()

// ── search: filters the palette's items by label (or operation name) ──
const query = ref('')
const match = (...texts: string[]) => {
  const q = query.value.trim().toLowerCase()
  if (!q) return true
  return texts.some((t) => (t ?? '').toLowerCase().includes(q))
}

// foreach has a dedicated item in the "Control" group (it is a container, not a normal
// operation of the chain)
const transformOps = computed(() =>
  props.operations.filter((o) => o !== 'foreach' && match(t(opMeta(o).label), o)),
)
const hasForeach = computed(() => props.operations.includes('foreach'))

// visibility of the single fixed items and of their groups
const showSource = computed(() => !props.readOnly && match(t(SOURCE_META.label), 'source'))
const showForeach = computed(() => !props.readOnly && hasForeach.value && match(t(opMeta('foreach').label), 'foreach'))
// «Refresh datasource» and «Run flow» are no longer offered: a flow group refreshes
// datasources and runs flows, in steps; the nodes of flows saved before still run
const showOutput = computed(() => !props.readOnly && match(t('opSidebar.outputLabel'), 'output'))
const showComment = computed(() => !props.readOnly && match(t('opSidebar.noteLabel'), 'comment', 'commento'))
const showScript = computed(() => !props.readOnly && (!!props.scripting || !!props.scriptingOff) && match(t('scripting.node.title'), 'script', 'python', 'r'))
const showContainer = computed(() => !props.readOnly && (!!props.containers || !!props.containersOff) && match(t('containers.node.title'), 'container', 'docker', 'image'))
const showControl = computed(() => showForeach.value || showScript.value || showContainer.value)
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
      <input v-model="query" type="text" class="search-input" :placeholder="$t('opSidebar.searchPlaceholder')" :aria-label="$t('opSidebar.searchPlaceholder')" />
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
        <span>{{ $t(SOURCE_META.label) }}</span>
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
        <span>{{ $t(opMeta(op).label) }}</span>
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
        <span>{{ $t(opMeta('foreach').label) }}</span>
        <GripVertical :size="13" class="grip" />
      </div>
      <div
        v-if="showScript"
        class="item"
        :class="{ off: scriptingOff }"
        :draggable="!scriptingOff"
        :title="scriptingOff ? $t('scripting.kubernetesOnly') : undefined"
        :aria-disabled="scriptingOff ? 'true' : undefined"
        :style="{ '--item-color': '#34d399' }"
        @dragstart="!scriptingOff && onDragStart($event, 'script')"
      >
        <FileCode2 :size="15" class="item-icon" />
        <span>{{ $t('scripting.node.title') }}</span>
        <GripVertical :size="13" class="grip" />
      </div>
      <div
        v-if="showContainer"
        class="item"
        :class="{ off: containersOff }"
        :draggable="!containersOff"
        :title="containersOff ? $t('scripting.kubernetesOnly') : undefined"
        :aria-disabled="containersOff ? 'true' : undefined"
        :style="{ '--item-color': '#818cf8' }"
        @dragstart="!containersOff && onDragStart($event, 'container')"
      >
        <Container :size="15" class="item-icon" />
        <span>{{ $t('containers.node.title') }}</span>
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
.item.off { opacity: .45; cursor: not-allowed; }
.opsidebar {
  height: 100%;
  overflow-y: auto;
  padding: 10px 8px;
  display: flex;
  flex-direction: column;
  gap: 3px;
}
/* search bar fixed at the top while the list scrolls */
.searchbar {
  position: sticky;
  top: -10px; /* compensates the aside's padding */
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
