<script setup lang="ts">
// The components' palette: drag an item onto the canvas to create the node. The type
// travels in the dataTransfer (application/tabularia), read by FlowEditor's drop handler.
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { ChevronRight, Container, FileCode2, GripVertical, HardDriveDownload, Sigma, Sparkles, StickyNote, Search } from 'lucide-vue-next'
import { opMeta, SOURCE_META } from '~/composables/useOpIcons'
import { statHintKey, statIsOp, statNameKey, statPalette } from '~/composables/statTests'
import { MODEL_COLOR, bayesUsable, modelGroupKey, modelPalette, modelShownGroups, predictAvailable } from '~/composables/modelCatalog'
import { modelIconOf } from '~/composables/modelIcons'
import { useModelGroups } from '~/composables/useModelGroups'

const { t } = useI18n()
// a statistics item chosen without dragging (Enter or Space on it): the editor adds the
// step after the selected node
const emit = defineEmits<{ (e: 'add', opType: string): void }>()

// readOnly: observer — transformations only, no sources, controls, outputs
// scripting: the person may write Script nodes (an environment is there for them)
// containers: the person may use Container nodes (a registry is granted to them)
// scriptingOff / containersOff: an installation without Kubernetes (Docker Compose): the
// two nodes are shown greyed out, not dragged, with the reason
// mlflowConnections: how many `mlflow` connections the person may use (Predict needs one)
const props = defineProps<{
  operations: string[]; readOnly?: boolean; scripting?: boolean; containers?: boolean
  scriptingOff?: boolean; containersOff?: boolean; mlflowConnections?: number
}>()

// ── search: filters the palette's items by label (or operation name) ──
const query = ref('')
const match = (...texts: string[]) => {
  const q = query.value.trim().toLowerCase()
  if (!q) return true
  return texts.some((t) => (t ?? '').toLowerCase().includes(q))
}

// foreach has a dedicated item in the "Control" group (it is a container, not a normal
// operation of the chain); the hypothesis tests have their own group, below
const transformOps = computed(() =>
  props.operations.filter((o) => o !== 'foreach' && !statIsOp(o) && match(t(opMeta(o).label), o)),
)

// ── Statistics: the hypothesis tests, by family ──────────────────────────────
// One group for all the tests the engine offers, closed until the person opens it (there
// are many, and most flows use none) and remembered in this browser. Its title is a button.
// A search opens it by itself and shows the tests whose name, hint or family matches.
const STAT_OPEN_KEY = 'tabularia.palette.statistics'
const statOpenSaved = ref(false)
onMounted(() => {
  try { statOpenSaved.value = localStorage.getItem(STAT_OPEN_KEY) === 'open' } catch { /* no storage: closed */ }
})
const searching = computed(() => !!query.value.trim())
const statGroups = computed(() => statPalette(props.operations, query.value, t))
const statCount = computed(() => statGroups.value.reduce((n, g) => n + g.tests.length, 0))
const statOpen = computed(() => searching.value || statOpenSaved.value)
function toggleStat() {
  statOpenSaved.value = !statOpenSaved.value
  try { localStorage.setItem(STAT_OPEN_KEY, statOpenSaved.value ? 'open' : 'closed') } catch { /* not remembered */ }
}
// ── Models: the statistical models, by group ────────────────────────────────
// A group of its own, after Statistics, closed until opened and remembered in this browser, with
// one subtitle per group the administrator turned on and this person may use; a group not turned on
// does not appear, and none turned on hides the whole thing. On Compose (no Kubernetes) the groups
// are shown greyed out, not dragged, with the reason. Predict is above the subtitles. Enter,
// Space or a click on an item adds it after the selected node, as on a statistics item: nothing
// needs a pointer (a screen reader or voice control clicks), nor a key a laptop may lack.
const MODEL_OPEN_KEY = 'tabularia.palette.models'
const modelOpenSaved = ref(false)
onMounted(() => {
  try { modelOpenSaved.value = localStorage.getItem(MODEL_OPEN_KEY) === 'open' } catch { /* no storage: closed */ }
})
const modelGroups = useModelGroups()
onMounted(() => { modelGroups.load() })
const modelsOff = computed(() => modelGroups.state.value?.disabled_reason === 'kubernetes')
const modelShown = computed(() => modelShownGroups(modelGroups.state.value))
const modelEntries = computed(() => (props.readOnly ? [] : modelPalette(modelShown.value, query.value, t)))
const showPredict = computed(() => !props.readOnly && predictAvailable(modelGroups.state.value, props.mlflowConnections ?? 0)
  && match(t('models.predict.title'), 'predict', 'prediction', 'mlflow'))
