// Protegge tutte le rotte: senza token → /login. La pagina /login è l'unica libera.
export default defineNuxtRouteMiddleware((to) => {
  const token = useAuthToken()
  if (to.path === '/login') {
    // già loggato? evita di rimanere sulla login
    if (token.value) return navigateTo('/')
    return
  }
  if (!token.value) {
    return navigateTo('/login')
  }
})
