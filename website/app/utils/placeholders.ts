// Tabularia's placeholder language, ported from backend/app/core/placeholders.py for the
// landing page's live example. It accepts and refuses what the product does, with the
// product's own messages: the grammar ({name}{±N unit}{:format}, case-sensitive names),
// the offsets (days on dates, h/min/s on date-times only, months clamped to the month's
// end, d/w on a date-time keeping the wall-clock time), the formats (readable yyyy-MM-dd
// with Excel's mm rule, quotes and the refusals; strftime with the C library's directives;
// epoch, epoch_ms, iso), list and range variables, and the Outputs' {run_id}, {flow},
// {date}, {datetime} with the file / path / text rules. Range expansion and the initial
// value of {last_refresh} are not ported: the page passes their values in.
// Tests: website/tests/placeholders.test.ts (the backend's cases and the README's examples).

export class PlaceholderError extends Error {}

export interface CivilDate { y: number, m: number, d: number }
export type Value =
  | { kind: 'date', date: CivilDate }
  | { kind: 'datetime', ms: number } // an instant; shown in the context's zone
  | { kind: 'text', value: string }

export interface Ctx {
  now: Date
  zone: string
  lastRefresh?: Date | null
  variables: Record<string, Value>
  output?: boolean
}

const DATE_BASES = ['today', 'yesterday', 'start_of_month', 'start_of_week', 'start_of_year']
const DATETIME_BASES = ['now', 'last_refresh']
const BUILTINS = new Set([...DATE_BASES, ...DATETIME_BASES])
const OUTPUT_TEXT = ['run_id', 'flow']
const OUTPUT_ALIASES: Record<string, string> = { date: 'today', datetime: 'now' }
const ALIAS_DEFAULT_FORMAT: Record<string, string> = { datetime: '%Y-%m-%d_%H%M%S' }
const END_SUFFIX = '_end'
export const MAX_NAME_BYTES = 255

const BODY = /^([A-Za-z_][A-Za-z0-9_]*)(?:([+-])(\d{1,5})(min|d|w|m|y|h|s)?)?(?::([\s\S]+))?$/
const STRFTIME = /%[-_]?[aAbBcdDeFgGhHIjklmMpPrRsSTuUVwWxXyYzZ%]/g
const READABLE = /''|'(?:[^']|'')*'|yyyy|yy|MM|mm|dd|HH|hh|ss|[A-Za-z]|[^A-Za-z']+|'/g
const READABLE_TOKENS: Record<string, string> = { yyyy: '%Y', yy: '%y', MM: '%m', dd: '%d', HH: '%H', ss: '%S' }
const QUOTED = /'(?:[^']|'')*'/g

interface Placeholder { raw: string, name: string, sign: string, amount: number, unit: string, fmt: string }
const text = (p: Placeholder) => '{' + p.raw + '}'
type Part = string | Placeholder

// ── time zones ────────────────────────────────────────────────────────────────
const fmtCache = new Map<string, Intl.DateTimeFormat>()
export function checkZone (name: string): string {
  const z = (name || 'UTC').trim() || 'UTC'
  const bad = () => new PlaceholderError(`unknown time zone '${z}': use an IANA name such as Europe/Rome or UTC`)
  if (z.includes('..') || z.startsWith('/')) throw bad()
  if (!fmtCache.has(z)) {
    try {
      fmtCache.set(z, new Intl.DateTimeFormat('en-US', {
        timeZone: z, hourCycle: 'h23', year: 'numeric', month: 'numeric', day: 'numeric', hour: 'numeric', minute: 'numeric', second: 'numeric',
      }))
    } catch { throw bad() }
  }
  return z
}
interface Civil extends CivilDate { H: number, M: number, S: number }
function wall (ms: number, zone: string): Civil {
  const p = Object.fromEntries(fmtCache.get(checkZone(zone))!.formatToParts(new Date(ms)).map(x => [x.type, x.value]))
  return { y: +p.year!, m: +p.month!, d: +p.day!, H: +p.hour! % 24, M: +p.minute!, S: +p.second! }
}
/** The zone's UTC offset at an instant, in seconds (whole seconds, as zoneinfo's for modern dates). */
function offsetAt (ms: number, zone: string): number {
  const c = wall(ms, zone)
  const asUtc = Date.UTC(c.y, c.m - 1, c.d, c.H, c.M, c.S)
  return Math.round((asUtc - Math.floor(ms / 1000) * 1000) / 1000)
}
/** The instant of a wall-clock time in the zone, with Python's fold=0 (first of an ambiguous
    hour; in a gap, the offset in force before the change). */
