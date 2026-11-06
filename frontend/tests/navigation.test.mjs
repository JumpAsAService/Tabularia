// Changing page: a click is never answered by a still screen, and the page that opens shows
// what its address names. Without a browser: the editor's address rules, and the places
// in the code that hold the rest (a moved line would bring the old behaviour back unseen).
// Run: npm test
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { register } from 'node:module'
import { readFileSync } from 'node:fs'

register('./alias-hooks.mjs', import.meta.url)
const { editorKeyOf, opensAnotherFlow } = await import('../composables/editorAddress.ts')
const read = (path) => readFileSync(new URL(`../${path}`, import.meta.url), 'utf8')

test('the editor is one per flow, and a new flow keeps its own when first saved', () => {
  assert.equal(editorKeyOf({ flow: '4' }), 'flow:4')
  assert.equal(editorKeyOf({ engine: 'duckdb', project: '1' }), 'new:duckdb:1')
  assert.equal(editorKeyOf({ flow: ['4', '5'] }), 'flow:4') // a repeated parameter: the first
  // another flow, from the search or a link
  assert.equal(opensAnotherFlow({ flow: '9' }, { flow: '4' }, 4), true)
  // the same flow again: nothing to replace
  assert.equal(opensAnotherFlow({ flow: '4' }, { flow: '4' }, 4), false)
  // a new flow saved for the first time takes its address: the same editor
  assert.equal(opensAnotherFlow({ flow: '12' }, { engine: 'duckdb', project: '1' }, 12), false)
  // from a flow to a new one, or to a new one on another engine or folder
  assert.equal(opensAnotherFlow({ engine: 'duckdb', project: '1' }, { flow: '4' }, 4), true)
  assert.equal(opensAnotherFlow({ engine: 'polars', project: '1' }, { engine: 'duckdb', project: '1' }, null), true)
  assert.equal(opensAnotherFlow({ engine: 'duckdb', project: '2' }, { engine: 'duckdb', project: '1' }, null), true)
  assert.equal(opensAnotherFlow({ engine: 'duckdb', project: '1' }, { engine: 'duckdb', project: '1' }, null), false)
})

test('the editor asks before another flow replaces it, and an untouched new flow has nothing to ask', () => {
  const editor = read('components/FlowEditor.vue')
  assert.match(editor, /onBeforeRouteUpdate\(\(to, from\) =>\s*!opensAnotherFlow\(to\.query, from\.query, flowId\.value\) \|\| leaveConfirmed \|\| wantedLeave\.value \? true : askToLeave\(\)\)/)
  // the new flow's starting name is its saved one: «Untitled flow» against '' was a change
  const fresh = editor.slice(editor.indexOf('if (flowId.value === null) {\n    // new flow'))
  assert.ok(fresh.slice(0, 400).includes('savedName.value = flowName.value.trim()'))
  assert.match(read('pages/editor.vue'), /<FlowEditor :key="editorKey" \/>/)
})

test('the designers are one per report and per dashboard, and ask before another replaces them', () => {
  for (const page of ['pages/report-design.vue', 'pages/dashboard-design.vue']) {
    const src = read(page)
    assert.ok(src.includes("definePageMeta({ key: (route) => `${route.path}?id=${route.query.id ?? ''}` })"), page)
    assert.match(src, /onBeforeRouteUpdate\(\(to, from\) => String\(to\.query\.id \?\? ''\) === String\(from\.query\.id \?\? ''\) \|\| !dirty\.value/, page)
  }
})

test('a page change shows at once: the bar while the code arrives, the code fetched before the click', () => {
  assert.match(read('app.vue'), /<NuxtLoadingIndicator color="var\(--accent\)" :height="2" :throttle="100" \/>/)
  assert.match(read('nuxt.config.ts'), /nuxtLink: \{ prefetchOn: \{ visibility: true, interaction: true \} \}/)
  const preload = read('plugins/route-preload.client.ts')
  for (const page of ['/editor', '/dashboard', '/report', '/viewer']) assert.ok(preload.includes(`'${page}'`), page)
})

test('a page asks the server once, side by side, not one request after another', () => {
  const explore = read('components/ProjectBrowser.vue')
  const load = explore.slice(explore.indexOf('async function loadFolder('), explore.indexOf('onMounted(async () => {\n  currentId.value = folderOfRoute()'))
  assert.equal((load.match(/await /g) ?? []).length, 2) // the lists at once, then the grant dialog's
  assert.match(load, /if \(mine !== folderLoad\) return/) // a folder no longer open gets nothing
  const admin = read('components/AdminPanel.vue')
  const all = admin.slice(admin.indexOf('async function loadAll('), admin.indexOf('onMounted(loadAll)'))
  assert.equal((all.match(/await /g) ?? []).length, 2)
  const editor = read('components/FlowEditor.vue')
  assert.match(editor, /const \[, ops\] = await Promise\.all\(\[\s*useAppInfo\(\)\.load\(\),/)
})
