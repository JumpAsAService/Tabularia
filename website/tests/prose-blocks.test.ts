// Build-time touches on the docs' Markdown for the site: lib/prose-blocks.mjs. Run: npm test
import { test } from 'node:test'
import assert from 'node:assert/strict'
// @ts-expect-error plain JavaScript module
import { markCallouts, markChecks, markZoomableImages, pageTocAfterTitle, stackTable } from '../lib/prose-blocks.mjs'

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

test('screenshots under media/ can be enlarged, unless they are a link already', () => {
  const shot = (src: string) => ['img', { src, alt: 'x' }]
  const nodes = [p(shot('/media/guide/a.png')), ['a', { href: 'x' }, shot('/media/b.png')], p(shot('https://img.shields.io/x.svg'))]
  markZoomableImages(nodes)
  assert.equal((nodes[0] as any)[2][1].zoom, 'true')
  assert.equal((nodes[1] as any)[2][1].zoom, undefined)
  assert.equal((nodes[2] as any)[2][1].zoom, undefined)
})

test('GitHub alerts become callouts with a title; a plain quote stays a quote', () => {
  const asSpan = ['blockquote', {}, p(['span', {}, '!WARNING'], '\n', ['strong', {}, 'Keep the key.'], ' If it changes…')]
  const asText = ['blockquote', {}, p('[!TIP]\nCilium works too.')]
  const alone = ['blockquote', {}, p(['span', {}, '!IMPORTANT']), p('Second paragraph.')]
  const plain = ['blockquote', {}, p('A quotation.')]
  markCallouts([asSpan, asText, alone, plain])
  assert.equal((asSpan as any)[1].class, 'callout callout--warning')
  assert.deepEqual((asSpan as any)[2], ['p', { class: 'callout__title' }, 'Warning'])
  assert.deepEqual((asSpan as any)[3].slice(2), [['strong', {}, 'Keep the key.'], ' If it changes…'])
  assert.equal((asText as any)[1].class, 'callout callout--note')
  assert.deepEqual((asText as any)[3], p('Cilium works too.'))
  assert.deepEqual((alone as any).slice(2).map((n: any) => n[2]), ['Important', 'Second paragraph.'])
  assert.equal((plain as any)[1].class, undefined)
})
