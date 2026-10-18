<script setup lang="ts">
// The product performing: a flow joins data from three real systems while the visitor scrolls.
// Last night's CSV arrives over SFTP, the web shop's orders as parquet on S3, the customers
// from the ERP's Postgres. The section pins the editor fragment; each part of the scroll
// selects the next node, and the preview shows that node's rows, as the editor does. The nodes
// are buttons too. Without script the last step is shown; under reduced motion the swaps are
// instant. The file names come from the placeholder language, resolved for the sample's date.
const t = useCopy()

type Cell = string | number | null
interface Table { cols: string[], types: string[], rows: Cell[][] }

// the sample morning: 18 October 2026 in Rome
const NOW = new Date(Date.UTC(2026, 9, 18, 5, 30))
const ZONE = 'Europe/Rome'
const name = (template: string) => render(template, { now: NOW, zone: ZONE, variables: {} })
const sftpTemplate = 'sales_{yesterday:yyyyMMdd}.csv'
const s3Template = 'orders_{yesterday:yyyyMMdd}.parquet'

const S = 'String'; const I = 'Int64'; const F = 'Float64'; const D = 'Date'
const store: Cell[][] = [
  ['S-1041', '2026-10-17', '101', '1.234,50 €'],
  ['S-1042', '2026-10-17', '102', '86,00'],
  ['S-1043', '17/10/2026', '103', '412,30 €'],
  ['S-1044', '2026-10-17', '101', 'n/a'],
]
const parseAmount = (a: string) => {
  const n = Number(a.replace('€', '').replace(/\./g, '').replace(',', '.').trim())
  return Number.isFinite(n) && a.trim() !== '' ? n : null
}
const computedRows: Cell[][] = store.map(([o, d, c, a]) => [o, d, Number(c), parseAmount(String(a))])
const typedRows: Cell[][] = computedRows.map(([o, d, c, a]) => [o, /^\d{4}-\d{2}-\d{2}$/.test(String(d)) ? d : null, c, a])
const web: Cell[][] = [
  ['W-2201', '2026-10-17', 104, 59.9],
  ['W-2202', '2026-10-17', 102, 240],
]
const unionRows = [...typedRows, ...web]
const customers: Record<number, [string, string]> = {
  101: ['Salumeria Rossi', 'Milano'], 102: ['Bar Centrale', 'Milano'], 103: ['Gastronomia Bianchi', 'Torino'], 104: ['Hotel Lago', 'Como'],
}
const joined: Cell[][] = unionRows.map(r => [...r, ...(customers[Number(r[2])] || [null, null])])
const kept = joined.filter(r => r[1] !== null && r[3] !== null)

const C4 = ['order', 'date', 'customer_id', 'amount']
const C6 = [...C4, 'customer', 'city']
const tables: Table[] = [
  { cols: C4, types: [S, S, S, S], rows: store },
  { cols: C4, types: [S, S, I, F], rows: computedRows },
  { cols: C4, types: [S, D, I, F], rows: typedRows },
  { cols: C4, types: [S, D, I, F], rows: web },
  { cols: C4, types: [S, D, I, F], rows: unionRows },
  { cols: C6, types: [S, D, I, F, S, S], rows: joined },
  { cols: C6, types: [S, D, I, F, S, S], rows: kept },
  { cols: C6, types: [S, D, I, F, S, S], rows: kept },
]

// the flow, laid out as in the editor: three sources on the left, one chain to the output
interface Node { id: string, label: string, sub: string, x: number, y: number, step: number }
const W = 104; const H = 60
const col = (i: number) => i * 112
const row = (i: number) => i * 70
const nodes: Node[] = [
  { id: 'sftp', label: 'Source', sub: `SFTP · ${name(sftpTemplate)}`, x: col(0), y: row(0), step: 0 },
  { id: 'computed', label: 'Computed column', sub: 'amount, customer_id', x: col(1), y: row(0), step: 1 },
  { id: 'type', label: 'Change type', sub: 'date → Date', x: col(2), y: row(0), step: 2 },
  { id: 's3', label: 'Source', sub: `S3 · ${name(s3Template)}`, x: col(0), y: row(1), step: 3 },
  { id: 'union', label: 'Union', sub: 'append rows', x: col(3), y: row(1), step: 4 },
  { id: 'pg', label: 'Source', sub: 'Postgres · customers', x: col(0), y: row(2), step: 5 },
  { id: 'join', label: 'Join', sub: 'on customer_id', x: col(4), y: row(1), step: 5 },
  { id: 'drop', label: 'Drop nulls', sub: 'date, amount', x: col(5), y: row(1), step: 6 },
  { id: 'out', label: 'Datasource output', sub: 'Daily sales', x: col(6), y: row(1), step: 7 },
]
const edges: [string, string][] = [['sftp', 'computed'], ['computed', 'type'], ['type', 'union'], ['s3', 'union'], ['union', 'join'], ['pg', 'join'], ['join', 'drop'], ['drop', 'out']]
const byId = Object.fromEntries(nodes.map(n => [n.id, n]))
const edgePath = ([a, b]: [string, string]) => {
  const p = byId[a]!; const q = byId[b]!
  const x1 = p.x + W; const y1 = p.y + H / 2; const x2 = q.x; const y2 = q.y + H / 2
  const m = (x1 + x2) / 2
  return `M${x1} ${y1} C${m} ${y1} ${m} ${y2} ${x2} ${y2}`
}
const graphW = col(6) + W
const graphH = row(2) + H

