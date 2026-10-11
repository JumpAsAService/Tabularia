// Numeri e date nella lingua dell'INTERFACCIA (vue-i18n), non in quella del
// browser né in un it-IT fisso: prima c'erano tre regimi, e un'interfaccia in
// inglese mostrava «42.007.616» e «11/10/26, 10:27» (che un americano legge come
// 10 novembre). I formatter costano: si costruiscono una volta per lingua e
// opzioni, non a ogni cella (il Viewer ne creava uno per cella a ogni render).
import { useI18n } from 'vue-i18n'

const numberCache = new Map<string, Intl.NumberFormat>()
const dateCache = new Map<string, Intl.DateTimeFormat>()

function numberFormat(locale: string, opts: Intl.NumberFormatOptions = {}): Intl.NumberFormat {
  const key = locale + JSON.stringify(opts)
  let f = numberCache.get(key)
  if (!f) numberCache.set(key, (f = new Intl.NumberFormat(locale, opts)))
  return f
}

function dateFormat(locale: string, opts: Intl.DateTimeFormatOptions): Intl.DateTimeFormat {
  const key = locale + JSON.stringify(opts)
  let f = dateCache.get(key)
  if (!f) dateCache.set(key, (f = new Intl.DateTimeFormat(locale, opts)))
  return f
}

function toDate(v: string | number | Date): Date | null {
  const d = v instanceof Date ? v : new Date(v)
  return Number.isNaN(d.getTime()) ? null : d
}

export function useFormat() {
  const { locale } = useI18n()

  /** Un conteggio: «42,007,616» in inglese, «42.007.616» in italiano. */
  function count(n: number | null | undefined): string {
    return n == null ? '—' : numberFormat(locale.value).format(n)
  }

  /** Un numero con al più `maxFractionDigits` decimali. */
  function number(n: number | null | undefined, maxFractionDigits = 2): string {
    return n == null ? '—' : numberFormat(locale.value, { maximumFractionDigits: maxFractionDigits }).format(n)
  }

  /** Data e ora brevi ma mai ambigue: il mese in lettere, l'anno solo se non è
   *  quello in corso («Oct 11, 10:27 AM», «11 ott, 10:27», «Oct 11, 2025, …»).
   *  `seconds` per i registri dove l'ordine al secondo conta (audit). */
  function dateTime(v: string | number | Date | null | undefined, opts: { seconds?: boolean } = {}): string {
    const d = v == null ? null : toDate(v)
    if (!d) return v == null ? '—' : String(v)
    const o: Intl.DateTimeFormatOptions = { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }
    if (opts.seconds) o.second = '2-digit'
    if (d.getFullYear() !== new Date().getFullYear()) o.year = 'numeric'
    return dateFormat(locale.value, o).format(d)
  }

  /** Solo la data, con lo stesso criterio. */
  function date(v: string | number | Date | null | undefined, opts: Intl.DateTimeFormatOptions = {}): string {
    const d = v == null ? null : toDate(v)
    if (!d) return v == null ? '—' : String(v)
    const o: Intl.DateTimeFormatOptions = { month: 'short', day: 'numeric', ...opts }
    if (!('year' in opts) && d.getFullYear() !== new Date().getFullYear()) o.year = 'numeric'
    return dateFormat(locale.value, o).format(d)
  }

  /** Il formatter di una colonna numerica di una griglia dati: stessi decimali su
   *  tutta la colonna (il massimo visto, fino a 4) e interi SENZA separatore delle
   *  migliaia — un codice o un periodo AAAAMM raggruppato («202.607») si legge
   *  come una quantità. I decimali tengono il separatore della lingua. */
  function columnFormatter(values: unknown[]): (v: unknown) => string {
    let decimals = 0
    let integersOnly = true
    for (const v of values) {
      if (typeof v !== 'number' || !Number.isFinite(v)) continue
      if (!Number.isInteger(v)) {
        integersOnly = false
        const frac = String(v).split(/[.e]/)[1]?.length ?? 4
        decimals = Math.max(decimals, Math.min(4, frac))
      }
    }
    const f = integersOnly
      ? numberFormat(locale.value, { useGrouping: false, maximumFractionDigits: 0 })
      : numberFormat(locale.value, { minimumFractionDigits: decimals, maximumFractionDigits: decimals })
    return (v) => (typeof v === 'number' ? f.format(v) : v == null ? '' : String(v))
  }

  return { count, number, dateTime, date, columnFormatter, locale }
}
