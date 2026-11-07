// Share links (docs/specs/share-links.md): a report or a dashboard opened from outside Tabularia
// with a link and its own password, or embedded in the pages of listed origins.
//
// - `useShareLinks` is the creators' side: the dialog of an object, the Administration tab.
// - `useShareClient` is the recipient's: the routes of /share/{token}, with no account. Its
//   requests never go through `apiFetch` (a 401 there means «enter the password», not «sign in»).
// - `shareReportsApi` / `shareDashboardsApi` are the viewers' APIs over a link: the report and
//   dashboard pages run unchanged on them (composables/viewerHost.ts).
// - The rest are pure helpers, tested in tests/share-links.test.mjs.
import { FIRST_PAUSE_MS, nextPause } from '~/composables/waits'
import { paramsQuery, type RenderOutcome, type ReportParams } from '~/composables/useReports'
import type { TileComment } from '~/composables/dashboardComments'

export type ShareKind = 'report' | 'dashboard'
export type ShareState = 'waiting' | 'active' | 'locked' | 'expired' | 'revoked'

export interface ShareLink {
  id: number
  hint: string
  kind: ShareKind
  object_id: number
  object_name?: string
  project_id?: number
  label: string
  created_by: { id: number; email: string; full_name: string } | null
  embeddable: boolean
  origins: string[]
  downloads: string[]
  params: Record<string, string[]>
  expires_at: string
  revoked_at: string | null
  created_at: string
  last_opened_at: string | null
  openings: number
  state: ShareState
  /** why a live link does not answer now: '' when it does */
  suspended: '' | 'creator_inactive' | 'creator_rights' | 'object_gone' | 'disabled'
}

export interface ShareInfo {
  enabled: boolean
  can_manage: boolean
  can_create: boolean
  max_days: number
  downloads: string[]
  /** the address the links are written with when the installation exposes them on a host of their own */
  public_base_url: string
  links: ShareLink[]
}

export interface ShareSettings {
  enabled: boolean
  max_days: number
  ai_comments_per_hour: number
  updated_at: string | null
  updated_by: string | null
}

export interface ShareCreate {
  kind: ShareKind
  object_id: number
  label: string
  expires_at: string
  embeddable: boolean
  origins: string[]
  confirmed: boolean
  password: string
  params: Record<string, string[]>
  downloads: string[]
}

export function useShareLinks() {
  const { apiFetch } = useApiClient()
  return {
    of: (kind: ShareKind, objectId: number) =>
      apiFetch<ShareInfo>(`/share-links?kind=${kind}&object_id=${objectId}`),
    create: (body: ShareCreate) =>
      apiFetch<{ link: ShareLink; token: string; path: string }>('/share-links', { method: 'POST', body }),
    newPassword: (id: number, password: string) =>
      apiFetch<void>(`/share-links/${id}/password`, { method: 'POST', body: { password } }),
    revoke: (id: number) => apiFetch<void>(`/share-links/${id}/revoke`, { method: 'POST' }),
    all: () => apiFetch<{ settings: ShareSettings; links: ShareLink[] }>('/admin/share-links'),
    saveSettings: (s: Pick<ShareSettings, 'enabled' | 'max_days' | 'ai_comments_per_hour'>) =>
      apiFetch<ShareSettings>('/admin/share-links/settings', { method: 'PUT', body: s }),
  }
}

// ── pure helpers ─────────────────────────────────────────────────────────────

/** The account rule (gatewaymodels.PasswordProblem): its code, or '' when it holds. */
export const PASSWORD_MIN = 12
export const PASSWORD_MAX_BYTES = 72
const KNOWN = new Set(['admin', 'administrator', 'change-me-in-production', 'change-me-2026', 'changeme', 'change-me', 'password',
  'tabularia', 'admin123', '12345678', '123456789012'])
export function passwordProblem(pw: string): '' | 'empty' | 'known' | 'short' | 'long' {
  if (!pw) return 'empty'
  if (KNOWN.has(pw.trim().toLowerCase())) return 'known'
  if ([...pw].length < PASSWORD_MIN) return 'short'
  if (new TextEncoder().encode(pw).length > PASSWORD_MAX_BYTES) return 'long'
  return ''
}

