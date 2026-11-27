// The guided tour: a walk through the app on the «Sample» folder's own objects, for a person who
// has it. Here the machine and everything that can be decided without a browser (which steps a
// person gets, where the card goes, what is remembered); components/tour/TourHost.vue draws it.
//
// Nothing of the tour asks the server anything until it is looked for: the first look is ONE
// light request (the folders), made once a page load and only when no answer is remembered (a
// person found without the sample is not asked about again in that browser session), and the
// sample's objects are asked for when the tour starts. What was seen, skipped or left half way
// lives in the browser (localStorage, keyed by the person), never in the database.
import { computed, ref } from 'vue'

export const TOUR_MIN_WIDTH = 900
export const TOUR_SAMPLE = 'Sample'

/** What the seed script (infrastructure/sampledb) calls the things the tour opens. */
export const TOUR_NAMES = {
  flows: 'Flows',
  private: 'Private tables',
  connection: 'Sample ERP (Postgres)',
  datasource: 'orders',
  flow: 'Margin by Category and Month',
  view: 'Margin by category — month by month',
  report: 'Margin review',
  dashboard: 'Sales overview',
} as const

// ── The sample ────────────────────────────────────────────────────────────────

export interface TourFolder { id: number; name: string; parent_id: number | null; personal_of?: number | null }
export interface TourNamed { id: number; name: string }

/** The «Sample» folder and the two subfolders the tour reads (null: not there or not visible). */
export interface TourSample { root: number; flows: number | null; tables: number | null }

const same = (a: string, b: string) => a.trim().toLowerCase() === b.trim().toLowerCase()

/** The sample among the folders this person sees: a top-level folder called «Sample», nobody's personal one. */
export function tourFindSample(folders: readonly TourFolder[]): TourSample | null {
  const root = folders.find((f) => f.parent_id === null && !f.personal_of && same(f.name, TOUR_SAMPLE))
  if (!root) return null
  const child = (name: string) => folders.find((f) => f.parent_id === root.id && same(f.name, name))?.id ?? null
  return { root: root.id, flows: child(TOUR_NAMES.flows), tables: child(TOUR_NAMES.private) }
}

export interface TourLists {
  flows: readonly TourNamed[]
  datasources: readonly TourNamed[]
  connections: readonly TourNamed[]
  views: readonly TourNamed[]
  reports: readonly TourNamed[]
  dashboards: readonly TourNamed[]
}

/** The objects the steps open: their ids, or null where the object is not there. */
export interface TourObjects {
  folder: number
  flow: number | null
  datasource: number | null
  connection: number | null
  view: number | null
  report: number | null
  dashboard: number | null
}

const idByName = (list: readonly TourNamed[], name: string) => list.find((o) => same(o.name, name))?.id ?? null

export function tourResolveObjects(sample: TourSample, lists: TourLists): TourObjects {
  return {
    folder: sample.root,
    flow: idByName(lists.flows, TOUR_NAMES.flow),
    datasource: idByName(lists.datasources, TOUR_NAMES.datasource),
    connection: idByName(lists.connections, TOUR_NAMES.connection),
    view: idByName(lists.views, TOUR_NAMES.view),
    report: idByName(lists.reports, TOUR_NAMES.report),
    dashboard: idByName(lists.dashboards, TOUR_NAMES.dashboard),
  }
}

// ── The steps ─────────────────────────────────────────────────────────────────

/** A step as written: where it goes, what it points at, and what it needs to make sense. */
export interface TourStepDef {
  id: string
  /** the place of the top bar this person must have (the bar decides: appKeys.ts, NAV_SECTIONS) */
  place?: string
  /** the sample object it opens: without it the step is left out */
  needs?: Exclude<keyof TourObjects, 'folder'>
  route?: (o: TourObjects) => string
  /** `data-tour` values, best first: the page shows the first one it has */
  anchors?: (o: TourObjects) => string[]
  /** with no anchor the step is skipped, instead of standing as a card at the centre */
  optional?: boolean
  /** a `data-tour` the step presses once when it arrives, if nothing is selected there yet: what
   *  the step talks about is then on screen (a node's preview and its settings need a node chosen) */
  press?: string
}

