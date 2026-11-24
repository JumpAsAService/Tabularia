// Part two of the browser check of the editor of Bayesian models (docs/specs/bayes-graph-editor.md, lot G5): the
// panels the keyboard check does not open, against a MOCK gateway (every call to the API base is answered here from
// the fixtures next to this file; nothing leaves the machine and no AI is called).
//
//   - the tab bar: names, tooltips and screen-reader access of every tab at 1440 px (the whole word) and at 1280 px
//     (the panel is 420 px wide: only the chosen tab says its name), nothing wraps, nothing overflows;
//   - «Stan code»: opens with scrollTop 0 with a node selected, the rows by ↑ ↓ and Enter, Esc from a row and from the
//     code, Tab out of the code;
//   - the assistant: the run picker, the version notice clear of the text below it, «Apply» offered and taken back by
//     one undo, «Apply» disabled when the gateway refuses the proposal, the proposal discarded;
//   - the node's MLflow section in the flow editor: no connection, a connection and no experiment, filled, cleared,
//     the gateway's refusal shown under Run;
//   - the node's «prior predictive» button (focus after Run, the body it sends, Run disabled meanwhile) and the
//     Report's switch between the posterior and the prior check, in the theme it is run in.
//
// Called by browser_check.cjs once per theme; the screenshots go to $SHOTS when it is set.
const fs = require('node:fs')
const path = require('node:path')

const FIX = path.join(__dirname, 'fixtures')
const read = (f) => fs.readFileSync(path.join(FIX, f), 'utf8')
const graph = JSON.parse(read('hier_slopes.graph.json'))
const program = read('hier_slopes.stan')
const mapping = JSON.parse(read('hier_slopes.mapping.json'))
const manifest = JSON.parse(read('hier_slopes.manifest.json'))
const SHA = '9f2b6c1a7d34e85f0a1b2c3d4e5f60718293a4b5c6d7e8f90123456789abcdef'
const AGO = (m) => new Date(Date.now() - m * 60000).toISOString()