function instantOf (c: Civil, zone: string, ms = 0): number {
  const guess = Date.UTC(c.y, c.m - 1, c.d, c.H, c.M, c.S) + ms
  const before = offsetAt(guess - 86400e3, zone)
  const after = offsetAt(guess + 86400e3, zone)
  const valid = [...new Set([before, after])].filter(o => offsetAt(guess - o * 1000, zone) === o)
  const o = valid.includes(before) || !valid.length ? before : valid[0]!
  return guess - o * 1000
}

// ── calendar arithmetic ───────────────────────────────────────────────────────
const daysIn = (y: number, m: number) => new Date(Date.UTC(y, m, 0)).getUTCDate()
export function addDays (c: CivilDate, n: number): CivilDate {
  const t = new Date(Date.UTC(c.y, c.m - 1, c.d + n))
  if (t.getUTCFullYear() < 1 || t.getUTCFullYear() > 9999) throw new RangeError('out of range')
  return { y: t.getUTCFullYear(), m: t.getUTCMonth() + 1, d: t.getUTCDate() }
}
function addMonths (c: CivilDate, n: number): CivilDate {
  const idx = c.y * 12 + (c.m - 1) + n
  const y = Math.floor(idx / 12)
  const m = idx - y * 12 + 1
  if (y < 1 || y > 9999) throw new PlaceholderError('the date goes out of range')
  return { y, m, d: Math.min(c.d, daysIn(y, m)) }
}
const weekdayMon0 = (c: CivilDate) => (new Date(Date.UTC(c.y, c.m - 1, c.d)).getUTCDay() + 6) % 7

// ── the grammar ───────────────────────────────────────────────────────────────
function isStrftime (fmt: string) { return fmt.replace(QUOTED, '').includes('%') }

function readableToStrftime (fmt: string, shown = ''): string {
  const where = shown || `'${fmt}'`
  const tokens = fmt.match(READABLE) || []
  const letters = tokens.map((t, i) => (/^[A-Za-z]/.test(t) ? i : -1)).filter(i => i >= 0)
  let out = ''
  tokens.forEach((t, i) => {
    if (t === "''") out += "'"
    else if (t === "'") throw new PlaceholderError(`a quote is not closed in the format of ${where}: text goes between 'quotes'`)
    else if (t.startsWith("'")) out += t.slice(1, -1).replace(/''/g, "'").replace(/%/g, '%%')
    else if (!/^[A-Za-z]/.test(t)) out += t.replace(/%/g, '%%')
    else if (t === 'hh') throw new PlaceholderError(`${where}: hh (12-hour clock) is not supported: use HH (00-23)`)
    else if (t === 'mm') {
      const k = letters.indexOf(i)
      const before = k > 0 ? tokens[letters[k - 1]!] : ''
      const after = k + 1 < letters.length ? tokens[letters[k + 1]!] : ''
      out += before === 'HH' || after === 'ss' ? '%M' : '%m'
    } else if (READABLE_TOKENS[t]) out += READABLE_TOKENS[t]
    else {
      throw new PlaceholderError(
        `unknown letter '${t}' in the format of ${where}: use yyyy yy MM dd HH mm ss, and put letters that are ` +
        `text in quotes: '${t}' (or write a strftime pattern such as %Y-%m-%d, or epoch, epoch_ms, iso)`)
    }
    if (/^[A-Za-z]/.test(t) && i > 0 && tokens[i - 1] === t) {
      throw new PlaceholderError(`${where}: ${t} is written twice in a row (${t}${t}): write it once`)
    }
  })
  if (!letters.length) throw new PlaceholderError(`${where}: the format has no date or time part (yyyy, MM, dd, HH, mm, ss)`)
  return out
}