// «Bayesian model» is an item of its own (its group is left out of the catalogue's entries): offered to whoever may use the group
// `bayes`, and greyed out on Compose with the reason, as the other models
const showBayes = computed(() => !props.readOnly && (bayesUsable(modelGroups.state.value) || modelsOff.value)
  && match(t('models.bayes_model.name'), t('models.groups.bayes'), 'bayes', 'bayesian', 'stan', 'posterior'))
const modelCount = computed(() => modelEntries.value.reduce((n, g) => n + g.items.length, 0) + (showPredict.value ? 1 : 0) + (showBayes.value ? 1 : 0))
const showModels = computed(() => modelEntries.value.length > 0 || showPredict.value || showBayes.value)
const modelOpen = computed(() => searching.value || modelOpenSaved.value)
function toggleModels() {
  modelOpenSaved.value = !modelOpenSaved.value
  try { localStorage.setItem(MODEL_OPEN_KEY, modelOpenSaved.value ? 'open' : 'closed') } catch { /* not remembered */ }
}
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
    !statGroups.value.length &&
    !showModels.value &&
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

    <template v-if="statGroups.length">
      <!-- while a search has text the group is open by itself: a title, as the others -->
      <div v-if="searching" class="group-title">{{ $t('opSidebar.statisticsGroup') }}</div>
      <button
        v-else
        type="button"
        class="group-title group-toggle"
        :aria-expanded="statOpen"
        aria-controls="palette-statistics"
        @click="toggleStat"
      >
        <ChevronRight :size="12" class="chev" :class="{ open: statOpen }" aria-hidden="true" />
        <span>{{ $t('opSidebar.statisticsGroup') }}</span>
        <span class="group-count" aria-hidden="true">{{ statCount }}</span>
      </button>
      <ExpandBody><div v-show="statOpen" id="palette-statistics" class="stat-group" role="group" :aria-label="$t('opSidebar.statisticsGroup')">
        <div v-for="g in statGroups" :key="g.family" class="stat-family" role="group" :aria-labelledby="`palette-stat-${g.family}`">
          <div :id="`palette-stat-${g.family}`" class="family-title">{{ $t(`stat.families.${g.family}`) }}</div>
          <div
            v-for="test in g.tests"
            :key="test.operation"
            class="item"
            draggable="true"
            role="button"
            tabindex="0"
            :title="$t('opSidebar.statisticsItemTitle', { hint: $t(statHintKey(test)) })"
            :style="{ '--item-color': opMeta(test.operation).color }"
            @dragstart="onDragStart($event, `op:${test.operation}`)"
            @click="emit('add', test.operation)"
            @keydown.enter.prevent="emit('add', test.operation)"
            @keydown.space.prevent="emit('add', test.operation)"
          >
            <component :is="opMeta(test.operation).icon" :size="15" class="item-icon" aria-hidden="true" />
            <span>{{ $t(statNameKey(test)) }}</span>
            <GripVertical :size="13" class="grip" aria-hidden="true" />
          </div>
        </div>
      </div></ExpandBody>
    </template>

    <template v-if="showModels">
      <!-- while a search has text the group is open by itself: a title, as the others -->
      <div v-if="searching" class="group-title">{{ $t('models.palette.group') }}</div>
      <button
        v-else
        type="button"
        class="group-title group-toggle"
        :aria-expanded="modelOpen"
        aria-controls="palette-models"
        @click="toggleModels"
      >
        <ChevronRight :size="12" class="chev" :class="{ open: modelOpen }" aria-hidden="true" />
        <span>{{ $t('models.palette.group') }}</span>
        <span class="group-count" aria-hidden="true">{{ modelCount }}</span>
      </button>
      <ExpandBody><div v-show="modelOpen" id="palette-models" class="stat-group" role="group" :aria-label="$t('models.palette.group')">
        <div
          v-if="showPredict"
          class="item"
          :class="{ off: modelsOff }"
          :draggable="!modelsOff"
          role="button"
          tabindex="0"
          :title="modelsOff ? $t('scripting.kubernetesOnly') : $t('models.palette.itemTitle', { hint: $t('models.predict.hint') })"
          :aria-disabled="modelsOff ? 'true' : undefined"
          :style="{ '--item-color': MODEL_COLOR }"
          @dragstart="!modelsOff && onDragStart($event, 'model_predict')"
          @click="!modelsOff && emit('add', 'model_predict')"
          @keydown.enter.prevent="!modelsOff && emit('add', 'model_predict')"
          @keydown.space.prevent="!modelsOff && emit('add', 'model_predict')"
        >
          <Sparkles :size="15" class="item-icon" aria-hidden="true" />
          <span>{{ $t('models.predict.title') }}</span>
          <GripVertical :size="13" class="grip" aria-hidden="true" />
        </div>
        <div v-for="g in modelEntries" :key="g.group" class="stat-family" role="group" :aria-labelledby="`palette-model-${g.group}`">
          <div :id="`palette-model-${g.group}`" class="family-title">{{ $t(modelGroupKey(g.group)) }}</div>
          <div
            v-for="item in g.items"
            :key="item.kind"
            class="item"
            :class="{ off: modelsOff, preset: !!item.preset }"
            :draggable="!modelsOff"
            role="button"
            tabindex="0"
            :title="modelsOff ? $t('scripting.kubernetesOnly') : $t('models.palette.itemTitle', { hint: $t(item.hintKey) })"
            :aria-disabled="modelsOff ? 'true' : undefined"
            :style="{ '--item-color': MODEL_COLOR }"
            @dragstart="!modelsOff && onDragStart($event, item.kind)"
            @click="!modelsOff && emit('add', item.kind)"
            @keydown.enter.prevent="!modelsOff && emit('add', item.kind)"
            @keydown.space.prevent="!modelsOff && emit('add', item.kind)"
          >
            <component :is="modelIconOf(item.model)" :size="15" class="item-icon" aria-hidden="true" />
            <span>{{ $t(item.nameKey) }}</span>
            <GripVertical :size="13" class="grip" aria-hidden="true" />
          </div>
        </div>
        <div v-if="showBayes" class="stat-family" role="group" aria-labelledby="palette-model-bayes">
          <div id="palette-model-bayes" class="family-title">{{ $t(modelGroupKey('bayes')) }}</div>
          <div
            class="item"
            :class="{ off: modelsOff }"
            :draggable="!modelsOff"
            role="button"
            tabindex="0"
            :title="modelsOff ? $t('scripting.kubernetesOnly') : $t('models.palette.itemTitle', { hint: $t('models.bayes_model.hint') })"
            :aria-disabled="modelsOff ? 'true' : undefined"
            :style="{ '--item-color': MODEL_COLOR }"
            @dragstart="!modelsOff && onDragStart($event, 'bayes_model')"
            @click="!modelsOff && emit('add', 'bayes_model')"
            @keydown.enter.prevent="!modelsOff && emit('add', 'bayes_model')"
            @keydown.space.prevent="!modelsOff && emit('add', 'bayes_model')"
          >
            <Sigma :size="15" class="item-icon" aria-hidden="true" />
            <span>{{ $t('models.bayes_model.name') }}</span>
            <GripVertical :size="13" class="grip" aria-hidden="true" />
          </div>
        </div>
      </div></ExpandBody>
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
.item.preset { padding-left: 22px; }
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
/* Statistics: the title is the button that opens the group, in the titles' own look */
.group-toggle {
  display: flex;
  align-items: center;
  gap: 5px;
  width: 100%;
  background: none;
  border: none;
  border-radius: 6px;
  font: inherit;
  font-size: 10px;
  text-align: left;
  justify-content: flex-start;
  cursor: pointer;
}
.group-toggle:hover { color: var(--text); background: none; box-shadow: none; }
.group-toggle:active { transform: none; }
.group-toggle .chev { flex-shrink: 0; transition: transform var(--dur-hover) var(--ease-out); }
.group-toggle .chev.open { transform: rotate(90deg); }
.group-count { margin-left: auto; font-variant-numeric: tabular-nums; letter-spacing: 0; }
@media (prefers-reduced-motion: reduce) {
  .group-toggle .chev { transition: none; }
}
.stat-group, .stat-family { display: flex; flex-direction: column; gap: 3px; }
.family-title {
  font-size: 11.5px;
  color: var(--muted);
  padding: 6px 6px 2px;
}
.item:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
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
  transition: background var(--dur-tint), border-color var(--dur-tint);
}
.item { position: relative; }
/* the category's colour as a thin bar on the left that WIDENS under the pointer: the item
   says what kind of step it is, and that it can be picked up */
.item::before {
  content: "";
  position: absolute;
  left: -1px;
  top: 7px;
  bottom: 7px;
  width: 4px;
  border-radius: 0 2px 2px 0;
  background: var(--item-color);
  transform: scaleX(0.5);
  transform-origin: left center;
  opacity: 0;
  transition: transform var(--dur-hover) var(--ease-out), opacity var(--dur-hover);
  pointer-events: none;
}
.item:hover {
  background: var(--panel-2);
  border-color: var(--border);
}
@media (hover: hover) and (pointer: fine) {
  .item:hover::before { opacity: 1; transform: scaleX(1); }
}
@media (prefers-reduced-motion: reduce) {
  .item::before { transform: none; }
}
.item:active { cursor: grabbing; }
.item-icon { color: var(--item-color); flex-shrink: 0; }
.item span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.grip { margin-left: auto; color: var(--border); flex-shrink: 0; }
.item:hover .grip { color: var(--muted); }
.empty { font-size: 12px; padding: 0 6px; }
</style>
