// The app's confirmation dialog: the queue behind useConfirm() (one question at a time, a Promise
// answered true or false, the oldest first), the words every kind of question has in every
// language, and that no native confirm()/alert()/prompt() is left in the frontend.
// Run: npm test
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { register } from 'node:module'

register('./alias-hooks.mjs', import.meta.url)
const C = await import('../composables/useConfirm.ts')
const FRONTEND = fileURLToPath(new URL('..', import.meta.url))

const { confirmAction, current, answer, queue } = C.useConfirm()
const flush = () => new Promise((r) => setTimeout(r, 0))

test('a question resolves true on the action and false on Cancel', async () => {
  const yes = confirmAction('Delete it?', 'delete')
  assert.equal(current().message, 'Delete it?')
  answer(true)
  assert.equal(await yes, true)
  const no = confirmAction('Delete it?', 'delete')
  answer(false)
  assert.equal(await no, false)
  assert.equal(current(), null)
})

test('one question is on screen at a time, the oldest first, and the next takes its place', async () => {
  const a = confirmAction('first', 'confirm')
  const b = confirmAction('second', 'revoke')
  assert.equal(queue.value.length, 2)
  assert.equal(current().message, 'first')
  let bDone = false
  b.then(() => { bDone = true })
  await flush()
  assert.equal(bDone, false, 'the second waits while the first is on screen')
  answer(true)
  assert.equal(await a, true)
  assert.equal(current().message, 'second')
  answer(false) // what Esc and a click outside do
  assert.equal(await b, false)
  assert.equal(queue.value.length, 0)
})

test('answering with nothing on screen does nothing', () => {
  answer(true)
  assert.equal(current(), null)
})

test('losing and cutting actions are destructive, the others are not, and the caller can overrule', () => {
  for (const k of ['delete', 'remove', 'revoke', 'discard', 'leave', 'stop', 'disable', 'reset', 'rotate', 'forget', 'close']) {
    confirmAction('x', k); assert.equal(current().destructive, true, k); answer(false)
  }
  for (const k of ['confirm', 'restore', 'grant', 'replace', 'overwrite']) {
    confirmAction('x', k); assert.equal(current().destructive, false, k); answer(false)
  }
  confirmAction('x', 'confirm', { destructive: true }); assert.equal(current().destructive, true); answer(false)
})

test('every kind has a title and an action label in the five languages, and Cancel too', async () => {
  const kinds = Object.keys(Object.fromEntries(C.DESTRUCTIVE_KINDS.map((k) => [k, 1])))
  kinds.push('confirm', 'restore', 'grant', 'replace', 'overwrite')
  for (const l of ['en', 'it', 'de', 'es', 'fr']) {
    const d = (await import(`../i18n/locales/${l}.gen.ts`)).default.confirmDialog
    assert.ok(d.cancel, `${l} cancel`)
    for (const k of kinds) {
      assert.ok(d[k]?.title && d[k]?.label, `${l} ${k}`)
      assert.notEqual(d[k].label.toLowerCase(), 'ok', `${l} ${k}: the button says the action`)
    }
  }
})

test('no native confirm(), alert() or prompt() is left in the frontend', () => {
  const out = execFileSync('git', ['grep', '-n', '-E', '(^|[^.a-zA-Z_])(confirm|alert|prompt)\\(|window\\.(confirm|alert|prompt)\\(', '--', '*.vue', '*.ts'], { cwd: FRONTEND, encoding: 'utf8' }).split('\n')
  const hits = out.filter((l) => l
    && !l.startsWith('tests/')
    // the dialogs' own «confirm» button handler (a function of that name, not the browser's)
    && !/function confirm\(\)/.test(l)
    && !/^\S+:\d+:\s*(\/\/|\*|<!--)/.test(l))
  assert.deepEqual(hits, [])
})
