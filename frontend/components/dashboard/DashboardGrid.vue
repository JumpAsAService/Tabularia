<script setup lang="ts">
// The grid: 12 columns on a wide screen, 6 on a medium one, one on a phone
// (composables/dashboardGrid.ts says where each tile goes). The page's order is the reading order,
// so the keyboard and a screen reader go through the tiles as the eye does. The width is the
// window's: the breakpoints are asked of the browser, which says when one is crossed, so
// resizing costs nothing in between.
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import DashboardTile from '~/components/dashboard/DashboardTile.vue'
import type { KindCtx } from '~/composables/chartKinds'
import { GRID_GAP_PX, GRID_ROW_PX, MEDIUM_FROM_PX, WIDE_FROM_PX, columnsOf, gridWidth, placeTiles, type GridWidth } from '~/composables/dashboardGrid'
import type { ClickSide, ClickValue, TileClick } from '~/composables/dashboardClicks'
import type { TileSlots } from '~/composables/dashboardTiles'

const props = defineProps<{ tiles: Record<string, any>[]; slots: TileSlots; ctx: KindCtx; saidAbove?: boolean; clicks?: TileClick[] }>()
const emit = defineEmits<{
  (e: 'retry', id: string): void
  (e: 'sort', id: string, column: number): void
  (e: 'toggle', id: string, key: unknown[], closed: boolean): void
  (e: 'page', id: string, page: number): void
  (e: 'pick', id: string, on: ClickSide, value: ClickValue, add: boolean): void
}>()

const width = ref<GridWidth>(import.meta.client ? gridWidth(window.innerWidth) : 'wide')
let queries: MediaQueryList[] = []
const measure = () => { width.value = gridWidth(window.innerWidth) }
onMounted(() => {
  queries = [WIDE_FROM_PX, MEDIUM_FROM_PX].map((px) => window.matchMedia(`(min-width: ${px}px)`))
  queries.forEach((q) => q.addEventListener('change', measure))
  measure()
})
onBeforeUnmount(() => queries.forEach((q) => q.removeEventListener('change', measure)))

const byId = computed(() => new Map(props.tiles.map((t) => [t.id as string, t])))
const hidden = computed(() => new Set(Object.entries(props.slots)
  .filter(([, slot]) => slot.answer && !('error' in slot.answer) && slot.answer.hidden).map(([id]) => id)))
const placed = computed(() => placeTiles(props.tiles as any, width.value, hidden.value))
const gridStyle = computed(() => ({
  gridTemplateColumns: `repeat(${columnsOf(width.value)}, minmax(0, 1fr))`,
  gridAutoRows: `${GRID_ROW_PX}px`,
  gap: `${GRID_GAP_PX}px`,
}))
</script>

<template>
  <div class="board" :style="gridStyle">
    <DashboardTile
      v-for="p in placed"
      :key="p.id"
      :tile="byId.get(p.id)!"
      :entry="slots[p.id]"
      :ctx="ctx"
      :said-above="saidAbove"
      :clicks="clicks"
      :style="{ gridColumn: `${p.x + 1} / span ${p.w}`, gridRow: `${p.y + 1} / span ${p.h}` }"
      @retry="emit('retry', p.id)"
      @sort="(c: number) => emit('sort', p.id, c)"
      @toggle="(k: unknown[], closed: boolean) => emit('toggle', p.id, k, closed)"
      @page="(n: number) => emit('page', p.id, n)"
      @pick="(on: ClickSide, v: ClickValue, add: boolean) => emit('pick', p.id, on, v, add)"
    />
  </div>
</template>

<style scoped>
.board { display: grid; min-width: 0; }
</style>
