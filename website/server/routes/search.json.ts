// The docs search index: one entry per section of every page, built at generate time
// and served as a static file (/search.json). The client loads it on first use.
export default defineEventHandler(async (event) => {
  const [docs, readme] = await Promise.all([
    queryCollectionSearchSections(event, 'docs', { ignoredTags: ['pre'] }),
    queryCollectionSearchSections(event, 'readme', { ignoredTags: ['pre'] }),
  ])
  const fixReadme = (s: any) => ({ ...s, id: s.id.replace(/^\/docs\/overview\/readme/, '/docs/overview') })
  return [...docs, ...readme.map(fixReadme)].map((s: any) => ({
    id: s.id,
    title: s.title,
    titles: s.titles,
    level: s.level,
    content: String(s.content || '').slice(0, 1200),
  }))
})
