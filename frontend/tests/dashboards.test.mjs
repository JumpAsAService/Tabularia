// Dashboards without a browser: what a new one starts from, where a search result leads.
// The modules are the app's own TypeScript, loaded through alias-hooks.mjs (`~/…`).
// Run: npm test
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { register } from 'node:module'

register('./alias-hooks.mjs', import.meta.url)
const { starterDashboard } = await import('../composables/useDashboards.ts')
const { searchHitTarget } = await import('../composables/useSearch.ts')

test('a new dashboard is empty, refreshed by hand, in the language of whoever makes it', () => {
  assert.deepEqual(starterDashboard('it', 'Europe/Rome'), {
    v: 1, locale: 'it', timezone: 'Europe/Rome', refresh_seconds: 0, parameters: [], datasets: [], tiles: [],
  })
})

test('a language or a zone the definition does not take falls back to what it does', () => {
  const started = starterDashboard('pt', '')
  assert.equal(started.locale, 'en')
  assert.equal(started.timezone, 'UTC')
})

test('a dashboard found by the search opens on its page', () => {
  assert.equal(searchHitTarget({ kind: 'dashboard', id: 7, name: 'Live' }), '/dashboard?id=7')
})
