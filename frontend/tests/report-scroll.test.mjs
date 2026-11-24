// Where a report's viewer stands when new pages arrive (composables/reportScroll.ts, used by
// components/report/ReportPages.vue): another document starts at the top, the same one drawn
// again keeps its place, an owner that names no documents keeps it too.
// Run: npm test
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { scrollOnSwap } from '../composables/reportScroll.ts'

const key = (run) => JSON.stringify(['model', 1, 'n1', run, 'en'])

test('another document starts at the top: the prior predictive check after the posterior report', () => {
  assert.equal(scrollOnSwap(key('run-posterior'), key('run-prior')), 'top')
  assert.equal(scrollOnSwap(key('run-a'), key('run-b')), 'top')
})

test('the same document drawn again keeps the place (a sort, a group closed, Update)', () => {
  assert.equal(scrollOnSwap(key('run-a'), key('run-a')), 'keep')
})

test('no names, no move: the viewer and the designer keep the place as they always did', () => {
  assert.equal(scrollOnSwap(null, null), 'keep')
  assert.equal(scrollOnSwap(undefined, undefined), 'keep')
  assert.equal(scrollOnSwap(key('run-a'), null), 'keep')
})

test('the first pages of a named document, after none (a node picked, the pages cleared): the top', () => {
  assert.equal(scrollOnSwap(null, key('run-a')), 'top')
  assert.equal(scrollOnSwap(undefined, key('run-a')), 'top')
})

test('the statistics view names its documents and the pages component moves on that name', () => {
  const view = readFileSync(new URL('../components/ui/StatReportView.vue', import.meta.url), 'utf8')
  const pages = readFileSync(new URL('../components/report/ReportPages.vue', import.meta.url), 'utf8')
  assert.match(view, /:doc-key="shown\?\.key \?\? null"/)
  assert.match(pages, /scrollOnSwap\(shownDoc, doc\)/)
  // the viewer and the designer's preview name nothing: their place is kept
  for (const f of ['../pages/report.vue', '../components/report/designer/RdPreview.vue']) {
    assert.doesNotMatch(readFileSync(new URL(f, import.meta.url), 'utf8'), /doc-key/)
  }
})
