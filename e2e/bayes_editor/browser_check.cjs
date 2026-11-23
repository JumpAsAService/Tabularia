// Browser check of the editor of Bayesian models' keyboard (docs/specs/bayes-graph-editor.md, «Keyboard»),
// on Chromium and Firefox of Playwright, with a PC's keyboard and a Mac's (the Mac's is a context whose
// platform says MacIntel, and whose shortcut key is Command). The page runs on `nuxt dev` with a made-up
// `tab_token` cookie and no backend: the editor works on the template opened and the mirror in the browser.
//
//   UI=http://127.0.0.1:3123 PLAYWRIGHT_CORE=<path of playwright-core> node e2e/bayes_editor/browser_check.cjs <chromium|firefox>
//   (browser_check.py runs it on both)
//
// Prints one line per check, «ok» or «KO», and exits 1 if any is KO.
const pw = require(process.env.PLAYWRIGHT_CORE || 'playwright-core')
const UI = process.env.UI || 'http://127.0.0.1:3123'
const which = process.argv[2] || 'chromium'
const results = []
const ok = (name, cond, detail = '') => {
  results.push(!!cond)
  console.log(`${cond ? '  ok  ' : '  KO  '}${name}${cond ? '' : ` — ${String(detail).slice(0, 300)}`}`)
}

