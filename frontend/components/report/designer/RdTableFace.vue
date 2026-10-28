<script setup lang="ts">
// A table (or a matrix) in the design view: its rows as the report will lay them out —
// the header, each group's header, the detail, the groups' totals, the grand total — with
// the expressions in the cells. The structure, not the data: the preview has the data.
// With `tags`, each row's role is written beside the page, on the right (the canvas leaves
// room for the widest in this language): smaller when the rows are thin at this zoom, not
// at all when they are too thin to hold a word.
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { PAPER, PT_MM, rowHeight, tableRows, type DDefinition, type DTable, type TableRow } from '~/composables/reportDesignModel'

const props = defineProps<{ table: DTable; def: DDefinition; ppm: number; tags?: boolean; tagLeft?: number }>()
const { t } = useI18n()

const rowPx = computed(() => rowHeight(props.def) * props.ppm)
const fontPx = computed(() => props.def.theme.size * PT_MM * props.ppm)
const rows = computed(() => tableRows(props.table))

interface Col { header: string; value: string; total: string; align: string; w: number; matrix?: 'by' | 'total' }

/** The columns as drawn: those with a width keep it, the others share what is left. */
const cols = computed<Col[]>(() => {
  const tb = props.table
  const m = tb.matrix
  const base: Col[] = m
    ? [
        { header: tb.columns[0]?.header ?? '', value: '', total: '', align: 'left', w: tb.columns[0]?.width ?? 0 },
        { header: m.columns_by, value: m.value, total: m.value, align: 'right', w: 0, matrix: 'by' },
        ...(m.total !== false ? [{ header: t('reportDesigner.face.total'), value: 'Σ', total: 'Σ', align: 'right', w: m.width ?? 0, matrix: 'total' as const }] : []),
      ]
    : tb.columns.map((c) => ({ header: c.header ?? '', value: c.value, total: c.total ?? '', align: c.align ?? 'left', w: c.width ?? 0 }))
  if (!base.length) return []
  const fixed = base.reduce((s, c) => s + (c.w || 0), 0)
  const free = base.filter((c) => !c.w).length
  const share = free ? Math.max(6, (tb.w - fixed) / free) : 0
  return base.map((c) => ({ ...c, w: c.w || share }))
})

function groupText(level: number): string {
  const g = props.table.groups?.[level]
  return (g?.label || g?.by || '').trim()
}

const tagOf = (r: TableRow) => {
  switch (r.kind) {
    case 'head': return t('reportDesigner.face.tagHead')
    case 'group-head': return t('reportDesigner.face.tagGroupHead', { n: r.level + 1 })
    case 'detail': return props.table.matrix ? t('reportDesigner.face.tagCells') : t('reportDesigner.face.tagDetail')
    case 'group-foot': return t('reportDesigner.face.tagGroupFoot', { n: r.level + 1 })
    case 'total': return t('reportDesigner.face.tagTotal')
    default: return ''
  }
}

const empty = computed(() => !props.table.matrix && !props.table.columns.length)
const tagMode = computed(() => (rowPx.value >= 18 ? 'full' : rowPx.value >= 11 ? 'small' : null))
</script>

<template>
  <div class="tface" :style="{ fontSize: `${fontPx}px`, '--row': `${rowPx}px`, '--rule': PAPER.rule }">
    <div v-if="empty" class="tempty" :style="{ height: `${rowPx * rows.length}px` }">
      {{ $t('reportDesigner.face.noColumns') }}
    </div>
    <template v-else>
      <div v-for="(r, i) in rows" :key="i" class="trow" :class="r.kind" :style="{ height: `${rowPx}px` }">
        <template v-if="r.kind === 'group-head'">
          <div class="tcell span" :style="{ paddingLeft: `${(1.5 + r.level * 3) * ppm}px`, background: r.level ? '#f4f6fc' : PAPER.band }">
            <span v-if="groupText(r.level)" class="chip">{{ groupText(r.level) }}</span>
            <span v-else class="hole">{{ $t('reportDesigner.face.groupBy') }}</span>
          </div>
        </template>
        <template v-else>
          <div
            v-for="(c, j) in cols"
            :key="j"
            class="tcell"
            :class="[c.align, { by: c.matrix === 'by' }]"
            :style="{ width: `${c.w * ppm}px`, padding: `0 ${1.5 * ppm}px`,
                      background: r.kind === 'head' ? PAPER.head : r.kind === 'total' ? PAPER.total : (table.zebra && r.kind === 'detail' ? '#f8f9fb' : 'transparent') }"
          >
            <template v-if="r.kind === 'head'">
              <span v-if="c.matrix === 'by'" class="chip">{{ c.header || '…' }}</span>
              <strong v-else class="htext">{{ c.header }}</strong>
            </template>
            <template v-else-if="r.kind === 'detail'">
              <span v-if="table.matrix && j === 0" class="chip">{{ groupText((table.groups?.length ?? 1) - 1) || '…' }}</span>
              <span v-else-if="c.value" class="chip">{{ c.value }}</span>
              <span v-else class="hole">{{ $t('reportDesigner.face.value') }}</span>
            </template>
            <template v-else>
              <strong v-if="j === 0 && !c.total" class="htext">{{ r.kind === 'total' ? $t('reportDesigner.face.total') : `${$t('reportDesigner.face.total')} ${groupText(r.level)}` }}</strong>
              <span v-else-if="c.total" class="chip">{{ c.total }}</span>
            </template>
          </div>
        </template>
        <span v-if="tags && tagMode" class="ttag" :class="tagMode" :style="{ left: `${tagLeft ?? 0}px`, ...(tagMode === 'small' ? { lineHeight: `${Math.floor(rowPx) - 3}px` } : {}) }">{{ tagOf(r) }}</span>
      </div>
    </template>
  </div>
</template>

<style scoped>
.tface { position: relative; width: 100%; color: #1f2933; line-height: 1.15; }
.trow { position: relative; display: flex; align-items: stretch; border-bottom: 1px solid var(--rule); }
.tcell { display: flex; align-items: center; min-width: 0; overflow: hidden; flex: none; }
.tcell.span { flex: 1; }
.tcell.right { justify-content: flex-end; }
.tcell.center { justify-content: center; }
.tcell.by { background-image: repeating-linear-gradient(90deg, transparent 0 18%, rgba(31, 41, 51, 0.06) 18% 19%) !important; }
.total .tcell { color: #ffffff; }
.htext { font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.chip {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.86em;
  color: #2f4ac0; background: rgba(59, 91, 219, 0.08); border-radius: 3px; padding: 0 0.35em;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 100%;
}
.total .chip { color: #dbe4ff; background: rgba(255, 255, 255, 0.12); }
.hole { color: #9aa3b5; font-style: italic; font-size: 0.86em; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.tempty {
  display: grid; place-items: center; color: #6b7590; font-size: 0.9em; text-align: center;
  border: 1px dashed #c3c9d5; border-radius: 3px; background: #fafbfc;
}
.ttag {
  position: absolute; top: 50%; transform: translateY(-50%); white-space: nowrap; pointer-events: none;
  font: 600 9.5px/1 'IBM Plex Sans', system-ui, sans-serif; letter-spacing: 0.03em; text-transform: uppercase;
  color: var(--muted); padding: 3px 6px; border-radius: 6px; background: var(--panel); border: 1px solid var(--border);
}
.ttag.small { font-size: 8.5px; padding: 0 4px; border-radius: 4px; }
</style>