function checkFormat (ph: Placeholder, isDate = false) {
  const fmt = ph.fmt
  if (!fmt || fmt === 'epoch' || fmt === 'epoch_ms' || fmt === 'iso') return
  if (!isStrftime(fmt)) { readableToStrftime(fmt, text(ph)); return }
  if (fmt.replace(STRFTIME, '').includes('%')) {
    throw new PlaceholderError(`bad format in ${text(ph)}: a '%' is not followed by a known directive (%Y, %m, %d, %H…)`)
  }
  const directives = new Set((fmt.match(STRFTIME) || []).map(d => d[d.length - 1]))
  if (directives.has('s')) throw new PlaceholderError(`${text(ph)}: %s is not supported: use :epoch (or :epoch_ms) for seconds since 1970`)
  if ((directives.has('z') || directives.has('Z')) && isDate) {
    throw new PlaceholderError(`${text(ph)}: %z and %Z are empty on a date: use them on {now} or {last_refresh}`)
  }
}

function placeholder (raw: string): Placeholder {
  const body = raw.trim()
  if (!body) throw new PlaceholderError('empty placeholder {}: write a name such as {today}, or {{}} for literal braces')
  const m = body.match(BODY)
  if (!m) {
    throw new PlaceholderError(`bad placeholder {${raw}}: expected a name, an optional offset (+1d, -2h, -1m) and an optional ` +
      "format after ':' — e.g. {today-7d:%Y%m%d}")
  }
  return { raw, name: m[1]!, sign: m[2] || '', amount: Number(m[3] || 0), unit: m[4] || '', fmt: m[5] || '' }
}

export function parse (template: string): Part[] {
  const parts: Part[] = []
  let buf = ''
  const s = template || ''
  for (let i = 0; i < s.length;) {
    const c = s[i]
    if (c === '{') {
      if (s.startsWith('{{', i)) { buf += '{'; i += 2; continue }
      const end = s.indexOf('}', i + 1)
      if (end < 0) throw new PlaceholderError(`a placeholder is not closed in '${s}': add '}', or write {{ for a literal brace`)
      const inner = s.slice(i + 1, end)
      if (inner.includes('{')) throw new PlaceholderError(`a placeholder is not closed before another '{' in '${s}'`)
      if (buf) { parts.push(buf); buf = '' }
      parts.push(placeholder(inner))
      i = end + 1
      continue
    }
    if (c === '}') {
      if (s.startsWith('}}', i)) { buf += '}'; i += 2; continue }
      throw new PlaceholderError(`a single '}' in '${s}': write }} for a literal brace`)
    }
    buf += c
    i++
  }
  if (buf) parts.push(buf)
  return parts
}

function rangeOf (name: string, ranges: Set<string>): string | null {
  if (ranges.has(name)) return name
  if (name.endsWith(END_SUFFIX) && ranges.has(name.slice(0, -END_SUFFIX.length))) return name.slice(0, -END_SUFFIX.length)
  return null
}

function unknown (ph: Placeholder, output: boolean) {
  if (output) {
    const known = [...DATE_BASES, 'now', ...Object.keys(OUTPUT_ALIASES), ...OUTPUT_TEXT].map(n => `{${n}}`).join(', ')
    return new PlaceholderError(`unknown placeholder ${text(ph)}: use ${known}`)
  }
  const known = [...DATE_BASES, ...DATETIME_BASES].map(n => `{${n}}`).join(', ')
  return new PlaceholderError(`unknown placeholder ${text(ph)}: use ${known} or a variable of the datasource`)
}

