// sitemap.xml, built at generate time: the home page, the docs index and every doc page.
export default defineEventHandler(async (event) => {
  const base = String(useRuntimeConfig().public.siteUrl).replace(/\/$/, '')
  const docs = await queryCollection(event, 'docs').select('path').all()
  const paths = ['/', '/docs', '/docs/overview', ...docs.map(d => d.path)]
  const urls = [...new Set(paths)].sort().map(p => `  <url><loc>${base}${p === '/' ? '/' : p}</loc></url>`).join('\n')
  setHeader(event, 'content-type', 'application/xml; charset=utf-8')
  return `<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n${urls}\n</urlset>\n`
})
