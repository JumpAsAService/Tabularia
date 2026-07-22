// Lingua dell'interfaccia, a livello di utente. Come tema/motore: scelta
// persistita (qui in un COOKIE, così l'SSR la conosce) e condivisa in tutta l'app.
import { useI18n } from 'vue-i18n'
import { LOCALES, isLocale, type LocaleCode } from '~/i18n'
import { LOCALE_COOKIE } from '~/plugins/i18n'

export function useLocale() {
  const { locale } = useI18n()
  const cookie = useCookie<LocaleCode>(LOCALE_COOKIE, {
    maxAge: 60 * 60 * 24 * 365,
    sameSite: 'lax',
  })

  function setLocale(code: LocaleCode) {
    if (!isLocale(code)) return
    locale.value = code
    cookie.value = code
    if (import.meta.client) document.documentElement.setAttribute('lang', code)
  }

  return { locale, setLocale, locales: LOCALES }
}