function checkPlaceholder (ph: Placeholder, variables: Set<string>, hasInitial: boolean, ranges: Set<string>, output: boolean) {
  let name = ph.name
  if (output) {
    if (OUTPUT_TEXT.includes(name)) {
      if (ph.sign || ph.fmt) throw new PlaceholderError(`{${name}} takes no offset or format (${text(ph)})`)
      return
    }
    if (name === 'last_refresh') {
      throw new PlaceholderError('{last_refresh} has no meaning in an Output: it is the last refresh of a datasource. ' +
        'Use {today}, {now} or {yesterday}')
    }
    name = OUTPUT_ALIASES[name] || name
    if (!BUILTINS.has(name)) throw unknown(ph, true)
  } else if (!BUILTINS.has(name)) {
    if (rangeOf(name, ranges)) { checkFormat(ph); return }
    if (!variables.has(name)) throw unknown(ph, false)
    if (ph.sign || ph.fmt) throw new PlaceholderError(`the variable {${ph.name}} takes no offset or format (${text(ph)})`)
    return
  }
  if (ph.sign && DATE_BASES.includes(name) && ['h', 'min', 's'].includes(ph.unit)) {
    throw new PlaceholderError(`${text(ph)}: hours, minutes and seconds apply to {now} and {last_refresh}, not to a date`)
  }
  if (ph.sign && DATETIME_BASES.includes(name) && !ph.unit) {
    throw new PlaceholderError(`${text(ph)}: say the unit of the offset (d, w, m, y, h, min, s), e.g. {${ph.name}-2h}`)
  }
  if (name === 'last_refresh' && !hasInitial) {
    throw new PlaceholderError('{last_refresh} has no value on the first refresh: set the initial value of last refresh ' +
      '(e.g. today-30) in the datasource')
  }
  checkFormat(ph, DATE_BASES.includes(name))
}

/** Checks a template without resolving it; returns the variables it uses. */
export function check (template: string, opts: { variables?: string[], hasInitial?: boolean, ranges?: string[], output?: boolean } = {}): Set<string> {
  const variables = new Set(opts.variables || [])
  const ranges = new Set(opts.ranges || [])
  const output = !!opts.output
  const used = new Set<string>()
  for (const part of parse(template)) {
    if (typeof part === 'string') continue
    checkPlaceholder(part, variables, !!opts.hasInitial, ranges, output)
    if (output) continue
    const owner = BUILTINS.has(part.name) ? null : rangeOf(part.name, ranges)
    if (owner) used.add(owner)
    else if (!BUILTINS.has(part.name)) used.add(part.name)
  }
  return used
}

// ── values ────────────────────────────────────────────────────────────────────
function today (ctx: Ctx): CivilDate { const c = wall(ctx.now.getTime(), ctx.zone); return { y: c.y, m: c.m, d: c.d } }

function base (name: string, ctx: Ctx): Value {
  const t = today(ctx)
  switch (name) {
    case 'today': return { kind: 'date', date: t }
    case 'yesterday': return { kind: 'date', date: addDays(t, -1) }
    case 'start_of_month': return { kind: 'date', date: { ...t, d: 1 } }
    case 'start_of_week': return { kind: 'date', date: addDays(t, -weekdayMon0(t)) }
    case 'start_of_year': return { kind: 'date', date: { y: t.y, m: 1, d: 1 } }
    case 'now': return { kind: 'datetime', ms: ctx.now.getTime() }
  }
  if (!ctx.lastRefresh) {
    throw new PlaceholderError('{last_refresh} has no value on the first refresh: set the initial value of last refresh ' +
      '(e.g. today-30) in the datasource')
  }
  return { kind: 'datetime', ms: ctx.lastRefresh.getTime() }
}

