// Checks every internal link of the generated site (.output/public):
//   - every href/src under the base path resolves to a generated file,
//   - every #fragment exists as an id on the target page.
//   - every link to a file of this repository on GitHub (…/blob|tree/<branch>/<path>) names a
//     file that is in git or is committed together with the site (lib/repo-files.mjs), and
//     never one kept local on purpose.
// Other external links are counted, not fetched. Exit code 1 on any broken link.
//   npm run generate && npm run check-links
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, relative, resolve } from 'node:path'
import { site } from '../site.config.mjs'
import { resolveFile } from './serve.mjs'
import { isPublished } from '../lib/repo-files.mjs'

const repoRoot = resolve(import.meta.dirname, '../..')
const escapeRe = t => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
const ghPrefix = new RegExp('^' + escapeRe(site.repo) + '/(?:blob|tree)/' + escapeRe(site.branch) + '/([^#?]+)')

const root = resolve(import.meta.dirname, '../.output/public')
const base = site.baseURL
const pages = []
;(function walk (d) {
  for (const f of readdirSync(d)) {
    const p = join(d, f)
    if (statSync(p).isDirectory()) walk(p)
    else if (f.endsWith('.html')) pages.push(p)
  }
})(root)

const idsCache = new Map()
function idsOf (file) {
  if (!idsCache.has(file)) {
    const html = readFileSync(file, 'utf8')
    idsCache.set(file, new Set([...html.matchAll(/\sid="([^"]+)"/g)].map(m => m[1])))
  }
  return idsCache.get(file)
}

const decode = s => s.replace(/&amp;/g, '&').replace(/&#x2F;/g, '/').replace(/&quot;/g, '"')
let checked = 0; let external = 0; let github = 0; const broken = []
for (const page of pages) {
  const rel = '/' + relative(root, page)
  if (rel === '/200.html') continue // SPA fallback, same shell as 404
  const html = readFileSync(page, 'utf8')
  for (const m of html.matchAll(/\s(?:href|src|poster)="([^"]+)"|<meta[^>]+(?:property|name)="(?:og:image|twitter:image)"[^>]+content="([^"]+)"/g)) {
    let raw = decode(m[1] || m[2])
    if (/^(mailto:|tel:|data:|javascript:)/.test(raw)) continue
    // absolute links to the site itself (canonical, og:image, the README's diagram links) are checked too
    if (raw.startsWith(site.url) || raw + '/' === site.url) raw = base + raw.slice(site.url.length)
    else if (/^https?:\/\//.test(raw)) {
      const gh = raw.match(ghPrefix)
      if (gh) {
        github++
        const path = decodeURIComponent(gh[1]).replace(/\/$/, '')
        if (!isPublished(repoRoot, path)) broken.push(`${rel}: GitHub link to a file that will not be on GitHub: ${path}`)
      } else external++
      continue
    }
    checked++
    const [pathPart, frag] = raw.split('#')
    let target = page
    if (pathPart) {
      if (!pathPart.startsWith('/')) { broken.push(`${rel}: relative link ${raw}`); continue }
      if (!pathPart.startsWith(base)) { broken.push(`${rel}: outside base ${raw}`); continue }
      target = resolveFile(pathPart)
      if (!target) { broken.push(`${rel}: missing ${raw}`); continue }
    }
    if (frag && target.endsWith('.html') && !idsOf(target).has(decodeURIComponent(frag))) {
      broken.push(`${rel}: missing anchor #${frag} in ${pathPart || 'same page'}`)
    }
  }
}
console.log(`${pages.length} pages, ${checked} internal links checked, ${github} GitHub links checked against the repository, ${external} other external links not fetched`)
if (broken.length) {
  console.log(`${broken.length} broken:`)
  for (const b of [...new Set(broken)]) console.log('  ' + b)
  process.exit(1)
}
console.log('No broken internal links.')