function makeMock () {
  const state = { checks: [], bodies: [], posts: [], nextRun: 11, live: {}, priorDone: false, ai: true, saves: 0, creates: 0 }
  const dataManifest = [...manifest.data, { stan: 'Ppc_on', kind: 'checks_on' }, { stan: 'Prior_o1_n', kind: 'prior_count' }]
  const ver = (version) => ({ version, note: 'edited', created_at: '2026-11-20T10:00:00Z', created_by: 3, graph, format_version: 1, semantic_hash: 'aaa', findings: [], program, program_sha256: SHA, mapping, data_manifest: dataManifest, quantity_manifest: manifest.quantities, codegen_version: 3, family: 'hierarchical', catalog_sha256: 'cd', codegen_current: 3, current: true })
  const model = { id: 17, name: 'School scores', description: '', project_id: 2, owner_id: 3, current_version: 4, template_id: null, template_version: null, family: 'hierarchical', created_at: '2026-11-01T10:00:00Z', updated_at: '2026-11-20T10:00:00Z', editable: true, runnable: true, compile: { state: 'ready', detail: '', at: null }, uses: 1, saved_by: 'ada@example.org' }
  const ME = { id: 3, email: 'ada@example.org', full_name: 'Ada', is_active: true, is_superuser: true, groups: [], locale: 'en' }
  const COLUMNS = [{ name: 'score', dtype: 'Float64' }, { name: 'hours', dtype: 'Float64' }, { name: 'school', dtype: 'String' }]
  const nodeData = { label: '', bayesModelId: 17, bayesModelVersion: 4, bindings: { g: 'school', x: 'hours', y: 'score' }, sampler: { chains: 4, warmup: 1000, draws: 1000, seed: 1840277361, adapt_delta: 0.8, max_treedepth: 10, init: null }, output: 'summary', replicatedDraws: 50, timeoutSeconds: 1800, retries: 0, retryDelaySeconds: 60 }
  const definition = { nodes: [
    { id: 'source', type: 'source', position: { x: 60, y: 140 }, data: { filename: 'schools.csv', bucket: 'data-prep', parquetKey: 'sample/schools.parquet', rows: 1240, columns: COLUMNS } },
    { id: 'ctl-1', type: 'bayes_model', position: { x: 340, y: 140 }, data: nodeData },
    { id: 'ctl-2', type: 'bayes_model', position: { x: 340, y: 340 }, data: { ...nodeData, bayesModelVersion: 3, label: 'Older pin', output: 'replicated' } }],
  edges: [{ id: 'e1', source: 'source', target: 'ctl-1', targetHandle: 'left' }, { id: 'e2', source: 'source', target: 'ctl-2', targetHandle: 'left' }] }
  const FLOW = { id: 1, name: 'Scores', description: '', project_id: 1, owner_id: 3, owner_name: 'Ada', engine: 'polars', production_engine: null, run_schedule: null, next_run_at: null, created_at: null, updated_at: '2026-11-24T10:00:00Z', last_run_status: null, last_run_at: null, definition: JSON.stringify(definition) }
  const GROUPS = { disabled_reason: null, groups: [{ id: 'bayes', enabled: true, usable: true, ready: true, max_timeout_seconds: 3600, max_rows: 1000000 }] }
  const CONNS = [{ id: 3, name: 'Tracking MLflow', db_type: 'mlflow', project_id: 1 }, { id: 4, name: 'Warehouse', db_type: 'postgres', project_id: 1 }]
  const AI = { enabled: true, models: ['made-up'], default_model: 'made-up', options: [{ id: 'made-up', model: 'made-up', provider: 'Prova' }], metadata_only: false }
  const RUNS = { runs: [
    { flow_id: 1, flow_name: 'Scores', node_id: 'ctl-1', node_label: 'School estimate', pinned_version: 3, run_id: 11, version: 3, trial: false, finished_at: '2026-11-24T09:00:00Z' },
    { flow_id: 1, flow_name: 'Scores', node_id: 'ctl-2', node_label: 'Sample try', pinned_version: 4, run_id: 12, version: 4, trial: true, finished_at: '2026-11-24T09:30:00Z' },
    { flow_id: 2, flow_name: 'Costs', node_id: 'ctl-1', node_label: 'Never run', pinned_version: 4, run_id: null, version: null, trial: false, finished_at: null }], hidden: 1 }
  const FACTS = { run_id: 11, flow_id: 1, node_id: 'ctl-1', trial: false, version: 3, sampler: { chains: 4, draws: 1000, divergences: 14, treedepth_hits: 0, treedepth_max: 10, e_bfmi: 0.71, stepsize: 0.08, accept_stat_mean: 0.83 }, nodes: [{ node: 'taua', rhat_max: 1.012, ess_bulk_min: 310, ess_tail_min: 420 }] }
  const REFUSAL = 'Bayesian model «School scores»: name the MLflow experiment before running the step.'
  const S1 = JSON.stringify({ node_id: 'ctl-1', label: 'Scores', model: 'bayes_model', group: 'bayes', version: 3, input_rows: 1240, attempts: 1, exit_code: 0, bayes_model: { id: 17, version: 4, name: 'School scores' }, compile: { state: 'ready', waiting: false, seconds: 14 }, check: 'prior_predictive', log: 'ok' })
  const run = (id, status, script, error = null) => ({ id, kind: 'model', flow_id: 1, status, launched_by: 3, trigger_type: 'manual', output_key: '', rows_written: null, error, publish_name: null, datasource_id: null, destination: null, script, started_at: AGO(3), finished_at: AGO(2), flow_name: 'Scores', launched_by_name: 'Ada' })
  const outputs = () => ({ 'ctl-1': {
    last: { run_id: 5, status: 'SUCCESS', trial: true, error: null, started_at: AGO(120), finished_at: AGO(118), attempts: 1, exit_code: 0, log: '', stale: false },
    output: { run_id: 5, bucket: 'b', key: 'k', rows: 12, trial: true, finished_at: AGO(118), stale: false },
    prior_check: { run_id: 9, status: 'SUCCESS', trial: true, error: null, started_at: AGO(60), finished_at: AGO(58), stale: false } } })
  const pageSvg = (n, c) => `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 595 842" width="595" height="842"><rect width="595" height="842" fill="#fff"/><text x="40" y="70" font-size="22" font-family="sans-serif" fill="#111">${n}</text><rect x="40" y="110" width="515" height="260" fill="${c}"/></svg>`
  const sse = (events) => events.map(([e, d]) => `event: ${e}\ndata: ${JSON.stringify(d)}\n\n`).join('')
  const done = (proposal) => ['done', { chat_id: 5, seq: 1, usage: { requests: 1, input_tokens: 10, output_tokens: 10, cost_usd: null }, chat_total: null, proposal }]
  state.assist = (body) => {
    const msg = String(body.message)
    if (/^PROPOSE/.test(msg)) {
      return sse([['text', { delta: 'I propose to **narrow** the scale between the groups and to try a heavier-tailed prior on the mean.' }], done({ summary: 'Two priors', changes: [
        { node: 'taua', field: 'expression', slot: 'prior.sigma', before: '1', value: '0.5', why: 'The half-normal has scale 0.5 now.' },
        { node: 'mua', field: 'prior_distribution', slot: '', before: 'normal', value: 'student_t', why: 'Heavier tails on the typical mean.' },
        { node: 'mua', field: 'prior_args', slot: '', before: { mu: '0', sigma: '2.5' }, value: { nu: '3', mu: '0', sigma: '2.5' }, why: 'Three degrees of freedom.' }] })])
    }
    if (/^BROKEN/.test(msg)) {
      return sse([['text', { delta: 'Here is a proposal with an expression that does not read.' }], done({ summary: 'A broken expression', changes: [
        { node: 'taua', field: 'expression', slot: 'prior.sigma', before: '1', value: 'tau ^ ', why: 'A try.' }] })])
    }
    return sse([['text', { delta: `Node <b>taua</b> is the scale. ${body.run ? 'In the run I read **' + body.run.sampler.divergences + ' divergences**.' : ''}` }], done(null)])
  }
  state.answer = (method, url, body) => {
    const p = url.pathname; let m
    if (p === '/auth/me') return ME
    if (p === '/ai/status') return state.ai ? AI : { ...AI, enabled: false, models: [], options: [] }
    if (p === '/models/groups') return GROUPS
    if (p === '/connections') return CONNS
    if (p === '/models/mlflow/3/experiments') return { experiments: [{ name: 'School scores' }, { name: 'Price elasticity' }] }
    if (p === '/bayes-models') return [model]
    if (p === '/projects') return [{ id: 1, name: 'Teaching', parent_id: null }, { id: 2, name: 'Schools', parent_id: 1 }]
    if (p === '/bayes-models/17') { if (method === 'PATCH') state.saves++; return { ...model, version: ver(4) } }
    if (p === '/projects/2/bayes-models' && method === 'POST') { state.creates++; return { ...model, id: 18, name: 'New model', current_version: 1, version: ver(1) } }
    if (p === '/bayes-models/18') return { ...model, id: 18, name: 'New model', current_version: 1, version: ver(1) }
    if (p === '/bayes-models/18/versions') return [{ version: 1, note: '', created_at: '2026-11-20T10:00:00Z', created_by: 'ada@example.org', semantic_hash: 'aaa', program_sha256: 'x', codegen_version: 3, family: 'hierarchical', current: true }]
    if (p === '/bayes-models/17/versions') return [{ version: 4, note: 'edited', created_at: '2026-11-20T10:00:00Z', created_by: 'ada@example.org', semantic_hash: 'aaa', program_sha256: 'x', codegen_version: 3, family: 'hierarchical', current: true }]
    if (p === '/bayes-models/17/versions/0') return ver(9)
    if (p.startsWith('/bayes-models/17/versions/')) return ver(4)
    if (p === '/bayes-models/17/runs') return RUNS
    if ((m = p.match(/^\/bayes-models\/17\/runs\/(\d+)\/facts$/))) return +m[1] === 11 ? FACTS : { __status: 404, detail: 'no' }
    if (p === '/bayes-models/check' && method === 'POST') {
      const g = JSON.parse(body).graph
      state.checks.push(g)
      const sig = g.nodes.find(n => n.id === 'taua')?.prior?.args?.sigma?.expr ?? ''
      if (/\^\s*$/.test(sig)) return { ok: false, findings: [{ code: 'expr.syntax', severity: 'error', node: 'taua', slot: 'prior.sigma', args: { pos: 4 } }] }
      return { ok: true, findings: [], program, program_sha256: SHA, mapping, codegen_version: 3, family: 'hierarchical', semantic_hash: 'bbb' }
    }
    if (p === '/flows/1') return FLOW
    if (p === '/tasks/preview') return { columns: COLUMNS, rows: [], row_count: 0 }
    if (p === '/tasks/operations') return ['cast', 'filter']
    if (p === '/scripting/flows/1/outputs') { const o = outputs(); if (state.priorDone) o['ctl-1'].prior_check.run_id = 13; return o }
    if (p === '/scripting/flows/1/nodes/ctl-1/run' && method === 'POST') {
      const b = JSON.parse(body || '{}'); state.posts.push(b)
      const id = state.nextRun++; state.live[id] = { polls: 0, check: b.check }
      return { run_id: id }
    }
    if ((m = p.match(/^\/runs\/(\d+)$/))) {
      const id = +m[1]; const l = state.live[id]
      if (!l) return run(id, 'SUCCESS', '{}')
      if (!l.check) return run(id, 'FAILURE', '{}', REFUSAL)
      const finished = ++l.polls >= 4
      if (finished) state.priorDone = true
      return run(id, finished ? 'SUCCESS' : 'STARTED', '{}')
    }
    if (p === '/tasks/model-report') return { task_id: 'rep-' + JSON.parse(body).run_id }
    if ((m = p.match(/^\/tasks\/exports\/rep-(\d+)$/))) return { status: 'ready', filename: 'r.json', size: 100, pages: 1 }
    if ((m = p.match(/^\/tasks\/exports\/rep-(\d+)\/file$/))) return { pages: [pageSvg(+m[1] === 5 ? 'Posterior model (run 5)' : 'Prior predictive check (run ' + m[1] + ')', +m[1] === 5 ? '#d9f2e4' : '#cfe3ff')], hotspots: [] }
    if (p === '/runs') return { items: [run(12, 'SUCCESS', S1)], total: 1, limit: 50, offset: 0 }
    if (['/datasources', '/flows'].includes(p)) return []
    return { __status: 404, detail: 'not mocked' }
  }
  return state
}

