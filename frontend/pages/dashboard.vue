<script setup lang="ts">
// A dashboard on screen (docs/specs/dashboards.md, «On the screen»): its parameters, its grid of
// tiles, the time of the numbers, Refresh, Full screen. It reads the data with the rights of
// whoever opens it, and a tile they may not read says so inside that tile.
//
// - The parameters' values live in the address (`?id=4&region=North`), as a report's: only those
//   that differ from the defaults, `{none}` an optional one emptied; a link opens the same
//   dashboard, Back goes back to the values before.
// - A click on a tile filters the others on its dataset (composables/dashboardClicks.ts): it is in
//   the address too, `&click.by_area=Nord-Est`, so that a link holds it and Back undoes it. Only the
//   tiles of that dataset are asked again, and the tile clicked keeps its bars. What the server found
//   comes back in the tile, `chosen`: a value no longer there goes, from the address as well, with no
//   step for Back. A link's click on a tile gone or switched off is left out when the page opens.
// - One request reads every tile; a page, a sort or a closed group asks only that tile, and
//   is not in the address. Only the latest request that asked for a tile counts for it
//   (composables/dashboardTiles.ts): a slow network never puts old numbers over new ones.
// - The automatic refresh waits for the interval and a little more, stops with the tab hidden and
//   survives a failure: the last numbers stay, with the time they are from, and the page says so.
//
// /dashboard?id=N
import { computed, nextTick, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { Clock, FolderTree, LayoutDashboard, LoaderCircle, Maximize, Minimize, RefreshCw, TriangleAlert } from 'lucide-vue-next'
import { errMessage } from '~/composables/useApi'
import { useChartTheme } from '~/composables/useChartTheme'
import { dashboardCtx } from '~/composables/dashboardCharts'
import {
  askSourceAgain, beginAsking, emptySlots, oldestRead, pagesAfter, pagesFor, readingData, refreshPause, tookAnswer, tookFailure, type TileSlots,
} from '~/composables/dashboardTiles'
import { useFolderPaths } from '~/composables/useFolderPaths'
import { useFormat } from '~/composables/useFormat'
import { dashboardQuery, formOfQuery, useDashboards, valuesInQuery, type DashboardDetail, type TileNumbers } from '~/composables/useDashboards'
import {
  isPlainState, paramsOfForm, stateAfter, whatListsHold, withChoices,
  type ReportFormValue, type ReportHotspot, type ReportParameter, type ReportState,
} from '~/composables/useReports'
import {
  askedAfterClicks, clickAfter, clickLabel, clickSends, clicksInQuery, clicksQuery, hasFilters, namingTheDataset, sameClicks, settledClicks, valueText,
  type ClickSide, type ClickValue, type TileClick,
} from '~/composables/dashboardClicks'
import DashboardClicks from '~/components/dashboard/DashboardClicks.vue'
import DashboardGrid from '~/components/dashboard/DashboardGrid.vue'
import ReportParamsBar from '~/components/report/ReportParamsBar.vue'

const { t } = useI18n()
const route = useRoute()
const router = useRouter()
const api = useDashboards()
const fmtx = useFormat()
const folderPaths = useFolderPaths()
const { ui } = useChartTheme()

const dashboardId = ref<number | null>(null)
const detail = ref<DashboardDetail | null>(null)
const loading = ref(true)
const notFound = ref(false)
const loadError = ref('')
const folderPath = computed(() => (detail.value ? folderPaths.pathOf(detail.value.project_id) ?? '' : ''))
const tiles = computed(() => detail.value?.content.tiles ?? [])
const tileIds = computed(() => tiles.value.map((tile) => tile.id as string))
const ctx = computed(() => dashboardCtx(ui.value, detail.value?.content.locale || 'en', t))
const announce = ref('')

// ── the parameters: the form, and the address ────────────────────────────────
const parameters = ref<ReportParameter[]>([])
const form = ref<Record<string, ReportFormValue>>({})
// the values the tiles are read with: the address's (the form may hold changes not applied yet)
const chosen = ref<Record<string, ReportFormValue>>({})
const fieldErrors = ref<Record<string, string>>({})
const toParams = (values: Record<string, ReportFormValue>) => paramsOfForm(parameters.value, values)
const queryKey = (q: Record<string, unknown>) =>
  JSON.stringify(Object.keys(q).sort().map((k) => [k, Array.isArray(q[k]) ? q[k] : [q[k]]]))

function formFromUrl() {
  form.value = formOfQuery(parameters.value, valuesInQuery(route.query))
  chosen.value = { ...form.value }
}
function missingRequired(values = form.value): string[] {
  const params = toParams(values)
  return parameters.value.filter((p) => p.required && params[p.name] === undefined).map((p) => p.name)
}

// choices that follow another parameter's value: asked again when a value changes
const followsValues = computed(() => parameters.value.length > 1
  && (detail.value?.content.parameters ?? []).some((p) => !!p.choices?.dataset))
let choicesTimer: ReturnType<typeof setTimeout> | null = null
let choicesSeq = 0
async function refreshChoices() {
  if (dashboardId.value == null || !followsValues.value) return
  const mine = ++choicesSeq
  try {
    const fresh = await api.parameters(dashboardId.value, toParams(form.value))
    if (mine !== choicesSeq) return
    parameters.value = withChoices(parameters.value, fresh)
    const held = whatListsHold(form.value, parameters.value)
    if (held !== form.value) onForm(held)
  } catch { /* the lists keep what they had */ }
}
function onForm(values: Record<string, ReportFormValue>) {
  const changed = parameters.value.filter((p) => JSON.stringify(values[p.name]) !== JSON.stringify(form.value[p.name])).map((p) => p.name)
  form.value = values
  if (changed.length) {
    const errors = { ...fieldErrors.value }
    for (const n of changed) delete errors[n]
    fieldErrors.value = errors
  }
  if (choicesTimer) clearTimeout(choicesTimer)
  choicesTimer = setTimeout(refreshChoices, 400)
}

// «Apply»: the values go in the address (Back returns to these), then the tiles
function apply() {
  const missing = missingRequired()
  if (missing.length) {
    fieldErrors.value = Object.fromEntries(missing.map((n) => [n, t('report.needsValue')]))
    document.getElementById(`rp-field-${missing[0]}`)?.focus()
    return
  }
  if (dashboardId.value == null) return
  const q = { ...dashboardQuery(dashboardId.value, parameters.value, form.value), ...clicksQuery(clicks.value) }
  if (queryKey(q) === queryKey(route.query as Record<string, unknown>)) {
    chosen.value = { ...form.value }
    askAll({ changed: false })
  } else router.push({ path: '/dashboard', query: q })
}

// ── what a click chose: the address holds it ─────────────────────────────────
const clicks = ref<TileClick[]>([])
const canClick = computed(() => hasFilters(tiles.value))
// what the screen said of a value when it was clicked: a table's other page does not say it any more
const saidOf = new Map<string, string>()
const saidKey = (tile: string, on: ClickSide, value: ClickValue) => `${tile}|${on}|${valueText(value)}`

function addressWith(next: TileClick[]) {
  return { ...dashboardQuery(dashboardId.value!, parameters.value, chosen.value), ...clicksQuery(next) }
}
function pick(tile: string, on: ClickSide, value: ClickValue, add: boolean) {
  if (dashboardId.value == null) return
  const answer = slots.value[tile]?.answer
  const said = clickLabel(answer, on, value)
  if (said !== undefined) saidOf.set(saidKey(tile, on, value), said)
  const next = clickAfter(clicks.value, tile, on, value, add)
  if (sameClicks(next, clicks.value)) return
  const name = said ?? String(value ?? t('report.emptyGroup'))
  const title = tiles.value.find((x) => x.id === tile)?.title ?? ''
  const still = next.some((c) => c.tile === tile && c.on === on && c.values.some((v) => valueText(v) === valueText(value)))
  announce.value = still ? t('dashboard.clickChosen', { tile: title, value: name }) : t('dashboard.clickRemoved', { tile: title, value: name })
  router.push({ path: '/dashboard', query: addressWith(next) })
}
function dropClick(tile: string, on: ClickSide) {
  announce.value = t('dashboard.clickCleared')
  router.push({ path: '/dashboard', query: addressWith(clicks.value.filter((c) => !(c.tile === tile && c.on === on))) })
}
function clearClicks() {
  announce.value = t('dashboard.clickCleared')
  router.push({ path: '/dashboard', query: addressWith([]) })
}
// the answer's word on a click: the address follows, with no step for Back and nothing to read again,
// as this answer already counted only what stayed
function settle(answered: Record<string, unknown>) {
  const settled = settledClicks(clicks.value, answered)
  if (!settled) return
  clicks.value = settled
  router.replace({ path: '/dashboard', query: addressWith(settled) })
}

// ── loading ──────────────────────────────────────────────────────────────────
async function load() {
  const id = Number(route.query.id)
  const known = Number.isInteger(id) && id > 0
  dashboardId.value = known ? id : null
  stopAsking()
  stopRefreshing()
  detail.value = null
  parameters.value = []
  clicks.value = []
  saidOf.clear()
  slots.value = {}
  pages.value = {}
  viewState.value = { items: {} }
  problem.value = ''
  waiting.value = false
  refreshFailedAt.value = null
  notFound.value = false
  loadError.value = ''
  if (!known) {
    notFound.value = true
    loading.value = false
    return
  }
  loading.value = true
  try {
    const found = await api.get(id)
    if (dashboardId.value !== id) return
    detail.value = found
    slots.value = emptySlots(found.content.tiles.map((tile) => tile.id))
    // the audit and the count of who reads it, once per opening; never in the way of reading
    api.opened(id).catch(() => {})
    if (found.parameters) {
      const params = await api.parameters(id)
      if (dashboardId.value !== id) return
      parameters.value = params
      // the choices that follow a value given in the link (only the lists: that answer has the
      // link's values as its defaults)
      const given = valuesInQuery(route.query)
      const named = Object.fromEntries(Object.entries(given).filter(([k]) => params.some((p) => p.name === k)))
      if (Object.keys(named).length && params.length > 1) {
        try { parameters.value = withChoices(params, await api.parameters(id, named)) } catch { /* the first answer holds */ }
      }
    }
    formFromUrl()
    clicks.value = clicksInQuery(route.query, found.content.tiles)
    // an old link: what it says of a tile gone or switched off leaves the address as well
    const here = route.query as Record<string, unknown>
    const clean = { ...Object.fromEntries(Object.entries(here).filter(([k]) => !k.startsWith('click.'))), ...clicksQuery(clicks.value) }
    if (queryKey(clean) !== queryKey(here)) router.replace({ path: '/dashboard', query: clean as Record<string, string> })
  } catch (e: any) {
    if ((e?.response?.status ?? e?.statusCode) === 404) notFound.value = true
    else loadError.value = errMessage(e)
    return
  } finally {
    if (dashboardId.value === id) loading.value = false
  }
  showTiles(true)
}

// the tiles need every required value: without one, the form asks for it first
const waiting = ref(false)
function showTiles(changed: boolean) {
  if (!tiles.value.length) return
  problem.value = ''
  waiting.value = missingRequired(chosen.value).length > 0
  if (waiting.value) {
    stopAsking()
    stopRefreshing()
    slots.value = emptySlots(tileIds.value)
    return
  }
  askAll({ changed })
}

watch(() => route.fullPath, () => {
  // another page's address is not this page's business: no «not found» on the way out
  if (route.path !== '/dashboard') return
  const id = Number(route.query.id)
  if (id !== dashboardId.value) {
    load()
    return
  }
  if (!detail.value) return
  const paramsBefore = JSON.stringify(toParams(chosen.value))
  const clicksBefore = clicks.value
  formFromUrl()
  fieldErrors.value = {}
  const clicksNow = clicksInQuery(route.query, tiles.value)
  const sameValues = JSON.stringify(toParams(chosen.value)) === paramsBefore
  clicks.value = clicksNow
  if (sameValues && sameClicks(clicksBefore, clicksNow)) return // the address caught up with the screen
  if (sameValues && !waiting.value) {
    // only a click: the tiles it changes; a table it filters goes back to its first page
    const { ids, still } = askedAfterClicks(namingTheDataset(tiles.value, detail.value.content.datasets), clicksBefore, clicksNow)
    const turned = { ...pages.value }
    for (const id of ids) if (!still.includes(id)) delete turned[id]
    pages.value = turned
    if (ids.length) ask(ids, { changed: true, still })
    return
  }
  refreshChoices()
  pages.value = {}
  showTiles(true)
})

// ── the numbers ──────────────────────────────────────────────────────────────
// not deep: an answer is never changed, only replaced, and a chart's thousands of numbers need no proxies
const slots = shallowRef<TileSlots>({})
const pages = ref<Record<string, number>>({})
const viewState = ref<ReportState>({ items: {} })
const problem = ref('') // a value of the form the server refused: no tile can answer
const asking = ref(0) // requests for the whole dashboard on their way
const refreshFailedAt = ref<string | null>(null)
const reading = computed(() => readingData(slots.value, tiles.value))
const oldest = computed(() => oldestRead(reading.value))
const troubled = computed(() => Object.values(reading.value).filter((s) => s.trouble).length)
// a new question on its way: the form's button says so; the same numbers read again do not stop it
const applying = computed(() => Object.values(slots.value).some((s) => s.waiting === 'new'))
let seq = 0
const inFlight = new Map<number, { ids: string[]; controller: AbortController }>()

const isAbort = (e: any) => e?.name === 'AbortError' || e?.cause?.name === 'AbortError'

function stopAsking() {
  for (const { controller } of inFlight.values()) controller.abort()
  inFlight.clear()
  asking.value = 0
}

/** Asks for some tiles, or all of them (`null`). `changed`: another question than the numbers on
 *  screen answer. What the answer brings counts only for the tiles this is still the latest
 *  request of; a request none of its tiles is waiting for any more is dropped. True when an answer came. */
async function ask(ids: string[] | null, how: { changed: boolean; refresh?: boolean; still?: string[] }): Promise<boolean> {
  const id = dashboardId.value
  if (id == null) return false
  const mine = ++seq
  const asked = ids ?? tileIds.value
  slots.value = beginAsking(slots.value, asked, mine, how.changed, how.still)
  for (const [other, request] of inFlight) {
    if (request.ids.every((tile) => slots.value[tile]?.asked !== other)) {
      request.controller.abort()
      inFlight.delete(other)
    }
  }
  const controller = new AbortController()
  inFlight.set(mine, { ids: asked, controller })
  if (!ids) asking.value++
  try {
    const answer = await api.data(id, {
      params: toParams(chosen.value),
      ...(ids ? { tiles: ids } : {}),
      page: pagesFor(pages.value, ids),
      state: isPlainState(viewState.value) ? null : viewState.value,
      clicks: clickSends(clicks.value),
      refresh: !!how.refresh,
    }, controller.signal)
    if (controller.signal.aborted || dashboardId.value !== id) return false
    slots.value = tookAnswer(slots.value, mine, answer.tiles, { keep: !how.changed, still: how.still })
    pages.value = pagesAfter(pages.value, slots.value, mine, answer.tiles)
    settle(Object.fromEntries(Object.entries(answer.tiles).filter(([tile]) => slots.value[tile]?.asked === mine)))
    if (!ids) {
      refreshFailedAt.value = null
      problem.value = ''
    }
    return true
  } catch (e: any) {
    if (isAbort(e) || dashboardId.value !== id) return false
    const status = e?.response?.status ?? e?.statusCode
    const detailOf = e?.data?.detail
    if (status === 404) { // gone, or no longer for this person: nothing of it stays
      stopRefreshing()
      detail.value = null
      notFound.value = true
      return false
    }
    if (how.changed && status === 400 && typeof detailOf?.parameter === 'string') {
      // a value that does not fit: the form says which, and the numbers of the values before go
      fieldErrors.value = { ...fieldErrors.value, [detailOf.parameter]: errMessage(e) }
      problem.value = errMessage(e)
      slots.value = emptySlots(tileIds.value)
      return false
    }
    slots.value = tookFailure(slots.value, mine, errMessage(e), { keep: !how.changed, still: how.still })
    if (!ids && !how.changed) refreshFailedAt.value = new Date().toISOString()
    return false
  } finally {
    inFlight.delete(mine)
    if (!ids) asking.value = Math.max(0, asking.value - 1)
  }
}

async function askAll(how: { changed: boolean; refresh?: boolean }) {
  const answered = await ask(null, how)
  scheduleRefresh()
  return answered
}

// Refresh, by hand: the server reads again at most every 10 seconds per dashboard, for everyone;
// past that the answer is what is kept, without an error
async function refreshNow() {
  if (asking.value || waiting.value) return
  if (await askAll({ changed: false, refresh: true })) announce.value = t('dashboard.updated')
}

// a tile's own actions: only that tile is asked for
function retryTile(id: string) {
  ask([id], { changed: false })
}
function changeView(id: string, hot: Pick<ReportHotspot, 'kind' | 'column' | 'key' | 'closed'>, keepPage: boolean) {
  viewState.value = stateAfter(viewState.value, { ...hot, item: id } as ReportHotspot)
  if (!keepPage) pages.value = { ...pages.value, [id]: 0 }
  ask([id], { changed: true })
}
function sortTile(id: string, column: number) {
  changeView(id, { kind: 'sort', column } as any, false)
  const sort = viewState.value.items[id]?.sort
  const answer = slots.value[id]?.answer as TileNumbers | null
  const name = answer?.columns?.[column]?.header ?? t('report.columnN', { n: column + 1 })
  announce.value = sort ? t(sort.dir === 'asc' ? 'report.sortedAsc' : 'report.sortedDesc', { name }) : t('report.sortCleared')
}
function toggleGroup(id: string, key: unknown[], closed: boolean) {
  changeView(id, { kind: 'toggle', key, closed } as any, true)
  const name = String(key[key.length - 1] ?? t('report.emptyGroup'))
  announce.value = t(closed ? 'report.groupOpened' : 'report.groupClosed', { name })
}
function pageTile(id: string, page: number) {
  pages.value = { ...pages.value, [id]: Math.max(0, page) }
  ask([id], { changed: true })
}

// ── the automatic refresh ────────────────────────────────────────────────────
const interval = computed(() => detail.value?.content.refresh_seconds ?? 0)
let refreshTimer: ReturnType<typeof setTimeout> | null = null
let dueAt = 0
let left = false // the page is gone: an answer on its way must not arm the timer again

function stopRefreshing() {
  if (refreshTimer) clearTimeout(refreshTimer)
  refreshTimer = null
  dueAt = 0
}
function scheduleRefresh() {
  stopRefreshing()
  if (left || !interval.value || !detail.value || waiting.value) return
  arm(refreshPause(interval.value))
}
function arm(pause: number) {
  dueAt = Date.now() + pause
  if (refreshTimer) clearTimeout(refreshTimer)
  // with the tab hidden nothing is armed: it would read for no one
  refreshTimer = document.hidden ? null : setTimeout(tick, pause)
}
async function tick() {
  refreshTimer = null
  if (left || document.hidden || !detail.value) return
  if (asking.value) { // someone asked just now: that answer is this tick's
    scheduleRefresh()
    return
  }
  const before = reading.value
  if (await ask(null, { changed: false }) && askSourceAgain(before, reading.value)) {
    await ask(null, { changed: false, refresh: true })
  }
  scheduleRefresh()
}
function onVisibility() {
  if (document.hidden) {
    if (refreshTimer) clearTimeout(refreshTimer)
    refreshTimer = null
  } else if (interval.value && detail.value && !waiting.value && dueAt) {
    // back in sight: what was due while nobody looked, in a moment and not all together
    arm(Math.max(dueAt - Date.now(), 0) + Math.round(Math.random() * 2000))
  }
}
const everyLabel = computed(() => {
  const s = interval.value
  if (!s) return ''
  return s % 60 === 0 ? t('dashboard.everyMinutes', { n: s / 60 }, s / 60) : t('dashboard.everySeconds', { n: s })
})
const timeOf = (iso: string | null) => (iso ? fmtx.dateTime(iso, { seconds: true }) : '')

// ── full screen, for a wall ──────────────────────────────────────────────────
const wall = ref(false)
const exitBtn = ref<HTMLButtonElement | null>(null)
const enterBtn = ref<HTMLButtonElement | null>(null)
let inBrowserFullScreen = false
async function enterWall() {
  wall.value = true
  nextTick(() => exitBtn.value?.focus())
  // the page covers the window either way; where the browser agrees, the screen too
  try {
    await document.documentElement.requestFullscreen?.()
    inBrowserFullScreen = !!document.fullscreenElement
  } catch { /* the window it is */ }
}
function leaveWall() {
  wall.value = false
  if (document.fullscreenElement) document.exitFullscreen().catch(() => {})
  inBrowserFullScreen = false
  nextTick(() => enterBtn.value?.focus())
}
function onFullScreenChange() {
  if (inBrowserFullScreen && !document.fullscreenElement) leaveWall()
}
function onKey(e: KeyboardEvent) {
  if (e.key === 'Escape' && wall.value) leaveWall()
}

// ── the page's life ──────────────────────────────────────────────────────────
onMounted(() => {
  document.addEventListener('visibilitychange', onVisibility)
  document.addEventListener('fullscreenchange', onFullScreenChange)
  document.addEventListener('keydown', onKey)
  folderPaths.load()
  load()
})
onBeforeUnmount(() => {
  left = true
  stopAsking()
  stopRefreshing()
  if (choicesTimer) clearTimeout(choicesTimer)
  document.removeEventListener('visibilitychange', onVisibility)
  document.removeEventListener('fullscreenchange', onFullScreenChange)
  document.removeEventListener('keydown', onKey)
  if (document.fullscreenElement) document.exitFullscreen().catch(() => {})
})
</script>

<template>
  <AppShell fluid>
    <div class="dv" :class="{ wall }">
      <!-- a dashboard that is not there, or not for this person: the same answer -->
      <div v-if="notFound" class="gone" role="alert">
        <LayoutDashboard :size="28" aria-hidden="true" />
        <h1>{{ $t('dashboard.notFoundTitle') }}</h1>
        <p>{{ $t('dashboard.notFoundBody') }}</p>
        <NuxtLink to="/" class="back">{{ $t('dashboard.backToExplore') }}</NuxtLink>
      </div>
      <div v-else-if="loadError" class="gone" role="alert">
        <TriangleAlert :size="28" aria-hidden="true" />
        <h1>{{ $t('dashboard.loadFailed') }}</h1>
        <p>{{ loadError }}</p>
        <button type="button" class="retry" @click="load"><RefreshCw :size="14" aria-hidden="true" /> {{ $t('dashboard.tryAgain') }}</button>
      </div>

      <template v-else-if="detail">
        <header class="head">
          <h1 :title="detail.name"><LayoutDashboard :size="18" aria-hidden="true" /><span class="name">{{ detail.name }}</span></h1>
          <NuxtLink v-if="folderPath && !wall" :to="`/?folder=${detail.project_id}`" class="where">
            <FolderTree :size="13" aria-hidden="true" /> {{ folderPath }}
          </NuxtLink>
          <div class="actions">
            <span v-if="oldest" class="when" :title="$t('dashboard.dataFromTitle')">
              <Clock :size="13" aria-hidden="true" /> {{ $t('dashboard.dataFrom', { time: timeOf(oldest) }) }}
            </span>
            <span v-if="everyLabel && !wall" class="every">{{ everyLabel }}</span>
            <template v-if="!wall">
              <button type="button" :disabled="!!asking || waiting || !tiles.length" :title="$t('dashboard.refreshTitle')" @click="refreshNow">
                <LoaderCircle v-if="asking" :size="14" class="spin" aria-hidden="true" /><RefreshCw v-else :size="14" aria-hidden="true" />
                <span class="lbl">{{ $t('dashboard.refresh') }}</span>
              </button>
              <button ref="enterBtn" type="button" :disabled="!tiles.length" :title="$t('dashboard.fullScreenTitle')" @click="enterWall">
                <Maximize :size="14" aria-hidden="true" />
                <span class="lbl">{{ $t('dashboard.fullScreen') }}</span>
              </button>
              <!-- Download joins here with the PDF and the image, Edit (detail.editable) with the designer -->
            </template>
            <button v-else ref="exitBtn" type="button" class="mini leave" :title="$t('dashboard.leaveFullScreen')" :aria-label="$t('dashboard.leaveFullScreen')" @click="leaveWall">
              <Minimize :size="14" aria-hidden="true" />
            </button>
          </div>
        </header>
        <span class="sr-only" role="status" aria-live="polite">{{ announce }}</span>
        <p v-if="detail.description && !wall" class="description">{{ detail.description }}</p>

        <p v-if="refreshFailedAt" class="notice" role="status">
          <TriangleAlert :size="14" aria-hidden="true" />
          <span>{{ oldest ? $t('dashboard.refreshFailed', { at: timeOf(refreshFailedAt), time: timeOf(oldest) }) : $t('dashboard.refreshFailedNoNumbers', { at: timeOf(refreshFailedAt) }) }}</span>
          <button v-if="!wall" type="button" class="mini" :disabled="!!asking" @click="refreshNow">{{ $t('dashboard.tryAgain') }}</button>
        </p>
        <p v-else-if="troubled" class="notice" role="status">
          <TriangleAlert :size="14" aria-hidden="true" />
          <span>{{ $t('dashboard.tilesOlder', { n: troubled }, troubled) }}</span>
        </p>

        <ReportParamsBar
          v-if="parameters.length && !wall"
          :parameters="parameters"
          :model-value="form"
          :errors="fieldErrors"
          :busy="applying"
          :submit-label="$t('dashboard.apply')"
          @update:model-value="onForm"
          @submit="apply"
        />

        <DashboardClicks
          v-if="canClick && !waiting && !problem"
          :clicks="clicks"
          :tiles="tiles"
          :slots="slots"
          :said-of="saidOf"
          :hint="!wall"
          @remove="dropClick"
          @clear="clearClicks"
        />

        <div v-if="!tiles.length" class="empty">
          <LayoutDashboard :size="32" aria-hidden="true" />
          <h2>{{ $t('dashboard.emptyTitle') }}</h2>
          <p>{{ $t('dashboard.emptyBody') }}</p>
        </div>
        <div v-else-if="problem" class="state" role="alert">
          <TriangleAlert :size="22" class="warnicon" aria-hidden="true" />
          <h2>{{ $t('dashboard.badValueTitle') }}</h2>
          <p>{{ problem }}</p>
        </div>
        <div v-else-if="waiting" class="state">
          <LayoutDashboard :size="22" aria-hidden="true" />
          <h2>{{ $t('dashboard.needsValuesTitle') }}</h2>
          <p>{{ $t('dashboard.needsValuesBody') }}</p>
        </div>
        <DashboardGrid
          v-else
          :tiles="tiles"
          :slots="slots"
          :ctx="ctx"
          :said-above="!!refreshFailedAt"
          :clicks="clicks"
          @pick="pick"
          @retry="retryTile"
          @sort="sortTile"
          @toggle="toggleGroup"
          @page="pageTile"
        />
      </template>

      <p v-else-if="loading" class="loading" role="status">{{ $t('dashboard.loading') }}</p>
    </div>
  </AppShell>
</template>

<style scoped>
.dv { display: flex; flex-direction: column; gap: 10px; min-width: 0; }
.head { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; min-width: 0; }
.head h1 { display: flex; align-items: center; gap: 8px; margin: 0; font-size: 16px; font-weight: 600; min-width: 0; color: var(--text); }
.head h1 svg { flex-shrink: 0; color: var(--accent); }
.name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.where { display: inline-flex; align-items: center; gap: 5px; font-size: 12px; color: var(--muted); }
.where:hover { color: var(--text); }
.actions { display: flex; align-items: center; gap: 8px; margin-left: auto; flex-wrap: wrap; justify-content: flex-end; }
.actions button { display: inline-flex; align-items: center; gap: 6px; white-space: nowrap; }
.when, .every { display: inline-flex; align-items: center; gap: 5px; font-size: 12px; color: var(--muted); font-variant-numeric: tabular-nums; white-space: nowrap; }
.every::before { content: "·"; margin-right: 3px; }
.description { margin: -4px 0 0; color: var(--muted); font-size: 12.5px; line-height: 1.5; max-width: 110ch; }
.notice {
  margin: 0; padding: 6px 10px; display: flex; align-items: center; gap: 8px; font-size: 12.5px; line-height: 1.4; color: var(--text);
  background: var(--panel); border: 1px solid color-mix(in srgb, var(--warning) 40%, var(--border)); border-radius: 8px;
}
.notice svg { flex-shrink: 0; color: var(--warning); }
.notice span { flex: 1; min-width: 0; }
.notice .mini { flex-shrink: 0; }
.empty {
  margin-top: 8px; padding: 56px 24px; display: flex; flex-direction: column; align-items: center; gap: 8px; text-align: center;
  color: var(--muted); border: 1px dashed var(--border); border-radius: var(--radius, 8px);
}
.empty h2, .state h2 { margin: 6px 0 0; font-size: 15px; font-weight: 600; color: var(--text); }
.empty p, .state p { margin: 0; line-height: 1.5; }
.state { margin: 8vh auto 0; max-width: 560px; padding: 0 24px; display: flex; flex-direction: column; align-items: center; gap: 8px; text-align: center; color: var(--muted); overflow-wrap: anywhere; }
.warnicon { color: var(--warning); }
.gone { margin: 12vh auto 0; max-width: 520px; display: flex; flex-direction: column; align-items: center; gap: 8px; text-align: center; color: var(--muted); }
.gone h1 { margin: 6px 0 0; font-size: 17px; color: var(--text); }
.gone p { margin: 0; line-height: 1.5; }
.back { margin-top: 8px; color: var(--accent-hi); }
.retry { display: inline-flex; align-items: center; gap: 6px; margin-top: 8px; }
.loading { color: var(--muted); }

/* the wall: the window to itself, no menu, the title and the time of the last update */
.dv.wall {
  position: fixed; inset: 0; z-index: 200; padding: 14px 16px; overflow: auto; box-sizing: border-box; background: var(--bg);
}
.wall .head h1 { font-size: 19px; font-weight: 700; }
.wall .when { font-size: 13px; }
.leave { opacity: 0.6; }
.leave:hover, .leave:focus-visible { opacity: 1; }

/* narrower: the labels of the actions give way to their icons */
@media (max-width: 900px) {
  .actions .lbl { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); white-space: nowrap; }
}
@media (max-width: 700px) {
  .actions { margin-left: 0; width: 100%; justify-content: flex-start; }
  .every { display: none; }
}
</style>
