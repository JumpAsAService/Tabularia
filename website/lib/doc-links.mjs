// Rewrites the links of the repository's Markdown so they work on the site.
//
// The docs are written for GitHub: `foo.md`, `../deploy/kubernetes.md#ingress`,
// `../gateway/app/db/migrations/versions/`. On the site
//   - a link to a published doc becomes the doc's route (/docs/...),
//   - the diagrams in docs/architecture stay on the site; a few repository images are served
//     by the site itself (SITE_IMAGES); the product GIFs of docs/media are not published,
//   - any other repository path becomes its GitHub URL (blob for files, tree for folders),
//   - a link to a file that will not be on GitHub (not in git, or kept local on purpose)
//     becomes plain text: the site never links somewhere that does not exist.
// Run on the raw Markdown before it is parsed (the `content:file:beforeParse` hook).

import { existsSync, statSync } from 'node:fs'
import { join, posix } from 'node:path'
import { isPublished } from './repo-files.mjs'

// Folders and files of docs/ that are not published as pages: internal notes, and the
// production compose guide that the owner keeps local.
export const PRIVATE_DOC_DIRS = ['audit', 'specs']
export const PRIVATE_DOC_FILES = ['deploy/docker-compose.md']

// Folders of docs/ published as plain files next to the pages.
export const PUBLIC_DOC_ASSET_DIRS = ['architecture']
// Only these kinds are served from the site; other files (JSON schemas) read better on GitHub.
const PUBLIC_ASSET_EXT = /\.(png|jpe?g|svg|webp|html)$/i
// Repository images the site serves itself (copied into website/public).
const SITE_IMAGES = {
  'frontend/public/logo.svg': '/logo.svg',
  // the Marquez screenshot, cropped to the graph (its header showed a test namespace)
  'docs/media/openlineage-marquez.png': '/shots/lineage-marquez.jpg',
}

export function isPublishedDoc (repoPath) {
  if (!repoPath.startsWith('docs/') || !repoPath.endsWith('.md')) return false
  const rel = repoPath.slice('docs/'.length)
  if (PRIVATE_DOC_FILES.includes(rel)) return false
  return !PRIVATE_DOC_DIRS.includes(rel.split('/')[0])
}

/** The site route of a published Markdown file of the repository. A README is its folder's page. */
export function docRoute (repoPath) {
  if (repoPath === 'README.md') return '/docs/overview'
  const rel = repoPath.slice('docs/'.length).replace(/\.md$/, '').replace(/(^|\/)README$/i, '')
  return ('/docs/' + rel.split('/').map(s => s.toLowerCase()).join('/')).replace(/\/$/, '')
}

function githubUrl (site, repoPath, isDir) {
  const kind = isDir ? 'tree' : 'blob'
  return `${site.repo}/${kind}/${site.branch}/${repoPath.replace(/\/$/, '')}`
}

const UNLINK = Symbol('unlink')

/**
 * Resolves one link target written in `fromRepoPath` (a Markdown file of the repo).
 * Returns the rewritten target, the original one when it is not a repository path, or
 * UNLINK when it points at a file that will not be published anywhere.
 */
export function rewriteTarget (target, fromRepoPath, { repoRoot, site }) {
  const t = target.trim()
  if (!t || /^[a-z][a-z0-9+.-]*:/i.test(t) || t.startsWith('#') || t.startsWith('//')) return target
  if (t.startsWith('/')) return target // already a site route
  const hashAt = t.indexOf('#')
  const pathPart = hashAt >= 0 ? t.slice(0, hashAt) : t
  const hash = hashAt >= 0 ? t.slice(hashAt) : ''
  const fromDir = posix.dirname(fromRepoPath)
  let repoPath = posix.normalize(posix.join(fromDir === '.' ? '' : fromDir, decodeURI(pathPart)))
  if (repoPath.startsWith('..')) return target // outside the repository: leave it alone
  if (repoPath === '.') repoPath = ''
  repoPath = repoPath.replace(/\/$/, '')
  const abs = join(repoRoot, repoPath)
  const exists = existsSync(abs)
  const isDir = exists && statSync(abs).isDirectory()

  if (SITE_IMAGES[repoPath]) return SITE_IMAGES[repoPath]
  if (exists && !isDir && (isPublishedDoc(repoPath) || repoPath === 'README.md')) return docRoute(repoPath) + hash
  const readme = posix.join(repoPath, 'README.md')
  if (isDir && existsSync(join(repoRoot, readme)) && isPublishedDoc(readme)) return docRoute(readme) + hash
  const top = repoPath.split('/')
  if (exists && !isDir && top[0] === 'docs' && PUBLIC_DOC_ASSET_DIRS.includes(top[1]) && PUBLIC_ASSET_EXT.test(repoPath) &&
      isPublished(repoRoot, repoPath)) {
    return '/' + top.slice(1).join('/') + hash
  }
  if (!exists || !isPublished(repoRoot, repoPath)) return UNLINK
  return githubUrl(site, repoPath, isDir) + hash
}