// answers every call to the API base from the mock (the assistant's stream as server-sent events)
async function install (ctx, mock, UI) {
  await ctx.route('http://localhost:8000/**', async (route) => {
    const req = route.request(); const url = new URL(req.url())
    const cors = { 'access-control-allow-origin': new URL(UI).origin, 'access-control-allow-credentials': 'true', 'access-control-allow-headers': '*', 'access-control-allow-methods': '*' }
    if (req.method() === 'OPTIONS') return route.fulfill({ status: 204, headers: cors })
    if (url.pathname === '/ai/bayes-assist') {
      const b = JSON.parse(req.postData()); mock.bodies.push(b)
      return route.fulfill({ status: 200, headers: { ...cors, 'content-type': 'text/event-stream' }, body: mock.assist(b) })
    }
    if (/\/bayes-models\/17\/versions\/0$/.test(url.pathname)) await new Promise(r => setTimeout(r, 1500))
    const b = mock.answer(req.method(), url, req.postData())
    return route.fulfill({ status: b?.__status ?? 200, headers: { ...cors, 'content-type': 'application/json' }, body: JSON.stringify(b) })
  })
}

async function panelsPart ({ browser, which, UI, theme, ok }) {
  const tag = `${which} ${theme}`
  const shots = process.env.SHOTS
  if (shots) fs.mkdirSync(shots, { recursive: true })
  const mock = makeMock()
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, colorScheme: theme === 'light' ? 'light' : 'dark' })
  await ctx.addCookies([{ name: 'tab_token', value: 'g5-check', url: UI }, { name: 'tabularia-locale', value: 'en', url: UI }])
  await ctx.addInitScript(([t]) => { try { localStorage.setItem('tabularia-theme', t); localStorage.setItem('tabularia.aiDisclosure', '2|Prova') } catch {} }, [theme])
  await install(ctx, mock, UI)
  const page = await ctx.newPage()
  const errors = []
  page.on('pageerror', e => errors.push(String(e)))
  page.on('console', m => { if (m.type() === 'error' && !/404|not mocked|Failed to load resource/.test(m.text())) errors.push('console: ' + m.text().slice(0, 200)) })
  const shot = (name) => shots ? page.screenshot({ path: path.join(shots, `${name}-${which}-${theme}-${page.viewportSize().width}.png`) }) : null
  const settle = (ms = 300) => page.waitForTimeout(ms)
  const openEditor = async (width) => {
    await page.setViewportSize({ width, height: width === 1440 ? 900 : 800 })
    await page.goto(`${UI}/bayes-model?id=17`, { waitUntil: 'networkidle', timeout: 180000 })
    await page.waitForSelector('.vue-flow__node[data-id="mua"]', { timeout: 90000 })
    await settle(1200)
  }
  // text under 4.5:1 on its own background, among the elements of a selector (disabled controls are exempt)
  const lowContrast = (sel) => page.evaluate((s) => {
    const parse = (c) => { const m = c.match(/[\d.]+/g).map(Number); return { r: m[0], g: m[1], b: m[2], a: m[3] ?? 1 } }
    const lum = ({ r, g, b }) => { const f = (x) => { x /= 255; return x <= 0.03928 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4 }; return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b) }
    const mix = (top, base) => ({ r: top.r * top.a + base.r * (1 - top.a), g: top.g * top.a + base.g * (1 - top.a), b: top.b * top.a + base.b * (1 - top.a), a: 1 })
    const bgOf = (el) => { const layers = []; for (let e = el; e; e = e.parentElement) { const c = parse(getComputedStyle(e).backgroundColor); if (c.a > 0) layers.push(c); if (c.a >= 1) break } let base = layers.pop() ?? { r: 0, g: 0, b: 0, a: 1 }; while (layers.length) base = mix(layers.pop(), base); return base }
    const out = []
    for (const root of document.querySelectorAll(s)) {
      for (const el of [root, ...root.querySelectorAll('*')]) {
        if (![...el.childNodes].some(n => n.nodeType === 3 && n.textContent.trim())) continue
        if (el.closest(':disabled, [disabled], .sr-only') || getComputedStyle(el).opacity < 1) continue
        let gradient = false; for (let e = el; e; e = e.parentElement) { if (getComputedStyle(e).backgroundImage !== 'none') gradient = true; if (parse(getComputedStyle(e).backgroundColor).a >= 1) break }
        if (gradient) continue // a gradient has no single colour to measure: the screenshots are read by eye
        const l1 = lum(parse(getComputedStyle(el).color)); const l2 = lum(bgOf(el))
        const cr = (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05)
        if (cr < 4.5) out.push(`${el.className || el.tagName} "${el.textContent.trim().slice(0, 20)}" ${cr.toFixed(2)}`)
      }
    }
    return out
  }, sel)

  // ---- 1. the tab bar -----------------------------------------------------------------------------------------
  // The bar is a container: at 540 px or less only the chosen tab says its name (the others keep it as their tooltip
  // and for a screen reader). The panel is 420 px wide, and 640 with the Stan code tab open at 1400 px or more.
  const TABS = [['inspector', 'Inspector'], ['code', 'Stan code'], ['problems', 'Problems'], ['assistant', 'Assistant'], ['versions', 'Versions']]
  const rowsOf = () => page.evaluate(() => {
    const t = document.querySelector('.tabs'); const bar = t.getBoundingClientRect().width
    return { bar: Math.round(bar), over: t.scrollWidth - t.clientWidth, tabs: [...t.querySelectorAll('[role=tab]')].map(b => {
      const l = b.querySelector('.lbl'); const r = l ? l.getBoundingClientRect() : null; const ic = b.querySelector('svg')
      return { id: b.id, sel: b.getAttribute('aria-selected') === 'true', title: b.getAttribute('title'), aria: b.getAttribute('aria-label'), text: l ? l.textContent.trim() : '', hasLbl: !!l, w: r ? r.width : 0, h: r ? r.height : 0, tabH: b.getBoundingClientRect().height, icon: !!ic && ic.getAttribute('aria-hidden') === 'true' && ic.getBoundingClientRect().width > 8 }
    }) }
  })
  for (const width of [1440, 1280]) {
    await openEditor(width)
    for (const [id, name] of TABS) {
      await page.click(`#bt-${id}`); await settle(250)
      const { bar, over, tabs } = await rowsOf()
      const named = tabs.filter(t => t.hasLbl)
      const me = named.find(t => t.id === `bt-${id}`)
      const others = named.filter(t => t.id !== `bt-${id}`)
      const narrowBar = bar <= 540
      const w = `${tag}: ${width} px, «${name}» chosen (bar ${bar} px)`
      ok(`${w}: ${narrowBar ? 'it alone says its name, the others show an icon' : 'every tab says its name'}`, me.sel && me.w > 8 && me.icon && others.every(t => !t.sel && t.icon && (narrowBar ? t.w <= 1.5 : t.w > 8)), JSON.stringify(named.map(t => [t.id, t.sel, Math.round(t.w)])))
      ok(`${w}: nothing wraps (one line each) and nothing overflows the bar`, named.every(t => t.tabH < 34) && me.h < 20 && over <= 0, JSON.stringify({ over, h: named.map(t => [t.id, t.tabH, t.h]) }))
      ok(`${w}: each tab keeps its tooltip, the same word as its name`, named.every(t => t.title && t.title === t.text))
      ok(`${w}: a screen reader finds the five tabs by name, and the glossary's by its label`, (await Promise.all(TABS.map(([, n]) => page.getByRole('tab', { name: n, exact: true }).count()))).every(n => n === 1) && tabs.some(t => t.id === 'bt-glossary' && t.aria && t.title === t.aria && t.icon))
      if (id === 'code' && width === 1440) ok(`${tag}: 1440 px: with the Stan tab open the panel is wide (≥ 540 px) and every tab says its name`, !narrowBar, String(bar))
    }
    ok(`${tag}: ${width} px: at least one state of the bar is the narrow one`, (await (async () => { await page.click('#bt-inspector'); await settle(200); return (await rowsOf()).bar <= 540 })()))
    await page.click('#bt-assistant'); await settle(300); await shot('tabs-assistant')
  }

  // ---- 1b. the tab bar without the assistant, in the five languages (review of the editor) ---------------------
  // five tabs with names, the longest of de/fr/es and the Problems counter: the bar never grows wider than the panel and
  // the glossary's button stays inside the window.
  mock.ai = false
  for (const lang of ['en', 'it', 'de', 'es', 'fr']) {
    await ctx.addCookies([{ name: 'tabularia-locale', value: lang, url: UI }])
    for (const width of [1440, 1280]) {
      await openEditor(width)
      ok(`${tag}: ${lang} ${width} px, no assistant: the tab bar has no assistant tab`, (await page.locator('#bt-assistant').count()) === 0)
      for (const id of ['inspector', 'code', 'problems', 'versions']) {
        await page.click(`#bt-${id}`); await settle(200)
        const m = await page.evaluate(() => {
          const t = document.querySelector('.tabs'); const bar = t.getBoundingClientRect()
          const tabs = [...t.querySelectorAll('[role=tab]')].map(b => b.getBoundingClientRect())
          const gl = document.getElementById('bt-glossary').getBoundingClientRect()
          return { bar: Math.round(bar.width), over: t.scrollWidth - t.clientWidth, outside: tabs.filter(r => r.right > bar.right + 0.5).length, glRight: Math.round(gl.right), win: window.innerWidth }
        })
        ok(`${tag}: ${lang} ${width} px, no assistant, «${id}» chosen (bar ${m.bar} px): nothing overflows the bar and the glossary is in the window`, m.over <= 0 && m.outside === 0 && m.glRight <= m.win, JSON.stringify(m))
      }
    }
  }
  await ctx.addCookies([{ name: 'tabularia-locale', value: 'en', url: UI }])
  mock.ai = true

  // ---- 1c. a new model: the template gallery makes the canvas inert; «Save and leave» leaves ------------------------
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto(`${UI}/bayes-model?project=2`, { waitUntil: 'networkidle', timeout: 180000 })
  await page.waitForSelector('.galwrap', { timeout: 90000 })
  ok(`${tag}: the gallery over an empty canvas makes the canvas inert`, await page.evaluate(() => !!document.querySelector('.cv .vue-flow')?.closest('[inert]')))
  await page.click('.pl [data-kind]'); await page.waitForSelector('.vue-flow__node', { timeout: 10000 }); await settle(800)
  await page.click('.brand'); await page.waitForSelector('.modal-foot .primary', { timeout: 10000 })
  await page.click('.modal-foot .primary'); await settle(1500)
  ok(`${tag}: «Save and leave» on a new model saves it (once) and leaves for where it was going`, mock.creates === 1 && new URL(page.url()).pathname === '/', `creates ${mock.creates} at ${page.url()}`)

  // ---- 2. «Stan code» ----------------------------------------------------------------------------------------
  await openEditor(1440)
  await page.click('.vue-flow__node[data-id="mua"] .bn'); await settle(300)
  await page.click('#bt-code')
  await page.waitForSelector('.monaco-editor .view-line', { timeout: 60000 })
  await page.waitForSelector('.rows .row', { timeout: 10000 })
  await settle(1000)
  const top = await page.evaluate(() => { const b = document.querySelector('#bp-code'); const h = b.querySelector('.head h2').getBoundingClientRect(); const t = document.querySelector('.tabs').getBoundingClientRect(); return { scrollTop: b.scrollTop, h2Top: h.top, tabsBottom: t.bottom } })
  ok(`${tag}: the Stan tab opens with scrollTop 0 and a node selected`, top.scrollTop === 0 && top.h2Top >= top.tabsBottom, JSON.stringify(top))
  ok(`${tag}: the selected node selects its row`, (await page.$$eval('.rows .row[aria-selected="true"]', r => r.length)) === 1)
  ok(`${tag}: the code is read only (typing adds nothing)`, await (async () => { await page.click('.monaco-editor .view-line'); await page.keyboard.type('zzz'); await settle(100); return page.evaluate(() => !document.querySelector('.monaco-editor').textContent.includes('zzz')) })())
  for (const mod of ['Control', 'Meta']) {
    // the key of the editor from the code's own field: it saves (the browser's «save the page» never opens)
    const before = mock.saves
    await page.click('.monaco-editor .view-line'); await page.keyboard.press(`${mod}+s`); await settle(700)
    ok(`${tag}: ${mod === 'Meta' ? '⌘S' : 'Ctrl+S'} with the focus inside the code saves the model`, mock.saves === before + 1, `${before} -> ${mock.saves}`)
  }
  await page.keyboard.press('Escape'); await settle(200)
  ok(`${tag}: Esc from the code goes to the tab`, (await page.evaluate(() => document.activeElement?.id)) === 'bt-code')
  await page.click('.monaco-editor .view-line'); await page.keyboard.press('Tab'); await settle(200)
  const afterTab = await page.evaluate(() => { const a = document.activeElement; return a ? (a.closest('.monaco-editor') ? 'monaco' : (a.className || a.tagName)) : '' })
  ok(`${tag}: Tab leaves the code for the page`, !/^monaco/.test(afterTab), afterTab)
  await page.focus('.rows .row[tabindex="0"]')
  const firstRow = await page.evaluate(() => document.activeElement?.textContent?.trim().slice(0, 30))
  await page.keyboard.press('ArrowDown'); await settle(100)
  const secondRow = await page.evaluate(() => document.activeElement?.textContent?.trim().slice(0, 30))
  ok(`${tag}: ↓ walks to the next row, ↑ back`, secondRow && secondRow !== firstRow && (await (async () => { await page.keyboard.press('ArrowUp'); await settle(100); return page.evaluate(() => document.activeElement?.textContent?.trim().slice(0, 30)) })()) === firstRow, `${firstRow} / ${secondRow}`)
  await page.keyboard.press('ArrowDown'); await page.keyboard.press('ArrowDown'); await page.keyboard.press('Enter'); await settle(300)
  const picked = await page.evaluate(() => document.querySelector('.vue-flow__node.selected')?.getAttribute('data-id'))
  ok(`${tag}: Enter on a row selects its node on the canvas, the tab stays`, !!picked && (await page.evaluate(() => document.getElementById('bt-code').getAttribute('aria-selected'))) === 'true', picked)
  await page.keyboard.press('Escape'); await settle(150)
  ok(`${tag}: Esc from a row goes to the tab`, (await page.evaluate(() => document.activeElement?.id)) === 'bt-code')
  const lowCode = await lowContrast('#bp-code .rows, #bp-code .head, #bp-code .notice')
  ok(`${tag}: the code tab's text is at 4.5:1 or more`, lowCode.length === 0, lowCode.join(' ; '))
  await shot('code-tab')

  // ---- 3. the assistant ------------------------------------------------------------------------------------------
  await openEditor(1440)
  await page.click('#bt-assistant')
  await page.waitForSelector('.ba-field textarea', { timeout: 10000 })
  ok(`${tag}: the assistant's field is enabled`, !(await page.$eval('.ba-field textarea', e => e.disabled)))
  await page.click('.ba-run summary')
  await page.waitForSelector('.ba-pick', { timeout: 5000 })
  const picks = await page.$$eval('.ba-pick', r => r.map(x => ({ t: x.textContent.replace(/\s+/g, ' ').trim(), off: x.querySelector('input').disabled })))
  ok(`${tag}: the run picker: no run, two runs, and a node never run that cannot be chosen`, picks.length === 4 && picks[3].off && !picks[1].off && !picks[2].off && /development run/.test(picks[2].t) && /not run yet/.test(picks[3].t), JSON.stringify(picks))
  ok(`${tag}: the radios are reachable by keyboard (a radio group, one name)`, (await page.$$eval('input[name="ba-run"]', r => r.length)) === 4)
  await page.focus('input[name="ba-run"]:checked'); await page.keyboard.press('ArrowDown'); await settle(150)
  ok(`${tag}: ↓ in the picker chooses the next run`, (await page.$$eval('.ba-pick.on', r => r.length)) === 1 && (await page.evaluate(() => document.querySelector('input[name="ba-run"]:checked')?.value)) !== '')
  await settle(500)
  ok(`${tag}: a run of another version says so`, /version 3/.test(await page.evaluate(() => document.querySelector('.ba-note.warn')?.textContent ?? '')) || /version/.test(await page.evaluate(() => document.querySelector('.ba-note.warn')?.textContent ?? '')))
  await shot('assistant-picker')
  await page.click('.ba-run summary')
  const ask = async (text) => {
    await page.fill('.ba-field textarea', text)
    await page.press('.ba-field textarea', 'Enter')
    await page.waitForFunction(() => !document.querySelector('.ba-activity') && document.querySelectorAll('.ba-a').length > 0, null, { timeout: 15000 })
    await settle(500)
  }
  const nodeText = (id) => page.$eval(`.vue-flow__node[data-id="${id}"]`, e => e.textContent.replace(/\s+/g, ' '))
  const before = await nodeText('mua')
  const tauaBefore = await nodeText('taua')
  await ask('PROPOSE two things')
  await page.waitForSelector('.ba-proposal.pending', { timeout: 5000 })
  await page.waitForFunction(() => document.querySelectorAll('.ba-pactions .primary:not([disabled])').length > 0, null, { timeout: 8000 })
  const geo = await page.evaluate(() => { const n = document.querySelector('.ba > .ba-note.warn').getBoundingClientRect(); const s = document.querySelector('.ba-stream'); return { noteBottom: n.bottom, streamTop: s.getBoundingClientRect().top, mask: getComputedStyle(s).maskImage || getComputedStyle(s).webkitMaskImage } })
  ok(`${tag}: the notice above the conversation does not overlap it, and the text fades under the edge`, geo.noteBottom <= geo.streamTop && /gradient/.test(geo.mask), JSON.stringify(geo))
  ok(`${tag}: «Apply all» is offered once the gateway read the graph with no new finding`, /adds no finding/.test(await page.$eval('.ba-proposal', e => e.textContent)))
  await shot('assistant-proposal')
  const lowAssist = await lowContrast('.ba')
  ok(`${tag}: the assistant's text is at 4.5:1 or more`, lowAssist.length === 0, lowAssist.slice(0, 4).join(' ; '))
  await page.click('.ba-pactions .primary'); await settle(700)
  ok(`${tag}: Apply all changes the canvas`, /student_t/.test(await nodeText('mua')) && (await nodeText('mua')) !== before)
  ok(`${tag}: the proposal says how to undo`, /Undo/.test(await page.$eval('.ba-proposal', e => e.textContent)))
  await page.keyboard.press('Control+z'); await settle(600)
  ok(`${tag}: one Ctrl+Z takes the whole proposal back`, (await nodeText('mua')) === before)
  // a proposal of two units applied one by one (review of the editor): the focus goes to the note of what happened, the
  // unit left can still be applied, and no verdict of the old graph stands while the new one is read
  await ask('PROPOSE one by one')
  await page.waitForFunction(() => document.querySelectorAll('.ba-a:last-child .ba-one:not([disabled])').length === 2, null, { timeout: 8000 })
  await page.locator('.ba-a:last-child .ba-one').first().click()
  await settle(80)
  ok(`${tag}: right after one unit is applied the other's «Apply» is off until the new graph is read`, await page.evaluate(() => [...document.querySelectorAll('.ba-a:last-child .ba-one')].every(b => b.disabled)))
  ok(`${tag}: after «Apply» the focus is on the note of what happened, not on the page`, await page.evaluate(() => /^ba-out-/.test(document.activeElement?.id ?? '')), await page.evaluate(() => document.activeElement?.outerHTML?.slice(0, 80)))
  await page.waitForFunction(() => { const b = [...document.querySelectorAll('.ba-a:last-child .ba-one')]; return b.length >= 1 && b.some(x => !x.disabled) }, null, { timeout: 8000 }).catch(() => {})
  ok(`${tag}: the unit left alone can be applied (its «Apply» is enabled)`, await page.evaluate(() => [...document.querySelectorAll('.ba-a:last-child .ba-one')].some(b => !b.disabled)))
  await page.locator('.ba-a:last-child .ba-pactions button', { hasText: 'Discard' }).click(); await settle(300)
  ok(`${tag}: after «Discard» the focus is on its note`, await page.evaluate(() => /^ba-out-/.test(document.activeElement?.id ?? '')))
  await page.keyboard.press('Delete'); await settle(300)
  ok(`${tag}: Delete with the focus on that note removes no node`, (await page.locator('.vue-flow__node[data-id="mua"]').count()) === 1 && (await page.locator('.vue-flow__node[data-id="taua"]').count()) === 1)
  await page.keyboard.press('Control+z'); await settle(600)
  ok(`${tag}: Ctrl+Z from there takes the applied unit back`, (await nodeText('mua')) === before && (await nodeText('taua')) === tauaBefore)
  await ask('BROKEN')
  await page.waitForFunction(() => /refuses/.test(document.querySelector('.ba-a:last-child .ba-proposal')?.textContent ?? ''), null, { timeout: 8000 })
  const refused = await page.$eval('.ba-a:last-child .ba-proposal', e => ({ buttons: [...e.querySelectorAll('button')].map(b => ({ t: b.textContent.trim(), off: b.disabled })) }))
  ok(`${tag}: a proposal the gateway refuses: «Apply» is disabled, «Send the findings back» and «Discard» are not`, refused.buttons.find(b => /^Apply/.test(b.t))?.off === true && refused.buttons.find(b => /Send the findings/.test(b.t))?.off === false && refused.buttons.find(b => /Discard/.test(b.t))?.off === false, JSON.stringify(refused))
  ok(`${tag}: the disabled «Apply» does nothing on a click`, await (async () => { const b = await nodeText('taua'); await page.locator('.ba-a:last-child .ba-pactions button', { hasText: /^Apply/ }).click({ force: true, timeout: 2000 }).catch(() => {}); await settle(300); return (await nodeText('taua')) === b })())
  await shot('assistant-refused')
  await page.click('.ba-a:last-child .ba-pactions button:has-text("Discard")'); await settle(300)
  ok(`${tag}: «Discard» marks the proposal discarded and removes its buttons`, /Discarded/.test(await page.$eval('.ba-a:last-child .ba-proposal', e => e.textContent)) && (await page.$$('.ba-a:last-child .ba-pactions')).length === 0)

  // ---- 4. the flow editor: the node's panel ----------------------------------------------------------------------
  const openNode = async () => {
    await page.setViewportSize({ width: 1440, height: 900 })
    await page.goto(`${UI}/editor?flow=1`, { waitUntil: 'networkidle', timeout: 180000 })
    await page.waitForSelector('.vue-flow__node[data-id="ctl-1"]', { timeout: 90000 })
    await settle(1500)
    await page.click('.vue-flow__node[data-id="ctl-1"] .node-title')
    await page.waitForSelector('.bayes-panel', { timeout: 30000 })
    await settle(1200)
    return page.evaluateHandle(() => { let el = document.querySelector('.bayes-panel'); while (el && el.scrollHeight <= el.clientHeight + 2) el = el.parentElement; return el })
  }
  // 4a. the prior predictive button and the Report's switch
  let sc = await openNode()
  await sc.evaluate(el => { el.scrollTop = el.scrollHeight }); await settle(300)
  await page.locator('.model-run button').first().focus(); await page.keyboard.press('Tab')
  ok(`${tag}: Tab from Run reaches the prior predictive button`, await page.evaluate(() => document.activeElement?.classList.contains('check')))
  ok(`${tag}: the button has a visible focus ring`, await page.evaluate(() => { const s = getComputedStyle(document.activeElement); return (s.outlineStyle !== 'none' && parseFloat(s.outlineWidth) >= 1) || (s.boxShadow && s.boxShadow !== 'none') }))
  await shot('prior-focus')
  const lowPrior = await lowContrast('.block.prior')
  ok(`${tag}: the prior predictive block's text is at 4.5:1 or more`, lowPrior.length === 0, lowPrior.join(' ; '))
  await page.keyboard.press('Enter'); await settle(700)
  ok(`${tag}: Enter sends the check (body {check: prior_predictive}) and Run is disabled meanwhile`, JSON.stringify(mock.posts.at(-1)) === '{"check":"prior_predictive"}' && (await page.locator('.model-run button').first().isDisabled()), JSON.stringify(mock.posts))
  await sc.evaluate(el => { el.scrollTop = el.scrollHeight }); await shot('prior-running')
  await page.waitForFunction(() => !document.querySelector('.block.prior button.check .spin'), null, { timeout: 40000 }); await settle(600)
  ok(`${tag}: when the check ends the button is enabled again`, await page.evaluate(() => !document.querySelector('.block.prior button.check').disabled))
  await page.getByRole('tab', { name: /Report/i }).click(); await page.waitForSelector('.sr', { timeout: 20000 }); await settle(2200)
  const sw = await page.evaluate(() => ({ n: document.querySelectorAll('.sr-switch button').length, pressed: [...document.querySelectorAll('.sr-switch button[aria-pressed="true"]')].map(b => b.textContent.trim()), group: document.querySelector('.sr-switch')?.getAttribute('role') ?? '', label: document.querySelector('.sr-switch')?.getAttribute('aria-label') ?? '' }))
  ok(`${tag}: the Report's switch has two buttons, one pressed, and a group name`, sw.n === 2 && sw.pressed.length === 1 && sw.group === 'group' && sw.label.length > 2, JSON.stringify(sw))
  const first = sw.pressed[0]
  await page.locator('.sr-switch button[aria-pressed="false"]').focus(); await page.keyboard.press('Enter'); await settle(2200)
  const sw2 = await page.evaluate(() => [...document.querySelectorAll('.sr-switch button[aria-pressed="true"]')].map(b => b.textContent.trim()))
  ok(`${tag}: Enter on the other button switches the report`, sw2.length === 1 && sw2[0] !== first, JSON.stringify([first, sw2]))
  const lowSw = await lowContrast('.sr-switch, .sr-sub')
  ok(`${tag}: the switch's text is at 4.5:1 or more (pressed and not)`, lowSw.length === 0, lowSw.join(' ; '))
  ok(`${tag}: the pressed button differs from the other by more than colour (weight, border or fill)`, await page.evaluate(() => { const [a, b] = ['true', 'false'].map(v => getComputedStyle(document.querySelector(`.sr-switch button[aria-pressed="${v}"]`))); return a.backgroundColor !== b.backgroundColor || a.fontWeight !== b.fontWeight || a.borderColor !== b.borderColor }))
  await shot('report-switch')
  // 4b. the MLflow section
  sc = await openNode()
  const mlflow = page.locator('.mlflow')
  const see = async () => { await mlflow.scrollIntoViewIfNeeded(); await settle(200) }
  await see()
  ok(`${tag}: MLflow, no connection: the hint and no experiment field`, (await mlflow.innerText()).includes('MLflow') && (await mlflow.locator('input[type=text]').count()) === 0)
  await shot('mlflow-none')
  await mlflow.locator('.sel-trigger').click(); await settle(200)
  const opts = await page.$$eval('.sel-panel [role=option]', r => r.map(x => x.textContent.trim()))
  ok(`${tag}: MLflow: only the MLflow connections are offered (and «none»)`, opts.length === 2 && opts.some(o => /Tracking MLflow/.test(o)) && !opts.some(o => /Warehouse/.test(o)), opts.join('|'))
  await page.locator('.sel-panel [role=option]', { hasText: 'Tracking MLflow' }).click(); await settle(500); await see()
  ok(`${tag}: MLflow, a connection and no experiment: the sentence (alert) and aria-invalid`, (await mlflow.locator('[role=alert]').count()) === 1 && (await mlflow.locator('input[aria-invalid=true]').count()) === 1)
  const lowMl = await lowContrast('.mlflow')
  ok(`${tag}: MLflow: the section's text, the problem included, is at 4.5:1 or more`, lowMl.length === 0, lowMl.join(' ; '))
  await shot('mlflow-problem')
  const exp = mlflow.locator('input[type=text]').first()
  ok(`${tag}: MLflow: the server's experiments are offered`, (await page.evaluate(() => document.querySelectorAll('datalist option').length)) === 2)
  await exp.fill('School scores'); await exp.blur(); await settle(300)
  await mlflow.locator('input[type=text]').nth(1).fill('school-scores'); await mlflow.locator('input[type=text]').nth(1).blur(); await settle(300)
  await mlflow.locator('input[role=switch]').focus(); await page.keyboard.press('Space'); await settle(300)
  ok(`${tag}: MLflow, filled: no problem left, the switch is on by keyboard`, (await mlflow.locator('[role=alert]').count()) === 0 && (await mlflow.locator('input[role=switch]').isChecked()))
  await see(); await shot('mlflow-filled')
  await mlflow.locator('.sel-trigger').click(); await settle(150)
  await page.locator('.sel-panel [role=option]').first().click(); await settle(400)
  ok(`${tag}: MLflow, connection cleared: the fields go away`, (await mlflow.locator('input[type=text]').count()) === 0)
  await mlflow.locator('.sel-trigger').click(); await settle(150)
  await page.locator('.sel-panel [role=option]', { hasText: 'Tracking MLflow' }).click(); await settle(400)
  ok(`${tag}: MLflow, connection again: no experiment and no registered name remembered`, (await mlflow.locator('input[type=text]').nth(1).inputValue()) === '' && (await mlflow.locator('input[type=text]').first().inputValue()) === '')
  await page.locator('.model-run button').first().click(); await settle(2500)
  const refusedText = await page.locator('.bayes-panel').innerText()
  ok(`${tag}: MLflow: the gateway's refusal is shown under Run`, /name the MLflow experiment/.test(refusedText), refusedText.slice(-200))
  await sc.evaluate(el => { el.scrollTop = el.scrollHeight }); await settle(300); await shot('mlflow-refusal')

  // 4c. «Use version N» on a node, then another node chosen while the version is read (review of the editor): the other
  // node keeps its own model and version
  sc = await openNode()
  await page.click('.vue-flow__node[data-id="ctl-2"] .node-title'); await settle(1200)
  await page.locator('.bayes-panel .notice button').click()
  await page.click('.vue-flow__node[data-id="ctl-1"] .node-title'); await settle(2500)
  const otherText = await page.locator('.bayes-panel').innerText()
  ok(`${tag}: a node chosen while «Use version» is read keeps its own version (no v9 from the other)`, /v4/.test(otherText) && !/v9/.test(otherText), otherText.slice(0, 200))

  // 4d. a version whose draws Stan cannot reread (correlated effects, 8 digits in the CSV; the owner's decision of 2026-11-24): the
  // replicated data cannot be chosen and say why, a node that held them goes back to the summary and says so, the prior check stays
  sc = await openNode()
  const radioOf = (v) => page.locator(`.bayes-panel input[type=radio][value="${v}"]`)
  ok(`${tag}: the replicated data cannot be chosen for a version that cannot be reread`, await radioOf('replicated').isDisabled() && !(await radioOf('summary').isDisabled()))
  ok(`${tag}: the panel says why the checks and the replicated data are off, and that the prior check stays`, /correlations between group effects/.test(await page.locator('[data-testid="bayes-noreread"]').innerText()) && /prior predictive check stays/.test(await page.locator('[data-testid="bayes-noreread"]').innerText()))
  ok(`${tag}: the prior predictive button is still offered`, (await page.locator('.block.prior button.check').count()) === 1)
  await page.click('.vue-flow__node[data-id="ctl-2"] .node-title'); await settle(1800)
  ok(`${tag}: a node that held the replicated data goes back to the summary and the panel says so`, await radioOf('summary').isChecked() && !(await radioOf('replicated').isChecked()) && (await page.locator('[data-testid="bayes-replicated-reset"]').count()) === 1)
  await shot('noreread')

  ok(`${tag}: no page error in the panels`, errors.length === 0, errors.join(' | '))
  await ctx.close()
}

module.exports = { panelsPart, makeMock, install }
