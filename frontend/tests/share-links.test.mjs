// Share links without a browser (docs/specs/share-links.md): the password rule the gateway holds,
// the temporary passwords, the origins of an embeddable link, the iframe to paste, the expiry,
// the values a link opens with, a comment as a tile shows it, and the share page's framing.
// Run: npm test
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { register } from 'node:module'

register('./alias-hooks.mjs', import.meta.url)
const m = await import('../composables/shareLinks.ts')
// the plugin's module registers itself with Nitro when imported: a stand-in for that call
Object.assign(globalThis, { defineNitroPlugin: (f) => f })
const { forShare } = await import('../server/plugins/csp.ts')

test('the password rule is the accounts\' one', () => {
  assert.equal(m.passwordProblem(''), 'empty')
  assert.equal(m.passwordProblem('Password'), 'known')
  assert.equal(m.passwordProblem(' change-me '), 'known')
  assert.equal(m.passwordProblem('elevenchars'), 'short')
  assert.equal(m.passwordProblem('twelve chars'), '')
  assert.equal(m.passwordProblem('é'.repeat(37)), 'long') // 74 bytes, 37 characters
  assert.equal(m.passwordProblem('é'.repeat(36)), '')
})

test('a temporary password is 16 characters without look-alikes, and passes the rule', () => {
  const seen = new Set()
  for (let i = 0; i < 200; i++) {
    const p = m.generatePassword()
    assert.equal(p.length, 16)
    assert.doesNotMatch(p, /[0O1lI]/)
    assert.equal(m.passwordProblem(p), '')
    seen.add(p)
  }
  assert.equal(seen.size, 200)
  // rejection sampling: a byte past the last full alphabet round is never used
  const fixed = m.generatePassword(4, (n) => new Uint8Array(n).fill(255).map((b, i) => (i % 2 ? 0 : b)))
  assert.equal(fixed, 'aaaa')
})

test('the origins of an embeddable link, as the gateway takes them', () => {
  assert.equal(m.originOf('https://Intranet.Example.com'), 'https://intranet.example.com')
  assert.equal(m.originOf('https://intranet.example.com:443/'), 'https://intranet.example.com')
  assert.equal(m.originOf('http://portal:8080'), 'http://portal:8080')
  for (const bad of ['intranet.example.com', 'https://*.example.com', 'https://a.example.com/page', 'ftp://a.com',
    'https://u@a.com', 'https://a.com?x=1', "'self'", 'https://a.com https://b.com', 'javascript:alert(1)', '']) {
    assert.equal(m.originOf(bad), null, bad)
  }
  assert.deepEqual(m.originsOf('https://a.com\n\n https://a.com/ \nbad one\nhttp://b:81'),
    { origins: ['https://a.com', 'http://b:81'], bad: ['bad one'] })
})

test('the iframe to paste escapes what it holds and sends no Referer', () => {
  const s = m.iframeSnippet('https://t.example.com/share/abc', 'Sales "Q1" <draft>')
  assert.match(s, /^<iframe src="https:\/\/t\.example\.com\/share\/abc" title="Sales &quot;Q1&quot; &lt;draft>"/)
  assert.match(s, /referrerpolicy="no-referrer"/)
})

test('the expiry is the end of a local day, the default thirty days on', () => {
  const from = new Date(2026, 0, 30, 15, 0, 0)
  assert.equal(m.dayAfter(30, from), '2026-03-01')
  const end = new Date(m.endOfDay('2026-03-01'))
  assert.equal(end.getFullYear(), 2026)
  assert.equal(end.getMonth(), 2)
  assert.equal(end.getDate(), 1)
  assert.equal(end.getHours(), 23)
  assert.match(m.endOfDay('2026-03-01'), /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$/)
})

test('a link opens with the values on screen, every one a text', () => {
  assert.deepEqual(m.paramsForLink({ region: 'North', year: 2026, ok: true, many: ['a', 2], none: null, empty: '' }),
    { region: ['North'], year: ['2026'], ok: ['true'], many: ['a', '2'] })
})

test('the link\'s address: the public host when there is one', () => {
  assert.equal(m.shareUrl('tok', 'https://share.example.com/'), 'https://share.example.com/share/tok')
})

test('a comment of the document as a tile shows it', () => {
  const ready = m.tileCommentOf({ text: 'Sales grew.', model: 'A model', numbers_at: '2026-11-07T10:00:00' })
  assert.equal(ready.state, 'ready')
  assert.equal(ready.text, 'Sales grew.')
  assert.equal(ready.can_ask, false)
  const old = m.tileCommentOf({ text: 'Old.', model: 'x' }, true)
  assert.equal(old.stale, true)
  const msg = m.tileCommentOf({ message: 'Not available now' })
  assert.equal(msg.state, 'failed')
  assert.equal(msg.message, 'Not available now')
  assert.equal(m.tileCommentOf(undefined).state, 'failed')
})

test('a share page is framed only by its link\'s origins', () => {
  const app = "default-src 'self'; frame-ancestors 'self'"
  assert.equal(forShare(app, "'none'"), "default-src 'self'; frame-ancestors 'none'")
  assert.equal(forShare(app, 'https://a.com http://b:81'), "default-src 'self'; frame-ancestors https://a.com http://b:81")
  for (const bad of [undefined, '', '*', "'self'", 'https://a.com; script-src *', 'javascript:x']) {
    assert.equal(forShare(app, bad), "default-src 'self'; frame-ancestors 'none'", String(bad))
  }
  assert.equal(forShare('', "'none'"), "frame-ancestors 'none'")
})
