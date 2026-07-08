// Wrapper di $fetch che allega il Bearer token e gestisce il 401 (→ /login).
// Usato da tutte le chiamate autenticate (dati + control plane).

export function useApiClient() {
  const base = useRuntimeConfig().public.apiBase as string
  const token = useAuthToken()

  async function apiFetch<T>(path: string, opts: Record<string, any> = {}): Promise<T> {
    const headers: Record<string, string> = { ...(opts.headers || {}) }
    if (token.value) headers.Authorization = `Bearer ${token.value}`
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
