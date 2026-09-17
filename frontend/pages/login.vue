<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount, nextTick } from 'vue'
import { useI18n } from 'vue-i18n'
import { LogIn, KeyRound, Eye, EyeOff, CircleAlert, ArrowUp } from 'lucide-vue-next'
import { errMessage } from '~/composables/useApi'
import { useLocale } from '~/composables/useLocale'
// import espliciti: nel container i componenti nuovi non vengono scansionati
import OrderingField from '~/components/ui/OrderingField.vue'
import Select from '~/components/ui/Select.vue'
import BrandMark from '~/components/ui/BrandMark.vue'

const { login, ssoConfig, ssoLogin } = useAuth()
const { t } = useI18n()
const { locale, setLocale, locales } = useLocale()

const email = ref('')
const password = ref('')
const busy = ref(false)
const error = ref('')
const showPassword = ref(false)
const capsLock = ref(false)

// La zona quieta del campo segue la card VERA: cambia altezza con l'SSO, con un
// errore, con la lingua. Una misura fissa lasciava un buco nero attorno al form
// e nascondeva proprio la fascia dove i valori si stanno sistemando.
const card = ref<HTMLElement | null>(null)
const emailEl = ref<HTMLInputElement | null>(null)
const passwordEl = ref<HTMLInputElement | null>(null)
const quiet = ref({ w: 440, h: 520 })
let cardRo: ResizeObserver | null = null
onMounted(() => {
  if (!card.value) return
  const measure = () => {
    const r = card.value!.getBoundingClientRect()
    quiet.value = { w: Math.round(r.width + 64), h: Math.round(r.height + 64) }
  }
  cardRo = new ResizeObserver(measure)
  cardRo.observe(card.value)
  measure()
  // `autofocus` vale solo al caricamento della pagina: dopo un logout si arriva
  // qui per navigazione interna e il campo resterebbe senza fuoco
  emailEl.value?.focus()
})
onBeforeUnmount(() => cardRo?.disconnect())

// SSO OIDC: il pulsante compare solo se il gateway è configurato con un IdP
const sso = ref<{ enabled: boolean; button_label: string }>({ enabled: false, button_label: '' })
onMounted(async () => {
  sso.value = await ssoConfig()
  // il callback SSO rimanda qui con ?sso_error=<codice> se qualcosa è andato storto
  const code = new URLSearchParams(window.location.search).get('sso_error')
  if (code) error.value = t('login.ssoError', { code })
})

// una password sbagliata per il Blocco Maiuscole è l'errore più frequente e il
// meno spiegato: lo si dice PRIMA dell'invio
function onPasswordKey(e: KeyboardEvent) {
  if (typeof e.getModifierState === 'function') capsLock.value = e.getModifierState('CapsLock')
}

