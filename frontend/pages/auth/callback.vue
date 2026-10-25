<script setup lang="ts">
// The SSO login's landing. The gateway sends back here AFTER writing the session's cookie
// (HttpOnly, the same as the local login's): the token goes neither through the URL nor
// through JavaScript, here only «who am I?» is asked. An error arrives instead in the
// URL's FRAGMENT (#error=<code>), which is cleaned at once.
import { ref, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { KeyRound } from 'lucide-vue-next'

const { completeSso } = useAuth()
const { t } = useI18n()
const error = ref('')

onMounted(async () => {
  const code = new URLSearchParams(window.location.hash.replace(/^#/, '')).get('error')
  // away with the fragment from the history before anything else
  history.replaceState(null, '', window.location.pathname)

  if (code) {
    await navigateTo(`/login?sso_error=${encodeURIComponent(code)}`)
    return
  }
  try {
    const me = await completeSso()
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
