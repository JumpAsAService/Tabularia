// The report designer's logic that has no screen: the server's check while one edits
// (composables/reportLiveCheck.ts), the problems of a refused save or preview mapped to
// their item and field (reportDesignModel.ts), and the expressions rewritten when a
// dataset or a parameter is renamed (reportExpressions.ts).
// Run: npm test (Node strips the types of the modules it imports).
import { register } from 'node:module'
import { test } from 'node:test'
import assert from 'node:assert/strict'

import { createLiveCheck } from '../composables/reportLiveCheck.ts'
import { emptyDefinition, problemsOf, problemFromText, withSeen, liveShown, stillReported } from '../composables/reportDesignModel.ts'
import { checkExpression, renameEverywhere, renameInExpression, stillWriting } from '../composables/reportExpressions.ts'

register('./alias-hooks.mjs', import.meta.url)
const { createDesignStore } = await import('../composables/reportDesignStore.ts')
const { newItem } = await import('../composables/reportDesignModel.ts')

// ── a clock and a server we drive by hand ───────────────────────────────────
function fakeClock () {
  let now = 0
  const due = []
  return {
    set: (fn, ms) => { const h = { at: now + ms, fn }; due.push(h); return h },
    clear: (h) => { const i = due.indexOf(h); if (i >= 0) due.splice(i, 1) },
    async tick (ms) {
      now += ms
      for (;;) {
        due.sort((a, b) => a.at - b.at)
        const next = due[0]
        if (!next || next.at > now) break
        due.shift()
        next.fn()
        await flush()
      }
      await flush()
    },
  }
}
const flush = () => new Promise((resolve) => setImmediate(resolve))

/** A gateway: each call waits until it is answered by the test. */
function fakeServer () {
  const calls = []
  return {
    calls,
    call: (definition, signal) => new Promise((resolve, reject) => {
      const c = { definition, signal, resolve, reject }
      signal.addEventListener('abort', () => { c.aborted = true; reject(Object.assign(new Error('aborted'), { name: 'AbortError' })) })
      calls.push(c)
    }),
  }
}

function harness ({ delay = 600, timeout = 8000 } = {}) {
  const clock = fakeClock()
  const server = fakeServer()
  const state = { text: 'v1', problems: null, clears: 0 }
  const live = createLiveCheck({
    call: server.call,
    current: () => state.text,
    onProblems: (list, text) => { state.problems = { list, text } },
    onClear: () => { state.clears++ },
    delay,
    timeout,
    timers: clock,
  })
  const edit = (text) => { state.text = text; live.schedule(text, () => ({ body: text })) }
  return { clock, server, state, live, edit }
}

test('live check: after the pause the definition is sent once, and its problems come back', async () => {
  const { clock, server, state, edit } = harness()
  edit('v1')
  await clock.tick(300)
  edit('v2')
  await clock.tick(300)
  assert.equal(server.calls.length, 0, 'still typing: nothing sent')
  await clock.tick(300)
  assert.equal(server.calls.length, 1)
  assert.deepEqual(server.calls[0].definition, { body: 'v2' })
  const p = { item: 't1', field: 'value', pos: 3, length: 2, message: 'a text added to a number' }
  server.calls[0].resolve({ problems: [p] })
  await flush()
  assert.deepEqual(state.problems, { list: [p], text: 'v2' })
})

test('live check: a gateway without the route (404) is not asked again, and nothing is shown', async () => {
  const { clock, server, state, live, edit } = harness()
  edit('v1')
  await clock.tick(600)
  server.calls[0].reject(Object.assign(new Error('Not Found'), { statusCode: 404 }))
  await flush()
  assert.equal(live.available, false)
  assert.equal(state.problems, null)
  assert.equal(state.clears, 1, 'what the server said before no longer holds')
  edit('v2')
  await clock.tick(5000)
  assert.equal(server.calls.length, 1, 'no second call')
})

