<script setup lang="ts">
// Atterraggio del login SSO. Il gateway rimanda qui col token nel FRAMMENTO
// dell'URL (#token=…): il frammento non viaggia verso nessun server, quindi il
// token non finisce nei log di accesso né nell'header Referer. Qui lo si sposta
// nel cookie di sessione (lo stesso del login locale) e si ripulisce subito la
// barra degli indirizzi.
import { ref, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { KeyRound } from 'lucide-vue-next'

const { adoptToken } = useAuth()
const { t } = useI18n()
const error = ref('')

onMounted(async () => {
  const params = new URLSearchParams(window.location.hash.replace(/^#/, ''))
  const token = params.get('token')
  const code = params.get('error')
  // via il frammento dalla cronologia prima di qualsiasi altra cosa
  history.replaceState(null, '', window.location.pathname)

  if (!token) {
    await navigateTo(`/login?sso_error=${encodeURIComponent(code || 'sso_failed')}`)
    return
  }
  try {
    const me = await adoptToken(token)
    if (!me) throw new Error('me')
    await navigateTo('/')
  } catch {
    error.value = t('login.ssoError', { code: 'session' })
    await navigateTo('/login?sso_error=session')
  }
})
</script>

<template>
  <div class="wrap">
    <div class="card">
      <KeyRound :size="18" />
      <p>{{ error || $t('login.ssoCompleting') }}</p>
    </div>
  </div>
</template>

<style scoped>
.wrap { display: flex; align-items: center; justify-content: center; height: 100vh; background: var(--bg); }
.card {
  display: flex; align-items: center; gap: 10px;
  padding: 18px 22px; border: 1px solid var(--border); border-radius: 10px;
  background: var(--panel); color: var(--muted); font-size: 14px;
}
</style>
