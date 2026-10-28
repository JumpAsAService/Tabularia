// The interface's Content-Security-Policy (audit 2026-10-08, M7).
//
// The session token sits in an HttpOnly cookie JavaScript does not read, but an injected
// script could still use the session while the page is open. The policy closes the two
// roads needed to do it: running scripts that are not ours (only the /_nuxt files and the
// inline scripts Nuxt writes, authorized one by one with their hash) and sending data
// elsewhere (fetch and images only towards the site itself and the gateway).
//
// The hashes are computed on the scripts of the <head> and the tail of the <body>, Nuxt's,
// NEVER on the app's HTML: content rendered as HTML would be authorized together with
// them. Not in development: Vite injects scripts and opens a websocket for hot reload.
const SCRIPT = /<script\b([^>]*)>([\s\S]*?)<\/script>/gi

// code to run: without `src` and without a data type (application/json…)
function runnable(attributes: string): boolean {
  if (/\bsrc\s*=/i.test(attributes)) return false
  const kind = /\btype\s*=\s*["']?([^"'\s>]+)/i.exec(attributes)?.[1]?.toLowerCase()
  return !kind || kind === 'module' || kind.includes('javascript')
}

// Web Crypto, not node:crypto: it is in Node and in the types the project has already
async function sha256(text: string): Promise<string> {
  const byte = new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text)))
  return btoa(String.fromCharCode(...byte))
}

export async function scriptHashes(html: string): Promise<string[]> {
  const seen = new Set<string>()
  for (const [, attributes, code] of html.matchAll(SCRIPT)) {
    if (runnable(attributes ?? '')) seen.add(`'sha256-${await sha256(code ?? '')}'`)
  }
  return [...seen]
}

// "/api" is the site itself; an absolute URL (the gateway on another host) is allowed by
// origin
function origin(url: unknown): string {
  if (typeof url !== 'string' || !/^https?:\/\//i.test(url)) return ''
  try {
    return new URL(url).origin
  } catch {
    return ''
  }
}

export async function policy(nuxtHtml: string, apiBase: unknown, grafanaUrl: unknown,
  notebooksOrigin?: unknown): Promise<string> {
  const api = origin(apiBase)
  const grafana = origin(grafanaUrl)
  const notebooks = origin(notebooksOrigin)
  return [
    "default-src 'self'",
    ["script-src 'self'", ...(await scriptHashes(nuxtHtml))].join(' '),
    // Vue Flow and the style attributes of the server-side render
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob:",
    "font-src 'self' data:",
    ["connect-src 'self'", api].filter(Boolean).join(' '),
    // the Monitoring tab embeds Grafana; the notebooks' app and editor are marimo behind
    // the gateway, on the notebooks' own address (NUXT_PUBLIC_NOTEBOOKS_ORIGIN; the API's
    // only where an installation serves them on the interface's, NOTEBOOKS__ALLOW_SAME_ORIGIN)
    ["frame-src 'self'", grafana, api, notebooks].filter(Boolean).join(' '),
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'self'",
  ].join('; ')
}

export default defineNitroPlugin((nitroApp) => {
  if (import.meta.dev) return
  nitroApp.hooks.hook('render:html', async (html, { event }) => {
    const { apiBase, grafanaUrl, notebooksOrigin } = useRuntimeConfig(event).public
    const nuxtScripts = [...html.head, ...html.bodyAppend].join('\n')
    setResponseHeader(event, 'content-security-policy', await policy(nuxtScripts, apiBase, grafanaUrl, notebooksOrigin))
  })
})