// what the preview header says for each step
const params = [
  `/in/${sftpTemplate} → ${name(sftpTemplate)}`,
  'amount = TRY_CAST(REPLACE(REPLACE(REPLACE(amount, \'€\', \'\'), \'.\', \'\'), \',\', \'.\') AS DOUBLE) · customer_id = TRY_CAST(customer_id AS BIGINT)',
  'date → Date',
  `web/${s3Template} → web/${name(s3Template)}`,
  'append rows: the store’s and the web shop’s',
  'inner join with Postgres erp.customers on customer_id',
  'drop the rows where date or amount is null',
  'publish the datasource «Daily sales»',
]

const n = tables.length
const active = ref(n - 1)
const section = ref<HTMLElement | null>(null)
const graph = ref<HTMLElement | null>(null)
// the graph is drawn at the product's size and scaled down to fit its column; below 0.8 it
// keeps the readable size and scrolls sideways instead (phones)
const fit = ref(1)
let ro: ResizeObserver | null = null
onMounted(() => {
  const box = graph.value
  if (!box) return
  const measure = () => { const s = box.clientWidth / graphW; fit.value = s >= 1 ? 1 : s >= 0.8 ? s : 1 }
  ro = new ResizeObserver(measure)
  ro.observe(box)
  measure()
})
onBeforeUnmount(() => ro?.disconnect())

let raf = 0
function onScroll () {
  if (raf) return
  raf = requestAnimationFrame(() => {
    raf = 0
    const el = section.value
    if (!el) return
    const r = el.getBoundingClientRect()
    const run = r.height - window.innerHeight
    if (run <= 0) return
    const p = Math.min(1, Math.max(0, -r.top / run))
    active.value = Math.min(n - 1, Math.floor(p * n))
  })
}
onMounted(() => {
  active.value = 0
  onScroll()
  window.addEventListener('scroll', onScroll, { passive: true })
  window.addEventListener('resize', onScroll)
})
onBeforeUnmount(() => {
  window.removeEventListener('scroll', onScroll)
  window.removeEventListener('resize', onScroll)
  cancelAnimationFrame(raf)
})
// keep the active node in view when the graph scrolls sideways (phones)
watch(active, (i) => {
  const box = graph.value
  const node = nodes.find(x => x.step === i && x.id !== 'pg')
  if (!box || !node) return
  if (node.x < box.scrollLeft || node.x + W > box.scrollLeft + box.clientWidth) box.scrollTo({ left: Math.max(0, node.x - 8) })
})

// clicking a node scrolls the page to that node's part of the story, so scroll and buttons agree
function go (i: number) {
  const el = section.value
  if (!el) { active.value = i; return }
  const run = el.offsetHeight - window.innerHeight
  const top = el.getBoundingClientRect().top + window.scrollY + (run * (i + 0.5)) / n
  const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches
  window.scrollTo({ top, behavior: reduce ? 'auto' : 'smooth' })
  active.value = i
}
const table = computed(() => tables[active.value]!)
const s = computed(() => t.story.steps[active.value]!)
const isOn = (nd: Node) => nd.step === active.value
const isDone = (nd: Node) => nd.step < active.value
const show = (v: Cell, type: string) => (v === null ? null : type === F ? Number(v).toFixed(2) : String(v))
const spaces = (v: string) => v.match(/^(\s*)(.*?)(\s*)$/)!
</script>