test('live check: 405 counts as a missing route; a 500 or a network error does not', async () => {
  const a = harness()
  a.edit('v1')
  await a.clock.tick(600)
  a.server.calls[0].reject({ response: { status: 405 } })
  await flush()
  assert.equal(a.live.available, false)

  const b = harness()
  b.edit('v1')
  await b.clock.tick(600)
  b.server.calls[0].reject(Object.assign(new Error('Internal'), { statusCode: 500 }))
  await flush()
  b.edit('v2')
  await b.clock.tick(600)
  b.server.calls[1].reject(new TypeError('Failed to fetch'))
  await flush()
  assert.equal(b.live.available, true)
  assert.equal(b.state.problems, null)
  b.edit('v3')
  await b.clock.tick(600)
  assert.equal(b.server.calls.length, 3, 'asked again at the next pause')
})

test('live check: a slow server is given up after the timeout, silently', async () => {
  const { clock, server, state, live, edit } = harness({ timeout: 8000 })
  edit('v1')
  await clock.tick(600)
  await clock.tick(8000)
  assert.equal(server.calls[0].aborted, true)
  assert.equal(state.problems, null)
  assert.equal(live.available, true)
  // the answer arriving late anyway is ignored
  server.calls[0].resolve({ problems: [{ item: 'x', field: 'value', message: 'late' }] })
  await flush()
  assert.equal(state.problems, null)
})

test('live check: an answer for a text that has changed since is dropped, and the newer pause cancels the call', async () => {
  const { clock, server, state, edit } = harness()
  edit('v1')
  await clock.tick(600)
  edit('v2') // typing goes on while v1 is being checked
  server.calls[0].resolve({ problems: [{ item: 't1', field: 'value', message: 'about v1' }] })
  await flush()
  assert.equal(state.problems, null, 'v1’s problems are not shown on v2')
  await clock.tick(600)
  assert.equal(server.calls.length, 2)
  edit('v3')
  await clock.tick(600)
  assert.equal(server.calls[1].aborted, true, 'the v2 call is cancelled by the v3 one')
  server.calls[2].resolve({ problems: [] })
  await flush()
  assert.deepEqual(state.problems, { list: [], text: 'v3' })
})

test('live check: the same text is not sent twice', async () => {
  const { clock, server, edit } = harness()
  edit('v1')
  await clock.tick(600)
  server.calls[0].resolve({ problems: [] })
  await flush()
  edit('v1')
  await clock.tick(600)
  assert.equal(server.calls.length, 1)
})

// ── problems mapped to their place ──────────────────────────────────────────
function sample () {
  const d = emptyDefinition()
  d.datasets = [{ name: 'orders', source: { kind: 'sql', sql: 'SELECT 1', connection_id: null, engine: null }, fields: [], calculated: [{ name: 'M', expr: '[a] - [b]' }] }]
  d.parameters = [{ name: 'region', type: 'text' }]
  d.sections.body.items = [
    { id: 'title', type: 'text', x: 0, y: 0, w: 60, h: 7, value: '="x" + 1' },
    { id: 'tb', type: 'table', x: 0, y: 10, w: 186, h: 20, dataset: 'orders', columns: [{ header: 'A', value: '[a]' }] },
  ]
  return d
}

test('problems: the live check’s list (`problems`) maps as the save’s `errors` do', () => {
  const d = sample()
  const list = problemsOf(d, { problems: [
    { item: 'title', field: 'value', pos: 1, length: 3, message: 'a text added to a number' },
    { item: null, field: 'sections.body.items[1].columns[0].value', pos: 0, length: 3, message: 'no field' },
    { item: null, field: 'datasets[orders].calculated[0]', pos: 0, length: 3, message: 'calc' },
    { item: null, field: 'parameters[0].default', pos: null, length: 0, message: 'param' },
  ] })
  assert.equal(list.length, 4)
  assert.deepEqual([list[0].item, list[0].path, list[0].pos, list[0].length], ['title', 'value', 1, 3])
  assert.deepEqual([list[1].item, list[1].path], ['tb', 'columns[0].value'])
  assert.deepEqual([list[2].dataset, list[2].path], ['orders', 'calculated[0]'])
  assert.deepEqual([list[3].parameter, list[3].path], ['region', 'default'])
})

