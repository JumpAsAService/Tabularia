// A number of a result as a person reads it: a sum of floats without its binary noise, with
// thousands. A whole-number column stays as it is: a year or an id has no thousands.
export function readableNumber(value: number, dtype: string, locale?: string): string {
  if (!Number.isFinite(value) || !/Float|Decimal/i.test(dtype)) return String(value)
  const digits = Math.abs(value) >= 1 || value === 0 ? { maximumFractionDigits: 2 } : { maximumSignificantDigits: 3 }
  return new Intl.NumberFormat(locale, digits).format(value)
}
