// Stato di autenticazione: token JWT in cookie (SSR-friendly) + utente corrente.

export interface Me {
  id: number
  email: string
  full_name: string
  is_active: boolean
  is_superuser: boolean
  // LEGGE i pannelli di amministrazione senza poterci scrivere: un
  // amministratore lo è per definizione (vedi permissions.is_observer)
  is_observer?: boolean
  groups: string[]
}

// Cookie condiviso: leggibile sia lato server (middleware) sia lato client.
export const useAuthToken = () =>
  useCookie<string | null>('tab_token', { sameSite: 'lax', maxAge: 60 * 60 * 24, path: '/' })

export function useAuth() {
  const base = useRuntimeConfig().public.apiBase as string
  const token = useAuthToken()
  const user = useState<Me | null>('auth_user', () => null)

  async function login(email: string, password: string) {
    const res = await $fetch<{ access_token: string }>(`${base}/auth/login`, {
      method: 'POST',
      body: { email, password },
    })
    token.value = res.access_token
    await fetchMe()
  }

  // SSO OIDC (opzionale): il gateway dice se è configurato. Mai un errore se
  // spento — la pagina di login deve funzionare comunque.
  async function ssoConfig(): Promise<{ enabled: boolean; button_label: string }> {
    try {
      return await $fetch<{ enabled: boolean; button_label: string }>(`${base}/auth/sso/config`)
    } catch {
      return { enabled: false, button_label: '' }
    }
  }

  // avvia il flusso SSO: navigazione vera (non fetch), il gateway redirige all'IdP
  function ssoLogin() {
    window.location.href = `${base}/auth/sso/login`
  }

  // token consegnato dal callback SSO: stessa sessione del login locale
  async function adoptToken(accessToken: string): Promise<Me | null> {
    token.value = accessToken
    return await fetchMe()
  }

  async function fetchMe(): Promise<Me | null> {
    if (!token.value) {
      user.value = null
      return null
    }
    try {
      user.value = await $fetch<Me>(`${base}/auth/me`, {
        headers: { Authorization: `Bearer ${token.value}` },
      })
    } catch (e: any) {
      // Solo un 401 dice che il token non vale più (scaduto, manomesso, utente
      // disattivato): allora si esce, come fa useApiClient. Una richiesta interrotta
      // da un cambio di pagina, un errore di rete o un 5xx (un gateway che si
      // riavvia) NON sono un logout: il token resta e la pagina dopo riprova.
      // Prima qualunque errore cancellava il token, e chi navigava in fretta o
      // durante un riavvio si ritrovava al login (trovato il 2026-10-10).
      const code = e?.response?.status ?? e?.statusCode
      if (code === 401) {
        token.value = null
        user.value = null
        if (import.meta.client) navigateTo('/login')
      }
    }
    return user.value
  }

  function logout() {
    token.value = null
    user.value = null
    navigateTo('/login')
  }

  return { token, user, login, logout, fetchMe, ssoConfig, ssoLogin, adoptToken }
}
