// The docs' system headings ("#### Windows", "#### Linux", "#### macOS", "#### Linux and
// macOS") grouped into tabs: lib/os-tabs.mjs. Run: npm test
import { test } from 'node:test'
import assert from 'node:assert/strict'
// @ts-expect-error plain JavaScript module
import { groupOsNodes, pruneOsToc } from '../lib/os-tabs.mjs'

const h = (lv: number, t: string) => [`h${lv}`, { id: t.toLowerCase() }, t]
const p = (t: string) => ['p', {}, t]

test('consecutive system blocks become one group, each block up to the next heading', () => {
  const out = groupOsNodes([
    h(2, 'Step'), p('common'),
    h(4, 'Windows'), p('w1'), p('w2'),
    h(4, 'Linux'), p('l'), ['h5', {}, 'Detail'], p('l2'),
    h(4, 'macOS'), p('m'),
    h(2, 'Next'), p('after'),
  ])
  assert.deepEqual(out[0], h(2, 'Step'))
  assert.deepEqual(out[1], p('common'))
  const [tag, props, ...panels] = out[2]
  assert.equal(tag, 'os-tabs')
  assert.equal(props.systems, 'windows linux macos')
  assert.deepEqual(panels.map((x: any) => [x[0], x[1].os, x[1].label, x.length - 2]),
    [['os-panel', 'windows', 'Windows', 3], ['os-panel', 'linux', 'Linux', 4], ['os-panel', 'macos', 'macOS', 2]])
  assert.deepEqual(out.slice(3), [h(2, 'Next'), p('after')])
})

test('"Linux and macOS" is one block', () => {
  const out = groupOsNodes([h(4, 'Windows'), p('w'), h(4, 'Linux and macOS'), p('u')])
  assert.equal(out.length, 1)
  assert.equal(out[0][1].systems, 'windows linux-macos')
})

test('a lone system heading, other h4s and other levels stay as they are', () => {
  const lone = [h(4, 'Windows'), p('only'), h(4, 'Notes'), p('n')]
  assert.deepEqual(groupOsNodes(lone), lone)
  const h3 = [h(3, 'Windows'), p('a'), h(3, 'Linux'), p('b')]
  assert.deepEqual(groupOsNodes(h3), h3)
})

test('a group ends at a heading of level 4 or above that is not a system', () => {
  const out = groupOsNodes([h(4, 'Windows'), p('w'), h(4, 'Linux'), p('l'), h(3, 'All systems'), p('c')])
  assert.equal(out[0][0], 'os-tabs')
  assert.deepEqual(out.slice(1), [h(3, 'All systems'), p('c')])
})

test('a system repeated starts a new group', () => {
  const out = groupOsNodes([h(4, 'Windows'), p('1'), h(4, 'Linux'), p('2'), h(4, 'Windows'), p('3'), h(4, 'macOS'), p('4')])
  assert.deepEqual(out.map((x: any) => [x[0], x[1].systems]), [['os-tabs', 'windows linux'], ['os-tabs', 'windows macos']])
})

test('the table of contents loses the system headings and "All systems"', () => {
  const toc = [{ id: 'a', text: 'Step', children: [
    { id: 'windows', text: 'Windows' }, { id: 'linux-and-macos', text: 'Linux and macOS' },
    { id: 'all-systems', text: 'All systems' }, { id: 'either', text: 'Either: MinIO' }] }]
  assert.deepEqual(pruneOsToc(toc), [{ id: 'a', text: 'Step', children: [{ id: 'either', text: 'Either: MinIO' }] }])
})
