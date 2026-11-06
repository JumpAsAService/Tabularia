// Build-time touches on the docs' Markdown for the site: lib/prose-blocks.mjs. Run: npm test
import { test } from 'node:test'
import assert from 'node:assert/strict'
// @ts-expect-error plain JavaScript module
import { pageTocAfterTitle } from '../lib/prose-blocks.mjs'

const h = (lv: number, t: string) => [`h${lv}`, { id: t.toLowerCase() }, t]
const p = (...c: any[]) => ['p', {}, ...c]

test('"On this page" goes right after the title, once', () => {
  const out = pageTocAfterTitle([h(1, 'Title'), p('intro'), h(2, 'A')])
  assert.deepEqual(out.map((n: any) => n[0]), ['h1', 'page-toc', 'p', 'h2'])
  assert.deepEqual(pageTocAfterTitle(out), out)
  assert.deepEqual(pageTocAfterTitle([p('no title')]), [p('no title')])
})
