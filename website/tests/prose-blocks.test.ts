// Build-time touches on the docs' Markdown for the site: lib/prose-blocks.mjs. Run: npm test
import { test } from 'node:test'
import assert from 'node:assert/strict'
// @ts-expect-error plain JavaScript module
import { markChecks, pageTocAfterTitle, stackTable } from '../lib/prose-blocks.mjs'

const h = (lv: number, t: string) => [`h${lv}`, { id: t.toLowerCase() }, t]
const p = (...c: any[]) => ['p', {}, ...c]

test('"On this page" goes right after the title, once', () => {
  const out = pageTocAfterTitle([h(1, 'Title'), p('intro'), h(2, 'A')])
  assert.deepEqual(out.map((n: any) => n[0]), ['h1', 'page-toc', 'p', 'h2'])
  assert.deepEqual(pageTocAfterTitle(out), out)
  assert.deepEqual(pageTocAfterTitle([p('no title')]), [p('no title')])
})

const table = (head: string[], ...rows: string[][]) => ['table', {},
  ['thead', {}, ['tr', {}, ...head.map(t => ['th', {}, t])]],
  ['tbody', {}, ...rows.map(r => ['tr', {}, ...r.map(t => ['td', {}, ['code', {}, t]])])]]
const cells = (t: any) => t[3].slice(2).map((tr: any) => tr.slice(2).map((td: any) => [td[1]['data-label'], td[1]['data-place']]))

test('a table that fits: labels and roles only', () => {
  const t = stackTable(table(['Port', 'Service'], ['3000', 'the app']))
  assert.equal(t[1].class, undefined)
  assert.equal(t[2][1].role, 'rowgroup')
  assert.equal(t[2][2][2][1].role, 'columnheader')
  assert.deepEqual(cells(t), [[['Port', 'title'], ['Service', 'meta']]])
})

test('five columns: stacked, a long last column below', () => {
  const d = 'Turns the policy on: the workers reach the internet and the databases, never the metadata address.'
  const t = stackTable(table(['Variable', 'Type', 'Default', 'Helm value', 'Description'], ['A', 'string', 'x', 'a.b', d]))
  assert.equal(t[1].class, 'is-stacked')
  assert.deepEqual(cells(t)[0].map((c: any) => c[1]), ['title', 'meta', 'meta', 'meta', 'body'])
})

test('few columns with long unbreakable values: stacked; a short last column stays on the line', () => {
  const t = stackTable(table(['Method', 'Path', 'Function'],
    ['GET', '/api/v1/projects/{project_id}/flows/{flow_id}/versions/{version}/lineage', 'app.api.routes.flows.get_flow_version_lineage_handler']))
  assert.equal(t[1].class, 'is-stacked')
  assert.deepEqual(cells(t)[0].map((c: any) => c[1]), ['title', 'meta', 'meta'])
})

test('a paragraph that starts with **Check:** is a check, at any depth; others are not', () => {
  const nodes = [p(['strong', {}, 'Check:'], ' it answers'), p('A ', ['strong', {}, 'Check:']),
    ['li', {}, p(['strong', {}, 'Check:'], ' nested')], p(['strong', {}, 'Check files'], ' x')]
  markChecks(nodes)
  assert.deepEqual(nodes.map((n: any) => (n[0] === 'li' ? n[2][1].class : n[1].class)), ['check', undefined, 'check', undefined])
})