test('problems: a refused preview (422 {message, problems}) gives every problem, not only the first', () => {
  const d = sample()
  const list = problemsOf(d, { message: 'The report has 2 problems', problems: [
    { item: 'title', field: 'value', pos: 1, length: 3, message: 'one' },
    { item: 'tb', field: 'columns[0].value', pos: 0, length: 3, message: 'two' },
  ] })
  assert.deepEqual(list.map((p) => p.message), ['one', 'two'])
  assert.equal(list[1].item, 'tb')
  // the gateway's own problem, without a list: the detail itself
  const one = problemsOf(d, { message: 'Too many datasets', where: 'datasets', item: null, errors: [] })
  assert.deepEqual(one.map((p) => [p.message, p.path]), [['Too many datasets', 'datasets']])
})

test('problems: the engine’s text of a refused render is still understood (older gateways)', () => {
  const p = problemFromText("the report's definition: item «k1», value: there is no field [x] (at character 5)")
  assert.deepEqual([p.item, p.path, p.pos, p.message], ['k1', 'value', 4, 'there is no field [x]'])
  assert.equal(problemFromText('The dataset «orders» has 12,000 rows'), null)
})

test('live problems: shown only while their field still holds what was checked, and is not empty', () => {
  const d = sample()
  const [p] = withSeen(d, problemsOf(d, { problems: [{ item: 'title', field: 'value', pos: 1, length: 3, message: 'm' }] }))
  assert.equal(liveShown(d, p), true)
  d.sections.body.items[0].value = '="x" & 1'
  assert.equal(liveShown(d, p), false, 'changed since: hidden at once')
  d.sections.body.items[0].value = '="x" + 1'
  assert.equal(liveShown(d, p), true)
  const [empty] = withSeen(d, problemsOf(d, { problems: [{ item: 'tb', field: 'columns[0].total', message: 'missing' }] }))
  assert.equal(liveShown(d, empty), false, 'an empty field is not judged while one writes')
})

// ── renames followed by the expressions ─────────────────────────────────────
test('rename: a dataset named as an aggregate’s scope follows its new name, nothing else does', () => {
  const r = (e) => renameInExpression(e, { dataset: ['orders', 'sales'] })
  assert.equal(r('SUM([amount], "orders")'), 'SUM([amount], "sales")')
  assert.equal(r('="Total " & FORMAT(sum( [amount] ,  "orders" ), "#,##0")'), '="Total " & FORMAT(sum( [amount] ,  "sales" ), "#,##0")')
  assert.equal(r('COUNT("orders")'), 'COUNT("sales")', 'COUNT takes the dataset alone')
  assert.equal(r('IF([x] = "orders", 1, 0)'), 'IF([x] = "orders", 1, 0)', 'a text compared is not a scope')
  assert.equal(r('CONCAT([a], "orders")'), 'CONCAT([a], "orders")', 'not an aggregate')
  assert.equal(r('SUM([orders])'), 'SUM([orders])', 'a field with the same name')
  assert.equal(r('SUM([a], "other")'), 'SUM([a], "other")')
})

test('rename: a parameter follows in PARAM(), however it is written', () => {
  const r = (e) => renameInExpression(e, { param: ['region', 'area'] })
  assert.equal(r('="Sales in " & PARAM("region")'), '="Sales in " & PARAM("area")')
  assert.equal(r('param( "region" ) & PARAM("regions")'), 'param( "area" ) & PARAM("regions")')
  assert.equal(r('IF([r] = "region", 1, 0)'), 'IF([r] = "region", 1, 0)')
  assert.equal(r('PARAM("region'), 'PARAM("region', 'an unclosed text is left alone')
})