export const TOUR_STEPS: readonly TourStepDef[] = [
  { id: 'welcome' },
  { id: 'topbar', route: () => '/', anchors: () => ['topbar'] },
  { id: 'home', route: () => '/', anchors: () => ['home-widgets'] },
  { id: 'explore', place: 'explore', route: () => '/explore', anchors: () => ['explore-sheet'] },
  { id: 'exploreNew', place: 'explore', route: () => '/explore', anchors: () => ['explore-new'], optional: true },
  { id: 'folder', place: 'explore', route: (o) => `/explore?folder=${o.folder}`, anchors: () => ['explore-filter', 'explore-sheet'] },
  { id: 'connections', place: 'connections', needs: 'connection', route: () => '/connections', anchors: (o) => [`connection-${o.connection}`, 'connections-list'] },
  { id: 'datasources', place: 'datasources', needs: 'datasource', route: () => '/datasources', anchors: (o) => [`datasource-${o.datasource}`, 'datasources-list'] },
  { id: 'editorCanvas', place: 'flows', needs: 'flow', route: (o) => `/editor?flow=${o.flow}`, anchors: () => ['editor-canvas'] },
  { id: 'editorPalette', place: 'flows', needs: 'flow', route: (o) => `/editor?flow=${o.flow}`, anchors: () => ['editor-palette'] },
  { id: 'editorPreview', place: 'flows', needs: 'flow', route: (o) => `/editor?flow=${o.flow}`, anchors: () => ['editor-preview'], press: 'node-op' },
  { id: 'editorParams', place: 'flows', needs: 'flow', route: (o) => `/editor?flow=${o.flow}`, anchors: () => ['editor-panel'], press: 'node-op' },
  { id: 'editorRun', place: 'flows', needs: 'flow', route: (o) => `/editor?flow=${o.flow}`, anchors: () => ['editor-toolbar'] },
  { id: 'editorOutput', place: 'flows', needs: 'flow', route: (o) => `/editor?flow=${o.flow}`, anchors: () => ['node-output', 'editor-canvas'] },
  { id: 'flows', place: 'flows', route: () => '/flows', anchors: () => ['flows-activity'] },
  { id: 'flowsList', place: 'flows', route: () => '/flows', anchors: () => ['flows-list'] },
  { id: 'viewerFields', place: 'viewer', needs: 'view', route: (o) => `/viewer?view=${o.view}`, anchors: () => ['viewer-fields'] },
  { id: 'viewerResult', place: 'viewer', needs: 'view', route: (o) => `/viewer?view=${o.view}`, anchors: () => ['viewer-result'] },
  { id: 'reportParams', place: 'reports', needs: 'report', route: (o) => `/report?id=${o.report}`, anchors: () => ['report-params'], optional: true },
  { id: 'report', place: 'reports', needs: 'report', route: (o) => `/report?id=${o.report}`, anchors: () => ['report-frame'] },
  { id: 'dashboard', place: 'dashboards', needs: 'dashboard', route: (o) => `/dashboard?id=${o.dashboard}`, anchors: () => ['dashboard-tiles'] },
  { id: 'dashboardFilters', place: 'dashboards', needs: 'dashboard', route: (o) => `/dashboard?id=${o.dashboard}`, anchors: () => ['dashboard-filters'], optional: true },
  { id: 'lineage', place: 'lineage', route: () => '/lineage', anchors: () => ['lineage-side'] },
  { id: 'runs', place: 'runs', route: () => '/runs', anchors: () => ['runs-list'] },
  { id: 'queue', place: 'queue', route: () => '/queue', anchors: () => ['queue-stats'] },
  { id: 'admin', place: 'admin', route: () => '/admin', anchors: () => ['admin-sections'] },
  { id: 'end', route: () => '/', anchors: () => ['settings'] },
]

/** A step as the person gets it. */
export interface TourStep {
  id: string
  /** where it is shown (null: wherever the person is) */
  route: string | null
  anchors: string[]
  optional: boolean
  press: string | null
}

/** The steps this person gets: the places they have (the top bar's own decision) and the objects the sample has. */
export function tourPlan(places: readonly string[], objects: TourObjects): TourStep[] {
  return TOUR_STEPS
    .filter((s) => (!s.place || places.includes(s.place)) && (!s.needs || objects[s.needs] != null))
    .map((s) => ({ id: s.id, route: s.route?.(objects) ?? null, anchors: s.anchors?.(objects) ?? [], optional: !!s.optional, press: s.press ?? null }))
}

// ── Moving along the steps ────────────────────────────────────────────────────

/** The next step's index, or null at the end. */
export const tourNextIndex = (index: number, total: number): number | null => (index + 1 < total ? index + 1 : null)
/** The step before; the first one stays where it is. */
export const tourBackIndex = (index: number): number => Math.max(0, index - 1)

