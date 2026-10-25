<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount, nextTick } from 'vue'
import { useI18n } from 'vue-i18n'
import { LogIn, KeyRound, Eye, EyeOff, CircleAlert, ArrowUp, Clock, UserPlus } from 'lucide-vue-next'
import { passwordProblem, usePasswordRules } from '~/composables/usePasswordRules'
import { errMessage } from '~/composables/useApi'
import { useLocale } from '~/composables/useLocale'
// explicit imports: in the container new components are not scanned
import OrderingField from '~/components/ui/OrderingField.vue'
import Select from '~/components/ui/Select.vue'
import BrandMark from '~/components/ui/BrandMark.vue'
import PrivacyNotice from '~/components/ui/PrivacyNotice.vue'

const { login, ssoConfig, ssoLogin, setupNeeded, setupAdmin } = useAuth()
const { rules } = usePasswordRules()
const { t, te } = useI18n()
const { locale, setLocale, locales } = useLocale()

const email = ref('')
const password = ref('')
const busy = ref(false)
const error = ref('')
// why one is here without having asked (?reason=idle: signed out for inactivity)
const notice = ref('')
const showPassword = ref(false)
const capsLock = ref(false)
// A new installation of the try-it stack: no account yet, so this card creates the first
// administrator instead of signing in (the gateway decides: GET /auth/setup).
const setup = ref(false)
const fullName = ref('')
const confirm = ref('')

// The field's quiet zone follows the REAL card: it changes height with SSO, with an
// error, with the language. A fixed size left a black hole around the form and hid
// exactly the band where the values are settling.
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
  // `autofocus` holds only at page load: after a logout one gets here by internal
  // navigation and the field would stay without focus
  emailEl.value?.focus()
})
onBeforeUnmount(() => cardRo?.disconnect())

// OIDC SSO: the button shows only when the gateway is configured with an IdP
const sso = ref<{ enabled: boolean; button_label: string }>({ enabled: false, button_label: '' })
onMounted(async () => {
  setup.value = await setupNeeded()
  sso.value = await ssoConfig()
  // the SSO callback sends back here with ?sso_error=<code> when something went wrong
  const query = new URLSearchParams(window.location.search)
  const code = query.get('sso_error')
  if (code) {
    // the code, and what to do when there is something to do about it
    const hint = `login.ssoHint.${code}`
    error.value = te(hint) ? `${t(hint)} (${code})` : t('login.ssoError', { code })
  }
  if (query.get('reason') === 'idle') notice.value = t('login.signedOutIdle')
})

// a password wrong because of Caps Lock is the most frequent and least explained error:
// it is said BEFORE sending
function onPasswordKey(e: KeyboardEvent) {
  if (typeof e.getModifierState === 'function') capsLock.value = e.getModifierState('CapsLock')
}

