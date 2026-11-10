// When the editor asks a step's column profile (composables/profileAsk.ts): after the count,
// the footer only; the whole profile when a column is opened, also when it was opened while
// the count still ran (before, that opening was lost and the column said «No profile for this
// column»). Run: npm test.
import { test } from 'node:test'
import assert from 'node:assert/strict'

import { profileOpen, profileStart } from '../composables/profileAsk.ts'

test('after the count, by itself: the footer only, or what is remembered', () => {
  assert.equal(profileStart(null, false), 'footer')
  assert.equal(profileStart({ partial: true }, false), 'known')
  assert.equal(profileStart({ partial: false }, false), 'known')
})

test('a column opened while the count ran: the whole profile is the first request', () => {
  assert.equal(profileStart(null, true), 'whole')
  // a footer remembered does not stand for the whole profile asked
  assert.equal(profileStart({ partial: true }, true), 'whole')
  assert.equal(profileStart({ partial: false }, true), 'known')
})

test('a column opened: remembered before the step is asked, asked once after', () => {
  assert.equal(profileOpen(false, null, false), 'remember')
  assert.equal(profileOpen(true, null, false), 'ask')
  assert.equal(profileOpen(true, { partial: true }, false), 'ask')
  assert.equal(profileOpen(true, { partial: true }, true), 'nothing')
  assert.equal(profileOpen(true, { partial: false }, false), 'nothing')
  assert.equal(profileOpen(false, { partial: false }, false), 'nothing')
})
