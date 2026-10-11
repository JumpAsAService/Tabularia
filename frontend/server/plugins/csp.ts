// Content-Security-Policy dell'interfaccia (audit 2026-10-08, M7).
//
// Il token di sessione sta in un cookie che il JavaScript legge, perché va
// nell'intestazione Authorization delle chiamate al gateway: un XSS qualsiasi se
// lo porterebbe via. La politica chiude le due strade che servono a farlo:
// eseguire script che non sono nostri (solo i file di /_nuxt e gli script inline
// che Nuxt scrive, autorizzati uno per uno con il loro hash) e mandare dati
// altrove (fetch e immagini solo verso il sito stesso e il gateway).
//
// Gli hash si calcolano sugli script dell'<head> e della coda del <body>, quelli
// di Nuxt, MAI sull'HTML dell'app: un contenuto reso come HTML verrebbe
// autorizzato insieme a loro. Non in sviluppo: Vite inietta script e apre un
// websocket per il ricaricamento a caldo.
const SCRIPT = /<script\b([^>]*)>([\s\S]*?)<\/script>/gi

// codice da eseguire: senza `src` e senza un tipo di dati (application/json…)
function eseguibile(attributi: string): boolean {
  if (/\bsrc\s*=/i.test(attributi)) return false
  const tipo = /\btype\s*=\s*["']?([^"'\s>]+)/i.exec(attributi)?.[1]?.toLowerCase()
  return !tipo || tipo === 'module' || tipo.includes('javascript')
}

// Web Crypto, non node:crypto: c'è in Node e nei tipi che il progetto ha già
async function sha256(testo: string): Promise<string> {
  const byte = new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(testo)))
  return btoa(String.fromCharCode(...byte))
}

export async function hashDegliScript(html: string): Promise<string[]> {
  const visti = new Set<string>()
  for (const [, attributi, codice] of html.matchAll(SCRIPT)) {
    if (eseguibile(attributi ?? '')) visti.add(`'sha256-${await sha256(codice ?? '')}'`)
  }
  return [...visti]
}

// "/api" è il sito stesso; un URL assoluto (il gateway su un altro host) va
// ammesso per origine
function origine(url: unknown): string {
  if (typeof url !== 'string' || !/^https?:\/\//i.test(url)) return ''
  try {
    return new URL(url).origin
  } catch {
    return ''
  }
}

export async function politica(htmlDiNuxt: string, apiBase: unknown, grafanaUrl: unknown): Promise<string> {
  const api = origine(apiBase)
  const grafana = origine(grafanaUrl)
  return [
    "default-src 'self'",
    ["script-src 'self'", ...(await hashDegliScript(htmlDiNuxt))].join(' '),
    // Vue Flow e gli attributi style del rendering lato server
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob:",
    "font-src 'self' data:",
    ["connect-src 'self'", api].filter(Boolean).join(' '),
    // la tab Monitoring incorpora Grafana
    ["frame-src 'self'", grafana].filter(Boolean).join(' '),
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'self'",
  ].join('; ')
}

export default defineNitroPlugin((nitroApp) => {
  if (import.meta.dev) return
  nitroApp.hooks.hook('render:html', async (html, { event }) => {
    const { apiBase, grafanaUrl } = useRuntimeConfig(event).public
    const diNuxt = [...html.head, ...html.bodyAppend].join('\n')
    setResponseHeader(event, 'content-security-policy', await politica(diNuxt, apiBase, grafanaUrl))
  })
})
