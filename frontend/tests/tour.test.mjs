// The guided tour: which steps a person gets, how it moves, what the browser remembers, when it
// starts by itself and where the card goes. Without a browser: the pure rules of useTour.ts.
// Run: npm test
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync, readdirSync } from 'node:fs'
import { register } from 'node:module'

register('./alias-hooks.mjs', import.meta.url)
const T = await import('../composables/useTour.ts')
const { NAV_SECTIONS } = await import('../composables/appKeys.ts')
const messages = {}
for (const code of ['en', 'it', 'fr', 'de', 'es']) messages[code] = (await import(`../i18n/locales/${code}.gen.ts`)).default

const folders = [
  { id: 1, name: 'Sample', parent_id: null },
  { id: 2, name: 'Flows', parent_id: 1 },
  { id: 3, name: 'Private tables', parent_id: 1 },
  { id: 4, name: 'Public tables', parent_id: 1 },
  { id: 9, name: 'Flows', parent_id: 8 },
]
const sample = T.tourFindSample(folders)
const lists = {
  flows: [{ id: 21, name: 'Margin by Category and Month' }],
  datasources: [{ id: 31, name: 'orders' }, { id: 32, name: 'order_rows' }],
  connections: [{ id: 41, name: 'Sample ERP (Postgres)' }],
  views: [{ id: 51, name: 'Margin by category — month by month' }],
  reports: [{ id: 61, name: 'Margin review' }],
  dashboards: [{ id: 71, name: 'Sales overview' }],
}
const all = ['explore', 'connections', 'datasources', 'flows', 'viewer', 'reports', 'dashboards', 'lineage', 'runs', 'queue', 'admin']
const full = T.tourResolveObjects(sample, lists)
const ids = (places, objects = full) => T.tourPlan(places, objects).map((s) => s.id)

test('the sample is the top-level folder of that name, with its subfolders; another person\'s folder or none is no sample', () => {
  assert.deepEqual(sample, { root: 1, flows: 2, tables: 3 })
  assert.equal(T.tourFindSample([{ id: 5, name: 'Sample', parent_id: 1 }]), null)
  assert.equal(T.tourFindSample([{ id: 5, name: 'Sample', parent_id: null, personal_of: 7 }]), null)
  assert.equal(T.tourFindSample([]), null)
  assert.equal(T.tourFindSample([{ id: 5, name: ' sample ', parent_id: null }]).root, 5)
})

test('the objects are found by name, and a missing one is null', () => {
  assert.deepEqual(full, { folder: 1, flow: 21, datasource: 31, connection: 41, view: 51, report: 61, dashboard: 71 })
  const none = T.tourResolveObjects(sample, { ...lists, reports: [], flows: [{ id: 3, name: 'Other' }] })
  assert.equal(none.report, null)
  assert.equal(none.flow, null)
  assert.equal(none.dashboard, 71)
})

test('the whole plan starts with the welcome and ends with the end, in the order of the walk', () => {
  const plan = ids(all)
  assert.equal(plan[0], 'welcome')
  assert.equal(plan.at(-1), 'end')
  assert.ok(plan.indexOf('topbar') < plan.indexOf('explore') && plan.indexOf('explore') < plan.indexOf('editorCanvas'))
  assert.ok(plan.indexOf('editorOutput') < plan.indexOf('flows') && plan.indexOf('flows') < plan.indexOf('viewerFields'))
  assert.ok(plan.indexOf('dashboard') < plan.indexOf('lineage') && plan.indexOf('lineage') < plan.indexOf('runs') && plan.indexOf('runs') < plan.indexOf('admin'))
  assert.equal(new Set(plan).size, plan.length)
})

test('a person without Administration or the Queue does not get those steps', () => {
  const plain = ids(all.filter((p) => p !== 'admin' && p !== 'queue'))
  assert.ok(!plain.includes('admin') && !plain.includes('queue'))
  assert.ok(plain.includes('runs'))
  assert.ok(ids(all).includes('admin') && ids(all).includes('queue'))
})