<template>
  <section id="how" ref="section" class="story" aria-labelledby="story-title">
    <div class="story__pin">
      <div class="story__text">
        <h2 id="story-title" class="story__kicker">{{ t.story.title }}</h2>
        <ol class="story__stages" role="list" aria-hidden="true">
          <li v-for="(st, i) in t.story.steps" :key="i" :class="{ 'is-on': i === active, 'is-done': i < active }" />
        </ol>
        <p class="story__stage">{{ s.stage }}</p>
        <div class="story__now" aria-live="polite">
          <Transition name="swap" mode="out-in">
            <div :key="active">
              <p class="story__title">{{ s.title }}</p>
              <p class="story__body">{{ s.text }}</p>
            </div>
          </Transition>
        </div>
      </div>

      <div class="story__ui" data-ui>
        <div ref="graph" class="ui-graph" role="group" :aria-label="t.story.controlsLabel" :style="{ height: `${graphH * fit}px` }">
          <div class="ui-canvas" :style="{ width: `${graphW}px`, height: `${graphH}px`, transform: fit < 1 ? `scale(${fit})` : undefined }">
            <svg class="ui-edges" :width="graphW" :height="graphH" :viewBox="`0 0 ${graphW} ${graphH}`" aria-hidden="true">
              <path v-for="e in edges" :key="e.join('-')" :d="edgePath(e)" :class="{ 'is-done': byId[e[1]]!.step <= active }" />
            </svg>
            <button
              v-for="nd in nodes" :key="nd.id" type="button" class="ui-node"
              :class="{ 'is-on': isOn(nd), 'is-done': isDone(nd), 'is-src': nd.label === 'Source' }"
              :style="{ left: `${nd.x}px`, top: `${nd.y}px`, width: `${W}px`, height: `${H}px` }"
              :aria-pressed="isOn(nd)" :aria-label="`${nd.label}: ${nd.sub}`" @click="go(nd.step)"
            >
              <span class="ui-node__name">{{ nd.label }}</span>
              <span class="ui-node__sub">{{ nd.sub }}</span>
            </button>
          </div>
        </div>

        <div class="ui-preview">
          <div class="ui-head">
            <p class="ui-meta tnum">{{ table.rows.length }} {{ t.story.rowsLabel }} · {{ table.cols.length }} {{ t.story.colsLabel }}</p>
            <span v-if="active >= n - 2" class="ui-ok">
              <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m5 12.5 4.5 4.5L19 7.5" /></svg>
              {{ t.story.contract }}
            </span>
          </div>
          <p class="ui-param"><code>{{ params[active] }}</code></p>
          <div class="ui-scroll">
            <table class="ui-table">
              <thead>
                <tr>
                  <th v-for="(c, i) in table.cols" :key="c" scope="col" :class="`col-${c}`">
                    <span class="ui-col">{{ c }}</span>
                    <span class="ui-type" :class="{ 'is-changed': table.types[i] !== 'String' }">{{ table.types[i] }}</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(r, ri) in table.rows" :key="ri">
                  <td v-for="(v, ci) in r" :key="ci" :class="[`col-${table.cols[ci]}`, { 'is-num': table.types[ci] === 'Float64' || table.types[ci] === 'Int64' }]">
                    <span v-if="v === null" class="ui-null">null</span>
                    <template v-else-if="table.types[ci] === 'String'">
                      <span v-if="spaces(String(v))[1]" class="ui-space">{{ '·'.repeat(spaces(String(v))[1]!.length) }}</span>{{ spaces(String(v))[2] }}<span v-if="spaces(String(v))[3]" class="ui-space">{{ '·'.repeat(spaces(String(v))[3]!.length) }}</span>
                    </template>
                    <template v-else>{{ show(v, table.types[ci]!) }}</template>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <p v-if="active === n - 1" class="ui-dest">
            <span>{{ t.story.publishedTo }}</span>
            <span v-for="d in t.story.destinations" :key="d" class="ui-chip">{{ d }}</span>
          </p>
          <p class="ui-note">{{ t.story.sampleNote }}</p>
        </div>
      </div>
    </div>
  </section>
</template>

<style scoped>
.story { position: relative; height: 340svh; margin-top: var(--section); }
.story__pin {
  position: sticky; top: 0; height: 100svh; min-height: 680px;
  display: grid; grid-template-columns: minmax(0, 4fr) minmax(0, 7fr); gap: 48px; align-items: center;
  padding-inline: var(--gutter);
  background: var(--tile); /* the story is one full-width colour field, the page's stage */
}
.story__kicker { font-size: var(--fs-body); font-weight: 600; color: var(--accent-ink); }
.story__stages { display: flex; gap: 6px; margin: 18px 0 0; padding: 0; list-style: none; }
.story__stages li { height: 6px; flex: 1; border-radius: 3px; background: var(--tile-3); transition: background-color 300ms ease; }
.story__stages li.is-done, .story__stages li.is-on { background: var(--accent); }
.story__stage { margin-top: 12px; font-size: var(--fs-small); font-weight: 600; color: var(--soft); }
.story__now { margin-top: 40px; min-height: 15em; }
.story__title { font-size: clamp(36px, 3.6vw, 56px); line-height: 1.02; letter-spacing: -0.04em; font-weight: 600; max-width: 15ch; text-wrap: balance; }
.story__body { margin-top: 24px; font-size: var(--fs-body); max-width: 40ch; overflow-wrap: anywhere; }

