// Serves the generated site (.output/public) under its base path, as GitHub Pages will:
//   npm run preview            → http://localhost:4173/Tabularia/
// No dependencies. Unknown paths get 404.html with status 404, like Pages.
import { createServer } from 'node:http'
import { createReadStream, existsSync, statSync } from 'node:fs'
import { extname, join, normalize, resolve } from 'node:path'
import { site } from '../site.config.mjs'

const root = resolve(import.meta.dirname, '../.output/public')
const base = site.baseURL
const port = Number(process.env.PORT || 4173)
const types = {
  '.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.mjs': 'text/javascript', '.css': 'text/css',
  '.json': 'application/json', '.svg': 'image/svg+xml', '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
  '.gif': 'image/gif', '.webp': 'image/webp', '.ico': 'image/x-icon', '.woff2': 'font/woff2', '.mp4': 'video/mp4',
  '.txt': 'text/plain; charset=utf-8', '.xml': 'application/xml', '.wasm': 'application/wasm',
}

export function resolveFile (urlPath) {
  if (!urlPath.startsWith(base)) return null
  const rel = decodeURIComponent(urlPath.slice(base.length).split('?')[0].split('#')[0])
  const p = normalize(join(root, rel))
  if (!p.startsWith(root)) return null
  for (const c of [p, join(p, 'index.html'), p + '.html']) {
    if (existsSync(c) && statSync(c).isFile()) return c
  }
  return null
}

if (import.meta.url === `file://${process.argv[1]}`) {
  createServer((req, res) => {
    const url = req.url || '/'
    if (url === '/' && base !== '/') { res.writeHead(302, { location: base }); return res.end() }
    const file = resolveFile(url)
    if (!file) {
      res.writeHead(404, { 'content-type': types['.html'] })
      return createReadStream(join(root, '404.html')).pipe(res)
    }
    res.writeHead(200, { 'content-type': types[extname(file)] || 'application/octet-stream' })
    createReadStream(file).pipe(res)
  }).listen(port, () => console.log(`Serving ${root} at http://localhost:${port}${base}`))
}
