// Protegge tutte le rotte: senza sessione → /login. Libere: /login e /auth/callback (SSO).
// «Senza sessione» è quel che sa il client (useSessionPresent): il cookie è
// HttpOnly, e se c'è ma non vale più lo dirà il primo 401.
export default defineNuxtRouteMiddleware((to) => {
  const present = useSessionPresent()
  // il ritorno dall'IdP: il cookie l'ha appena scritto il gateway
  if (to.path === '/auth/callback') return
  if (to.path === '/login') {
    // già dentro? evita di rimanere sulla login
    if (present.value) return navigateTo('/')
    return
  }
  if (!present.value) {
    return navigateTo('/login')
  }
})
