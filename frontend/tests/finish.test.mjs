// The surface finish preference: validation and clamp of the saved values, the storage keys, the
// anti-flash script in nuxt.config that must read the same keys and values, and the menu that
// shows the sheen slider only with a finish.
// Run: npm test
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { register } from 'node:module'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'

register('./alias-hooks.mjs', import.meta.url)
const m = await import('../composables/useFinish.ts')
const read = (path) => readFileSync(new URL(`../${path}`, import.meta.url), 'utf8')

test('finishOf accepts the known finishes and falls back to plain', () => {
  for (const f of ['plain', 'carbon', 'titanium', 'marble', 'slate', 'glass']) assert.equal(m.finishOf(f), f)
  for (const bad of [null, undefined, '', 'wood', 'CARBON', 3, {}]) assert.equal(m.finishOf(bad), 'plain')
  assert.deepEqual(m.FINISHES, ['plain', 'carbon', 'titanium', 'marble', 'slate', 'glass'])
})

test('sheenOf clamps to 0-100, rounds to the step of 5, and defaults to 50', () => {
  assert.equal(m.sheenOf('50'), 50)
  assert.equal(m.sheenOf(73), 75)
  assert.equal(m.sheenOf(72), 70)
  assert.equal(m.sheenOf(-20), 0)
  assert.equal(m.sheenOf('250'), 100)
  for (const bad of [null, undefined, '', 'abc', NaN, Infinity, {}]) assert.equal(m.sheenOf(bad), 50)
})

test('the storage keys', () => {
  assert.equal(m.FINISH_STORAGE_KEY, 'tabularia-finish')
  assert.equal(m.SHEEN_STORAGE_KEY, 'tabularia-sheen')
})

test('the anti-flash script reads the same keys and the same finishes as the composable', () => {
  const cfg = read('nuxt.config.ts')
  const line = cfg.split('\n').find((l) => l.includes('tabularia-finish'))
  assert.ok(line, 'the anti-flash script handles the finish')
  assert.ok(line.includes(`'${m.FINISH_STORAGE_KEY}'`) && line.includes(`'${m.SHEEN_STORAGE_KEY}'`))
  const list = line.match(/tabularia-finish[^[]*\[([^\]]*)\]/)[1].match(/'(\w+)'/g).map((s) => s.slice(1, -1))
  assert.deepEqual(list, m.FINISHES.filter((f) => f !== 'plain'))
  assert.ok(line.includes("'data-finish'") && line.includes("'--finish-sheen'"))
})

test('the anti-flash script and the composable give the same finish and sheen for whatever is in storage', () => {
  // the script itself, run on a pretend page: what it sets before the first paint must be what the
  // composable sets at boot, or the reflections jump between the two
  const code = read('nuxt.config.ts').split('\n').find((l) => l.includes('tabularia-finish')).trim().replace(/^"|",?$/g, '')
  const stored = [null, '', '  ', 'abc', '0', '5', '50', '73', '100', '250', '-20', '1e2', 'NaN']
  const finishes = [null, '', 'plain', 'wood', 'carbon', 'titanium', 'marble', 'slate', 'glass', 'GLASS']
  for (const sheen of stored) {
    for (const finish of finishes) {
      const attrs = {}
      const props = {}
      const page = {
        localStorage: { getItem: (k) => ({ 'tabularia-theme': 'light', 'tabularia-finish': finish, 'tabularia-sheen': sheen })[k] ?? null },
        document: { documentElement: { setAttribute: (k, v) => { attrs[k] = v }, style: { setProperty: (k, v) => { props[k] = v } } } },
      }
      vm.runInNewContext(code, page)
      const wantFinish = m.finishOf(finish)
      assert.equal(attrs['data-finish'], wantFinish === 'plain' ? undefined : wantFinish, `finish ${JSON.stringify(finish)}`)
      assert.equal(props['--finish-sheen'], String(m.sheenOf(sheen) / 100), `sheen ${JSON.stringify(sheen)}`)
    }
  }
})

test('every finish has its name in every language', () => {
  for (const lang of ['en', 'it', 'fr', 'de', 'es']) {
    const words = read(`i18n/locales/${lang}.ts`)
    for (const f of m.FINISHES) assert.ok(words.includes(`finish${f[0].toUpperCase()}${f.slice(1)}:`) || words.includes(`"finish${f[0].toUpperCase()}${f.slice(1)}":`), `${lang}: ${f}`)
    assert.ok(/\bsheen["']?:/.test(words), `${lang}: sheen`)
  }
})

test('nothing in the finishes stylesheet reaches a page that has no finish', () => {
  // every rule hangs on :root[data-finish…]: without the attribute the app is the app as it was
  const css = read('assets/finishes.css').replace(/\/\*[\s\S]*?\*\//g, '')
  const flat = css.replace(/@(media|supports)[^{]*\{/g, '').replace(/@property[^{]*\{[^}]*\}/g, '')
  const selectors = [...flat.matchAll(/(^|\})\s*([^{}@]+)\{/g)].map((x) => x[2].trim()).filter(Boolean)
  assert.ok(selectors.length > 10)
  for (const group of selectors) {
    for (const one of group.split(/,(?![^(]*\))/)) assert.ok(one.trim().startsWith(':root[data-finish'), one.trim().slice(0, 80))
  }
})

test('the menu shows the sheen slider only with a finish, labelled and with a visible value', () => {
  const shell = read('components/AppShell.vue')
  assert.match(shell, /v-if="finish !== 'plain'"[^>]*class="sheen-row"/)
  assert.match(shell, /<label[^>]*for="settings-sheen"/)
  assert.match(shell, /id="settings-sheen"[\s\S]*?type="range"[\s\S]*?min="0"[\s\S]*?max="100"[\s\S]*?step="5"/)
  assert.ok(shell.includes('{{ sheen }}%'))
  assert.ok(shell.indexOf("t('settings.theme')") < shell.indexOf("t('settings.finish')"))
  assert.ok(shell.indexOf("t('settings.finish')") < shell.indexOf("t('settings.language')"))
})

test('a plugin starts the preference at boot', () => {
  assert.match(read('plugins/finish.client.ts'), /useFinish\(\)/)
})
