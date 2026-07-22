// Registra vue-i18n. La lingua iniziale arriva da un COOKIE (non localStorage)
// così l'SSR renderizza già nella lingua giusta: niente flash né mismatch di
// idratazione. Il cambio lingua avviene poi via composables/useLocale.
import { createI18n } from 'vue-i18n'
import { messages, DEFAULT_LOCALE, isLocale } from '~/i18n'

export const LOCALE_COOKIE = 'tabularia-locale'

export default defineNuxtPlugin((nuxtApp) => {
  const saved = useCookie(LOCALE_COOKIE, { maxAge: 60 * 60 * 24 * 365, sameSite: 'lax' }).value
  const locale = isLocale(saved) ? saved : DEFAULT_LOCALE

  const i18n = createI18n({
    legacy: false, // Composition API
    globalInjection: true, // $t disponibile nei template
    locale,
    fallbackLocale: DEFAULT_LOCALE,
    messages,
  })

  nuxtApp.vueApp.use(i18n)
})