test('a step whose sample object is missing is left out, with nothing else lost', () => {
  const noFlow = ids(all, { ...full, flow: null })
  assert.ok(noFlow.every((id) => !id.startsWith('editor')))
  assert.ok(noFlow.includes('flows') && noFlow.includes('report'))
  assert.ok(!ids(all, { ...full, report: null }).includes('report'))
  assert.ok(!ids(all, { ...full, view: null }).includes('viewerFields'))
  assert.ok(!ids(all, { ...full, connection: null }).includes('connections'))
})

test('the routes carry the objects\' ids, and an anchor list names the object first and the page after', () => {
  const plan = Object.fromEntries(T.tourPlan(all, full).map((s) => [s.id, s]))
  assert.equal(plan.folder.route, '/explore?folder=1')
  assert.equal(plan.editorCanvas.route, '/editor?flow=21')
  assert.equal(plan.viewerFields.route, '/viewer?view=51')
  assert.equal(plan.report.route, '/report?id=61')
  assert.equal(plan.dashboard.route, '/dashboard?id=71')
  assert.deepEqual(plan.datasources.anchors, ['datasource-31', 'datasources-list'])
  assert.deepEqual(plan.connections.anchors, ['connection-41', 'connections-list'])
  assert.equal(plan.welcome.route, null)
  assert.deepEqual(plan.welcome.anchors, [])
})

test('every route of a step is a place the app has, and the places it asks for exist in the top bar', () => {
  const paths = new Set(['/', ...NAV_SECTIONS.map((s) => s.to), '/editor', '/report', '/dashboard'])
  for (const s of T.tourPlan(all, full)) if (s.route) assert.ok(paths.has(s.route.split('?')[0]), s.route)
  const places = new Set(NAV_SECTIONS.map((s) => s.id))
  for (const s of T.TOUR_STEPS) if (s.place) assert.ok(places.has(s.place), s.place)
})

test('every anchor a step names is on a component of the app', () => {
  const read = (p) => readFileSync(new URL(`../${p}`, import.meta.url), 'utf8')
  const code = ['components', 'pages'].flatMap((d) => {
    const walk = (dir) => readdirSync(new URL(`../${dir}/`, import.meta.url), { withFileTypes: true })
      .flatMap((e) => (e.isDirectory() ? walk(`${dir}/${e.name}`) : e.name.endsWith('.vue') ? [read(`${dir}/${e.name}`)] : []))
    return walk(d)
  }).join('\n')
  const wanted = new Set(T.tourPlan(all, full).flatMap((s) => s.anchors).map((a) => a.replace(/-\d+$/, '')))
  for (const a of wanted) {
    const plain = new RegExp(`data-tour="${a}"`).test(code)
    const dynamic = code.includes(`:data-tour="\`${a}-\${`)
    assert.ok(plain || dynamic, `no component carries data-tour="${a}"`)
  }
})

test('moving: forward to the end, back never below the first', () => {
  assert.equal(T.tourNextIndex(0, 3), 1)
  assert.equal(T.tourNextIndex(2, 3), null)
  assert.equal(T.tourBackIndex(0), 0)
  assert.equal(T.tourBackIndex(2), 1)
  const plan = T.tourPlan(all, full)
  assert.equal(T.tourIndexOf(plan, 'explore'), plan.findIndex((s) => s.id === 'explore'))
  assert.equal(T.tourIndexOf(plan, 'gone'), 0)
})

test('what the browser remembers: read back, kept per person, and a broken value is nothing', () => {
  const mem = new Map()
  const store = { getItem: (k) => mem.get(k) ?? null, setItem: (k, v) => mem.set(k, v) }
  assert.equal(T.tourLoad(7, store), null)
  T.tourSave(7, { status: 'progress', step: 'home' }, store)
  T.tourSave(8, { status: 'skipped' }, store)
  assert.deepEqual(T.tourLoad(7, store), { status: 'progress', step: 'home' })
  assert.deepEqual(T.tourLoad(8, store), { status: 'skipped' })
  assert.notEqual(T.tourStorageKey(7), T.tourStorageKey(8))
  assert.equal(T.tourDecode('not json'), null)
  assert.equal(T.tourDecode('{"status":"progress"}'), null)
  assert.equal(T.tourDecode('{"status":"other"}'), null)
  assert.deepEqual(T.tourDecode(T.tourEncode({ status: 'done' })), { status: 'done' })
})