function move (v: Value, n: number, unit: string, zone: string): Value {
  if (v.kind === 'text') return v
  if (unit === 'm' || unit === 'y') {
    const months = unit === 'm' ? n : 12 * n
    if (v.kind === 'date') return { kind: 'date', date: addMonths(v.date, months) }
    const w = wall(v.ms, zone)
    const d = addMonths(w, months)
    return { kind: 'datetime', ms: instantOf({ ...w, ...d }, zone, v.ms % 1000) }
  }
  if (unit === 'd' || unit === 'w') {
    const days = n * (unit === 'w' ? 7 : 1)
    if (v.kind === 'date') return { kind: 'date', date: addDays(v.date, days) }
    const w = wall(v.ms, zone) // the same wall-clock time that many days away
    return { kind: 'datetime', ms: instantOf({ ...w, ...addDays(w, days) }, zone, v.ms % 1000) }
  }
  const step = unit === 'h' ? 3600e3 : unit === 'min' ? 60e3 : 1e3
  if (v.kind !== 'datetime') throw new Error('a date is made a date-time before moving it by hours')
  return { kind: 'datetime', ms: v.ms + n * step }
}

function shift (v: Value, ph: Placeholder, zone: string): Value {
  if (!ph.sign || v.kind === 'text') return v
  const n = ph.sign === '+' ? ph.amount : -ph.amount
  const unit = ph.unit || 'd'
  let value = v
  if (['h', 'min', 's'].includes(unit) && value.kind === 'date') {
    value = { kind: 'datetime', ms: instantOf({ ...value.date, H: 0, M: 0, S: 0 }, zone) }
  }
  try {
    const r = move(value, n, unit, zone)
    if (r.kind === 'datetime' && (!Number.isFinite(r.ms) || Math.abs(r.ms) > 253402300799000)) throw new RangeError('range')
    return r
  } catch (e) {
    if (e instanceof PlaceholderError && !String(e.message).includes('out of range')) throw e
    throw new PlaceholderError(`${text(ph)}: the date goes out of range`)
  }
}

function evaluate (ph: Placeholder, ctx: Ctx): Value {
  let name = ph.name
  if (ctx.output) {
    if (OUTPUT_TEXT.includes(name)) {
      const value = ctx.variables[name]
      if (!value) {
        if (name === 'run_id') throw new PlaceholderError('{run_id} is only known when the flow runs')
        throw new PlaceholderError(`unknown placeholder ${text(ph)}`)
      }
      return value
    }
    name = OUTPUT_ALIASES[name] || name
    if (!BUILTINS.has(name)) throw unknown(ph, true)
  }
  if (!BUILTINS.has(name)) {
    const value = ctx.variables[name]
    if (!value) throw new PlaceholderError(`unknown placeholder ${text(ph)}`)
    return value.kind === 'text' ? value : shift(value, ph, ctx.zone)
  }
  return shift(base(name, ctx), ph, ctx.zone)
}

// ── formatting ────────────────────────────────────────────────────────────────
const pad = (n: number, w = 2, ch = '0') => String(n).padStart(w, ch)
const DAYS = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday']
const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December']

function isoWeek (c: CivilDate) {
  const t = new Date(Date.UTC(c.y, c.m - 1, c.d))
  const dow = (t.getUTCDay() + 6) % 7
  t.setUTCDate(t.getUTCDate() - dow + 3) // the Thursday of this week
  const year = t.getUTCFullYear()
  const first = new Date(Date.UTC(year, 0, 4))
  const week = 1 + Math.round(((t.getTime() - first.getTime()) / 86400e3 - 3 + ((first.getUTCDay() + 6) % 7)) / 7)
  return { year, week }
}

function offsetText (seconds: number, colon: boolean) {
  const sign = seconds < 0 ? '-' : '+'
  const a = Math.abs(seconds)
  return `${sign}${pad(Math.floor(a / 3600))}${colon ? ':' : ''}${pad(Math.floor((a % 3600) / 60))}`
}

