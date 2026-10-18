// Which repository files will exist on GitHub once the site ships: everything `git ls-files`
// lists, plus the files that are committed together with the site (they are new in the
// working tree now). The docs build and the link checker both use it, so a link to a file
// that only lives on someone's disk (an uncommitted note, a local-only folder) is never
// published: the rewriter turns it into plain text, the checker fails on it.

import { execFileSync } from 'node:child_process'
import { existsSync, readdirSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'

// committed with the site; matched against paths relative to the repository root
export const SHIPPED_WITH_SITE = [
  /^website\//,
  /^docs\/configuration\.md$/,
  /^\.github\/scripts\/config_reference\.[^/]+$/,
  /^\.github\/scripts\/test_config_reference\.py$/,
  /^\.github\/workflows\/[^/]+\.yml$/,
]
// never published, whatever git says (decided by the owner: the production compose stays local)
export const NEVER_PUBLISHED = [
  /^infrastructure\/production(\/|$)/,
  /^docs\/deploy\/docker-compose\.md$/,
  /^docs\/specs\//,
]
const IGNORED_DIRS = new Set(['node_modules', '.nuxt', '.output', '.data', '.git', 'dist'])

let cache = null
export function repoFiles (repoRoot) {
  if (cache) return cache
  const files = new Set()
  let git = true
  try {
    for (const f of execFileSync('git', ['ls-files', '-z'], { cwd: repoRoot, encoding: 'utf8', maxBuffer: 64 << 20 }).split('\0')) {
      if (f) files.add(f)
    }
  } catch {
    git = false // no git (a tarball): every file on disk counts
  }
  const walk = (dir) => {
    for (const name of readdirSync(dir)) {
      if (IGNORED_DIRS.has(name)) continue
      const p = join(dir, name)
      const st = statSync(p, { throwIfNoEntry: false })
      if (!st) continue
      if (st.isDirectory()) walk(p)
      else {
        const rel = relative(repoRoot, p).split('\\').join('/')
        if (!git || SHIPPED_WITH_SITE.some(re => re.test(rel))) files.add(rel)
      }
    }
  }
  if (!git) walk(repoRoot)
  else for (const top of ['website', 'docs', '.github']) if (existsSync(join(repoRoot, top))) walk(join(repoRoot, top))
  for (const f of [...files]) if (NEVER_PUBLISHED.some(re => re.test(f))) files.delete(f)
  cache = files
  return files
}

/** A file, or a folder holding at least one published file. */
export function isPublished (repoRoot, path) {
  const files = repoFiles(repoRoot)
  const p = path.replace(/\/+$/, '')
  if (!p) return true
  if (files.has(p)) return true
  const prefix = p + '/'
  for (const f of files) if (f.startsWith(prefix)) return true
  return false
}
