// The report designer's state, shared by its parts (canvas, panels, dialogs) through
// provide/inject: the definition being edited, the selection, the problems of the last
// save, the history for undo and redo, the zoom and the snapping.
//
// The history keeps the definition as JSON after every pause in the editing (or at the end
// of a drag): an image is kept once and named in the snapshots, so a report with a logo
// does not copy its half megabyte at every step.
import { computed, inject, ref, watch, type InjectionKey } from 'vue'
import {
  allowedIn, bodyPageHeight, contentWidth, copyItem, emptyDefinition, mm1, rowConflicts, samePath, stillStanding, tableHeight, tableRows,
  withSeen, type BandName, type DDefinition, type DItem, type DTable, type DesignProblem, type DField, type InsertKind,
} from '~/composables/reportDesignModel'

const HISTORY = 200
const PAUSE_MS = 350
export const BANDS: BandName[] = ['page_header', 'body', 'page_footer']

let clipboard: string | null = null

/** What is being dragged from the panels onto the page: an element to insert, a field of a
 *  dataset, a ready-made text. Kept here because a drag's data cannot be read until the drop. */
export type DragPayload =
  | { kind: 'insert'; type: InsertKind }
  | { kind: 'field'; dataset: string; field: DField }
  | { kind: 'snippet'; value: string; w: number }
export const DRAG_TYPE = 'application/x-tabularia-report'
export const dragged: { value: DragPayload | null } = { value: null }