/** strftime with the C library's directives (C locale), on a date or a date-time in a zone. */
function strftime (fmt: string, v: Value, zone: string): string {
  const isDate = v.kind === 'date'
  const c: Civil = v.kind === 'date' ? { ...v.date, H: 0, M: 0, S: 0 } : wall((v as { ms: number }).ms, zone)
  const dow = new Date(Date.UTC(c.y, c.m - 1, c.d)).getUTCDay()
  const doy = (Date.UTC(c.y, c.m - 1, c.d) - Date.UTC(c.y, 0, 1)) / 86400e3 + 1
  const off = isDate ? null : offsetAt((v as { ms: number }).ms, zone)
  const num = (n: number, w: number, flag: string, space = false) =>
    flag === '-' ? String(n) : flag === '_' || space ? pad(n, w, ' ') : pad(n, w)
  const h12 = c.H % 12 === 0 ? 12 : c.H % 12
  const one = (k: string, flag: string): string => {
    switch (k) {
      case 'a': return DAYS[dow]!.slice(0, 3)
      case 'A': return DAYS[dow]!
      case 'b': case 'h': return MONTHS[c.m - 1]!.slice(0, 3)
      case 'B': return MONTHS[c.m - 1]!
      case 'c': return `${one('a', '')} ${one('b', '')} ${one('e', '')} ${one('T', '')} ${c.y}`
      case 'd': return num(c.d, 2, flag)
      case 'D': return `${one('m', '')}/${one('d', '')}/${one('y', '')}`
      case 'e': return num(c.d, 2, flag, true)
      case 'F': return `${pad(c.y, 4)}-${pad(c.m)}-${pad(c.d)}`
      case 'g': return num(isoWeek(c).year % 100, 2, flag)
      case 'G': return String(isoWeek(c).year)
      case 'H': return num(c.H, 2, flag)
      case 'I': return num(h12, 2, flag)
      case 'j': return num(doy, 3, flag)
      case 'k': return num(c.H, 2, flag, true)
      case 'l': return num(h12, 2, flag, true)
      case 'm': return num(c.m, 2, flag)
      case 'M': return num(c.M, 2, flag)
      case 'p': return c.H < 12 ? 'AM' : 'PM'
      case 'P': return c.H < 12 ? 'am' : 'pm'
      case 'r': return `${one('I', '')}:${one('M', '')}:${one('S', '')} ${one('p', '')}`
      case 'R': return `${one('H', '')}:${one('M', '')}`
      case 'S': return num(c.S, 2, flag)
      case 'T': return `${one('H', '')}:${one('M', '')}:${one('S', '')}`
      case 'u': return String(dow === 0 ? 7 : dow)
      case 'U': return num(Math.floor((doy - 1 + 7 - dow) / 7), 2, flag)
      case 'V': return num(isoWeek(c).week, 2, flag)
      case 'w': return String(dow)
      case 'W': return num(Math.floor((doy - 1 + 7 - ((dow + 6) % 7)) / 7), 2, flag)
      case 'x': return one('D', '')
      case 'X': return one('T', '')
      case 'y': return num(c.y % 100, 2, flag)
      case 'Y': return String(c.y)
      case 'z': return off === null ? '' : offsetText(off, false)
      case 'Z': {
        if (off === null) return ''
        if (zone === 'UTC') return 'UTC'
        const p = new Intl.DateTimeFormat('en-GB', { timeZone: zone, timeZoneName: 'short' }).formatToParts(new Date((v as { ms: number }).ms))
        return p.find(x => x.type === 'timeZoneName')?.value || offsetText(off, false)
      }
      case '%': return '%'
    }
    throw new PlaceholderError(`unknown strftime directive %${k}`)
  }
  return fmt.replace(STRFTIME, d => one(d[d.length - 1]!, d.length === 3 ? d[1]! : ''))
}

function iso (v: Value, zone: string): string {
  if (v.kind === 'date') return `${pad(v.date.y, 4)}-${pad(v.date.m)}-${pad(v.date.d)}`
  if (v.kind === 'text') return v.value
  const c = wall(v.ms, zone)
  return `${pad(c.y, 4)}-${pad(c.m)}-${pad(c.d)}T${pad(c.H)}:${pad(c.M)}:${pad(c.S)}${offsetText(offsetAt(v.ms, zone), true)}`
}

