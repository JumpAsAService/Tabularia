<script setup lang="ts">
// One tile: its title, and its numbers, or a skeleton on its first load, or its own error with a
// way to try again, or «no access» without a word about what is behind it. New numbers on their way
// for a new question dim the old ones (they are not its answer); read again they stay as they
// are, with their time. Motion only for that state, and not with reduced motion.
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { Clock, LoaderCircle, Lock, RefreshCw, TriangleAlert } from 'lucide-vue-next'
import KpiCard from '~/components/ui/KpiCard.vue'
import TileChart from '~/components/dashboard/TileChart.vue'
import TileTable from '~/components/dashboard/TileTable.vue'
import type { KindCtx } from '~/composables/chartKinds'
import { canFilter, clicksOfTile, type ClickSide, type ClickValue, type TileClick } from '~/composables/dashboardClicks'
import { tileStyleOf } from '~/composables/dashboardStyle'
import { tileStates, type TileSlot } from '~/composables/dashboardTiles'
import { useFormat } from '~/composables/useFormat'
import type { TileNumbers } from '~/composables/useDashboards'

const props = defineProps<{
  tile: Record<string, any>
  entry: TileSlot
  ctx: KindCtx
  /** the page says it already: every tile is older, as the whole refresh failed */
  saidAbove?: boolean
  clicks?: TileClick[]
  /** in the designer: the title as it is being written, which the numbers on screen are older than */
  drawing?: boolean
}>()
const emit = defineEmits<{
  (e: 'retry'): void
  (e: 'sort', column: number): void
  (e: 'toggle', key: unknown[], closed: boolean): void
  (e: 'page', page: number): void
  (e: 'pick', on: ClickSide, value: ClickValue, add: boolean): void
}>()

const { t, te } = useI18n()
const fmtx = useFormat()
const state = computed(() => tileStates(props.entry))
const answer = computed(() => props.entry.answer)
const numbers = computed(() => (state.value.numbers ? (answer.value as TileNumbers) : null))
const failure = computed(() => (answer.value && 'error' in answer.value ? answer.value : null))
const kind = computed<string>(() => props.tile.type)
const title = computed(() => (props.drawing ? '' : numbers.value?.title) || props.tile.title || props.tile.label || '')
// a KPI says its name itself, as its card does, when it has a card to show; a text or an image
// with none needs no header
const headed = computed(() => !!title.value && (kind.value !== 'kpi' || !numbers.value))
const headingId = computed(() => `tile-${props.tile.id}`)

const errorTitle = computed(() => {
  const code = failure.value?.code ?? 'error'
  return t(te(`dashboard.err_${code}`) ? `dashboard.err_${code}` : 'dashboard.err_error')
})
const noAccess = computed(() => failure.value?.code === 'no_access')
// what the author wrote on a KPI or a text; a KPI's figure takes the size, its label does not
const owned = computed(() => {
  const style = tileStyleOf(props.tile.style, props.ctx.ui.panel)
  if (kind.value !== 'kpi' || !style.fontSize) return style
  const { fontSize, ...rest } = style
  return { ...rest, '--kpi-size': fontSize }
})
const showStale = computed(() => !!state.value.trouble && !!props.tile.dataset && !props.saidAbove)
const staleTime = computed(() => {
  const at = props.entry.read ?? numbers.value?.computed_at
  return at ? fmtx.dateTime(at, { seconds: true }) : ''
})
const kpiChange = computed(() => (numbers.value?.change
  ? { ratio: numbers.value.change.ratio, text: '', shown: numbers.value.change.text }
  : null))
// a data URL the gateway checked when it was saved; anything else is not drawn
const imageSrc = computed(() => (typeof numbers.value?.src === 'string' && numbers.value.src.startsWith('data:image/') ? numbers.value.src : null))
const FITS: Record<string, 'contain' | 'cover' | 'fill'> = { contain: 'contain', cover: 'cover', stretch: 'fill' }
const chosenRows = computed(() => clicksOfTile(props.clicks ?? [], props.tile.id))
const chosenColumns = computed(() => clicksOfTile(props.clicks ?? [], props.tile.id, 'columns'))
</script>

