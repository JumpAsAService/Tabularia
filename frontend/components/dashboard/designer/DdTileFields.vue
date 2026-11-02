<script setup lang="ts">
// What a tile has that a report's item does not: its place on the grid, in cells, and whether a
// click on it filters the others. KPIs, texts and images have their title here (a chart and a table
// have one of their own). Worked out by the grid's rules, as a drag is.
import { computed } from 'vue'
import RdNum from '~/components/report/designer/RdNum.vue'
import RdSection from '~/components/report/designer/RdSection.vue'
import { GRID_COLUMNS, MAX_GRID_ROW, MAX_TILE_ROWS, MIN_TILE, pushed, type GridBox } from '~/composables/dashboardGrid'
import { placeTile, tilesOf, type DTile } from '~/composables/dashboardDesignModel'
import { useDesign } from '~/composables/reportDesignStore'

const props = defineProps<{ item: Record<string, any> }>()
const store = useDesign()
const tile = computed(() => props.item as DTile)
const least = computed(() => MIN_TILE[tile.value.type] ?? [1, 1])
const filterable = computed(() => ['chart', 'table'].includes(tile.value.type))
const hasOwnTitle = computed(() => !filterable.value)
const gridProblem = computed(() => store.problemAt(tile.value.id, 'grid') ?? store.liveAt(tile.value.id, 'grid'))
const titleProblem = computed(() => store.problemAt(tile.value.id, 'title') ?? store.liveAt(tile.value.id, 'title'))

function setBox(change: Partial<GridBox>) {
  const now = { ...tile.value.grid, ...change }
  now.w = Math.max(least.value[0], Math.min(now.w, GRID_COLUMNS))
  now.h = Math.max(least.value[1], Math.min(now.h, MAX_TILE_ROWS))
  now.x = Math.max(0, Math.min(now.x, GRID_COLUMNS - now.w))
  now.y = Math.max(0, Math.min(now.y, MAX_GRID_ROW))
  placeTile(store.doc.value, pushed(tilesOf(store.doc.value), tile.value.id, now))
}
function setTitle(e: Event) {
  const v = (e.target as HTMLInputElement).value
  if (v) tile.value.title = v
  else delete tile.value.title
  store.clearProblem(tile.value.id, 'title')
}
function setFilters(e: Event) {
  // on is what a tile does when it says nothing: nothing is written for it
  if ((e.target as HTMLInputElement).checked) delete tile.value.filters_others
  else tile.value.filters_others = false
}
</script>

<template>
  <RdSection :title="$t('dashboardDesigner.onTheGrid')" skey="tile-grid" :bad="!!store.problemAt(tile.id, 'grid')">
    <div class="ddt-grid" data-section="grid">
      <label class="rp-f"><span>{{ $t('dashboardDesigner.column') }}</span><RdNum :model-value="tile.grid.x + 1" :decimals="0" :step="1" :min="1" :max="GRID_COLUMNS" @update:model-value="setBox({ x: ($event ?? 1) - 1 })" /></label>
      <label class="rp-f"><span>{{ $t('dashboardDesigner.row') }}</span><RdNum :model-value="tile.grid.y + 1" :decimals="0" :step="1" :min="1" :max="MAX_GRID_ROW + 1" @update:model-value="setBox({ y: ($event ?? 1) - 1 })" /></label>
      <label class="rp-f"><span>{{ $t('dashboardDesigner.width') }}</span><RdNum :model-value="tile.grid.w" :decimals="0" :step="1" :min="least[0]" :max="GRID_COLUMNS" @update:model-value="setBox({ w: $event ?? least[0] })" /></label>
      <label class="rp-f"><span>{{ $t('dashboardDesigner.height') }}</span><RdNum :model-value="tile.grid.h" :decimals="0" :step="1" :min="least[1]" :max="MAX_TILE_ROWS" @update:model-value="setBox({ h: $event ?? least[1] })" /></label>
    </div>
    <p class="rp-help">{{ $t('dashboardDesigner.gridHelp', { w: least[0], h: least[1] }) }}</p>
    <p v-if="gridProblem" class="rp-err" role="alert">{{ gridProblem.message }}</p>
  </RdSection>
  <RdSection v-if="hasOwnTitle || filterable" :title="$t('dashboardDesigner.onTheScreen')" skey="tile-screen">
    <label v-if="hasOwnTitle" class="rp-f">
      <span>{{ $t('dashboardDesigner.title') }}</span>
      <input type="text" data-field="tile-title" maxlength="200" :value="tile.title ?? ''" @input="setTitle" />
    </label>
    <p v-if="titleProblem" class="rp-err" role="alert">{{ titleProblem.message }}</p>
    <template v-if="filterable">
      <label class="rp-check">
        <input type="checkbox" data-field="filters-others" :checked="tile.filters_others !== false" @change="setFilters" />
        {{ $t('dashboardDesigner.filtersOthers') }}
      </label>
      <p class="rp-help">{{ $t('dashboardDesigner.filtersOthersHelp') }}</p>
    </template>
  </RdSection>
</template>

<style scoped src="~/assets/reportprops.css" />
<style scoped>
.ddt-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
</style>
