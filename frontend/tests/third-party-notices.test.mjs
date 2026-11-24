// The notices of the npm packages (scripts/third-party-notices.mjs).
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { collect, render } from '../scripts/third-party-notices.mjs'

function project() {
  const root = mkdtempSync(join(tmpdir(), 'notices-'))
  const pkg = (path, manifest, files = {}) => {
    mkdirSync(join(root, path), { recursive: true })
    writeFileSync(join(root, path, 'package.json'), JSON.stringify(manifest))
    for (const [name, text] of Object.entries(files)) writeFileSync(join(root, path, name), text)
  }
  pkg('node_modules/kept', { name: 'kept', version: '1.0.0', license: 'MIT' }, { LICENSE: 'Copyright Kept', NOTICE: 'Kept notice', 'readme.md': 'no' })
  pkg('node_modules/bare', { name: 'bare', version: '2.0.0', license: 'ISC' })
  pkg('node_modules/@scope/old', { name: '@scope/old', version: '3.0.0', license: { type: 'BSD-3-Clause' } }, { 'LICENCE.txt': 'Copyright Old' })
  pkg('node_modules/kept/node_modules/bare', { name: 'bare', version: '2.0.0', license: 'ISC' })
  pkg('node_modules/tool', { name: 'tool', version: '9.0.0', license: 'MIT' }, { LICENSE: 'Copyright Tool' })
  writeFileSync(join(root, 'package-lock.json'), JSON.stringify({
    packages: {
      '': { name: 'app' },
      'node_modules/kept': { version: '1.0.0', license: 'MIT' },
      'node_modules/bare': { version: '2.0.0', license: 'ISC' },
      'node_modules/kept/node_modules/bare': { version: '2.0.0', license: 'ISC' },
      'node_modules/@scope/old': { version: '3.0.0' },
      'node_modules/tool': { version: '9.0.0', license: 'MIT', dev: true },
      'node_modules/other-platform': { version: '4.0.0', license: 'MIT', optional: true },
    },
  }))
  return root
}

test('every shipped package once, with its licence and notices; no dev ones, none missing', () => {
  const found = collect(project())
  assert.deepEqual(found.map((p) => `${p.name}@${p.version}:${p.licence}`), ['@scope/old@3.0.0:BSD-3-Clause', 'bare@2.0.0:ISC', 'kept@1.0.0:MIT'])
  assert.deepEqual(found.find((p) => p.name === 'kept').texts, ['Copyright Kept', 'Kept notice'])
})

test('the file holds the notices, and says when a package ships none', () => {
  const root = project()
  writeFileSync(join(root, 'OFL.txt'), 'Font licence')
  const text = render(collect(root), [{ name: 'IBM Plex Sans', licence: 'OFL-1.1', file: join(root, 'OFL.txt') }])
  for (const piece of ['GNU AGPL-3.0', 'IBM Plex Sans', 'Font licence', 'kept 1.0.0', 'Licence: MIT', 'Copyright Kept', 'Kept notice', 'Copyright Old'])
    assert.ok(text.includes(piece), piece)
  assert.match(text, /bare 2\.0\.0\nLicence: ISC\n\n\(the package ships no licence file\)/)
  assert.ok(!text.includes('Copyright Tool'))
})

test('the vendored Timestral code is in the notices, with the same NOTICE and Apache licence the gateway embeds', () => {
  const here = dirname(fileURLToPath(import.meta.url))
  const mine = readFileSync(join(here, '../assets/third-party/timestral.txt'), 'utf8')
  const vendored = join(here, '../../gateway-go/internal/modelgroups/groups/foundation_forecast/runner/py/timestral')
  // the repository is beside the frontend here; in a build of the frontend alone it is not, and the file stands on its own
  if (existsSync(vendored))
    for (const name of ['NOTICE', 'LICENSE']) assert.ok(mine.includes(readFileSync(join(vendored, name), 'utf8').trim()), name)
  assert.ok(mine.includes('Apache License') && mine.includes('Mistral AI'))
  for (const manifest of ['../package.json', '../../website/package.json']) {
    const scripts = JSON.parse(readFileSync(join(here, manifest), 'utf8')).scripts
    const line = scripts.prebuild ?? scripts.pregenerate
    assert.ok(line.includes('Timestral inference code') && line.includes('timestral.txt'), manifest)
  }
})

test('the CmdStan family is an extra of the three scripts, and its notices are the files the Bayesian group ships, unchanged', () => {
  const here = dirname(fileURLToPath(import.meta.url))
  const mine = readFileSync(join(here, '../assets/third-party/cmdstan.txt'), 'utf8')
  for (const manifest of ['../package.json', '../../website/package.json']) {
    const scripts = JSON.parse(readFileSync(join(here, manifest), 'utf8')).scripts
    const line = scripts.prebuild ?? scripts.pregenerate
    assert.ok(line.includes('CmdStan 2.40.0') && line.includes('cmdstan.txt') && line.includes('timestral.txt'), manifest)
  }
  // the group's folder is beside the frontend in the repository; in a build of the frontend alone it is not
  const shipped = join(here, '../../gateway-go/internal/modelgroups/groups/bayes/licence')
  if (existsSync(shipped)) {
    const files = []
    const walk = (dir) => { for (const e of readdirSync(dir, { withFileTypes: true })) e.isDirectory() ? walk(join(dir, e.name)) : files.push(join(dir, e.name)) }
    walk(shipped)
    assert.equal(files.length, 22)
    for (const file of files) assert.ok(mine.includes(readFileSync(file, 'utf8').replace(/\r\n/g, '\n').trim()), file.slice(shipped.length))
  }
  // the four projects' own licences, and the libraries they bundle
  for (const piece of ['Copyright (c) 2014, Stan', 'Copyright (c) 2011--2020, Stan Developers', 'Copyright (c) 2011-2020, Stan Developers', 'Copyright (c) 2011--2018, Stan Developers', 'Boost Software License', 'Mozilla Public License', 'Apache License'])
    assert.ok(mine.includes(piece), piece)
})
