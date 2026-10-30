// Writes the copyright notices and licences of the npm packages the app ships. MIT, BSD and
// Apache ask to keep them in every copy, and the build leaves none in the bundled JavaScript.
// Run before the build, so that the file lands in the static assets the server knows of. It
// lists every package that is not a dev dependency: a few only serve the build, and listing
// one too many costs nothing.
//
//   node scripts/third-party-notices.mjs OUTPUT [--extra "NAME|LICENCE|FILE"]...
import { existsSync, mkdirSync, readFileSync, readdirSync, statSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { pathToFileURL } from 'node:url'

const NOTICE_FILE = /^(licen[cs]e|copying|notice|third.?party)/i

function licenceOf(entry, manifest) {
  const given = entry.license ?? manifest.license
  return (typeof given === 'object' ? given?.type : given) || 'not declared'
}

function noticesIn(dir) {
  return readdirSync(dir)
    .filter((name) => NOTICE_FILE.test(name) && statSync(join(dir, name)).isFile())
    .sort()
    .map((name) => readFileSync(join(dir, name), 'utf8').trim())
}

/** The packages of the lockfile that are installed and not for development, once per version. */
export function collect(root) {
  const lock = JSON.parse(readFileSync(join(root, 'package-lock.json'), 'utf8'))
  const seen = new Map()
  for (const [path, entry] of Object.entries(lock.packages ?? {})) {
    if (!path || entry.dev || entry.link) continue
    const dir = join(root, path)
    // a binary for another platform, or an optional package: in the lockfile and not here
    if (!existsSync(join(dir, 'package.json'))) continue
    const manifest = JSON.parse(readFileSync(join(dir, 'package.json'), 'utf8'))
    const name = manifest.name ?? path.split('node_modules/').pop()
    const key = `${name}@${manifest.version}`
    if (!seen.has(key)) seen.set(key, { name, version: manifest.version, licence: licenceOf(entry, manifest), texts: noticesIn(dir) })
  }
  return [...seen.values()].sort((a, b) => a.name.localeCompare(b.name) || String(a.version).localeCompare(String(b.version)))
}

export function render(packages, extras = []) {
  const line = '-'.repeat(78)
  const parts = ['Third-party software in Tabularia', '',
    'Tabularia is licensed under the GNU AGPL-3.0. It includes the software listed here, under',
    'the licence each one declares, with the notices it ships.', '']
  for (const e of extras) parts.push(line, `${e.name}`, `Licence: ${e.licence}`, '', readFileSync(e.file, 'utf8').trim(), '')
  for (const p of packages) {
    parts.push(line, `${p.name} ${p.version}`, `Licence: ${p.licence}`, '')
    parts.push(p.texts.length ? p.texts.join('\n\n') : '(the package ships no licence file)', '')
  }
  return parts.join('\n')
}

if (import.meta.url === pathToFileURL(process.argv[1]).href) {
  const args = process.argv.slice(2)
  const output = args.shift()
  const extras = []
  while (args[0] === '--extra') {
    const [name, licence, file] = args[1].split('|')
    extras.push({ name, licence, file })
    args.splice(0, 2)
  }
  if (!output || args.length) throw new Error('usage: third-party-notices.mjs OUTPUT [--extra "NAME|LICENCE|FILE"]...')
  const packages = collect(process.cwd())
  mkdirSync(dirname(output), { recursive: true })
  writeFileSync(output, render(packages, extras))
  console.log(`${output}: ${packages.length} packages`)
}