function formatValue (v: Value, ph: Placeholder, zone: string, output: boolean): string {
  if (v.kind === 'text') return v.value
  const fmt = ph.fmt || (output ? ALIAS_DEFAULT_FORMAT[ph.name] || '' : '')
  if (fmt === 'epoch' || fmt === 'epoch_ms') {
    const ms = v.kind === 'datetime' ? v.ms : instantOf({ ...v.date, H: 0, M: 0, S: 0 }, zone)
    return fmt === 'epoch_ms' ? String(Math.trunc(ms)) : String(Math.trunc(ms / 1000))
  }
  if (!fmt || fmt === 'iso') return iso(v, zone)
  checkFormat({ ...ph, fmt })
  return strftime(isStrftime(fmt) ? fmt : readableToStrftime(fmt, text(ph)), v, zone)
}

/** Every placeholder resolved; `quote` is applied to the values only. */
export function render (template: string, ctx: Ctx, quote?: (v: string) => string): string {
  checkZone(ctx.zone)
  return parse(template).map((part) => {
    if (typeof part === 'string') return part
    const value = formatValue(evaluate(part, ctx), part, ctx.zone, !!ctx.output)
    return quote ? quote(value) : value
  }).join('')
}

// ── outputs ───────────────────────────────────────────────────────────────────
export function safeFlowName (flow: string): string {
  return (flow || '').replace(/[^\p{L}\p{N}_.-]+/gu, '_').replace(/^[._]+|[._]+$/g, '') || 'flow'
}
const bytes = (s: string) => new TextEncoder().encode(s).length
function noControl (t: string, what: string) {
  // eslint-disable-next-line no-control-regex
  if (/[\x00-\x1f\x7f]/.test(t)) throw new PlaceholderError(`the ${what} cannot hold a control character (a line break, a tab…)`)
}

/** An Output's template: `file` (no folder from a value), `path` (levels allowed, never `..`), `text` (a subject). */
export function renderOutput (template: string, ctx: Ctx, kind: 'file' | 'path' | 'text', what: string): string {
  const octx: Ctx = { ...ctx, output: true }
  if (kind === 'text') {
    const t = render(template, octx)
    if (t.includes('\r') || t.includes('\n')) throw new PlaceholderError(`the ${what} cannot hold a line break`)
    return t
  }
  const flow = octx.variables.flow
  const named: Ctx = { ...octx, variables: { ...octx.variables, flow: { kind: 'text', value: safeFlowName(flow?.kind === 'text' ? flow.value : '') } } }
  if (kind === 'file') {
    const name = render(template, named, (value) => {
      if (value.includes('/') || value.includes('\\')) {
        throw new PlaceholderError(`a placeholder puts a '/' or '\\' in the ${what} ('${value}'): a ${what} cannot hold a folder — ` +
          'choose a format without slashes, or put the folder in its own field')
      }
      return value
    })
    noControl(name, what)
    if (bytes(name) > MAX_NAME_BYTES) throw new PlaceholderError(`the ${what} is longer than ${MAX_NAME_BYTES} bytes (${bytes(name)}): shorten it`)
    return name
  }
  const rendered = render(template, named)
  noControl(rendered, what)
  const segments = rendered.replace(/\\/g, '/').split('/')
  if (segments.some(s => s === '..' || s === '.')) {
    throw new PlaceholderError(`'..' and '.' are not allowed in the ${what} ('${rendered}'): write the folder you mean`)
  }
  const long = segments.find(s => bytes(s) > MAX_NAME_BYTES)
  if (long !== undefined) throw new PlaceholderError(`a level of the ${what} is longer than ${MAX_NAME_BYTES} bytes: shorten it ('${long.slice(0, 40)}…')`)
  return rendered
}

/** The values of `day = today-N .. today step 1d`: the N days before today, today excluded. */
export function lastDays (now: Date, zone: string, n: number): CivilDate[] {
  const t = today({ now, zone, variables: {} })
  return Array.from({ length: n }, (_, i) => addDays(t, -n + i))
}