test('a storage that throws is no reason to break: nothing read, nothing kept', () => {
  const broken = { getItem() { throw new Error('blocked') }, setItem() { throw new Error('full') } }
  assert.equal(T.tourLoad(1, broken), null)
  assert.doesNotThrow(() => T.tourSave(1, { status: 'done' }, broken))
  assert.equal(T.tourLoad(1, null), null)
})

test('it starts by itself only on a first visit with the sample, on a wide page, away from the public ones, with nothing modal open', () => {
  const ok = { stored: null, sampleFound: true, width: 1280, path: '/', modalOpen: false }
  assert.equal(T.tourShouldAutostart(ok), true)
  assert.equal(T.tourShouldAutostart({ ...ok, stored: { status: 'skipped' } }), false)
  assert.equal(T.tourShouldAutostart({ ...ok, stored: { status: 'done' } }), false)
  assert.equal(T.tourShouldAutostart({ ...ok, stored: { status: 'progress', step: 'home' } }), false)
  assert.equal(T.tourShouldAutostart({ ...ok, sampleFound: false }), false)
  assert.equal(T.tourShouldAutostart({ ...ok, width: 899 }), false)
  assert.equal(T.tourShouldAutostart({ ...ok, width: 900 }), true)
  assert.equal(T.tourShouldAutostart({ ...ok, modalOpen: true }), false)
  for (const path of ['/login', '/share/abc', '/auth/callback']) assert.equal(T.tourShouldAutostart({ ...ok, path }), false, path)
})

test('the card goes below the anchor, else above, else beside, else in the corner; with no anchor, near the centre', () => {
  const vp = { w: 1200, h: 800 }
  const card = { w: 360, h: 200 }
  const top = T.tourPlaceCard({ x: 100, y: 20, w: 300, h: 40 }, card, vp)
  assert.equal(top.side, 'below')
  assert.ok(top.y >= 20 + 40)
  const bottom = T.tourPlaceCard({ x: 100, y: 700, w: 300, h: 60 }, card, vp)
  assert.equal(bottom.side, 'above')
  assert.ok(bottom.y + card.h <= 700)
  const left = T.tourPlaceCard({ x: 20, y: 20, w: 300, h: 760 }, card, vp)
  assert.equal(left.side, 'right')
  assert.ok(left.x >= 320)
  const right = T.tourPlaceCard({ x: 880, y: 20, w: 300, h: 760 }, card, vp)
  assert.equal(right.side, 'left')
  const whole = T.tourPlaceCard({ x: 0, y: 0, w: 1200, h: 800 }, card, vp)
  assert.equal(whole.side, 'inside')
  const none = T.tourPlaceCard(null, card, vp)
  assert.equal(none.side, 'center')
  assert.ok(Math.abs(none.x - 420) < 1)
})

test('the card never leaves the window, however the anchor sits', () => {
  const vp = { w: 700, h: 500 }
  const card = { w: 360, h: 200 }
  for (const t of [null, { x: 0, y: 0, w: 40, h: 40 }, { x: 660, y: 460, w: 40, h: 40 }, { x: 300, y: 200, w: 100, h: 100 }, { x: -50, y: -50, w: 900, h: 700 }]) {
    const p = T.tourPlaceCard(t, card, vp)
    assert.ok(p.x >= 12 && p.x + card.w <= vp.w - 12 + 0.001, `x ${p.x}`)
    assert.ok(p.y >= 12 && p.y + card.h <= vp.h - 12 + 0.001, `y ${p.y}`)
  }
})

test('the spotlight is the anchor with a little air', () => {
  assert.deepEqual(T.tourSpotBox({ x: 10, y: 10, w: 100, h: 50 }), { x: 6, y: 6, w: 108, h: 58 })
})