test('rename: every expression of the report follows a renamed dataset or parameter', () => {
  const d = sample()
  d.datasets.push({ name: 'kpis', source: { kind: 'sql', sql: 'SELECT 1', connection_id: null, engine: null }, fields: [],
    calculated: [{ name: 'Share', expr: '[v] / SUM([v], "orders")' }] })
  d.parameters.push({ name: 'pick', type: 'text', choices: { dataset: 'orders', value: 'FIRST([code], "orders")', label: '[name]' } })
  d.sections.page_header.items = [{ id: 'h', type: 'text', x: 0, y: 0, w: 60, h: 7, value: '="Total: " & SUM([amount], "orders") & " in " & PARAM("region")' }]
  d.sections.body.items.push({ id: 'k', type: 'kpi', x: 0, y: 40, w: 50, h: 20, dataset: 'kpis', label: 'orders', value: 'COUNT("orders")', compare: null })
  d.sections.body.items[1].columns.push({ header: 'orders', value: '[a]', total: 'SUM([a]) / SUM([a], "orders")' })
  renameEverywhere(d, { dataset: ['orders', 'sales'] })
  renameEverywhere(d, { param: ['region', 'area'] })
  assert.equal(d.sections.page_header.items[0].value, '="Total: " & SUM([amount], "sales") & " in " & PARAM("area")')
  assert.equal(d.datasets[1].calculated[0].expr, '[v] / SUM([v], "sales")')
  assert.equal(d.parameters[1].choices.value, 'FIRST([code], "sales")')
  assert.equal(d.sections.body.items[2].value, 'COUNT("sales")')
  assert.equal(d.sections.body.items[2].label, 'orders', 'a KPI\'s label is text')
  assert.equal(d.sections.body.items[1].columns[1].total, 'SUM([a]) / SUM([a], "sales")')
  assert.equal(d.sections.body.items[1].columns[1].header, 'orders', 'a header is text')
})

test('writing: an expression on its way is not a mistake; a finished one that is wrong is', () => {
  const ctx = { fields: ['ricavo', 'costo'], params: ['since'] }
  const held = (text, caret = text.length) => stillWriting(checkExpression(text, ctx), caret)
  for (const on of ['S', 'SU', 'SUM', 'SUM(', 'SUM([', 'SUM([ric', 'SUM([ricavo]', '[ricavo] +', '[ricavo] > 1 AN', '"Sales', '="Sales" &', '(', 'IF([ricavo] > 1,', 'PARAM("si', '{tod']) {
    assert.ok(checkExpression(on, ctx), `${on} has something to say`)
    assert.equal(held(on), true, on)
  }
  // finished and wrong: said, also while the field has the focus
  for (const wrong of ['SUM([ricavoo])', '[ricavoo]', 'SUMM([ricavo])', 'SUM()', 'PARAM("nope")', '[ricavo] != 1']) {
    assert.ok(checkExpression(wrong, ctx), wrong)
    assert.equal(held(wrong), false, wrong)
  }
  // the name being written is the one under the caret: another one, away from it, is wrong
  assert.equal(held('SU + [ricavo]', 2), true)
  assert.equal(held('SU + [ricavo]', 13), false)
  // inside the brackets of a field being corrected
  assert.equal(held('SUM([ricavoo])', 8), true)
})

test('problems: those of a save go when the server\'s check does not find them any more', () => {
  const p = (item, path) => ({ message: 'x', item, dataset: null, parameter: null, path, pos: null, length: 0 })
  const saved = [p('t', 'columns[0].value'), p('t', 'groups[0].by'), p('k', 'value')]
  assert.deepEqual(stillReported(saved, [p('k', 'value')]).map((x) => x.item), ['k'])
  assert.deepEqual(stillReported(saved, []), [])
})

test('live check: a session that has ended (401) is not asked again at every pause', async () => {
  const { clock, server, live, edit } = harness()
  edit('v1')
  await clock.tick(700)
  server.calls[0].reject({ statusCode: 401 })
  await flush()
  edit('v2')
  await clock.tick(700)
  assert.equal(server.calls.length, 1)
  assert.equal(live.available, false)
})

