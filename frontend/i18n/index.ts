// Registro delle lingue e dei cataloghi. L'inglese è la lingua BASE (fallback):
// una chiave mancante in un'altra lingua ricade sull'inglese, mai UI rotta.
//
// Ogni lingua ha due file: quello BASE (nav/settings/login, scritto a mano) e
// quello *.gen.ts (namespace per componente/pagina, prodotto dall'i18n sweep).
// I namespace sono disgiunti → merge shallow.
import enBase from './locales/en'
import itBase from './locales/it'
import frBase from './locales/fr'
import deBase from './locales/de'
import esBase from './locales/es'
import enGen from './locales/en.gen'
import itGen from './locales/it.gen'
import frGen from './locales/fr.gen'
import deGen from './locales/de.gen'
import esGen from './locales/es.gen'

export const messages = {
  en: { ...enBase, ...enGen },
  it: { ...itBase, ...itGen },
  fr: { ...frBase, ...frGen },
  de: { ...deBase, ...deGen },
  es: { ...esBase, ...esGen },
}

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
