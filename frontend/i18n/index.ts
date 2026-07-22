// Registro delle lingue e dei cataloghi. L'inglese è la lingua BASE (fallback):
// una chiave mancante in un'altra lingua ricade sull'inglese, mai UI rotta.
import en from './locales/en'
import it from './locales/it'
import fr from './locales/fr'
import de from './locales/de'
import es from './locales/es'

export const messages = { en, it, fr, de, es }

export type LocaleCode = keyof typeof messages

// etichette mostrate nel picker (nel loro stesso idioma)
export const LOCALES: { code: LocaleCode; label: string }[] = [
  { code: 'en', label: 'English' },
  { code: 'it', label: 'Italiano' },
  { code: 'fr', label: 'Français' },
  { code: 'de', label: 'Deutsch' },
  { code: 'es', label: 'Español' },
]

export const DEFAULT_LOCALE: LocaleCode = 'en'

export function isLocale(v: unknown): v is LocaleCode {
  return typeof v === 'string' && v in messages
}
