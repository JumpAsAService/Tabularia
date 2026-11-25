// The line-by-line difference the script assistant shows before a proposal is applied, and the
// places in its card that keep a proposal from replacing code unseen. Run: npm test
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { register } from 'node:module'
import { readFileSync } from 'node:fs'

register('./alias-hooks.mjs', import.meta.url)
const { lineDiff, diffHunks } = await import('../composables/lineDiff.ts')
const read = (path) => readFileSync(new URL(`../${path}`, import.meta.url), 'utf8')
const kinds = (d) => d.lines.map((l) => `${l.kind[0]}:${l.text}`).join(' ')

test('equal texts have no difference, and an empty one is all added or all removed', () => {
  assert.deepEqual(lineDiff('a\nb', 'a\nb'), { added: 0, removed: 0, lines: [
    { kind: 'same', text: 'a', before: 1, after: 1 }, { kind: 'same', text: 'b', before: 2, after: 2 }] })
  assert.equal(kinds(lineDiff('', 'a\nb')), 'a:a a:b')
  assert.equal(kinds(lineDiff('a\nb', '')), 'r:a r:b')
  assert.deepEqual(lineDiff('', ''), { added: 0, removed: 0, lines: [] })
})

test('a line changed in the middle is one removed and one added, with the rest unchanged', () => {
  const d = lineDiff('import polars as pl\ndf = pl.read_parquet(src)\ndf.write_parquet(out)', 'import polars as pl\ndf = pl.scan_parquet(src)\ndf.write_parquet(out)')
  assert.equal(d.added, 1); assert.equal(d.removed, 1)
  assert.equal(kinds(d), 's:import polars as pl r:df = pl.read_parquet(src) a:df = pl.scan_parquet(src) s:df.write_parquet(out)')
  assert.deepEqual(d.lines[1], { kind: 'removed', text: 'df = pl.read_parquet(src)', before: 2 })
  assert.deepEqual(d.lines[3], { kind: 'same', text: 'df.write_parquet(out)', before: 3, after: 3 })
})

test('a repeated line counts each time, and a moved one is removed and added: what a set of lines got wrong', () => {
  // the old count compared two sets of lines: three «pass» gone read as nothing removed
  const gone = lineDiff('pass\npass\npass\nx = 1', 'x = 1')
  assert.equal(gone.removed, 3); assert.equal(gone.added, 0)
  const moved = lineDiff('a\nb\nc', 'c\na\nb')
  assert.equal(moved.added, 1); assert.equal(moved.removed, 1)
  assert.equal(kinds(moved), 'a:c s:a s:b r:c')
})

test('insertions and removals away from each other keep the lines between them unchanged', () => {
  const d = lineDiff('a\nb\nc\nd\ne', 'a\nX\nb\nc\ne')
  assert.equal(kinds(d), 's:a a:X s:b s:c r:d s:e')
  assert.deepEqual(d.lines.at(-1), { kind: 'same', text: 'e', before: 5, after: 5 })
})

test('Windows line ends are not a difference', () => {
  assert.equal(lineDiff('a\r\nb\r\n', 'a\nb\n').added, 0)
})

test('a rewrite too long to compare line by line is given whole, between what did not change', () => {
  const body = (tag) => Array.from({ length: 2100 }, (_, i) => `${tag}${i}`).join('\n')
  const d = lineDiff(`head\n${body('a')}\ntail`, `head\n${body('b')}\ntail`)
  assert.equal(d.removed, 2100); assert.equal(d.added, 2100)
  assert.equal(d.lines[0].kind, 'same'); assert.equal(d.lines.at(-1).text, 'tail')
})

test('the card shows the changed lines with two around them, and says how many it leaves out', () => {
  const before = Array.from({ length: 20 }, (_, i) => `l${i + 1}`)
  const after = [...before]; after[9] = 'changed'
  const shown = diffHunks(lineDiff(before.join('\n'), after.join('\n')))
  assert.deepEqual(shown.map((l) => (l.kind === 'gap' ? `…${l.text}` : `${l.kind[0]}:${l.text}`)),
    ['…7', 's:l8', 's:l9', 'r:l10', 'a:changed', 's:l11', 's:l12', '…8'])
  assert.deepEqual(diffHunks(lineDiff('a', 'a')), [])
})

test('the card warns when a file was edited after the proposal, and Undo is offered only while nothing changed since', () => {
  const card = read('components/scripting/ScriptAssistant.vue')
  assert.match(card, /turn\.sent = sent/)
  assert.match(card, /\[\.\.\.p\.changed, \.\.\.p\.removed\]\.filter\(\(path\) => \(turn\.sent!\[path\] \?\? null\) !== contentNow\(path\)\)/)
  assert.match(card, /:class="\{ primary: !edited\(turn\)\.length \}"/)
  assert.match(card, /p\.changed\.every\(\(path\) => contentNow\(path\) === \(p\.files\.find\(\(f\) => f\.path === path\)\?\.content \?\? null\)\)\s+&& p\.removed\.every\(\(path\) => contentNow\(path\) === null\)/)
  // what Undo puts back is read before the files change, the removed files with their content
  assert.match(card, /turn\.before = \{\s+files: \[\.\.\.p\.changed, \.\.\.p\.removed\]\.map\(\(path\) => \(\{ path, content: contentNow\(path\) \}\)\),\s+entrypoint: props\.entrypoint,\s+\}\s+emit\('apply', p\)/)
})

test('the editor changes its files in one function, for a proposal applied and for one taken back', () => {
  const editor = read('components/scripting/ScriptEditor.vue')
  assert.match(editor, /@apply="applyProposal"\s+@revert="changeFiles\(\$event\.files, \$event\.entrypoint\)"/)
  assert.match(editor, /if \(c\.content === null\) \{\s+local\.value = local\.value\.filter\(\(f\) => f\.path !== c\.path\)/)
  // the entrypoint never points at a file that is gone
  assert.match(editor, /entry\.value = has\(entrypoint\) \? entrypoint : has\(entry\.value\) \? entry\.value : \(local\.value\[0\]\?\.path \?\? ''\)/)
})