/**
 * The plan without the step at `index` (an optional step whose anchor the page does not have), and
 * where the tour stands then: on the step that took its place going forward, on the one before
 * going back (at the first, on the one after); null past the end. The step leaves the plan, so the
 * «n of N» the person reads counts the steps they are shown and never jumps a number.
 */
export function tourDropMissing<T>(plan: readonly T[], index: number, dir: 1 | -1): { plan: T[]; index: number | null } {
  const rest = plan.filter((_, i) => i !== index)
  if (dir === 1) return { plan: rest, index: index < rest.length ? index : null }
  return { plan: rest, index: rest.length ? Math.max(0, index - 1) : null }
}

// ── Keys on the card ──────────────────────────────────────────────────────────

export interface TourKey { key: string; repeat?: boolean; ctrlKey?: boolean; metaKey?: boolean; altKey?: boolean }
export type TourKeyAct = 'next' | 'back' | 'leave' | null

/**
 * What a key pressed on the card does. A key held down, or one pressed while the step is still
 * arriving (the card has the focus but is not shown yet), does nothing: no step goes by unseen.
 * Esc leaves, which remembers the step: only the «Skip tour» button says «never again».
 */
export function tourCardKey(e: TourKey, f: { ready: boolean; first: boolean; onButton: boolean }): TourKeyAct {
  if (e.ctrlKey || e.metaKey || e.altKey || e.repeat) return null
  if (e.key === 'Escape') return 'leave'
  if (!f.ready) return null
  if (e.key === 'ArrowRight' || (e.key === 'Enter' && !f.onButton)) return 'next'
  if (e.key === 'ArrowLeft' && !f.first) return 'back'
  return null
}

