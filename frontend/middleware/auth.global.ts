// Protegge tutte le rotte: senza token → /login. Libere: /login e /auth/callback (SSO).
// rotte raggiungibili SENZA token: la login e il ritorno dall'IdP (che il token
// lo porta nel frammento dell'URL e lo salva proprio lì)
const PUBLIC_PATHS = ['/login', '/auth/callback']

export default defineNuxtRouteMiddleware((to) => {
  const token = useAuthToken()
  if (to.path === '/auth/callback') return
  if (to.path === '/login') {
    // già loggato? evita di rimanere sulla login
    if (token.value) return navigateTo('/')
    return
  }
  if (!token.value) {
    return navigateTo('/login')
  }
})
