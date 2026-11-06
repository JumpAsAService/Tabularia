// Build-time touches on the docs' parsed Markdown (Nuxt Content's minimark tree), for the
// site only: the Markdown in docs/ stays plain, as GitHub shows it.
//
// - pageTocAfterTitle: an <page-toc> right after the page's title, the "On this page" list
//   for narrow screens (components/content/PageToc.vue).

const isEl = (n, tag) => Array.isArray(n) && (tag ? n[0] === tag : typeof n[0] === 'string')

/** Puts <page-toc> after the first h1 of the page (nothing when there is no h1). */
export function pageTocAfterTitle (nodes) {
  const i = nodes.findIndex(n => isEl(n, 'h1'))
  if (i < 0 || nodes.some(n => isEl(n, 'page-toc'))) return nodes
  return [...nodes.slice(0, i + 1), ['page-toc', {}], ...nodes.slice(i + 1)]
}

/** Nuxt Content hook helper: every touch above, in place. */
export function enhanceProse (content) {
  const body = content?.body
  if (!body || body.type !== 'minimark' || !Array.isArray(body.value)) return
  body.value = pageTocAfterTitle(body.value)
}
