// robots.txt with the site's own base path. On GitHub Pages under /Tabularia/ crawlers read
// the host's root robots.txt, not this one; it starts to matter on a custom domain.
export default defineEventHandler((event) => {
  const site = String(useRuntimeConfig().public.siteUrl).replace(/\/$/, '')
  setHeader(event, 'content-type', 'text/plain; charset=utf-8')
  return `User-agent: *\nAllow: /\nSitemap: ${site}/sitemap.xml\n`
})
