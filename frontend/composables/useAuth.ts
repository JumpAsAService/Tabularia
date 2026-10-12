// Stato di autenticazione: la sessione sta in un cookie HttpOnly che scrive il
// gateway (il JavaScript non lo legge e non lo tocca) + utente corrente.

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
  // la pagina esce da sé dopo tanti minuti senza che nessuno la tocchi (0 = mai)
  session_idle_minutes?: number
  // l'installazione permette i token API personali (AUTH__PERSONAL_TOKENS)
  personal_tokens?: boolean
  // la scadenza più lunga che si può dare a un token (AUTH__API_TOKEN_MAX_DAYS)
  api_token_max_days?: number
}

// Il cookie della sessione: lo scrive il gateway all'accesso, `HttpOnly`, quindi
// uno script iniettato nella pagina non se lo porta via.
export const SESSION_COOKIE = 'tab_token'

// Ogni richiesta che si presenta col solo cookie e modifica qualcosa porta
// questa intestazione: un modulo di un altro sito non la può aggiungere, e il
// gateway rifiuta la richiesta che non ce l'ha (vedi gateway/app/services/sessions.py).
export const CSRF_HEADERS = { 'X-Tabularia-Csrf': '1' } as const

// «C'è una sessione?» Il JavaScript non vede il cookie: lo vede il server che
// rende la prima pagina, e lo dice al client nello stato della pagina. Da lì lo
// tiene aggiornato il client: vero all'accesso, falso a un 401 o all'uscita.
// Serve solo a scegliere la pagina (accesso o app): chi decide resta il gateway.
export const useSessionPresent = () =>
  useState<boolean>('auth_present', () => (import.meta.server ? !!useCookie(SESSION_COOKIE).value : false))

// Vero solo durante un'uscita VOLUTA (dal menu, o per inattività): l'editor la
// lascia passare senza chiedere delle modifiche. Un 401 qualunque (sessione
// scaduta, revocata) no: lì il dialogo resta, e «resta» permette almeno di
// copiare i nodi prima di rientrare.
export const useLeaving = () => useState<boolean>('auth_leaving', () => false)

// Il gateway ha detto 401: la sessione non c'è più. L'utente invece resta in
// memoria fino a un'uscita voluta o a un accesso nuovo: l'editor lo usa per sapere
// che chi guarda può modificare, e senza diventerebbe di sola lettura — cioè «niente
// da salvare», e le modifiche se ne andrebbero senza chiedere. Il conto
// dell'inattività guarda la presenza, e sulla pagina di accesso si ferma.
export function dropSession() {
  useSessionPresent().value = false
}

// I secondi che restano prima dell'uscita per inattività, nell'ultimo minuto;
// null fuori da lì (vedi plugins/idle-logout.client.ts e IdleWarning.vue)
export const useIdleWarning = () => useState<number | null>('idle_warning', () => null)

export function useAuth() {
  const base = useRuntimeConfig().public.apiBase as string
  const present = useSessionPresent()
  const user = useState<Me | null>('auth_user', () => null)

  async function login(email: string, password: string) {
    // Il cookie lo scrive il gateway. Agli script il token torna anche nel corpo;
    // con l'intestazione anti-CSRF no: alla pagina basta il cookie, e quello che
    // non riceve non lo può perdere
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

  // ritorno dall'SSO: il gateway ha già scritto il cookie, resta da chiedere chi sono
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
      user.value = await $fetch<Me>(`${base}/auth/me`, { credentials: 'include' })
    } catch (e: any) {
      // Solo un 401 dice che la sessione non vale più (chiusa, scaduta, utente
      // disattivato): allora si esce, come fa useApiClient. Una richiesta interrotta
      // da un cambio di pagina, un errore di rete o un 5xx (un gateway che si
      // riavvia) NON sono un logout: la sessione resta e la pagina dopo riprova.
      // Prima qualunque errore faceva uscire, e chi navigava in fretta o
      // durante un riavvio si ritrovava al login (trovato il 2026-10-10).
      const code = e?.response?.status ?? e?.statusCode
      if (code === 401) {
        dropSession()
        if (import.meta.client) navigateTo('/login')
      }
    }
    return user.value
  }

  // Uscita vera: il gateway chiude la sessione, e il suo token smette di valere
  // ovunque sia stato copiato. `reason` arriva alla pagina di accesso, che lo dice.
  async function logout(reason?: 'idle') {
    useLeaving().value = true
    try {
      await $fetch(`${base}/auth/logout`, { method: 'POST', credentials: 'include', headers: CSRF_HEADERS })
    } catch {
      // già chiusa, o il gateway non risponde: dal browser si esce comunque
    }
    present.value = false
    user.value = null
    await navigateTo(reason ? `/login?reason=${reason}` : '/login')
  }

  return { present, user, login, logout, fetchMe, ssoConfig, ssoLogin, completeSso }
}