export function createDesignStore() {
  const doc = ref<DDefinition>(emptyDefinition())
  const selectedId = ref<string | null>(null)
  const selectedBand = ref<BandName | null>(null)
  const problems = ref<DesignProblem[]>([])
  const zoom = ref(1)
  const snap = ref(true)
  /** a drag or a resize under way: the history waits for its end */
  const gesture = ref(false)
  /** a chart just placed: the canvas asks which kind it is */
  const chartPicker = ref<string | null>(null)
  /** the left panel's tab: a problem in a dataset brings «Data» forward */
  const sideTab = ref<'insert' | 'data'>('insert')
  /** a line for the status bar: what the editor did on its own (an item moved out of a
   *  table's row), cleared by the next gesture */
  const notice = ref('')

  // ── snapshots ──
  const assets = new Map<string, string>()
  const assetOf = new Map<string, string>()
  function serialize(d: DDefinition): string {
    return JSON.stringify(d, (k, v) => {
      if (k !== 'asset' || typeof v !== 'string' || v.length < 256) return v
      let token = assets.get(v)
      if (!token) {
        token = `\u0000asset:${assets.size}`
        assets.set(v, token)
        assetOf.set(token, v)
      }
      return token
    })
  }
  function deserialize(s: string): DDefinition {
    return JSON.parse(s, (k, v) => (k === 'asset' && typeof v === 'string' && v.startsWith('\u0000asset:') ? assetOf.get(v) ?? '' : v))
  }

  const text = computed(() => serialize(doc.value))
  let current = text.value
  const past = ref<string[]>([])
  const future = ref<string[]>([])
  let timer: ReturnType<typeof setTimeout> | null = null

  function record() {
    if (timer) { clearTimeout(timer); timer = null }
    const now = text.value
    if (now === current) return
    past.value.push(current)
    if (past.value.length > HISTORY) past.value.shift()
    future.value = []
    current = now
  }
  watch(text, () => {
    if (gesture.value) return
    if (timer) clearTimeout(timer)
    timer = setTimeout(record, PAUSE_MS)
  })
  watch(gesture, (on) => { if (!on) record() })

  function restore(s: string) {
    current = s
    doc.value = deserialize(s)
    if (selectedId.value && !find(selectedId.value)) selectedId.value = null
  }
  function undo() {
    record()
    const prev = past.value.pop()
    if (prev == null) return
    future.value.push(current)
    restore(prev)
  }
  function redo() {
    record()
    const next = future.value.pop()
    if (next == null) return
    past.value.push(current)
    restore(next)
  }
  const canUndo = computed(() => past.value.length > 0 || text.value !== current)
  const canRedo = computed(() => future.value.length > 0)

  /** A definition loaded: a history of its own, nothing selected. */
  function reset(d: DDefinition) {
    if (timer) { clearTimeout(timer); timer = null }
    tableShape.clear()
    for (const it of d.sections.body.items) {
      if (it.type === 'table') tableShape.set(it.id, `${tableRows(it).length}:${d.theme.size}`)
    }
    doc.value = d
    current = text.value
    past.value = []
    future.value = []
    selectedId.value = null
    selectedBand.value = null
    problems.value = []
  }

  // ── a table's height follows its rows when they change (a group added, the theme's
  // size): not when a report is opened, whose tables keep the height they were saved with
  // (a report made through the API may space what is under a table by it) ──
  const tableShape = new Map<string, string>()
  const shapes = () => doc.value.sections.body.items
    .filter((i): i is DTable => i.type === 'table')
    .map((t) => [t.id, `${tableRows(t).length}:${doc.value.theme.size}`] as const)
  watch(shapes, (list) => {
    for (const [id, shape] of list) {
      const before = tableShape.get(id)
      tableShape.set(id, shape)
      if (before === undefined || before === shape) continue
      const hit = find(id)
      if (hit?.item.type === 'table') hit.item.h = tableHeight(doc.value, hit.item)
    }
  }, { deep: true })

  // ── items ──
  function find(id: string): { band: BandName; index: number; item: DItem } | null {
    for (const band of BANDS) {
      const index = doc.value.sections[band].items.findIndex((i) => i.id === id)
      if (index >= 0) return { band, index, item: doc.value.sections[band].items[index] }
    }
    return null
  }
  const selected = computed(() => (selectedId.value ? find(selectedId.value) : null))

  function select(id: string | null) {
    selectedId.value = id
    if (id) selectedBand.value = find(id)?.band ?? null
  }

  function add(band: BandName, item: DItem) {
    doc.value.sections[band].items.push(item)
    select(item.id)
  }

  function remove(id: string) {
    const hit = find(id)
    if (!hit) return
    doc.value.sections[hit.band].items.splice(hit.index, 1)
    if (selectedId.value === id) selectedId.value = null
    problems.value = problems.value.filter((p) => p.item !== id)
  }

  /** Where a new item of `kind` goes when it is added without a drop: in the band chosen
   *  when it can hold it, else the body; under what is there already. */
  function freeSpot(kind: InsertKind, h = 10): { band: BandName; x: number; y: number } {
    const type = kind === 'matrix' ? 'table' : kind
    const band = selectedBand.value && allowedIn(selectedBand.value, type) ? selectedBand.value : 'body'
    const items = doc.value.sections[band].items
    const bottom = items.reduce((m, i) => Math.max(m, i.y + Math.max(i.h, 0)), 0)
    if (band === 'body') return { band, x: 0, y: mm1(items.length ? bottom + 4 : 0) }
    const room = doc.value.sections[band].height
    return { band, x: 0, y: mm1(Math.max(0, Math.min(items.length ? bottom + 1 : 0, room - h))) }
  }

  function duplicate(id: string) {
    const hit = find(id)
    if (!hit) return
    const copy = copyItem(doc.value, hit.item, hit.item.type === 'table' ? 0 : 5, hit.item.type === 'table' ? hit.item.h + 4 : 5)
    keepInside(hit.band, copy)
    add(hit.band, copy)
    settleRow(copy)
  }

  function copy(id: string) {
    const hit = find(id)
    if (hit) clipboard = JSON.stringify({ band: hit.band, item: hit.item })
  }
  function paste(): boolean {
    if (!clipboard) return false
    const { band: from, item } = JSON.parse(clipboard) as { band: BandName; item: DItem }
    const band = selectedBand.value && allowedIn(selectedBand.value, item.type) ? selectedBand.value : allowedIn(from, item.type) ? from : 'body'
    const pasted = copyItem(doc.value, item)
    keepInside(band, pasted)
    add(band, pasted)
    settleRow(pasted)
    return true
  }

  /** The item moved inside its band and the page (after a paste, a change of size). */
  function keepInside(band: BandName, it: DItem) {
    const width = contentWidth(doc.value)
    if (it.type === 'line') return
    it.w = mm1(Math.min(it.w, width))
    it.x = mm1(Math.max(0, Math.min(it.x, width - it.w)))
    if (band === 'body') {
      it.h = mm1(Math.min(it.h, bodyPageHeight(doc.value)))
      it.y = mm1(Math.max(0, it.y))
    } else {
      const room = doc.value.sections[band].height
      it.h = mm1(Math.min(it.h, room))
      it.y = mm1(Math.max(0, Math.min(it.y, room - it.h)))
    }
  }

  /** A table takes its row of the body alone: an item dropped beside one goes under it,
   *  a table dropped beside items goes under them. */
  function settleRow(it: DItem): boolean {
    const items = doc.value.sections.body.items
    if (!items.includes(it)) return false
    for (let guard = 0; guard < 50; guard++) {
      const clash = rowConflicts(items, it)[0]
      if (!clash) return guard > 0
      if (it.type === 'table' && clash.table === it) {
        it.y = mm1(clash.other.y + Math.max(clash.other.h, 0) + 2)
      } else {
        it.y = mm1(clash.table.y + clash.table.h + 2)
      }
    }
    return true
  }

  // ── data ──
  /** The dataset an item reads: its own, or the only one the report has. */
  function datasetOf(it: DItem | null | undefined): string | null {
    const name = it && 'dataset' in it ? it.dataset : null
    if (name) return name
    return doc.value.datasets.length === 1 ? doc.value.datasets[0].name : null
  }

  /** A dataset's fields and its calculated fields; null when its fields were never read. */
  function fieldsOf(dataset: string | null | undefined): DField[] | null {
    const ds = doc.value.datasets.find((d) => d.name === dataset)
    if (!ds) return null
    const own = ds.fields ?? []
    const calc = (ds.calculated ?? []).map((c) => ({ name: c.name, type: 'calculated' }))
    return own.length ? [...own, ...calc] : calc.length ? calc : null
  }

  const params = computed(() => doc.value.parameters.map((p) => p.name))

  // ── problems ──
  function problemAt(item: string | null, path: string): DesignProblem | null {
    return problems.value.find((p) => p.item === item && samePath(p.path, path)) ?? null
  }
  function clearProblem(item: string | null, path: string) {
    problems.value = problems.value.filter((p) => !(p.item === item && samePath(p.path, path)))
  }
  // a problem goes once its property changes, however it changed (typing, undo, a dialog)
  let sweep: ReturnType<typeof setTimeout> | null = null
  watch(text, () => {
    if (!problems.value.length) return
    if (sweep) clearTimeout(sweep)
    sweep = setTimeout(() => {
      const left = stillStanding(doc.value, problems.value)
      if (left.length !== problems.value.length) problems.value = left
    }, 120)
  })
  function setProblems(list: DesignProblem[]) {
    problems.value = withSeen(doc.value, list)
  }
  const itemsWithProblems = computed(() => new Set(problems.value.map((p) => p.item).filter(Boolean) as string[]))

  return {
    doc, text, selectedId, selectedBand, selected, problems, zoom, snap, gesture, chartPicker, notice, sideTab,
    undo, redo, canUndo, canRedo, reset, record,
    find, select, add, remove, duplicate, copy, paste, freeSpot, keepInside, settleRow,
    datasetOf, fieldsOf, params,
    problemAt, clearProblem, itemsWithProblems, setProblems,
  }
}

export type DesignStore = ReturnType<typeof createDesignStore>
export const DESIGN: InjectionKey<DesignStore> = Symbol('report-design')

export function useDesign(): DesignStore {
  const store = inject(DESIGN)
  if (!store) throw new Error('the report designer is not provided')
  return store
}
