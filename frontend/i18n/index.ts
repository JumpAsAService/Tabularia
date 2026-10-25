// Register of the languages and the catalogs. English is the BASE language (fallback): a
// key missing in another language falls back on English, never broken UI.
//
// Every language has two files: the BASE one (nav/settings/login, written by hand) and
// the *.gen.ts one (namespaces per component/page, produced by the i18n sweep). The
// namespaces are disjoint → shallow merge.
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

// labels shown in the picker (in their own language)
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