test('the store: what is put beside a table goes under it, however it gets there', () => {
  const store = createDesignStore()
  const d = store.doc.value
  const table = newItem(d, 'table', { x: 0, y: 0 })
  store.add('body', table)
  const text = newItem(d, 'text', { x: 130, y: 2 })
  store.add('body', text)
  assert.equal(store.settleRow(text), true)
  assert.equal(store.find(text.id).item.y, Math.round((table.h + 2) * 10) / 10)
  // a table dropped beside what is there: under it
  const other = newItem(store.doc.value, 'table', { x: 0, y: store.find(text.id).item.y + 1 })
  store.add('body', other)
  assert.equal(store.settleRow(other), true)
  assert.ok(store.find(other.id).item.y >= store.find(text.id).item.y + 7)
  // a copy too
  store.select(text.id)
  store.copy(text.id)
  store.select(table.id)
  const pasted = store.paste()
  assert.ok(pasted)
  const y = store.find(pasted.item.id).item.y
  assert.equal(store.doc.value.sections.body.items.filter((i) => i.type === 'table' && i.y < y + 7 && i.y + i.h > y).length, 0)
})

test('the store: undo and redo do not change a table\'s height behind the history', async () => {
  const store = createDesignStore()
  const table = newItem(store.doc.value, 'table', { x: 0, y: 0 })
  table.h = 50 // a height saved long ago
  store.reset({ ...store.doc.value, sections: { ...store.doc.value.sections, body: { items: [table] } } })
  store.doc.value.sections.body.items[0].groups.push({ by: '[a]' })
  await flush()
  store.record()
  store.undo()
  await flush()
  assert.equal(store.doc.value.sections.body.items[0].h, 50)
  assert.equal(store.canRedo.value, true)
})

test('the store: a line keeps both its ends on a page made narrower', () => {
  const store = createDesignStore()
  store.doc.value.page.orientation = 'landscape'
  const line = { id: 'l', type: 'line', x: 20, y: 5, w: 250, h: 0 }
  store.add('body', line)
  store.doc.value.page.orientation = 'portrait'
  const hit = store.find('l')
  store.keepInside('body', hit.item)
  assert.deepEqual([hit.item.x, hit.item.w], [20, 166])
  // a line that runs backwards too
  const back = { id: 'b', type: 'line', x: 180, y: 8, w: -200, h: 4 }
  store.add('page_footer', back)
  const b = store.find('b')
  store.keepInside('page_footer', b.item)
  assert.deepEqual([b.item.x, b.item.w, b.item.y, b.item.h], [180, -180, 8, 2])
})

test('the store: the fields read from the data are no step of the undo', async () => {
  const store = createDesignStore()
  store.doc.value.datasets.push({ name: 'd', source: { kind: 'sql', sql: 'SELECT 1', connection_id: null, engine: null }, fields: [] })
  // they arrive: the dataset is one step, with them
  const ds = store.doc.value.datasets[0]
  store.quietly(() => { ds.fields = [{ name: 'a', type: 'string' }] })
  await flush()
  store.doc.value.sections.body.items.push({ id: 'r', type: 'rect', x: 0, y: 0, w: 5, h: 5 })
  store.record()
  store.undo()
  assert.deepEqual(store.doc.value.datasets[0].fields, [{ name: 'a', type: 'string' }], 'the rectangle goes, the fields stay')
  store.undo()
  assert.equal(store.doc.value.datasets.length, 0, 'the next one takes the dataset away')
  store.redo()
  assert.deepEqual(store.doc.value.datasets[0].fields, [{ name: 'a', type: 'string' }], 'and it comes back with its fields')
})

test('the store: an item added and removed at once is two steps: Ctrl+Z brings it back', () => {
  const store = createDesignStore()
  store.doc.value.datasets.push({ name: 'd', source: { kind: 'sql', sql: 'SELECT 1', connection_id: null, engine: null } })
  store.record()
  store.add('body', { id: 'r', type: 'rect', x: 0, y: 0, w: 5, h: 5 })
  store.remove('r')
  store.undo()
  assert.ok(store.find('r'), 'the item, not the dataset before it')
  assert.equal(store.doc.value.datasets.length, 1)
})
