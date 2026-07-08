// Stato di autenticazione: token JWT in cookie (SSR-friendly) + utente corrente.

export interface Me {
  id: number
  email: string
  full_name: string
  is_active: boolean
  is_superuser: boolean
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

  async function fetchMe(): Promise<Me | null> {
    if (!token.value) {
      user.value = null
      return null
    }
    try {
      user.value = await $fetch<Me>(`${base}/auth/me`, {
        headers: { Authorization: `Bearer ${token.value}` },
      })
    } catch {
      token.value = null
      user.value = null
    }
    return user.value
  }

  function logout() {
    token.value = null
    user.value = null
    navigateTo('/login')
  }

  return { token, user, login, logout, fetchMe }
}