.swap-enter-active, .swap-leave-active { transition: opacity 200ms ease, transform 200ms ease; }
.swap-enter-from { opacity: 0; transform: translateY(12px); }
.swap-leave-to { opacity: 0; transform: translateY(-12px); }

/* the editor fragment, at the product's own size */
.story__ui { background: var(--ground); border-radius: var(--radius); padding: 16px; font-size: 14px; min-width: 0; }
.ui-graph { overflow-x: auto; padding-bottom: 4px; }
.ui-canvas { position: relative; transform-origin: 0 0; }
.ui-edges { position: absolute; inset: 0; overflow: visible; }
.ui-edges path { fill: none; stroke: var(--tile-3); stroke-width: 2; }
.ui-edges path.is-done { stroke: var(--accent); }
.ui-node {
  position: absolute; display: grid; align-content: center; gap: 2px; text-align: left; padding: 4px 8px; cursor: pointer; overflow: hidden;
  border: 1.5px solid var(--tile-3); border-radius: 6px; background: var(--tile); color: var(--text);
  transition: border-color 160ms ease, background-color 160ms ease;
}
.ui-node:hover { border-color: var(--muted); }
.ui-node.is-src { border-left: 4px solid var(--ok); }
.ui-node.is-on { border-color: var(--accent); background: color-mix(in srgb, var(--accent) 16%, var(--tile)); }
.ui-node__name { font-weight: 600; font-size: 13px; line-height: 1.2; }
.ui-node__sub { font-size: 13px; color: var(--soft); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

.ui-preview { margin-top: 12px; background: var(--tile); border-radius: 6px; padding: 12px; }
.ui-head { display: flex; justify-content: space-between; align-items: center; gap: 12px; min-height: 26px; }
.ui-meta { font-size: 13px; color: var(--soft); }
.ui-ok { display: inline-flex; align-items: center; gap: 6px; padding: 2px 10px; border-radius: 6px; background: var(--ok); color: var(--on-ok); font-weight: 600; font-size: 13px; }
.ui-param { margin-top: 6px; font-family: var(--mono); font-size: 13px; color: var(--soft); overflow-wrap: anywhere; } /* taste-skill-ignore: node parameters, literal */
.ui-scroll { overflow-x: auto; margin-top: 8px; }
.ui-table { border-collapse: collapse; width: 100%; font-size: 14px; }
.ui-table th, .ui-table td { text-align: left; padding: 6px 10px; border-bottom: 1px solid var(--tile-3); white-space: nowrap; }
.ui-table th { vertical-align: bottom; }
.ui-col { display: block; font-weight: 600; }
.ui-type { display: block; font-size: 13px; font-weight: 400; color: var(--soft); }
.ui-type.is-changed { color: var(--accent-ink); }
.ui-table td.is-num { text-align: right; font-variant-numeric: tabular-nums; }
.ui-null { color: var(--soft); font-style: italic; }
.ui-space { color: var(--warn-ink); }
.ui-dest { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; margin-top: 10px; font-size: 13px; color: var(--soft); }
.ui-chip { padding: 2px 8px; border-radius: 4px; background: var(--chip-bg); color: var(--chip-ink); font-weight: 600; }
.ui-note { margin-top: 10px; font-size: 13px; color: var(--soft); }

@media (max-width: 900px) {
  .story { height: 320svh; }
  .story__pin { grid-template-columns: 1fr; gap: 14px; align-content: center; min-height: 0; }
  .story__now { margin-top: 16px; min-height: 0; }
  .story__title { font-size: 28px; max-width: none; }
  .story__body { margin-top: 8px; font-size: 15px; }
  .story__ui { padding: 10px; }
  .ui-preview { padding: 8px; }
  .ui-table th, .ui-table td { padding: 4px 6px; }
}
@media (max-width: 600px) {
  .ui-table .col-customer_id, .ui-table .col-city { display: none; }
  .ui-param { display: none; }
}
@media (max-height: 760px) and (max-width: 900px) {
  .story__body { display: none; }
}
</style>
