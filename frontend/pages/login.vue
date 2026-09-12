<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useI18n } from 'vue-i18n'
import { LogIn, KeyRound } from 'lucide-vue-next'
import { errMessage } from '~/composables/useApi'

const { login, ssoConfig, ssoLogin } = useAuth()
const { t } = useI18n()

const email = ref('')
const password = ref('')
const busy = ref(false)
const error = ref('')

// SSO OIDC: il pulsante compare solo se il gateway è configurato con un IdP
const sso = ref<{ enabled: boolean; button_label: string }>({ enabled: false, button_label: '' })
onMounted(async () => {
  sso.value = await ssoConfig()
  // il callback SSO rimanda qui con ?sso_error=<codice> se qualcosa è andato storto
  const code = new URLSearchParams(window.location.search).get('sso_error')
  if (code) error.value = t('login.ssoError', { code })
})

async function onSubmit() {
  busy.value = true
  error.value = ''
  try {
    await login(email.value, password.value)
    await navigateTo('/')
  } catch (e) {
    error.value = errMessage(e)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="login-wrap">
    <form class="login-card" @submit.prevent="onSubmit">
      <div class="brand-mark"><img src="/logo.png" alt="Tabularia" /></div>
      <h1 class="brand">Tabularia</h1>
      <p class="muted sub">{{ $t('login.subtitle') }}</p>

      <label>{{ $t('login.email') }}</label>
      <input v-model="email" type="email" autocomplete="username" required />

      <label>{{ $t('login.password') }}</label>
      <input v-model="password" type="password" autocomplete="current-password" required />

      <p v-if="error" class="err">{{ error }}</p>

      <button class="primary" type="submit" :disabled="busy">
        <LogIn :size="15" /> {{ busy ? $t('login.signingIn') : $t('login.signIn') }}
      </button>

      <!-- SSO opzionale: senza IdP configurato questo blocco non esiste -->
      <template v-if="sso.enabled">
        <div class="sep"><span>{{ $t('login.or') }}</span></div>
        <button class="sso" type="button" @click="ssoLogin()">
          <KeyRound :size="15" /> {{ sso.button_label || $t('login.signInWithSso') }}
        </button>
      </template>
    </form>
  </div>
</template>

<style scoped>
.sep { display: flex; align-items: center; gap: 10px; margin: 14px 0 10px; color: var(--muted); font-size: 12px; }
.sep::before, .sep::after { content: ""; flex: 1; height: 1px; background: var(--border); }
.sso { display: inline-flex; align-items: center; justify-content: center; gap: 7px; width: 100%; padding: 9px 12px; }
.login-wrap {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100vh;
  background:
    radial-gradient(900px 500px at 20% 10%, rgba(79, 140, 255, 0.08), transparent 60%),
    radial-gradient(700px 500px at 85% 90%, rgba(110, 231, 183, 0.05), transparent 60%),
    var(--bg);
}
.login-card {
  width: 340px;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 14px;
  padding: 32px 28px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  box-shadow: var(--shadow-2);
}
.brand-mark {
  width: 48px;
  height: 48px;
  border-radius: 12px;
  background: #fff;
  display: flex;
  align-items: center;
  justify-content: center;
  margin-bottom: 6px;
  overflow: hidden;
  box-shadow: 0 4px 16px rgba(20, 30, 80, 0.35);
}
.brand-mark img { width: 100%; height: 100%; object-fit: contain; }
.brand {
  margin: 0;
  font-size: 26px;
  letter-spacing: 0.5px;
}
.sub {
  margin: 0 0 12px;
}
.login-card label {
  font-size: 12px;
  color: var(--muted);
  margin-top: 8px;
}
.login-card button {
  margin-top: 18px;
}
.err {
  color: var(--danger);
  font-size: 13px;
  margin: 8px 0 0;
}
</style>
