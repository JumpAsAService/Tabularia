<script setup lang="ts">
import { ref } from 'vue'
import { Table2, LogIn } from 'lucide-vue-next'
import { errMessage } from '~/composables/useApi'

const { login } = useAuth()

const email = ref('')
const password = ref('')
const busy = ref(false)
const error = ref('')

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
      <div class="brand-mark"><Table2 :size="26" /></div>
      <h1 class="brand">Tabularia</h1>
      <p class="muted sub">Accedi per continuare</p>

      <label>Email</label>
      <input v-model="email" type="email" autocomplete="username" required />

      <label>Password</label>
      <input v-model="password" type="password" autocomplete="current-password" required />

      <p v-if="error" class="err">{{ error }}</p>

      <button class="primary" type="submit" :disabled="busy">
        <LogIn :size="15" /> {{ busy ? 'Accesso…' : 'Accedi' }}
      </button>
    </form>
  </div>
</template>

<style scoped>
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
  background: var(--grad-accent);
  color: #fff;
  display: flex;
  align-items: center;
  justify-content: center;
  margin-bottom: 6px;
  box-shadow: 0 4px 16px rgba(79, 140, 255, 0.35);
}
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
