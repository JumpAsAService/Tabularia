// A $fetch wrapper that sends the session's cookie (with the anti-CSRF header) and
// handles the 401 (→ /login). Used by every authenticated call (data + control plane).
import { LOCALE_COOKIE } from '~/plugins/i18n'

export function useApiClient() {
  const base = useRuntimeConfig().public.apiBase as string
  const language = useCookie<string | null>(LOCALE_COOKIE)

  /**
   * The interface's language (the cookie that keeps it): the gateway writes in it the
   * messages for a person, like the reasons of a refused dbt export. Read at every call: a
   * language change holds from the next request.
   */
  function currentLanguage(): string | null {
    if (import.meta.client) {
      const m = document.cookie.match(/(?:^|;\s*)tabularia-locale=([a-z]{2})/)
      if (m) return m[1]
    }
    return language.value ?? null
  }

  async function apiFetch<T>(path: string, opts: Record<string, any> = {}): Promise<T> {
    const headers: Record<string, string> = { ...CSRF_HEADERS, ...(opts.headers || {}) }
    const l = currentLanguage()
    if (l && !headers['Accept-Language']) headers['Accept-Language'] = l
    try {
      return await $fetch<T>(`${base}${path}`, { ...opts, headers, credentials: 'include' })
    } catch (e: any) {
      const code = e?.response?.status ?? e?.statusCode
      if (code === 401) {
        dropSession()
        if (import.meta.client) navigateTo('/login')
      }
      throw e
    }
  }

  return { apiFetch }
}
