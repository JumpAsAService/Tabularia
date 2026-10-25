// The interface's language, per user. Like theme/engine: a persisted choice (here in a
// COOKIE, so SSR knows it) shared across the whole app.
import { useI18n } from 'vue-i18n'
import { LOCALES, isLocale, type LocaleCode } from '~/i18n'
import { LOCALE_COOKIE } from '~/plugins/i18n'
import { syncLocale, useAuth } from '~/composables/useAuth'

export function useLocale() {
  const { locale } = useI18n()
  const cookie = useCookie<LocaleCode>(LOCALE_COOKIE, {
    maxAge: 60 * 60 * 24 * 365,
    sameSite: 'lax',
  })

  const { user } = useAuth()

  function setLocale(code: LocaleCode) {
    if (!isLocale(code)) return
    locale.value = code
    cookie.value = code // <html lang> follows `locale` from plugins/i18n.ts
    // the emails the product sends this user follow the language chosen here
    syncLocale(user.value, code)
  }

  return { locale, setLocale, locales: LOCALES }
}
