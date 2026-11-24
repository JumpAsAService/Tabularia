// The defects an independent review found in the Bayesian model editor, held without a browser (the behaviour itself is
// checked in the browser in e2e/bayes_editor/): the tab bar's rule, «Save and leave» on a new model, the stale answers of
// an address, the inert canvas under the gallery, the assistant's focus, its «Apply» of the unit left and during a new check.
// Run: npm test
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const FRONTEND = fileURLToPath(new URL('..', import.meta.url))
const read = (p) => readFileSync(`${FRONTEND}${p}`, 'utf8')
const page = read('pages/bayes-model.vue')
const assistant = read('components/bayes/BayesAssistant.vue')
const between = (src, from, to) => src.slice(src.indexOf(from), src.indexOf(to, src.indexOf(from)))

test('the tab bar shortens the names of the tabs it cannot fit with or without the assistant, and never grows wider than the panel', () => {
  assert.doesNotMatch(page, /\.tabs\.many/, 'the narrow rule is not tied to the assistant\'s tab')
  assert.doesNotMatch(page, /class="tabs" :class="\{ many/)
  assert.match(page, /@container bayes-tabs \(max-width: 540px\) \{\s*\.tabs \.tab:not\(\[aria-selected="true"\]\) \.lbl/)
  assert.match(page, /\.tabs \{[^}]*overflow-x: auto/)
})

test('«Save and leave» on a new model goes where it was going: the save moves the address, which cancels the wait, so it stays and navigates again', () => {
  const fn = between(page, 'async function saveAndLeave', 'const wantedLeave')
  assert.match(fn, /const wasNew = mode\.value === 'new'/)
  assert.match(fn, /answerLeaving\(false\)\s*\n\s*await navigateTo\(to\)/)
  assert.match(page, /onBeforeRouteLeave\(\(to\) =>[^\n]*askToLeave\(to\)/)
  assert.match(page, /askToLeave\(to\)\.then/)
  assert.match(between(page, 'function askToLeave', 'function answerLeaving'), /leaveTo = to\.fullPath/)
})

test('an address, a model and its versions answer only for the latest ask; a restore opens the address once', () => {
  assert.match(page, /let openSeq = 0/)
  const open = between(page, 'async function openAddress', 'async function loadModel')
  assert.match(open, /const mine = \+\+openSeq/)
  assert.match(open, /loadModel\(id, version, mine\)/)
  const load = between(page, 'async function loadModel', 'let versionsSeq')
  assert.equal((load.match(/mine !== openSeq/g) ?? []).length >= 3, true, 'after each await and in the catch')
  assert.match(page, /let versionsSeq = 0/)
  assert.match(between(page, 'async function loadVersions', 'another address of this page'), /mine === versionsSeq/)
  const restore = between(page, 'async function restore', 'const savedWords')
  assert.match(restore, /const sameAddress = JSON\.stringify\(route\.query\)/)
  assert.match(restore, /if \(sameAddress\) await openAddress/)
  assert.doesNotMatch(restore, /shownVersion\.value !== d\.current_version/)
})

test('the template gallery over a canvas makes the canvas inert (no focus, no key, no pointer under it)', () => {
  assert.match(page, /<BayesCanvas\s+ref="canvas"\s+:inert="galleryOpen \? '' : undefined"/)
})

test('the assistant: the focus goes to the note of what happened after Apply or Discard, a unit left alone takes the verdict of the whole, and no old verdict stands while a new one is read', () => {
  assert.match(assistant, /function focusOutcome/)
  assert.match(between(assistant, 'function apply', 'function discard'), /focusOutcome\(turn\)/)
  assert.match(between(assistant, 'function discard', 'The words of what a unit changes'), /focusOutcome\(turn\)/)
  for (const id of ['`ba-out-${ti}`', '`ba-out-${ti}-unit`']) assert.ok(assistant.includes(`:id="${id}"`), id)
  assert.match(assistant, /id="`ba-out-\$\{ti\}-unit`" tabindex="-1"|:id="`ba-out-\$\{ti\}-unit`" tabindex="-1"/)
  // the unit left alone
  assert.match(assistant, /key !== '\*' && turn === active\.value && openUnits\.value\.length === 1 \? turn\.checks\['\*'\]/)
  // the verdicts are «running» as soon as a check is scheduled
  const sched = between(assistant, 'function scheduleChecks', '// a proposal arrives')
  assert.match(sched, /state: 'run', added: \[\], applicable: false/)
  assert.ok(sched.indexOf("state: 'run'") < sched.indexOf('setTimeout'))
})
