// Authentication state: the session sits in an HttpOnly cookie the gateway writes (the
// JavaScript neither reads nor touches it) + the current user.

export interface Me {
  id: number
  email: string
  full_name: string
  is_active: boolean
  is_superuser: boolean
  // READS the administration panels without being able to write in them: an administrator
  // is one by definition (see permissions.is_observer)
  is_observer?: boolean
  groups: string[]
  // the page signs out by itself after that many minutes without anyone touching it (0 = never)
  session_idle_minutes?: number
  // the installation allows personal API tokens (AUTH__PERSONAL_TOKENS)
  personal_tokens?: boolean
  // the longest expiry a token can be given (AUTH__API_TOKEN_MAX_DAYS)
  api_token_max_days?: number
  // no local password: the account signs in through the identity provider (SSO)
  sso_only?: boolean
  // the language last chosen in the interface: the emails of the product follow it
  locale?: string | null
}

// The session's cookie: the gateway writes it at sign-in, `HttpOnly`, so a script
// injected into the page does not take it away.
export const SESSION_COOKIE = 'tab_token'

// Every request that comes with the cookie alone and changes something carries this
// header: another site's form cannot add it, and the gateway refuses the request that
// lacks it (see gateway/app/services/sessions.py).
export const CSRF_HEADERS = { 'X-Tabularia-Csrf': '1' } as const

// «Is there a session?» The JavaScript does not see the cookie: the server that renders
// the first page sees it, and tells the client in the page's state. From there the client
// keeps it updated: true at sign-in, false at a 401 or at sign-out. It serves only to
// choose the page (sign-in or app): the gateway stays the one that decides.
export const useSessionPresent = () =>
  useState<boolean>('auth_present', () => (import.meta.server ? !!useCookie(SESSION_COOKIE).value : false))

// True only during a WANTED sign-out (from the menu, or for inactivity): the editor lets
// it through without asking about the changes. Any 401 (session expired, revoked) does
// not: there the dialog stays, and «stay» at least allows copying the nodes before
// signing in again.
export const useLeaving = () => useState<boolean>('auth_leaving', () => false)

// The gateway said 401: the session is no longer there. The user instead stays in memory
// until a wanted sign-out or a new sign-in: the editor uses it to know that whoever looks
// may edit, and without it it would become read-only — that is «nothing to save», and
// the changes would go away without asking. The inactivity count looks at the presence,
// and stops on the sign-in page.
export function dropSession() {
  useSessionPresent().value = false
}

// The seconds left before the sign-out for inactivity, in the last minute; null outside
// it (see plugins/idle-logout.client.ts and IdleWarning.vue)
export const useIdleWarning = () => useState<number | null>('idle_warning', () => null)

/** Tells the gateway the language of the interface when it is not the one it
 *  knows (PUT /users/me/locale). Never in the way: a failure is silent. */
export function syncLocale(me: Me | null, code?: string) {
  if (!me || !import.meta.client) return
  const current = code ?? document.cookie.match(/(?:^|;\s*)tabularia-locale=([a-z]{2})/)?.[1] ?? 'en'
  if (me.locale === current) return
  const base = useRuntimeConfig().public.apiBase as string
  $fetch(`${base}/users/me/locale`, { method: 'PUT', body: { locale: current }, credentials: 'include', headers: CSRF_HEADERS })
    .then(() => { me.locale = current })
    .catch(() => { /* the next load tries again */ })
}

// what the tab keeps for the whole session and depends on who is signed in (the Scripting
// tab, the registries of the Container nodes): dropped when the person changes
function forgetPerUserCaches() {
  resetScriptingEditor()
  resetRegistriesEditor()
}

export function useAuth() {
  const base = useRuntimeConfig().public.apiBase as string
  const present = useSessionPresent()
  const user = useState<Me | null>('auth_user', () => null)

  async function login(email: string, password: string) {
    // The gateway writes the cookie. To the scripts the token comes back in the body too;
    // with the anti-CSRF header it does not: the cookie is enough for the page, and what it
    // does not receive it cannot lose
    await $fetch(`${base}/auth/login`, {
      method: 'POST',
      body: { email, password },
      credentials: 'include',
      headers: CSRF_HEADERS,
    })
    useLeaving().value = false
    present.value = true
    await fetchMe()
  }

  // The try-it stack's first start: while no account exists, the sign-in page creates
  // the first administrator (gateway: services/first_run.py). Never an error: off or
  // unreachable simply means the ordinary sign-in.
  async function setupNeeded(): Promise<boolean> {
    try {
      return (await $fetch<{ needed: boolean }>(`${base}/auth/setup`)).needed
    } catch {
      return false
    }
  }

  async function setupAdmin(email: string, password: string, fullName: string) {
    await $fetch(`${base}/auth/setup`, {
      method: 'POST',
      body: { email, password, full_name: fullName },
      credentials: 'include',
      headers: CSRF_HEADERS,
    })
    useLeaving().value = false
    present.value = true
    await fetchMe()
  }

  // OIDC SSO (optional): the gateway says whether it is configured. Never an error when
  // off — the login page must work anyway.
  async function ssoConfig(): Promise<{ enabled: boolean; button_label: string }> {
    try {
      return await $fetch<{ enabled: boolean; button_label: string }>(`${base}/auth/sso/config`)
    } catch {
      return { enabled: false, button_label: '' }
    }
  }

  // starts the SSO flow: a real navigation (not a fetch), the gateway redirects to the IdP
  function ssoLogin() {
    window.location.href = `${base}/auth/sso/login`
  }

  // back from SSO: the gateway has written the cookie already, it remains to ask who I am
  async function completeSso(): Promise<Me | null> {
    present.value = true
    useLeaving().value = false
    return await fetchMe()
  }

  async function fetchMe(): Promise<Me | null> {
    if (!present.value) {
      user.value = null
      return null
    }
    try {
      const me = await $fetch<Me>(`${base}/auth/me`, { credentials: 'include' })
      if (user.value?.id !== me.id) forgetPerUserCaches()
      user.value = me
      // at sign-in (and on every load of the app) the gateway learns the language
      // of the interface: the emails it sends this user will speak it
      if (import.meta.client) syncLocale(user.value)
    } catch (e: any) {
      // Only a 401 says the session no longer holds (closed, expired, user disabled): then it
      // signs out, as useApiClient does. A request broken off by a page change, a network error
      // or a 5xx (a gateway restarting) are NOT a logout: the session stays and the next page
      // retries. Before, any error signed out, and whoever navigated quickly or during a
      // restart found themselves at the login (found on 2026-10-10).
      const code = e?.response?.status ?? e?.statusCode
      if (code === 401) {
        dropSession()
        if (import.meta.client) navigateTo('/login')
      }
    }
    return user.value
  }

  // A real sign-out: the gateway closes the session, and its token stops holding wherever
  // it was copied. `reason` reaches the sign-in page, which says it.
  async function logout(reason?: 'idle') {
    useLeaving().value = true
    try {
      await $fetch(`${base}/auth/logout`, { method: 'POST', credentials: 'include', headers: CSRF_HEADERS })
    } catch {
      // closed already, or the gateway does not answer: from the browser one signs out anyway
    }
    present.value = false
    user.value = null
    forgetPerUserCaches()
    await navigateTo(reason ? `/login?reason=${reason}` : '/login')
  }

  return { present, user, login, logout, fetchMe, ssoConfig, ssoLogin, completeSso, setupNeeded, setupAdmin }
}