async function run (browser, mac) {
  const MOD = mac ? 'Meta' : 'Control'
  const tag = `${which} ${mac ? 'Mac' : 'PC'}`
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } })
  await ctx.addCookies([{ name: 'tab_token', value: 'g5-check', url: UI }, { name: 'tabularia-locale', value: 'en', url: UI }])
  if (mac) {
    // the app reads userAgentData.platform first (Chromium), then platform
    await ctx.addInitScript(() => {
      Object.defineProperty(Navigator.prototype, 'platform', { get: () => 'MacIntel' })
      if ('userAgentData' in navigator) Object.defineProperty(Navigator.prototype, 'userAgentData', { get: () => ({ platform: 'macOS', mobile: false, brands: [] }) })
    })
  }
  const page = await ctx.newPage()
  const errors = []
  page.on('pageerror', e => errors.push(String(e)))
  await page.goto(`${UI}/bayes-model?template=hier_intercepts`, { waitUntil: 'networkidle', timeout: 180000 })
  await page.waitForSelector('.modal-foot button.primary', { timeout: 90000 })
  await page.click('.modal-foot button.primary')
  await page.waitForSelector('.vue-flow__node[data-id="bx"]', { timeout: 30000 })
  await page.waitForTimeout(900)
  const node = id => `.vue-flow__node[data-id="${id}"]`
  const has = id => page.locator(node(id)).count()
  const pos = id => page.evaluate(i => document.querySelector(`.vue-flow__node[data-id="${i}"]`)?.style.transform ?? null, id)
  const zoom = () => page.evaluate(() => document.querySelector('.vue-flow__transformationpane')?.style.transform ?? '')
  const selectedId = () => page.evaluate(() => document.querySelector('.vue-flow__node.selected')?.getAttribute('data-id') ?? null)
  const settle = (ms = 300) => page.waitForTimeout(ms)
  const key = async (k) => { await page.keyboard.press(k); await settle(250) }
  const onNode = async (id) => { await page.click(`${node(id)} .bn`); await page.focus(node(id)); await settle(200) }

  // Shift+arrow moves the node by one step of the history; the mod+Z takes it back
  await onNode('bx')
  const p0 = await pos('bx')
  await key('Shift+ArrowRight')
  ok(`${tag}: Shift+→ moves the node`, p0 !== await pos('bx'))
  await key(`${MOD}+z`)
  ok(`${tag}: ${mac ? '⌘Z' : 'Ctrl+Z'} undoes the move`, p0 === await pos('bx'))
  // a bare arrow moves nothing
  await page.focus(node('bx'))
  await key('ArrowRight')
  ok(`${tag}: a bare arrow moves nothing`, p0 === await pos('bx'))
  // Alt+arrows walk to the nearest node
  await onNode('bx')
  const before = await selectedId()
  let walked = null
  for (const d of ['ArrowRight', 'ArrowLeft', 'ArrowUp', 'ArrowDown']) {
    await page.focus(node(before)); await key(`Alt+${d}`)
    const now = await selectedId()
    if (now && now !== before) { walked = `${d} → ${now}`; break }
  }
  ok(`${tag}: Alt+arrows walk to another node`, walked, `from ${before}`)
  // remove: Backspace on both, Delete on a PC; undo and redo (Ctrl+Y on a PC, never ⌘Y)
  await onNode('bx')
  await key('Backspace')
  ok(`${tag}: Backspace removes the node`, (await has('bx')) === 0)
  await key(`${MOD}+z`)
  ok(`${tag}: undo brings it back`, (await has('bx')) === 1)
  await key(`${MOD}+Shift+z`)
  ok(`${tag}: ${mac ? '⌘⇧Z' : 'Ctrl+Shift+Z'} redoes the removal`, (await has('bx')) === 0)
  await key(`${MOD}+z`)
  await key(`${MOD}+y`)
  ok(`${tag}: ${mac ? '⌘Y does nothing (the browser’s history)' : 'Ctrl+Y redoes'}`, (await has('bx')) === (mac ? 1 : 0))
  if (!mac) {
    await key(`${MOD}+z`)
    await onNode('bx')
    await key('Delete')
    ok(`${tag}: Delete removes the node`, (await has('bx')) === 0)
    await key(`${MOD}+z`)
  } else if ((await has('bx')) === 0) await key(`${MOD}+z`)
  // duplicate
  await onNode('bx')
  const n0 = await page.locator('.vue-flow__node-bayes').count()
  await key(`${MOD}+d`)
  ok(`${tag}: ${mac ? '⌘D' : 'Ctrl+D'} duplicates the node`, (await page.locator('.vue-flow__node-bayes').count()) === n0 + 1)
  await key(`${MOD}+z`)
  // n: the palette's first item takes the focus
  await page.click('.vue-flow__pane', { position: { x: 20, y: 20 } })
  await key('n')
  ok(`${tag}: n goes to the palette`, await page.evaluate(() => !!document.activeElement?.getAttribute('data-kind')))
  // m opens the node's commands; Shift+F10 too on a PC
  await onNode('sigma')
  await key('m')
  ok(`${tag}: m opens the node's commands`, (await page.locator('.rowmenu').count()) > 0)
  await key('Escape')
  if (!mac) {
    await onNode('sigma')
    await key('Shift+F10')
    ok(`${tag}: Shift+F10 opens the node's commands`, (await page.locator('.rowmenu').count()) > 0)
    await key('Escape')
  }
  // zoom
  await page.click('.vue-flow__pane', { position: { x: 20, y: 20 } })
  const z0 = await zoom()
  await key('+')
  const z1 = await zoom()
  await key('-'); await key('-')
  const z2 = await zoom()
  await key('0')
  ok(`${tag}: + zooms in, - out, 0 fits`, z1 !== z0 && z2 !== z1 && (await zoom()) !== z2, [z0, z1, z2])
  ok(`${tag}: ${mac ? '⌘' : 'Ctrl'}+ does not zoom the canvas`, await (async () => { const a = await zoom(); await page.keyboard.press(`${MOD}+Equal`); await settle(250); return a === await zoom() })())
  // the panel's tabs by , and .
  await onNode('ag')
  const tabOf = () => page.evaluate(() => document.querySelector('[role="tablist"] [aria-selected="true"]')?.id ?? '')
  const t0 = await tabOf()
  await page.click('.vue-flow__pane', { position: { x: 20, y: 20 } })
  await key('.')
  const t1 = await tabOf()
  await key(',')
  ok(`${tag}: . and , change the panel's tab`, t0 !== t1 && (await tabOf()) === t0, [t0, t1])
  // Esc from a field goes back to the node
  await onNode('ag')
  await page.click('#bf-ag-prior-mu')
  await key('Escape')
  ok(`${tag}: Esc from a field returns to the node`, await page.evaluate(() => !!document.activeElement?.closest('.vue-flow__node')), await page.evaluate(() => document.activeElement?.outerHTML?.slice(0, 80)))
  // ? opens the list of keys, written for this keyboard
  await page.click('.vue-flow__pane', { position: { x: 20, y: 20 } })
  await key('?')
  const list = await page.evaluate(() => document.querySelector('[role="dialog"]')?.textContent ?? '')
  ok(`${tag}: ? lists the keys with this keyboard's names`, mac ? /⌘Z/.test(list) && /⌥/.test(list) && !/Ctrl/.test(list.replace(/Ctrl\+Click/g, '')) : /Ctrl\+Z/.test(list) && /Alt\+/.test(list), list.slice(0, 200))
  ok(`${tag}: no page error`, errors.length === 0, errors.join(' | '))
  await ctx.close()
}

;(async () => {
  const browser = await pw[which].launch({ headless: true })
  for (const mac of [false, true]) await run(browser, mac)
  await browser.close()
  const bad = results.filter(r => !r).length
  console.log(`${results.length - bad}/${results.length} checks passed on ${which}`)
  process.exit(bad ? 1 : 0)
})().catch(e => { console.error(e); process.exit(2) })
