// Every kind of connection has a generic icon and a colour, drawn in every theme, and no
// kind is left with the label alone. Run: npm test
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { register } from 'node:module'

register('./alias-hooks.mjs', import.meta.url)
const { CONN_ICONS, connIcon, CONN_ICON_FALLBACK } = await import('../composables/connectionIcons.ts')
const read = (p) => readFileSync(fileURLToPath(new URL('../' + p, import.meta.url)), 'utf8')

const types = [...read('composables/useConnections.ts').matchAll(/\{ value: '(\w+)', label: '/g)].map((m) => m[1])

test('the connection kinds were read from DB_TYPES', () => {
  assert.ok(types.length >= 14, types.join())
})

test('every kind has an icon and a colour, and no icon is for a kind that does not exist', () => {
  for (const t of types) {
    assert.ok(CONN_ICONS[t]?.icon, `no icon for ${t}`)
    assert.match(CONN_ICONS[t].color, /^var\(--conn-\w+\)$/, `no colour token for ${t}`)
  }
  assert.deepEqual(Object.keys(CONN_ICONS).sort(), [...types].sort())
  assert.equal(connIcon('nothing'), CONN_ICON_FALLBACK)
  assert.equal(connIcon(null), CONN_ICON_FALLBACK)
})

test('the SQL engines are told apart by colour', () => {
  const sql = ['postgresql', 'mysql', 'mariadb', 'clickhouse', 'trino', 'oracle', 'sqlserver']
  assert.equal(new Set(sql.map((t) => CONN_ICONS[t].color)).size, sql.length)
})

test('every icon is a lucide export the component imports', async () => {
  const lucide = await import('lucide-vue-next')
  const component = read('components/ui/ConnIcon.vue')
  for (const spec of [...Object.values(CONN_ICONS), CONN_ICON_FALLBACK]) {
    assert.ok(lucide[spec.icon], `${spec.icon} is not a lucide icon`)
    assert.match(component, new RegExp(`\\b${spec.icon}\\b`))
  }
  assert.match(component, /aria-hidden="true"/)
})

test('the colour of every kind is defined in all five themes with 3:1 against the surfaces', () => {
  const css = read('assets/main.css')
  const themes = [':root {', ':root[data-theme="light"] {', ':root[data-theme="dracula"] {', ':root[data-theme="monokai"] {', ':root[data-theme="imperium"] {']
  const lum = (h) => {
    const c = [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16) / 255).map((x) => (x <= 0.03928 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4))
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
  }
  const ratio = (a, b) => { const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p); return (x + 0.05) / (y + 0.05) }
  for (const head of themes) {
    // the block that defines the kinds' colours for this theme: the one with `--conn-` after the head
    const at = css.split(head).slice(1).map((chunk) => chunk.slice(0, chunk.indexOf('}'))).filter((b) => b.includes('--conn-'))
    assert.equal(at.length, 1, head)
    const surface = css.split(head).slice(1).map((chunk) => chunk.slice(0, chunk.indexOf('}'))).find((b) => b.includes('--panel:'))
    const grab = (b, v) => b.match(new RegExp(`--${v}:\\s*(#[0-9a-fA-F]{6})`))?.[1]
    const bgs = ['bg', 'bg-soft', 'panel', 'panel-2'].map((v) => grab(surface, v)).filter(Boolean)
    assert.ok(bgs.length >= 3, head)
    for (const t of types) {
      const c = grab(at[0], `conn-${t}`)
      assert.ok(c, `${t} has no colour in ${head}`)
      for (const bg of bgs) assert.ok(ratio(c, bg) >= 3, `${t} ${c} on ${bg} in ${head}`)
    }
  }
})
