// Registers vue-i18n. The initial language comes from a COOKIE (not localStorage) so SSR
// renders in the right language already: no flash nor hydration mismatch. The language
// change then happens through composables/useLocale.
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
    globalInjection: true, // $t available in the templates
    locale,
    fallbackLocale: DEFAULT_LOCALE,
    messages,
  })

  nuxtApp.vueApp.use(i18n)
  if (import.meta.client) clientTranslate = (key) => i18n.global.t(key)
  // <html lang> from the server, and updated at the language change: speech synthesis reads
  // the interface with the right language's voice (before, the server did not write it)
  useHead({ htmlAttrs: { lang: computed(() => i18n.global.locale.value) } })
})
