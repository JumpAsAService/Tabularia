// Module hooks for the tests that import the app's own TypeScript (Node strips the types):
// `~/composables/x` is the frontend's `composables/x.ts`, as Nuxt resolves it. Only the
// modules that need nothing of Nuxt at import time can be loaded this way (their Nuxt
// calls sit inside functions the tests do not call).
// Registered by the test files: register('./alias-hooks.mjs', import.meta.url)
import { statSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const FRONTEND = join(dirname(fileURLToPath(import.meta.url)), '..')
const isFile = (path) => statSync(path, { throwIfNoEntry: false })?.isFile() ?? false

export async function resolve (specifier, context, next) {
  if (specifier.startsWith('~/')) {
    const base = join(FRONTEND, specifier.slice(2))
    const file = [base, `${base}.ts`, join(base, 'index.ts')].find(isFile)
    if (file) return { url: pathToFileURL(file).href, shortCircuit: true }
  }
  return next(specifier, context)
}
