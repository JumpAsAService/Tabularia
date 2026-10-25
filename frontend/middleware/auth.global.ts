// Protects every route: without a session → /login. Free: /login and /auth/callback (SSO).
// «Without a session» is what the client knows (useSessionPresent): the cookie is
// HttpOnly, and if it is there but no longer holds the first 401 will say so.
export default defineNuxtRouteMiddleware((to) => {
  const present = useSessionPresent()
  // back from the IdP: the gateway has just written the cookie
  if (to.path === '/auth/callback') return
  if (to.path === '/login') {
    // in already? avoids staying on the login
    if (present.value) return navigateTo('/')
    return
  }
  if (!present.value) {
    return navigateTo('/login')
  }
})
