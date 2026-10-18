import { resolve } from 'node:path'
import { defineCollection, defineContentConfig } from '@nuxt/content'
import { readdirSync, statSync } from 'node:fs'
import { PRIVATE_DOC_DIRS, PRIVATE_DOC_FILES } from './lib/doc-links.mjs'
import { isPublished } from './lib/repo-files.mjs'

// The documentation is read where it lives, in the repository's docs/ folder, at build time:
// nothing is copied. Left out: the internal folders (audits, specs), the production compose
// guide the owner keeps local, and any Markdown file that is not in git (nor shipped with the
// site), so a build on a working copy publishes the same pages as CI on a clean clone.
const repoRoot = resolve(__dirname, '..')
const docsDir = resolve(repoRoot, 'docs')
function unpublishedDocs (dir = docsDir, rel = ''): string[] {
  const out: string[] = []
  for (const name of readdirSync(dir)) {
    const p = resolve(dir, name)
    const r = rel ? `${rel}/${name}` : name
    if (statSync(p).isDirectory()) out.push(...unpublishedDocs(p, r))
    else if (name.endsWith('.md') && !isPublished(repoRoot, `docs/${r}`)) out.push(r)
  }
  return out
}
const EXCLUDED = [...PRIVATE_DOC_DIRS.map(d => `${d}/**`), ...PRIVATE_DOC_FILES, ...unpublishedDocs()]
export default defineContentConfig({
  collections: {
    docs: defineCollection({
      type: 'page',
      source: [
        {
          cwd: resolve(__dirname, '../docs'),
          include: '**/*.md',
          exclude: EXCLUDED,
          prefix: '/docs',
        },
        {
          // pages written for the site only (the getting-started path)
          cwd: resolve(__dirname, 'content'),
          include: '**/*.md',
          prefix: '/docs',
        },
      ],
    }),
    // The README, published as the overview page of the docs.
    readme: defineCollection({
      type: 'page',
      source: { cwd: resolve(__dirname, '..'), include: 'README.md', prefix: '/docs/overview' },
    }),
  },
})