/** A temporary password: 16 characters from an alphabet without look-alikes (no 0/O, 1/l/I). */
const ALPHABET = 'abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789'
export function generatePassword(length = 16, random: (n: number) => Uint8Array = (n) => crypto.getRandomValues(new Uint8Array(n))): string {
  let out = ''
  while (out.length < length) {
    for (const b of random(length * 2)) {
      // rejection sampling: every character equally likely
      if (b < 256 - (256 % ALPHABET.length)) out += ALPHABET[b % ALPHABET.length]
      if (out.length === length) break
    }
  }
  return out
}

/** An origin as the gateway checks it (sharelinks.Origins), or null: scheme://host[:port]. */
export function originOf(text: string): string | null {
  const s = text.trim()
  if (!s || /[*'\s;,"\\]/.test(s)) return null
  let u: URL
  try { u = new URL(s) } catch { return null }
  if (!['http:', 'https:'].includes(u.protocol) || !u.hostname || u.username || u.password || u.search || u.hash) return null
  if (u.pathname !== '/' && u.pathname !== '') return null
  if (/\/[^/]/.test(s.replace(/^[a-z]+:\/\//i, ''))) return null // a path that URL normalised away
  return u.origin.toLowerCase()
}

/** The origins of a textarea, one per line: the good ones (without duplicates), the bad as typed. */
export function originsOf(text: string): { origins: string[]; bad: string[] } {
  const origins: string[] = []
  const bad: string[] = []
  for (const line of text.split(/\r?\n/)) {
    if (!line.trim()) continue
    const o = originOf(line)
    if (!o) bad.push(line.trim())
    else if (!origins.includes(o)) origins.push(o)
  }
  return { origins, bad }
}

/** The HTML that embeds a link in a host page. */
export function iframeSnippet(url: string, title: string): string {
  const esc = (s: string) => s.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;')
  return `<iframe src="${esc(url)}" title="${esc(title)}" width="100%" height="720" style="border:0" referrerpolicy="no-referrer"></iframe>`
}

/** YYYY-MM-DD of a day `days` from `from`, in the local calendar. */
export function dayAfter(days: number, from = new Date()): string {
  const d = new Date(from.getFullYear(), from.getMonth(), from.getDate() + days)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
}

/** The end of a local day, as the instant the gateway takes (RFC 3339, UTC). */
export function endOfDay(day: string): string {
  const [y, m, d] = day.split('-').map(Number)
  return new Date(y!, (m ?? 1) - 1, d ?? 1, 23, 59, 59).toISOString().replace(/\.\d{3}Z$/, 'Z')
}

/** The values a link opens with, as the gateway keeps them: every value a text. */
export function paramsForLink(params: ReportParams): Record<string, string[]> {
  const out: Record<string, string[]> = {}
  for (const [k, v] of Object.entries(params)) {
    if (v === null || v === undefined || v === '') continue
    out[k] = (Array.isArray(v) ? v : [v]).map((x) => String(x))
  }
  return out
}

export function shareUrl(token: string, base?: string): string {
  const root = (base || (typeof window !== 'undefined' ? window.location.origin : '')).replace(/\/+$/, '')
  return `${root}/share/${token}`
}

/** A comment of a document, as the engine draws it ({text, model, numbers_at} or {message}), as a tile shows it. */
export function tileCommentOf(said: any, stale = false): TileComment {
  const empty = { written_at: null, stale, can_ask: false, reason: '', model: '', numbers_at: null, text: '', message: '' }
  if (said && typeof said.text === 'string' && said.text) {
    return { ...empty, state: 'ready', text: said.text, model: String(said.model ?? ''), numbers_at: said.numbers_at ?? null }
  }
  return { ...empty, state: 'failed', message: typeof said?.message === 'string' ? said.message : '' }
}

// ── the recipient's side ─────────────────────────────────────────────────────

export interface ShareAbout {
  kind: ShareKind
  name: string
  description: string
  label: string
  embeddable: boolean
  downloads: string[]
  params: Record<string, string[]>
  has_comment: boolean
  expires_at: string
  content: any
}

export interface ShareComments {
  comments: Record<string, any>
  stale: boolean
  available: boolean
  given?: Record<string, string[]>
  at?: string
}

/** The routes of one link. `session` holds an embeddable link's session (it has no cookie). */
export function useShareClient(token: string) {
  const apiBase = useRuntimeConfig().public.apiBase as string
  const root = `${apiBase}/share/${encodeURIComponent(token)}`
  let session = ''
  const headers = (): Record<string, string> => ({
    'X-Tabularia-Csrf': '1',
    ...(session ? { 'X-Tabularia-Share-Session': session } : {}),
  })
  function call<T>(path: string, opts: Record<string, any> = {}): Promise<T> {
    return $fetch<T>(`${root}${path}`, { ...opts, headers: { ...headers(), ...(opts.headers ?? {}) }, credentials: 'include' })
  }
  async function file(path: string): Promise<Blob> {
    const res = await fetch(`${root}${path}`, { headers: headers(), credentials: 'include' })
    if (!res.ok) throw Object.assign(new Error(`HTTP ${res.status}`), { statusCode: res.status })
    return res.blob()
  }

  async function followRender(taskId: string, opts: { signal?: AbortSignal } = {}): Promise<RenderOutcome> {
    const since = Date.now()
    let delay = FIRST_PAUSE_MS
    for (;;) {
      await new Promise<void>((resolve, reject) => {
        const t = setTimeout(resolve, delay)
        opts.signal?.addEventListener('abort', () => { clearTimeout(t); reject(Object.assign(new Error('aborted'), { name: 'AbortError' })) }, { once: true })
      })
      delay = nextPause(delay, since, 2000)
      const state = await call<any>(`/exports/${encodeURIComponent(taskId)}`, { signal: opts.signal })
      if (state.status === 'failed') {
        throw Object.assign(new Error(state.detail ?? 'The render failed'), { statusCode: state.code, data: { detail: state.detail } })
      }
      if (state.status !== 'ready') continue
      const filePath = `/exports/${encodeURIComponent(taskId)}/file`
      return {
        status: 'ready', task_id: taskId, filename: state.filename ?? '', size: state.size ?? null, pages: state.pages ?? null,
        warnings: state.warnings ?? [],
        fileText: async () => (await file(filePath)).text(),
        download: (name?: string) => {
          file(filePath).then((blob) => {
            const a = document.createElement('a')
            a.href = URL.createObjectURL(blob)
            a.download = name ?? state.filename ?? ''
            a.click()
            setTimeout(() => URL.revokeObjectURL(a.href), 60_000)
          }).catch(() => { /* the viewer said it was ready: a second click asks again */ })
        },
      }
    }
  }

  return {
    setSession: (s: string) => { session = s },
    frame: () => call<{ available: boolean; embeddable: boolean }>('/frame'),
    open: (password?: string) =>
      call<{ change_password?: boolean; rules?: { min_length: number; max_bytes: number }; session?: string }>('/session', {
        method: 'POST', body: password === undefined ? {} : { password },
      }),
    choose: (password: string) => call<void>('/password', { method: 'POST', body: { password } }),
    about: () => call<ShareAbout>(''),
    parameters: (given: ReportParams = {}) => {
      const q = paramsQuery(given)
      return call<any[]>(`/parameters${q ? `?${q}` : ''}`)
    },
    data: (body: unknown, signal?: AbortSignal) => call<any>('/data', { method: 'POST', body, signal }),
    render: (body: unknown, signal?: AbortSignal) => call<{ task_id: string; filename: string }>('/render', { method: 'POST', body, signal }),
    comments: (params: ReportParams, signal?: AbortSignal) => call<ShareComments>('/comment', { method: 'POST', body: { params }, signal }),
    followRender,
  }
}
export type ShareClient = ReturnType<typeof useShareClient>