async function onSubmit() {
  error.value = ''
  if (setup.value) {
    const problem = passwordProblem(password.value, rules.value)
    if (problem) { error.value = t(problem.key, problem.values); return }
    if (password.value !== confirm.value) { error.value = t('login.passwordsDiffer'); return }
  }
  busy.value = true
  try {
    if (setup.value) await setupAdmin(email.value, password.value, fullName.value)
    else await login(email.value, password.value)
    await navigateTo('/')
  } catch (e) {
    // the two answers of a wrong sign-in, in the page's language (the server's are in English)
    const said = errMessage(e)
    const wait = /^Too many sign-in attempts\. Try again in (\d+) seconds\.$/.exec(said)
    error.value = said === 'Incorrect email or password' ? t('login.wrongCredentials')
      : wait ? t('login.tooManyAttempts', { s: wait[1] }) : said
    // it is almost always the password: the focus goes back there, ready to type again
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
    <!-- a veil of the background colour behind the card: the values fade before reaching under
         it, and nothing moves around the fields -->
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
      <p v-if="!setup" class="tagline">{{ $t('login.tagline') }}</p>
      <template v-else>
        <h2 class="setup-title">{{ $t('login.setupTitle') }}</h2>
        <p class="tagline">{{ $t('login.setupLead') }}</p>
        <div class="field">
          <label for="login-name">{{ $t('login.name') }}</label>
          <input id="login-name" v-model="fullName" type="text" autocomplete="name" />
        </div>
      </template>

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
            :autocomplete="setup ? 'new-password' : 'current-password'"
            required
            :aria-describedby="capsLock ? 'login-caps' : setup ? 'login-rule' : undefined"
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
        <p v-if="capsLock" id="login-caps" class="hint" role="status"><ArrowUp :size="13" /> {{ $t('login.capsLock') }}</p>
        <p v-else-if="setup" id="login-rule" class="hint">{{ $t('login.passwordRule', { n: rules.min_length }) }}</p>
      </div>

      <div v-if="setup" class="field">
        <label for="login-confirm">{{ $t('login.confirmPassword') }}</label>
        <input id="login-confirm" v-model="confirm" :type="showPassword ? 'text' : 'password'" autocomplete="new-password" required />
      </div>

      <p v-if="error" class="err" role="alert"><CircleAlert :size="15" /> <span>{{ error }}</span></p>
      <p v-else-if="notice" class="err note" role="status"><Clock :size="15" /> <span>{{ notice }}</span></p>

      <button class="primary submit" type="submit" :disabled="busy">
        <span v-if="busy" class="spin" aria-hidden="true" /><UserPlus v-else-if="setup" :size="15" /><LogIn v-else :size="15" />
        <template v-if="setup">{{ busy ? $t('login.creating') : $t('login.createAdmin') }}</template>
        <template v-else>{{ busy ? $t('login.signingIn') : $t('login.signIn') }}</template>
      </button>

      <!-- optional SSO: without an IdP configured this block does not exist -->
      <template v-if="sso.enabled && !setup">
        <div class="sep"><span>{{ $t('login.or') }}</span></div>
        <button class="sso" type="button" @click="ssoLogin()">
          <KeyRound :size="15" /> {{ sso.button_label || $t('login.signInWithSso') }}
        </button>
      </template>
    </form>

    <!-- the notice is read BEFORE signing in: it is the only moment someone can still decide
         not to sign in -->
    <PrivacyNotice />

    <!-- the field's two sides, named: on the left what comes in, on the right what goes out -->
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
  /* min-height and not height: with the SSO block on a short screen the card exceeds the
     viewport, and a fixed height would cut its top without any way to reach it. dvh follows
     the mobile address bar; 100vh is the fallback. */
  min-height: 100vh;
  min-height: 100dvh;
  /* at the bottom room is left for the notice's bar, which is FIXED at the bottom: without
     it, on a short screen — or with the SSO block — the card would end up under the bar and
     scrolling could not reach it */
  padding: 72px 16px 132px;
  background: var(--bg);
}
.login ::selection { background: color-mix(in srgb, var(--accent) 38%, transparent); }

.veil {
  position: absolute;
  z-index: 1;
  top: 50%;
  left: 50%;
  /* a bit more than the card: enough to fade the values before they reach under it */
  width: calc(var(--qw, 440px) + 150px);
  height: calc(var(--qh, 520px) + 130px);
  transform: translate(-50%, -50%);
  background: radial-gradient(closest-side, var(--bg) 64%, transparent);
  pointer-events: none;
}

.card {
  position: relative;
  z-index: 2;
  /* under a 380px viewport the card left the screen and dragged the page into horizontal
     scrolling */
  width: min(380px, calc(100vw - 32px));
  display: flex;
  flex-direction: column;
  gap: 16px;
  padding: 32px 30px 30px;
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 14px;
  /* only the system's shadow, which is themed already: a second long shadow was a black
     stain on the light theme and, derived from --text, a white halo on the dark */
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
.setup-title { margin: 4px 0 0; font-size: 18px; font-weight: 600; letter-spacing: -0.01em; }

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
  /* --danger on a danger tint made 3.9:1 in light/dracula/monokai: the text takes --text,
     the red stays to the icon */
  color: var(--text);
  font-size: 13px;
  line-height: 1.4;
}
.err svg { flex: none; margin-top: 1px; color: var(--danger); }
/* a warning, not an error: the same shape, quiet colours */
.err.note { background: var(--panel-2); color: var(--text); }
.err.note svg { color: var(--muted); }

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
  /* above the notice's bar, which is fixed at the bottom: without it, the field's two labels
     would end up under it */
  bottom: 88px;
  margin: 0;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 11px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--muted);
  /* the values pass under here too: a background, or the label cannot be read */
  padding: 3px 8px;
  border-radius: 6px;
  background: var(--bg);
}
.side.raw { left: 26px; }
.side.ready { right: 26px; }
/* on narrow or short screens the labels would end up under the card */
@media (max-width: 720px), (max-height: 640px) {
  .side { display: none; }
  .login { padding: 64px 16px 140px; }
}
/* short viewport with SSO + error + caps warning: the card shrinks */
@media (max-height: 640px) {
  .card { gap: 12px; padding: 22px 26px; }
  .tagline { margin-bottom: 0; }
}
</style>
