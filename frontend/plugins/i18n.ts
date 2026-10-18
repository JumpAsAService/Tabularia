// Registra vue-i18n. La lingua iniziale arriva da un COOKIE (non localStorage)
// così l'SSR renderizza già nella lingua giusta: niente flash né mismatch di
// idratazione. Il cambio lingua avviene poi via composables/useLocale.
import { createI18n } from 'vue-i18n'
import { messages, DEFAULT_LOCALE, isLocale } from '~/i18n'

export const LOCALE_COOKIE = 'tabularia-locale'

// The translator of the page, for the few helpers that are not components (as
// `errMessage`). Only in the browser: on the server one module serves every
// request, and a module-level instance would mix the languages of two visitors.
export let clientTranslate: ((key: string) => string) | null = null

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
  if (import.meta.client) clientTranslate = (key) => i18n.global.t(key)
  // <html lang> dal server, e aggiornato al cambio lingua: la sintesi vocale legge
  // l'interfaccia con la voce della lingua giusta (prima il server non lo scriveva)
  useHead({ htmlAttrs: { lang: computed(() => i18n.global.locale.value) } })
})