test('the tour\'s words are in the five languages, with the same steps and the same placeholders', () => {
  const placeholders = (s) => [...s.matchAll(/\{(\w+)\}/g)].map((m) => m[1]).sort().join()
  const en = messages.en.tour
  const stepIds = T.TOUR_STEPS.map((s) => s.id)
  assert.deepEqual(Object.keys(en.steps), stepIds)
  for (const code of ['it', 'fr', 'de', 'es']) {
    const tour = messages[code].tour
    assert.deepEqual(Object.keys(tour).sort(), Object.keys(en).sort(), code)
    assert.deepEqual(Object.keys(tour.steps), stepIds, code)
    for (const k of Object.keys(en)) if (k !== 'steps') assert.equal(placeholders(tour[k]), placeholders(en[k]), `${code}.${k}`)
    for (const id of stepIds) for (const part of ['title', 'text']) {
      assert.ok(tour.steps[id][part].length > 3, `${code}.${id}.${part}`)
      if (part === 'text') assert.notEqual(tour.steps[id][part], en.steps[id][part], `${code}.${id}.${part} is still English`)
    }
  }
})

test('the tour\'s English speaks in whole sentences, with no long dash between words and no step of more than four sentences', () => {
  const en = messages.en.tour
  for (const [id, s] of Object.entries(en.steps)) {
    assert.ok(!/ [—–] /.test(s.title + s.text), `${id}: a dash for an aside`)
    assert.ok(s.text.trim().endsWith('.'), id)
    const sentences = s.text.split(/(?<=[.!?])\s+/).length
    assert.ok(sentences >= 2 && sentences <= 4, `${id}: ${sentences} sentences`)
  }
})

