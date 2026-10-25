// Numbers and dates in the INTERFACE's language (vue-i18n), not in the browser's nor in a
// fixed it-IT: before there were three regimes, and an English interface showed
// «42.007.616» and «11/10/26, 10:27» (which an American reads as 10 November). The
// formatters cost: they are built once per language and options, not at every cell (the
// Viewer created one per cell at every render).
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

const relativeCache = new Map<string, Intl.RelativeTimeFormat>()

function relativeFormat(locale: string): Intl.RelativeTimeFormat {
  let f = relativeCache.get(locale)
  if (!f) relativeCache.set(locale, (f = new Intl.RelativeTimeFormat(locale, { numeric: 'auto' })))
  return f
}

function toDate(v: string | number | Date): Date | null {
  const d = v instanceof Date ? v : new Date(v)
  return Number.isNaN(d.getTime()) ? null : d
}

export function useFormat() {
  const { locale } = useI18n()

  /** A count: «42,007,616» in English, «42.007.616» in Italian. */
  function count(n: number | null | undefined): string {
    return n == null ? '—' : numberFormat(locale.value).format(n)
  }

  /** A number with at most `maxFractionDigits` decimals. */
  function number(n: number | null | undefined, maxFractionDigits = 2): string {
    return n == null ? '—' : numberFormat(locale.value, { maximumFractionDigits: maxFractionDigits }).format(n)
  }

  /**
   * Short but never ambiguous date and time: the month in letters, the year only when it is
   * not the current one («Oct 11, 10:27 AM», «11 ott, 10:27», «Oct 11, 2025, …»).
   * `seconds` for the logs where the order to the second counts (audit).
   */
  function dateTime(v: string | number | Date | null | undefined, opts: { seconds?: boolean } = {}): string {
    const d = v == null ? null : toDate(v)
    if (!d) return v == null ? '—' : String(v)
    const o: Intl.DateTimeFormatOptions = { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }
    if (opts.seconds) o.second = '2-digit'
    if (d.getFullYear() !== new Date().getFullYear()) o.year = 'numeric'
    return dateFormat(locale.value, o).format(d)
  }

  /** The date only, with the same criterion. */
  function date(v: string | number | Date | null | undefined, opts: Intl.DateTimeFormatOptions = {}): string {
    const d = v == null ? null : toDate(v)
    if (!d) return v == null ? '—' : String(v)
    const o: Intl.DateTimeFormatOptions = { month: 'short', day: 'numeric', ...opts }
    if (!('year' in opts) && d.getFullYear() !== new Date().getFullYear()) o.year = 'numeric'
    return dateFormat(locale.value, o).format(d)
  }

  /**
   * How long ago, in the interface's language: «now», «5 minutes ago», «3 ore fa»,
   * «yesterday». Beyond a week, the date.
   */
  function ago(v: string | number | Date | null | undefined): string {
    const d = v == null ? null : toDate(v)
    if (!d) return '—'
    const s = Math.round((d.getTime() - Date.now()) / 1000)
    const f = relativeFormat(locale.value)
    if (Math.abs(s) < 60) return f.format(0, 'second')
    if (Math.abs(s) < 3600) return f.format(Math.round(s / 60), 'minute')
    if (Math.abs(s) < 86400) return f.format(Math.round(s / 3600), 'hour')
    if (Math.abs(s) < 7 * 86400) return f.format(Math.round(s / 86400), 'day')
    return dateTime(d)
  }

  /**
   * The formatter of a data grid's numeric column: the same decimals on the whole column
   * (the most seen, up to 4) and integers WITHOUT a thousands separator — a code or a
   * YYYYMM period grouped («202.607») reads as a quantity. The decimals keep the
   * language's separator.
   */
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

  return { count, number, dateTime, date, ago, columnFormatter, locale }
}
