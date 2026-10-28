// How many rows a report's dataset gives, as «Refresh fields» counted them (the gateway's
// POST /reports/{id}/fields: `rows`, `over`, `max_rows`, counted with the parameters'
// defaults up to REPORTS__MAX_ROWS), and what the designer says about it next to the
// dataset: the number in a quiet line; past SLOW_ROWS a warning with the time a render
// takes; past the cap, that the report will not render. A count belongs to the source it
// was made on: another SQL, and it is no longer shown.

export interface RowCount {
  rows: number // at most max_rows
  over: boolean // more than max_rows: a render stops on the dataset
  max_rows: number
}

/** A count kept for a dataset, with the source it counted (`sourceKey`). */
export interface CountedRows extends RowCount {
  source: string
}

/** Past this many rows a dataset gets the warning: half a million rows are far more than
 *  pages can show (a table of them stops at REPORTS__MAX_PAGES), and totals the SQL's
 *  GROUP BY would give at once still cost the worker their memory. */
export const SLOW_ROWS = 500_000

/** What a render takes per million rows of a dataset, measured since the reports compute in
 *  columns with DuckDB: some 1.3 s for totals, 1.8 with a matrix, and half a GiB in the
 *  worker (backend/app/core/config.py, ReportsSettings). */
export const SECONDS_PER_MILLION = 2

export type RowCountLevel = 'plain' | 'slow' | 'over'

export interface RowCountNote {
  level: RowCountLevel
  rows: number // as said: exact when plain, rounded when slow, the cap when over
  seconds: number // the time a render takes to read them (slow)
}

/** The count of a fields answer, or null when it has none (it could not be made). */
export function rowCountOf(got: unknown): RowCount | null {
  if (!got || typeof got !== 'object') return null
  const { rows, over, max_rows: max } = got as Record<string, unknown>
  if (typeof rows !== 'number' || typeof over !== 'boolean' || typeof max !== 'number') return null
  if (!Number.isFinite(rows) || rows < 0 || !Number.isFinite(max) || max < 1) return null
  return { rows, over, max_rows: max }
}

/** Three significant figures: «about 842,000», not a precision the data will not keep. */
export function aboutRows(n: number): number {
  if (n < 1000) return Math.round(n)
  const step = 10 ** (Math.floor(Math.log10(n)) - 2)
  return Math.round(n / step) * step
}

/** The seconds a render takes to read `rows`: whole seconds under ten (one at least), then
 *  by fives — it is a guess, said as one. */
export function aboutSeconds(rows: number): number {
  const s = (rows / 1_000_000) * SECONDS_PER_MILLION
  return s < 10 ? Math.max(1, Math.round(s)) : Math.round(s / 5) * 5
}

/** What to say about a count: nothing without one. */
export function rowCountNote(c: RowCount | null | undefined): RowCountNote | null {
  if (!c) return null
  if (c.over) return { level: 'over', rows: c.max_rows, seconds: aboutSeconds(c.max_rows) }
  if (c.rows > SLOW_ROWS) return { level: 'slow', rows: aboutRows(c.rows), seconds: aboutSeconds(c.rows) }
  return { level: 'plain', rows: c.rows, seconds: aboutSeconds(c.rows) }
}

/** A source as a key that does not depend on the order of its properties: a dataset read
 *  from the server and the same one built by its dialog are the same source. */
export function sourceKey(source: unknown): string {
  const sorted = (v: unknown): unknown => {
    if (Array.isArray(v)) return v.map(sorted)
    if (v && typeof v === 'object') {
      return Object.fromEntries(Object.keys(v as object).sort().map((k) => [k, sorted((v as Record<string, unknown>)[k])]))
    }
    return v
  }
  return JSON.stringify(sorted(source ?? null))
}

/** The count kept for a dataset while its source is still the one counted. */
export function countFor(kept: CountedRows | null | undefined, source: unknown): RowCount | null {
  return kept && kept.source === sourceKey(source) ? kept : null
}