/** Lines and blocks of the README that the site leaves out (see website/README.md). */
function siteOnlyReadme (body) {
  return body
    // the donation badge: a third-party image request on every view
    .replace(/^\[!\[Buy Me a Coffee\][^\n]*\n+/m, '')
    // the product GIFs still show the logo replaced on 2026-10-18; their captions go with them
    .replace(/<p align="center">\s*<img src="[^"]*\.gif"[\s\S]*?<\/p>\s*(<p align="center"><em>[\s\S]*?<\/em><\/p>\s*)?/g, '')
}

const INLINE = /(`+)([\s\S]*?)\1|(!?)\[((?:[^\][]|\[[^\]]*\])*)\]\(\s*(<[^>]+>|[^)\s]+)(\s+"[^"]*")?\s*\)/g

/** Rewrites every link of a Markdown body: inline links/images, reference definitions, HTML src/href. */
export function rewriteMarkdownLinks (body, fromRepoPath, opts) {
  if (fromRepoPath === 'README.md') body = siteOnlyReadme(body)
  const parts = body.split(/(^ {0,3}(?:```|~~~)[^\n]*\n[\s\S]*?^ {0,3}(?:```|~~~)[ \t]*$)/m)
  return parts.map((part, i) => {
    if (i % 2 === 1) return part // fenced code: untouched
    return part
      // inline links and images, outside code spans: [text](target "title")
      .replace(INLINE, function fix (m, ticks, _code, bang, label, target, title) {
        if (ticks) return m
        const inner = label.replace(INLINE, fix) // an image inside a link: [![alt](img.png)](url)
        const bare = target.startsWith('<') ? target.slice(1, -1) : target
        const fixed = rewriteTarget(bare, fromRepoPath, opts)
        if (fixed === UNLINK) return bang ? '' : inner
        return `${bang}[${inner}](${fixed}${title || ''})`
      })
      // reference definitions: [label]: target
      .replace(/^( {0,3}\[[^\]]+\]:\s*)(\S+)/gm, (m, lead, target) => {
        const fixed = rewriteTarget(target, fromRepoPath, opts)
        return fixed === UNLINK ? '' : lead + fixed
      })
      // raw HTML: an unpublished image goes, an unpublished link keeps its text
      .replace(/<img\b[^>]*\bsrc="([^"]+)"[^>]*>/g, (m, target) => {
        const fixed = rewriteTarget(target, fromRepoPath, opts)
        return fixed === UNLINK ? '' : m.replace(`src="${target}"`, `src="${fixed}"`)
      })
      .replace(/<a\b([^>]*)\bhref="([^"]+)"([^>]*)>([\s\S]*?)<\/a>/g, (m, pre, target, post, inner) => {
        const fixed = rewriteTarget(target, fromRepoPath, opts)
        return fixed === UNLINK ? inner : `<a${pre}href="${fixed}"${post}>${inner}</a>`
      })
  }).join('')
}

/**
 * Gives the headings of a parsed page (minimark body) the ids GitHub gives them, so the
 * anchors the docs were written with (`#run-it-on-your-engine--or-on-your-warehouse`) work.
 * Rewrites the table of contents to match.
 */
export function githubHeadingIds (content, Slugger) {
  const body = content?.body
  if (!body || body.type !== 'minimark' || !Array.isArray(body.value)) return
  const slugger = new Slugger()
  const renamed = new Map()
  const text = n => (typeof n === 'string' ? n : Array.isArray(n) ? n.slice(2).map(text).join('') : '')
  const walk = (n) => {
    if (!Array.isArray(n)) return
    const [tag, props] = n
    if (/^h[1-6]$/.test(tag) && props) {
      const id = slugger.slug(text(n))
      if (props.id && props.id !== id) renamed.set(props.id, id)
      props.id = id
    }
    n.slice(2).forEach(walk)
  }
  body.value.forEach(walk)
  const fixToc = links => (links || []).forEach((l) => { if (renamed.has(l.id)) l.id = renamed.get(l.id); fixToc(l.children) })
  fixToc(body.toc?.links)
}
