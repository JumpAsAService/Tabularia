<script setup lang="ts">
// The strip under the parameters: what the clicks chose, `By area: Nord-Est ×`, and Clear all. With
// nothing chosen it says what a click does, in the same room: a choice must not move the bar under the
// pointer. On one line that scrolls inside itself, for the same reason.
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { X } from 'lucide-vue-next'
import { clickLabel, valueText, type ClickSide, type ClickValue, type TileClick } from '~/composables/dashboardClicks'
import type { TileSlots } from '~/composables/dashboardTiles'

const props = defineProps<{
  clicks: TileClick[]
  tiles: Record<string, any>[]
  slots: TileSlots
  /** what the screen said of a value when it was clicked */
  saidOf: Map<string, string>
  hint?: boolean
}>()
const emit = defineEmits<{ (e: 'remove', tile: string, on: ClickSide): void; (e: 'clear'): void }>()
const { t } = useI18n()

const SHOWN = 3 // values written before «+2 more»: the whole list is in the tooltip

function wordOf(c: TileClick, value: ClickValue): string {
  const said = clickLabel(props.slots[c.tile]?.answer, c.on, value) ?? props.saidOf.get(`${c.tile}|${c.on}|${valueText(value)}`)
  if (said !== undefined) return said
  const path = Array.isArray(value) ? value : [value]
  return path.map((x) => (x === null ? t('report.emptyGroup') : String(x))).join(' · ')
}
const entries = computed(() => props.clicks.map((c) => {
  const tile = props.tiles.find((x) => x.id === c.tile)
  const title = tile?.title || tile?.label || c.tile
  const words = c.values.map((v) => wordOf(c, v))
  const more = words.length - SHOWN
  return {
    key: `${c.tile}.${c.on}`, tile: c.tile, on: c.on, title, whole: words.join(', '),
    shown: more > 0 ? `${words.slice(0, SHOWN).join(', ')} ${t('dashboard.clickMore', { n: more })}` : words.join(', '),
  }
}))
</script>

<template>
  <div class="strip" role="group" :aria-label="$t('dashboard.clicksLabel')">
    <template v-if="entries.length">
      <span v-for="e in entries" :key="e.key" class="chip" :title="`${e.title}: ${e.whole}`">
        <span class="who">{{ e.title }}:</span>
        <b class="what">{{ e.shown }}</b>
        <button type="button" class="x" :aria-label="$t('dashboard.removeClick', { name: `${e.title}: ${e.whole}` })" :title="$t('dashboard.removeClick', { name: e.whole })" @click="emit('remove', e.tile, e.on)">
          <X :size="12" aria-hidden="true" />
        </button>
      </span>
      <button type="button" class="clear" @click="emit('clear')">{{ $t('dashboard.clearClicks') }}</button>
    </template>
    <span v-else-if="hint" class="hint">{{ $t('dashboard.clickHint') }}</span>
  </div>
</template>

<style scoped>
.strip {
  display: flex; align-items: center; gap: 6px; min-height: 26px; flex-wrap: nowrap; overflow-x: auto; overflow-y: hidden; white-space: nowrap; scrollbar-width: thin;
  font-size: 12px;
}
.hint { color: var(--muted); overflow: hidden; text-overflow: ellipsis; }
.chip {
  display: inline-flex; align-items: center; gap: 4px; flex-shrink: 0; max-width: 60vw; height: 24px; box-sizing: border-box; padding: 0 2px 0 8px;
  background: var(--panel); color: var(--text); border: 1px solid var(--border); border-radius: 12px;
}
.who { color: var(--muted); }
.what { font-weight: 600; overflow: hidden; text-overflow: ellipsis; }
.x, .clear { all: unset; cursor: pointer; box-sizing: border-box; }
.x { display: inline-flex; align-items: center; justify-content: center; width: 18px; height: 18px; flex-shrink: 0; border-radius: 50%; color: var(--muted); }
.x:hover { color: var(--text); background: var(--panel-2); }
.clear { flex-shrink: 0; padding: 2px 6px; border-radius: 4px; color: var(--accent-hi); font-weight: 600; }
.clear:hover { text-decoration: underline; }
.x:focus-visible, .clear:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
</style>
