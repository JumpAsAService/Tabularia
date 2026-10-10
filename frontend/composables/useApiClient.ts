// Wrapper di $fetch che allega il Bearer token e gestisce il 401 (→ /login).
// Usato da tutte le chiamate autenticate (dati + control plane).
import { LOCALE_COOKIE } from '~/plugins/i18n'

export function useApiClient() {
  const base = useRuntimeConfig().public.apiBase as string
  const token = useAuthToken()
  const lingua = useCookie<string | null>(LOCALE_COOKIE)

  /** La lingua dell'interfaccia (il cookie che la tiene): il gateway scrive in quella
   *  i messaggi per una persona, come i motivi di un export dbt rifiutato. Letta a
   *  ogni chiamata: un cambio di lingua vale dalla richiesta dopo. */
  function linguaCorrente(): string | null {
    if (import.meta.client) {
      const m = document.cookie.match(/(?:^|;\s*)tabularia-locale=([a-z]{2})/)
      if (m) return m[1]
    }
    return lingua.value ?? null
  }

  async function apiFetch<T>(path: string, opts: Record<string, any> = {}): Promise<T> {
    const headers: Record<string, string> = { ...(opts.headers || {}) }
    if (token.value) headers.Authorization = `Bearer ${token.value}`
    const l = linguaCorrente()
    if (l && !headers['Accept-Language']) headers['Accept-Language'] = l
    try {
      return await $fetch<T>(`${base}${path}`, { ...opts, headers })
    } catch (e: any) {
      const code = e?.response?.status ?? e?.statusCode
      if (code === 401) {
        token.value = null
        if (import.meta.client) navigateTo('/login')
      }
      throw e
    }
  }

  return { apiFetch }
}