const CARD_KEYS = new Set(['Escape', 'Enter', ' ', 'Backspace', 'Delete', 'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'Home', 'End', 'PageUp', 'PageDown'])

/**
 * The keys that move or act in a list are the card's own while it has the focus: they do not reach
 * the page below, where Backspace would go up a folder and Delete would ask to delete a row. The
 * letters go on, so the app's own keys (/, ?, G and a letter) work during the tour.
 */
export const tourKeepsKey = (e: TourKey): boolean => !e.ctrlKey && !e.metaKey && !e.altKey && CARD_KEYS.has(e.key)

/** The index of a step by its id (0 when it is not in the plan any more). */
export const tourIndexOf = (plan: readonly TourStep[], id: string | undefined): number => Math.max(0, plan.findIndex((s) => s.id === id))

// ── What is remembered (the browser's, per person) ────────────────────────────

export type TourStored = { status: 'done' | 'skipped' } | { status: 'progress'; step: string }

export const tourStorageKey = (userId: number | string) => `tabularia.tour.v1.${userId}`

export function tourDecode(raw: string | null | undefined): TourStored | null {
  if (!raw) return null
  try {
    const v = JSON.parse(raw)
    if (v?.status === 'done' || v?.status === 'skipped') return { status: v.status }
    if (v?.status === 'progress' && typeof v.step === 'string' && v.step) return { status: 'progress', step: v.step }
  } catch { /* not ours */ }
  return null
}

export const tourEncode = (s: TourStored): string => JSON.stringify(s)

type Store = Pick<Storage, 'getItem' | 'setItem'>
const browserStore = (): Store | null => { try { return globalThis.localStorage ?? null } catch { return null } }

export function tourLoad(userId: number | string, store: Store | null = browserStore()): TourStored | null {
  try { return tourDecode(store?.getItem(tourStorageKey(userId))) } catch { return null }
}

export function tourSave(userId: number | string, s: TourStored, store: Store | null = browserStore()): void {
  try { store?.setItem(tourStorageKey(userId), tourEncode(s)) } catch { /* private window, full disk: it will start again, which is fine */ }
}

// A person who has no sample is asked about once a browser session, not at every page load: the
// answer «none» is kept in the session (never «there is one»: that is read fresh, the folder can go).
type SessionStore = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>
const sessionStore = (): SessionStore | null => { try { return globalThis.sessionStorage ?? null } catch { return null } }

export const tourNoSampleKey = (userId: number | string) => `tabularia.tour.nosample.${userId}`

export function tourKnowsNoSample(userId: number | string, store: SessionStore | null = sessionStore()): boolean {
  try { return store?.getItem(tourNoSampleKey(userId)) === '1' } catch { return false }
}

export function tourNoteSample(userId: number | string, found: boolean, store: SessionStore | null = sessionStore()): void {
  try {
    if (found) store?.removeItem(tourNoSampleKey(userId))
    else store?.setItem(tourNoSampleKey(userId), '1')
  } catch { /* it will ask again, which is fine */ }
}

// ── Whether it starts by itself ───────────────────────────────────────────────

/** The pages with no signed-in person: the sign-in, the shared links, the identity provider's way back. */
export const tourIsPublicPath = (path: string): boolean => path === '/login' || path.startsWith('/share') || path.startsWith('/auth')

export interface TourStartFacts {
  /** what the browser remembers of this person (null: nothing, a first visit) */
  stored: TourStored | null
  sampleFound: boolean
  width: number
  path: string
  /** a modal dialog is open */
  modalOpen: boolean
}

/** The tour opens by itself on a first visit, to someone who sees the sample, on a wide screen, away from the public pages and with nothing modal open. */
export const tourShouldAutostart = (f: TourStartFacts): boolean =>
  f.stored === null && f.sampleFound && f.width >= TOUR_MIN_WIDTH && !tourIsPublicPath(f.path) && !f.modalOpen

// ── Where the card goes ───────────────────────────────────────────────────────

export interface TourBox { x: number; y: number; w: number; h: number }
export type TourSide = 'below' | 'above' | 'right' | 'left' | 'inside' | 'center'

/** The box the spotlight draws around an anchor. */
export const tourSpotBox = (b: TourBox, pad = 4): TourBox => ({ x: b.x - pad, y: b.y - pad, w: b.w + 2 * pad, h: b.h + 2 * pad })

const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(v, hi))

/**
 * The card's top-left corner: below the anchor if there is room, else above, else beside it, else
 * (an anchor that fills the screen) in the corner of the viewport nearest the anchor's bottom.
 * No anchor: the middle of the screen, a little above the centre.
 */
export function tourPlaceCard(
  target: TourBox | null,
  card: { w: number; h: number },
  vp: { w: number; h: number },
  gap = 14,
  margin = 12,
): { x: number; y: number; side: TourSide } {
  const maxX = Math.max(margin, vp.w - card.w - margin)
  const maxY = Math.max(margin, vp.h - card.h - margin)
  if (!target) return { x: clamp((vp.w - card.w) / 2, margin, maxX), y: clamp((vp.h - card.h) * 0.4, margin, maxY), side: 'center' }
  const spot = tourSpotBox(target)
  const centredX = clamp(target.x + target.w / 2 - card.w / 2, margin, maxX)
  const centredY = clamp(target.y + target.h / 2 - card.h / 2, margin, maxY)
  if (spot.y + spot.h + gap + card.h + margin <= vp.h) return { x: centredX, y: spot.y + spot.h + gap, side: 'below' }
  if (spot.y - gap - card.h >= margin) return { x: centredX, y: spot.y - gap - card.h, side: 'above' }
  if (spot.x + spot.w + gap + card.w + margin <= vp.w) return { x: spot.x + spot.w + gap, y: centredY, side: 'right' }
  if (spot.x - gap - card.w >= margin) return { x: spot.x - gap - card.w, y: centredY, side: 'left' }
  return { x: maxX, y: maxY, side: 'inside' }
}

// ── The tour, as the app uses it ──────────────────────────────────────────────

interface Running { plan: TourStep[]; index: number; dir: 1 | -1 }

// one tour for the page: the pages come and go (each one wraps its own shell), the tour stays
const sample = ref<TourSample | null | undefined>(undefined) // undefined: not asked yet
const objects = ref<TourObjects | null>(null)
const running = ref<Running | null>(null)
const loading = ref(false)
const stored = ref<TourStored | null>(null)
const narrow = ref(false)
let asking: Promise<TourSample | null> | null = null
let person = 0 // goes up when the signed-in person changes: an answer for the one before is dropped

async function askFolders(userId: number | string | null): Promise<TourSample | null> {
  const mine = person
  let found: TourSample | null = null
  let answered = false
  try {
    found = tourFindSample(await useProjects().list())
    answered = true
  } catch { /* not signed in or unreachable: no tour, no noise */ }
  if (mine !== person) return null
  sample.value = found
  if (answered && userId != null) tourNoteSample(userId, !!found)
  return found
}

/**
 * Another person signs in on this tab (or this one signs out): what the tour knew was the other's.
 * Their sample folder is not this one's, and a tour on screen was theirs. What the browser
 * remembers per person is read again by the host when the person is known.
 */
export function resetTour(): void {
  person++
  sample.value = undefined
  objects.value = null
  running.value = null
  loading.value = false
  asking = null
}

/** The sample's lists: the few things the steps open, each refused list simply empty. */
async function askObjects(s: TourSample): Promise<TourObjects> {
  const settle = <T,>(p: Promise<T[]> | null): Promise<T[]> => (p ? p.catch(() => [] as T[]) : Promise.resolve([] as T[]))
  const [flows, datasources, connections, views, reports, dashboards] = await Promise.all([
    settle<TourNamed>(s.flows != null ? useFlows().listByProject(s.flows) : null),
    settle<TourNamed>(s.tables != null ? useDatasources().listByProject(s.tables) : null),
    settle<TourNamed>(useConnections().listByProject(s.root)),
    settle<TourNamed>(useSavedViews().listByProject(s.root)),
    settle<TourNamed>(useReports().listByProject(s.root)),
    settle<TourNamed>(useDashboards().listByProject(s.root)),
  ])
  return tourResolveObjects(s, { flows, datasources, connections, views, reports, dashboards })
}

export function useTour() {
  const { user } = useAuth()
  const sections = useGoToSections()

  const userId = () => user.value?.id ?? null
  const remember = (s: TourStored) => {
    stored.value = s
    const id = userId()
    if (id != null) tourSave(id, s)
  }

  const current = computed(() => (running.value ? running.value.plan[running.value.index] : null))

  /** One request, once: is the sample among this person's folders? */
  function ensureSample(): Promise<TourSample | null> {
    if (sample.value !== undefined) return Promise.resolve(sample.value)
    return asking ??= askFolders(userId())
  }

  function finish() {
    running.value = null
    remember({ status: 'done' })
  }

  function go(index: number | null, dir: 1 | -1) {
    const r = running.value
    if (!r) return
    if (index === null) return finish()
    r.index = index
    r.dir = dir
    remember({ status: 'progress', step: r.plan[index].id })
  }

  async function start(): Promise<boolean> {
    if (running.value || loading.value || narrow.value) return false
    const s = await ensureSample()
    if (!s) return false
    const mine = person
    // said at once, before the lists are asked: another tab of the same first visit reads it and stays quiet
    remember({ status: 'progress', step: stored.value?.status === 'progress' ? stored.value.step : 'welcome' })
    loading.value = true
    let found: TourObjects
    try {
      found = await askObjects(s)
    } finally { loading.value = false }
    if (mine !== person) return false // the person changed while the lists were coming
    objects.value = found
    const plan = tourPlan(sections.value.map((p) => p.id), found)
    const from = stored.value?.status === 'progress' ? tourIndexOf(plan, stored.value.step) : 0
    running.value = { plan, index: from, dir: 1 }
    remember({ status: 'progress', step: plan[from].id })
    return true
  }

  return {
    sampleFound: computed(() => !!sample.value),
    /** what the browser remembers of this person */
    stored,
    current,
    index: computed(() => running.value?.index ?? 0),
    total: computed(() => running.value?.plan.length ?? 0),
    dir: computed(() => running.value?.dir ?? 1),
    active: computed(() => !!running.value),
    loading,
    narrow,
    /** the menu's entry: only with the sample, only where the tour can run */
    menuVisible: computed(() => !!sample.value && !narrow.value && !running.value),
    /** left half way: the menu offers to go on */
    resumable: computed(() => stored.value?.status === 'progress' && stored.value.step !== 'welcome'),

    /** a first visit's state is read from the browser once the person is known */
    readStored() { const id = userId(); stored.value = id == null ? null : tourLoad(id) },
    ensureSample,
    start,
    next() { if (running.value) go(tourNextIndex(running.value.index, running.value.plan.length), 1) },
    back() { if (running.value) go(tourBackIndex(running.value.index), -1) },
    /** a step with no anchor and no wish to be a card: it leaves the plan, and the tour goes on in the direction it was travelling */
    passOver() {
      const r = running.value
      if (!r) return
      const d = tourDropMissing(r.plan, r.index, r.dir)
      if (d.index === null) return finish()
      running.value = { plan: d.plan, index: d.index, dir: r.dir }
      remember({ status: 'progress', step: d.plan[d.index].id })
    },
    /** whether this person was already found to have no sample in this browser session (nothing is asked again) */
    knowsNoSample() { const id = userId(); return id != null && tourKnowsNoSample(id) },
    skip() { running.value = null; remember({ status: 'skipped' }) },
    finish,
    /** the person went elsewhere by themselves, or the screen got narrow: the tour ends here, remembering where it was (the menu offers to resume) */
    leave() { running.value = null },
  }
}