async function onSubmit() {
  busy.value = true
  error.value = ''
  try {
    await login(email.value, password.value)
    await navigateTo('/')
  } catch (e) {
    error.value = errMessage(e)
    // quasi sempre è la password: il fuoco torna lì, pronta da riscrivere
    await nextTick()
    passwordEl.value?.select()
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <main class="login">
    <OrderingField :quiet-width="quiet.w" :quiet-height="quiet.h" />
    <!-- un velo del colore di fondo dietro la card: i valori si spengono prima di
         arrivarle sotto, e attorno ai campi non si muove nulla -->
    <div class="veil" aria-hidden="true" :style="{ '--qw': quiet.w + 'px', '--qh': quiet.h + 'px' }" />

    <div class="lang">
      <Select
        :model-value="locale"
        :options="locales.map((l) => ({ value: l.code, label: l.label }))"
        :aria-label="$t('login.language')"
        @update:model-value="setLocale($event as any)"
      />
    </div>

    <form ref="card" class="card" :aria-busy="busy" @submit.prevent="onSubmit">
      <header class="id">
        <BrandMark :size="40" />
        <h1>Tabularia</h1>
      </header>
      <p class="tagline">{{ $t('login.tagline') }}</p>

      <div class="field">
        <label for="login-email">{{ $t('login.email') }}</label>
        <input id="login-email" ref="emailEl" v-model="email" type="email" autocomplete="username" inputmode="email" required autofocus />
      </div>

      <div class="field">
        <label for="login-password">{{ $t('login.password') }}</label>
        <div class="with-peek">
          <input
            id="login-password"
            ref="passwordEl"
            v-model="password"
            :type="showPassword ? 'text' : 'password'"
            autocomplete="current-password"
            required
            :aria-describedby="capsLock ? 'login-caps' : undefined"
            @keydown="onPasswordKey"
            @keyup="onPasswordKey"
            @blur="capsLock = false"
          />
          <button
            class="peek"
            type="button"
            :aria-label="showPassword ? $t('login.hidePassword') : $t('login.showPassword')"
            :title="showPassword ? $t('login.hidePassword') : $t('login.showPassword')"
            @click="showPassword = !showPassword"
          >
            <EyeOff v-if="showPassword" :size="16" /><Eye v-else :size="16" />
          </button>
        </div>
        <p v-if="capsLock" id="login-caps" class="hint"><ArrowUp :size="13" /> {{ $t('login.capsLock') }}</p>
      </div>

      <p v-if="error" class="err" role="alert"><CircleAlert :size="15" /> <span>{{ error }}</span></p>

      <button class="primary submit" type="submit" :disabled="busy">
        <span v-if="busy" class="spin" aria-hidden="true" /><LogIn v-else :size="15" />
        {{ busy ? $t('login.signingIn') : $t('login.signIn') }}
      </button>

      <!-- SSO opzionale: senza IdP configurato questo blocco non esiste -->
      <template v-if="sso.enabled">
        <div class="sep"><span>{{ $t('login.or') }}</span></div>
        <button class="sso" type="button" @click="ssoLogin()">
          <KeyRound :size="15" /> {{ sso.button_label || $t('login.signInWithSso') }}
        </button>
      </template>
    </form>

    <!-- i due lati del campo, nominati: a sinistra ciò che entra, a destra ciò che esce -->
    <p class="side raw" aria-hidden="true">{{ $t('login.rawSide') }}</p>
    <p class="side ready" aria-hidden="true">{{ $t('login.readySide') }}</p>
  </main>
</template>

<style scoped>
.login {
  position: relative;
  isolation: isolate;
  overflow: hidden;
  display: grid;
  place-items: center;
  /* min-height e non height: con il blocco SSO su schermo basso la card supera
     il viewport, e un'altezza fissa ne taglierebbe la cima senza poterla
     raggiungere. dvh segue la barra degli indirizzi mobile; 100vh è il fallback. */
  min-height: 100vh;
  min-height: 100dvh;
  padding: 72px 16px;
  background: var(--bg);
}
.login ::selection { background: color-mix(in srgb, var(--accent) 38%, transparent); }

.veil {
  position: absolute;
  z-index: 1;
  top: 50%;
  left: 50%;
  /* poco più della card: basta a spegnere i valori prima che le arrivino sotto */
  width: calc(var(--qw, 440px) + 150px);
  height: calc(var(--qh, 520px) + 130px);
  transform: translate(-50%, -50%);
  background: radial-gradient(closest-side, var(--bg) 64%, transparent);
  pointer-events: none;
}

.card {
  position: relative;
  z-index: 2;
  /* sotto i 380px di viewport la card usciva dallo schermo e trascinava la
     pagina a scorrere in orizzontale */
  width: min(380px, calc(100vw - 32px));
  display: flex;
  flex-direction: column;
  gap: 16px;
  padding: 32px 30px 30px;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 14px;
  /* solo l'ombra del sistema, che è già tematizzata: una seconda ombra lunga era
     una macchia nera sul tema chiaro e, derivata da --text, un alone bianco sul buio */
  box-shadow: var(--shadow-2);
}
@media (prefers-reduced-motion: no-preference) {
  .card { animation: card-in 620ms cubic-bezier(0.16, 1, 0.3, 1) backwards; }
  @keyframes card-in {
    from { opacity: 0; transform: translateY(10px); }
  }
}

.id { display: flex; align-items: center; gap: 12px; }
h1 { margin: 0; font-size: 26px; font-weight: 600; letter-spacing: -0.01em; line-height: 1.1; }
.tagline { margin: -6px 0 8px; color: var(--muted); font-size: 14px; line-height: 1.45; text-wrap: balance; }

.field { display: flex; flex-direction: column; gap: 6px; }
.field label { font-size: 12px; font-weight: 500; color: var(--muted); }
.field input { width: 100%; padding: 10px 12px; font-size: 14px; caret-color: var(--accent); }
.with-peek { position: relative; }
.with-peek input { padding-right: 42px; }
.peek {
  position: absolute;
  top: 50%;
  right: 5px;
  transform: translateY(-50%);
  display: grid;
  place-items: center;
  width: 32px;
  height: 32px;
  padding: 0;
  border: 0;
  border-radius: 8px;
  background: transparent;
  color: var(--muted);
  box-shadow: none;
}
.peek:hover { color: var(--text); background: var(--panel-2); box-shadow: none; }
.peek:active { transform: translateY(-50%); }
.hint { display: flex; align-items: center; gap: 5px; margin: 0; font-size: 12px; color: var(--muted); }

.err {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  margin: 0;
  padding: 10px 12px;
  border-radius: 9px;
  background: color-mix(in srgb, var(--danger) 12%, transparent);
  /* --danger su tinta danger faceva 3,9:1 in light/dracula/monokai: il testo
     prende --text, il rosso resta all'icona */
  color: var(--text);
  font-size: 13px;
  line-height: 1.4;
}
.err svg { flex: none; margin-top: 1px; color: var(--danger); }

.submit, .sso {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  width: 100%;
  padding: 11px 12px;
  font-size: 14px;
  font-weight: 500;
}
.submit { margin-top: 4px; }
.spin {
  width: 14px;
  height: 14px;
  border: 2px solid color-mix(in srgb, currentColor 35%, transparent);
  border-top-color: currentColor;
  border-radius: 50%;
  animation: spin 700ms linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }

.sep { display: flex; align-items: center; gap: 10px; margin: -2px 0; color: var(--muted); font-size: 12px; }
.sep::before, .sep::after { content: ""; flex: 1; height: 1px; background: var(--border); }

.lang { position: absolute; z-index: 3; top: 18px; right: 18px; width: 132px; }

.side {
  position: absolute;
  z-index: 1;
  bottom: 22px;
  margin: 0;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 11px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--muted);
  /* i valori passano anche qui sotto: un fondo, o l'etichetta non si legge */
  padding: 3px 8px;
  border-radius: 6px;
  background: var(--bg);
}
.side.raw { left: 26px; }
.side.ready { right: 26px; }
/* su schermi stretti o bassi le etichette finirebbero sotto la card */
@media (max-width: 720px), (max-height: 640px) {
  .side { display: none; }
  .login { padding: 64px 16px 24px; }
}
/* viewport basso con SSO + errore + avviso maiuscole: la card si stringe */
@media (max-height: 640px) {
  .card { gap: 12px; padding: 22px 26px; }
  .tagline { margin-bottom: 0; }
}
</style>