<template>
  <section
    class="tile"
    :class="[`t-${kind}`, { dim: state.dim }]"
    :aria-labelledby="headed ? headingId : undefined"
    :aria-label="headed ? undefined : title || $t(`dashboard.type_${kind}`)"
    :aria-busy="state.loading ? 'true' : 'false'"
    :style="kind === 'kpi' || kind === 'text' ? owned : undefined"
  >
    <header v-if="headed" class="thead">
      <h2 :id="headingId" :title="title">{{ title }}</h2>
    </header>
    <p v-if="showStale" class="stale" :class="{ over: !headed }" role="status" :title="$t('dashboard.staleTitle', { error: state.trouble! })">
      <Clock :size="11" aria-hidden="true" /> <span>{{ $t('dashboard.staleFrom', { time: staleTime }) }}</span>
    </p>

    <div class="tbody">
      <!-- the first load: the shape of what is coming, in the place it will take -->
      <div v-if="!answer" class="skel" aria-hidden="true">
        <template v-if="kind === 'kpi'">
          <span class="sk" style="width: 46%; height: 11px" />
          <span class="sk" style="width: 62%; height: 26px" />
        </template>
        <template v-else-if="kind === 'chart'">
          <div class="skbars"><span v-for="(h, i) in [46, 72, 58, 88, 64, 40, 76]" :key="i" class="sk" :style="{ height: `${h}%` }" /></div>
        </template>
        <template v-else-if="kind === 'table'">
          <span v-for="i in 6" :key="i" class="sk" style="width: 100%; height: 13px" />
        </template>
        <template v-else-if="kind === 'text'">
          <span class="sk" style="width: 88%" /><span class="sk" style="width: 64%" />
        </template>
        <span v-else class="sk" style="width: 100%; height: 100%" />
      </div>
      <span v-if="!answer" class="sr-only">{{ $t('dashboard.tileLoading') }}</span>

      <!-- no access: that it refuses, and nothing about what -->
      <div v-else-if="noAccess" class="refused" role="note">
        <Lock :size="15" aria-hidden="true" />
        <p>{{ $t('dashboard.err_no_access') }}</p>
      </div>
      <div v-else-if="failure" class="failed" role="alert">
        <p class="ftitle"><TriangleAlert :size="14" aria-hidden="true" /> {{ errorTitle }}</p>
        <p class="fmsg" :title="failure.error">{{ failure.error }}</p>
        <button type="button" class="mini" :disabled="state.loading" @click="emit('retry')">
          <LoaderCircle v-if="state.loading" :size="12" class="spin" aria-hidden="true" /><RefreshCw v-else :size="12" aria-hidden="true" />
          {{ $t('dashboard.tryAgain') }}
        </button>
      </div>

      <template v-else-if="numbers">
        <KpiCard
          v-if="kind === 'kpi'"
          fit
          :label="numbers.label || title"
          :value="numbers.value"
          :text="numbers.text"
          :change="kpiChange"
          :spark="numbers.spark"
        />
        <TileChart
          v-else-if="kind === 'chart'"
          :answer="numbers"
          :ctx="ctx"
          :label="title"
          :small="tile.grid.h < 6"
          :filters="canFilter(tile)"
          :chosen="chosenRows"
          @pick="(v: ClickValue, add: boolean) => emit('pick', 'rows', v, add)"
        />
        <TileTable
          v-else-if="kind === 'table'"
          :answer="numbers"
          :panel="ctx.ui.panel"
          :label="title"
          :busy="state.loading"
          :filters-rows="canFilter(tile)"
          :filters-columns="canFilter(tile, 'columns')"
          :chosen-rows="chosenRows"
          :chosen-columns="chosenColumns"
          @pick="(on: ClickSide, v: ClickValue, add: boolean) => emit('pick', on, v, add)"
          @sort="(c: number) => emit('sort', c)"
          @toggle="(k: unknown[], closed: boolean) => emit('toggle', k, closed)"
          @page="(p: number) => emit('page', p)"
        />
        <p v-else-if="kind === 'text'" class="ttext">{{ numbers.text }}</p>
        <img v-else-if="imageSrc" class="timg" :src="imageSrc" :alt="title" :style="{ objectFit: FITS[numbers.fit] ?? 'contain' }" />
      </template>
    </div>
  </section>
</template>

<style scoped>
.tile {
  position: relative; display: flex; flex-direction: column; min-width: 0; min-height: 0; overflow: hidden; box-sizing: border-box;
  padding: 10px 12px; background: var(--panel); border: 1px solid var(--border-soft); border-radius: 10px; color: var(--text);
  /* not at once: an answer from the cache comes back before the eye would notice */
  transition: opacity 0.15s ease 0.15s;
}
.tile.dim { opacity: 0.45; }
.t-kpi { justify-content: center; }
.thead { display: flex; align-items: center; gap: 8px; min-width: 0; margin-bottom: 6px; flex-shrink: 0; }
.thead h2 { margin: 0; font-size: 12.5px; font-weight: 600; color: inherit; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; min-width: 0; }
.tbody { flex: 1; display: flex; flex-direction: column; min-height: 0; min-width: 0; justify-content: inherit; }
.stale {
  margin: 0 0 4px; padding: 1px 6px; display: flex; align-items: center; gap: 4px; align-self: flex-start; max-width: 100%; flex-shrink: 0;
  font-size: 11px; color: var(--warning); background: var(--panel-2); border: 1px solid color-mix(in srgb, var(--warning) 40%, var(--border)); border-radius: 6px;
}
.stale span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.stale svg { flex-shrink: 0; }
/* where it would push a small card's figure out: over it, in the corner */
.stale.over { position: absolute; top: 8px; right: 10px; margin: 0; z-index: 2; max-width: calc(100% - 20px); }
.skel { flex: 1; display: flex; flex-direction: column; gap: 8px; min-height: 0; justify-content: center; }
.t-table .skel { justify-content: flex-start; }
.skbars { flex: 1; display: flex; align-items: flex-end; gap: 8px; min-height: 0; }
.skbars .sk { flex: 1; }
.refused, .failed { margin: auto; display: flex; flex-direction: column; align-items: center; gap: 6px; max-width: 100%; text-align: center; min-height: 0; }
.refused { color: var(--muted); font-size: 12.5px; line-height: 1.4; }
.refused p, .failed p { margin: 0; }
.ftitle { display: inline-flex; align-items: center; gap: 6px; font-size: 12.5px; font-weight: 600; color: var(--text); }
.ftitle svg { color: var(--warning); flex-shrink: 0; }
.fmsg {
  font-size: 12px; line-height: 1.4; color: var(--muted); overflow-wrap: anywhere;
  display: -webkit-box; -webkit-line-clamp: 3; line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden;
}
.failed .mini { display: inline-flex; align-items: center; gap: 4px; }
.ttext { margin: 0; white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.45; overflow: auto; min-height: 0; }
.timg { flex: 1; min-height: 0; width: 100%; height: 100%; display: block; }
@media (prefers-reduced-motion: reduce) { .tile { transition: none; } }
</style>