test('main.css and finishes.css are untouched by the tour: its card has a solid ground of its own', () => {
  const host = readFileSync(new URL('../components/tour/TourHost.vue', import.meta.url), 'utf8')
  assert.match(host, /background:\s*var\(--panel\)/)
  assert.doesNotMatch(host, /#[0-9a-fA-F]{3,8}\b/)
  assert.doesNotMatch(host, /border-radius:\s*(6|8|10)px/)
})


test('the tour is mounted in the browser only', () => {
  // rendered on the server it reached for the window and the sign-in page answered 500: the health
  // probes ask for that page, and the pods of a release with it never became ready
  const app = readFileSync(new URL('../app.vue', import.meta.url), 'utf8')
  assert.match(app, /<ClientOnly>\s*<TourHost \/>\s*<\/ClientOnly>/)
})

test('an optional step the page does not have leaves the plan, so the count never jumps a number', () => {
  const plan = ['a', 'b', 'c', 'd']
  // forward: the step after takes its place, at the same number
  assert.deepEqual(T.tourDropMissing(plan, 1, 1), { plan: ['a', 'c', 'd'], index: 1 })
  // back: the step before
  assert.deepEqual(T.tourDropMissing(plan, 2, -1), { plan: ['a', 'b', 'd'], index: 1 })
  // the last one going forward: the tour is over
  assert.deepEqual(T.tourDropMissing(plan, 3, 1), { plan: ['a', 'b', 'c'], index: null })
  // the first one going back: there is nothing before, the one after is shown
  assert.deepEqual(T.tourDropMissing(plan, 0, -1), { plan: ['b', 'c', 'd'], index: 0 })
  assert.deepEqual(T.tourDropMissing(['a'], 0, -1), { plan: [], index: null })
  assert.deepEqual(plan, ['a', 'b', 'c', 'd'], 'the plan given is not changed')
})

test('keys on the card: no step goes by unseen, Esc leaves, the first step has no way back', () => {
  const shown = { ready: true, first: false, onButton: false }
  assert.equal(T.tourCardKey({ key: 'ArrowRight' }, shown), 'next')
  assert.equal(T.tourCardKey({ key: 'Enter' }, shown), 'next')
  assert.equal(T.tourCardKey({ key: 'ArrowLeft' }, shown), 'back')
  assert.equal(T.tourCardKey({ key: 'Escape' }, shown), 'leave')
  // Enter on a button is the button's own
  assert.equal(T.tourCardKey({ key: 'Enter' }, { ...shown, onButton: true }), null)
  assert.equal(T.tourCardKey({ key: 'ArrowLeft' }, { ...shown, first: true }), null)
  // the step is still arriving (the card has the focus and is not shown): nothing moves, but Esc still leaves
  const arriving = { ...shown, ready: false }
  assert.equal(T.tourCardKey({ key: 'ArrowRight' }, arriving), null)
  assert.equal(T.tourCardKey({ key: 'Enter' }, arriving), null)
  assert.equal(T.tourCardKey({ key: 'Escape' }, arriving), 'leave')
  // a key held down, or one with a modifier, is not the tour's
  assert.equal(T.tourCardKey({ key: 'ArrowRight', repeat: true }, shown), null)
  assert.equal(T.tourCardKey({ key: 'Escape', repeat: true }, shown), null)
  assert.equal(T.tourCardKey({ key: 'ArrowRight', altKey: true }, shown), null)
  assert.equal(T.tourCardKey({ key: 'x' }, shown), null)
})

test('the keys that act on a list stay on the card; the letters go on to the app\'s own keys', () => {
  for (const key of ['Backspace', 'Delete', 'ArrowDown', 'ArrowUp', 'Enter', ' ', 'Escape', 'Home', 'End']) assert.equal(T.tourKeepsKey({ key }), true, key)
  for (const key of ['/', '?', 'g', 'e', ',', '.', 'Tab']) assert.equal(T.tourKeepsKey({ key }), false, key)
  assert.equal(T.tourKeepsKey({ key: 'Backspace', ctrlKey: true }), false)
})

test('a person found without the sample is not asked about again in the session; found, the note goes', () => {
  const kept = new Map()
  const store = { getItem: (k) => kept.get(k) ?? null, setItem: (k, v) => kept.set(k, v), removeItem: (k) => kept.delete(k) }
  assert.equal(T.tourKnowsNoSample(7, store), false)
  T.tourNoteSample(7, false, store)
  assert.equal(T.tourKnowsNoSample(7, store), true)
  assert.equal(T.tourKnowsNoSample(8, store), false, 'kept per person')
  T.tourNoteSample(7, true, store)
  assert.equal(T.tourKnowsNoSample(7, store), false)
  const broken = { getItem() { throw new Error('no') }, setItem() { throw new Error('no') }, removeItem() { throw new Error('no') } }
  assert.equal(T.tourKnowsNoSample(7, broken), false)
  assert.doesNotThrow(() => T.tourNoteSample(7, false, broken))
})

test('the host: Esc leaves and never skips, a refused navigation ends the tour, a change of person forgets it', () => {
  const host = readFileSync(new URL('../components/tour/TourHost.vue', import.meta.url), 'utf8')
  const script = host.slice(0, host.indexOf('<template>'))
  // «never again» is the button's alone
  assert.equal((script.match(/tour\.skip\(\)/g) ?? []).length, 0, 'no key skips the tour')
  assert.match(host, /@click="tour\.skip\(\)"/)
  assert.match(script, /refused = await router\.push\(s\.route\)/)
  assert.match(script, /if \(refused\) \{[^}]*tour\.leave\(\)/)
  assert.match(script, /tourCardKey\(e, \{ ready: ready\.value/)
  // the card is a dialog the app's keys do not stop for
  assert.match(host, /role="dialog"\s+data-tour-card/)
  const keys = readFileSync(new URL('../components/ui/AppKeys.vue', import.meta.url), 'utf8')
  assert.match(keys, /\[role="dialog"\]:not\(\[data-tour-card\]\)/)
  // what the tab keeps per person includes the tour
  const auth = readFileSync(new URL('../composables/useAuth.ts', import.meta.url), 'utf8')
  assert.match(auth.slice(auth.indexOf('function forgetPerUserCaches'), auth.indexOf('export function useAuth')), /resetTour\(\)/)
})
