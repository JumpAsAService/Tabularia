// The reports' expression language in the editor: its tokens (for the highlighting), its
// functions with their signatures (for the completion), and a check of the grammar and
// the names done here while the person types. The language is the engine's
// (backend/app/reports/expressions.py), which alone judges an expression: what is checked
// here is what can be told without the server — a text or a bracket not closed, an
// unknown function, a wrong number of arguments, a field the dataset does not have, a
// parameter the report does not have. Types, scopes and formats are checked by the
// server when the report is saved.
import type { DDefinition } from '~/composables/reportDesignModel'

export type TokenKind = 'num' | 'str' | 'field' | 'ph' | 'name' | 'fn' | 'kw' | 'op' | 'punct' | 'space' | 'bad'
export interface Token { kind: TokenKind; text: string; pos: number; end: number; value?: string }

export interface ExprError { pos: number; length: number; code: string; args?: Record<string, string | number> }

const NUMBER = /(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?/y
const NAME = /[A-Za-z_][A-Za-z0-9_]*/y
const OPERATORS = ['<=', '>=', '<>', '=', '<', '>', '&', '+', '-', '*', '/']
const KEYWORDS = new Set(['AND', 'OR', 'NOT', 'TRUE', 'FALSE', 'NULL'])
const HINTS: Record<string, string> = { '!=': 'notEqual', '==': 'doubleEqual', '&&': 'and', '||': 'or', '^': 'power', '%': 'percent', "'": 'quote' }

// ── the functions ────────────────────────────────────────────────────────────
export type FnGroup = 'aggregate' | 'logic' | 'math' | 'text' | 'date' | 'format' | 'report'
export interface FnInfo { name: string; sig: string; group: FnGroup; min: number; max: number | null }

const F = (name: string, sig: string, group: FnGroup, min: number, max: number | null): FnInfo => ({ name, sig, group, min, max })

/** Every function of the language, its signature as the completion shows it and its
 *  arguments (backend/app/reports/expressions.py, ARITY). The descriptions are the
 *  interface's (reportDesigner.fn.<NAME>). */
export const FUNCTIONS: FnInfo[] = [
  F('SUM', 'SUM(value, [dataset])', 'aggregate', 1, 2),
  F('AVG', 'AVG(value, [dataset])', 'aggregate', 1, 2),
  F('MIN', 'MIN(value, [dataset])', 'aggregate', 1, 2),
  F('MAX', 'MAX(value, [dataset])', 'aggregate', 1, 2),
  F('COUNT', 'COUNT([value], [dataset])', 'aggregate', 0, 2),
  F('COUNTDISTINCT', 'COUNTDISTINCT(value, [dataset])', 'aggregate', 1, 2),
  F('FIRST', 'FIRST(value, [dataset])', 'aggregate', 1, 2),
  F('LAST', 'LAST(value, [dataset])', 'aggregate', 1, 2),
  F('PERCENT', 'PERCENT(aggregate)', 'aggregate', 1, 1),
  F('RUNNING', 'RUNNING(aggregate)', 'aggregate', 1, 1),
  F('ROWNUMBER', 'ROWNUMBER()', 'aggregate', 0, 0),
  F('IF', 'IF(condition, then, else)', 'logic', 2, 3),
  F('IFS', 'IFS(condition1, value1, …, else)', 'logic', 2, null),
  F('ISNULL', 'ISNULL(value)', 'logic', 1, 1),
  F('COALESCE', 'COALESCE(value1, value2, …)', 'logic', 1, null),
  F('ROUND', 'ROUND(number, decimals)', 'math', 1, 2),
  F('ABS', 'ABS(number)', 'math', 1, 1),
  F('FLOOR', 'FLOOR(number)', 'math', 1, 1),
  F('CEILING', 'CEILING(number)', 'math', 1, 1),
  F('POWER', 'POWER(number, exponent)', 'math', 2, 2),
  F('UPPER', 'UPPER(text)', 'text', 1, 1),
  F('LOWER', 'LOWER(text)', 'text', 1, 1),
  F('LEFT', 'LEFT(text, count)', 'text', 2, 2),
  F('RIGHT', 'RIGHT(text, count)', 'text', 2, 2),
  F('MID', 'MID(text, start, count)', 'text', 3, 3),
  F('LEN', 'LEN(text)', 'text', 1, 1),
  F('TRIM', 'TRIM(text)', 'text', 1, 1),
  F('REPLACE', 'REPLACE(text, old, new)', 'text', 3, 3),
  F('CONTAINS', 'CONTAINS(text, part)', 'text', 2, 2),
  F('STARTSWITH', 'STARTSWITH(text, start)', 'text', 2, 2),
  F('TEXT', 'TEXT(value)', 'text', 1, 1),
  F('CONCAT', 'CONCAT(value1, value2, …)', 'text', 1, null),
  F('DATE', 'DATE(year, month, day)', 'date', 3, 3),
  F('YEAR', 'YEAR(date)', 'date', 1, 1),
  F('MONTH', 'MONTH(date)', 'date', 1, 1),
  F('DAY', 'DAY(date)', 'date', 1, 1),
  F('WEEKDAY', 'WEEKDAY(date)', 'date', 1, 1),
  F('DATEADD', 'DATEADD("d"|"w"|"m"|"y", n, date)', 'date', 3, 3),
  F('DATEDIFF', 'DATEDIFF("d"|"w"|"m"|"y", start, end)', 'date', 3, 3),
  F('TODAY', 'TODAY()', 'date', 0, 0),
  F('NOW', 'NOW()', 'date', 0, 0),
  F('FORMAT', 'FORMAT(value, "#,##0.00")', 'format', 2, 2),
  F('PAGE', 'PAGE()', 'report', 0, 0),
  F('PAGES', 'PAGES()', 'report', 0, 0),
  F('PARAM', 'PARAM("name")', 'report', 1, 1),
  F('REPORT', 'REPORT()', 'report', 0, 0),
]
export const FN = new Map(FUNCTIONS.map((f) => [f.name, f]))
export const AGGREGATES = new Set(['SUM', 'AVG', 'MIN', 'MAX', 'COUNT', 'COUNTDISTINCT', 'FIRST', 'LAST'])

// ── tokens ───────────────────────────────────────────────────────────────────
/** The tokens of a text, for the highlighting: never throws, an unclosed text or field
 *  runs to the end. Spaces are tokens too, so the parts put back give the text. */
export function highlightTokens(text: string): Token[] {
  const out: Token[] = []
  let i = 0
  const n = text.length
  const push = (kind: TokenKind, end: number, value?: string) => {
    out.push({ kind, text: text.slice(i, end), pos: i, end, value })
    i = end
  }
  while (i < n) {
    const c = text[i]
    if (/\s/.test(c)) {
      let j = i
      while (j < n && /\s/.test(text[j])) j++
      push('space', j)
      continue
    }
    if (/\d/.test(c) || (c === '.' && /\d/.test(text[i + 1] ?? ''))) {
      NUMBER.lastIndex = i
      const m = NUMBER.exec(text)
      push('num', m ? i + m[0].length : i + 1)
      continue
    }
    if (c === '"') {
      let j = i + 1
      while (j < n) {
        if (text[j] === '"') {
          if (text[j + 1] === '"') { j += 2; continue }
          j++
          break
        }
        j++
      }
      push('str', Math.min(j, n))
      continue
    }
    if (c === '[') {
      let j = i + 1
      while (j < n) {
        if (text[j] === ']') {
          if (text[j + 1] === ']') { j += 2; continue }
          j++
          break
        }
        j++
      }
      push('field', Math.min(j, n))
      continue
    }
    if (c === '{') {
      const j = text.indexOf('}', i + 1)
      push('ph', j < 0 ? n : j + 1)
      continue
    }
    NAME.lastIndex = i
    const m = NAME.exec(text)
    if (m) {
      const word = m[0]
      let k = i + word.length
      while (k < n && /\s/.test(text[k])) k++
      const kind: TokenKind = KEYWORDS.has(word.toUpperCase()) ? 'kw' : text[k] === '(' ? 'fn' : 'name'
      push(kind, i + word.length)
      continue
    }
    const op = OPERATORS.find((o) => text.startsWith(o, i))
    if (op) {
      push('op', i + op.length)
      continue
    }
    if ('(),'.includes(c)) {
      push('punct', i + 1)
      continue
    }
    push('bad', i + 1)
  }
  return out
}

// ── the grammar, checked ─────────────────────────────────────────────────────
interface PTok { kind: 'num' | 'str' | 'field' | 'ph' | 'name' | 'op' | '(' | ')' | ',' | 'end'; value: string; pos: number; end: number }

class Stop extends Error {
  err: ExprError
  constructor(err: ExprError) {
    super(err.code)
    this.err = err
  }
}
const stop = (code: string, pos: number, length: number, args?: ExprError['args']): never => {
  throw new Stop({ code, pos, length, args })
}

function lex(text: string): PTok[] {
  const out: PTok[] = []
  let i = 0
  const n = text.length
  while (i < n) {
    const c = text[i]
    if (/\s/.test(c)) { i++; continue }
    if (/\d/.test(c) || (c === '.' && /\d/.test(text[i + 1] ?? ''))) {
      NUMBER.lastIndex = i
      const m = NUMBER.exec(text)!
      out.push({ kind: 'num', value: m[0], pos: i, end: i + m[0].length })
      i += m[0].length
      continue
    }
    if (c === '"') {
      let j = i + 1
      let buf = ''
      for (;;) {
        if (j >= n) stop('textOpen', i, 1)
        if (text[j] === '"') {
          if (text[j + 1] === '"') { buf += '"'; j += 2; continue }
          break
        }
        buf += text[j++]
      }
      out.push({ kind: 'str', value: buf, pos: i, end: j + 1 })
      i = j + 1
      continue
    }
    if (c === '[') {
      let j = i + 1
      let buf = ''
      for (;;) {
        if (j >= n) stop('fieldOpen', i, 1)
        if (text[j] === ']') {
          if (text[j + 1] === ']') { buf += ']'; j += 2; continue }
          break
        }
        buf += text[j++]
      }
      if (!buf.trim()) stop('fieldEmpty', i, 2)
      out.push({ kind: 'field', value: buf, pos: i, end: j + 1 })
      i = j + 1
      continue
    }
    if (c === '{') {
      const j = text.indexOf('}', i + 1)
      if (j < 0) stop('placeholderOpen', i, 1)
      out.push({ kind: 'ph', value: text.slice(i + 1, j), pos: i, end: j + 1 })
      i = j + 1
      continue
    }
    NAME.lastIndex = i
    const m = NAME.exec(text)
    if (m) {
      out.push({ kind: 'name', value: m[0], pos: i, end: i + m[0].length })
      i += m[0].length
      continue
    }
    const hint = Object.keys(HINTS).find((h) => text.startsWith(h, i))
    if (hint) stop(`hint.${HINTS[hint]}`, i, hint.length, { what: hint })
    const op = OPERATORS.find((o) => text.startsWith(o, i))
    if (op) {
      out.push({ kind: 'op', value: op, pos: i, end: i + op.length })
      i += op.length
      continue
    }
    if ('(),'.includes(c)) {
      out.push({ kind: c as '(' | ')' | ',', value: c, pos: i, end: i + 1 })
      i++
      continue
    }
    stop('unexpectedChar', i, 1, { what: c })
  }
  out.push({ kind: 'end', value: '', pos: n, end: n })
  return out
}

export interface CheckContext {
  /** the fields of the expression's dataset; null = not known (any name passes) */
  fields: string[] | null
  params: string[]
}

const COMPARE = new Set(['=', '<>', '<', '>', '<=', '>='])
const NAMED_SCOPE = /\b(SUM|AVG|MIN|MAX|COUNT|COUNTDISTINCT|FIRST|LAST)\s*\([^()]*,\s*"[^"]*"\s*\)/i

/** The first mistake of an expression this side can see, or null. A leading «=» is
 *  allowed (a text's expression starts with it): positions stay those of `text`. */
export function checkExpression(text: string, ctx: CheckContext): ExprError | null {
  // an aggregate of another dataset (SUM([x], "orders")) reads that dataset's fields: the
  // server checks them, this side does not know which they are
  if (NAMED_SCOPE.test(text)) ctx = { ...ctx, fields: null }
  let src = text
  const lead = /^\s*=/.exec(src)
  if (lead) src = src.slice(0, lead[0].length - 1) + ' ' + src.slice(lead[0].length)
  try {
    const toks = lex(src)
    let i = 0
    let depth = 0
    const tok = () => toks[i]
    const take = () => toks[i++]
    const kw = (w: string) => tok().kind === 'name' && tok().value.toUpperCase() === w

    const or = (): void => { and(); while (kw('OR')) { take(); and() } }
    const and = (): void => { not(); while (kw('AND')) { take(); not() } }
    const not = (): void => { if (kw('NOT')) { take(); nested(not) } else cmp() }
    const cmp = (): void => {
      concat()
      if (tok().kind === 'op' && COMPARE.has(tok().value)) {
        take()
        concat()
        if (tok().kind === 'op' && COMPARE.has(tok().value)) stop('oneComparison', tok().pos, tok().end - tok().pos)
      }
    }
    const concat = (): void => { add(); while (tok().kind === 'op' && tok().value === '&') { take(); add() } }
    const add = (): void => { mul(); while (tok().kind === 'op' && '+-'.includes(tok().value)) { take(); mul() } }
    const mul = (): void => { unary(); while (tok().kind === 'op' && '*/'.includes(tok().value)) { take(); unary() } }
    const unary = (): void => { if (tok().kind === 'op' && tok().value === '-') { take(); nested(unary) } else primary() }
    const nested = (fn: () => void) => {
      if (++depth > 64) stop('tooDeep', tok().pos, 1)
      try { fn() } finally { depth-- }
    }
    const primary = (): void => {
      const t = tok()
      if (t.kind === 'num' || t.kind === 'str' || t.kind === 'ph') { take(); return }
      if (t.kind === 'field') {
        take()
        if (ctx.fields && !ctx.fields.includes(t.value)) {
          const near = closest(t.value, ctx.fields)
          stop(near ? 'noFieldNear' : 'noField', t.pos, t.end - t.pos, { name: t.value, near: near ?? '' })
        }
        return
      }
      if (t.kind === '(') {
        take()
        nested(or)
        if (tok().kind !== ')') stop('parenOpen', t.pos, 1)
        take()
        return
      }
      if (t.kind === 'name') {
        const word = t.value.toUpperCase()
        if (['TRUE', 'FALSE', 'NULL'].includes(word)) { take(); return }
        if (KEYWORDS.has(word)) stop('keywordAlone', t.pos, t.end - t.pos, { what: t.value })
        take()
        if (tok().kind !== '(') stop('unknownName', t.pos, t.end - t.pos, { name: t.value })
        take()
        const args: PTok[] = []
        let count = 0
        if (tok().kind !== ')') {
          for (;;) {
            args.push(tok())
            nested(or)
            count++
            if (tok().kind === ',') { take(); continue }
            break
          }
        }
        if (tok().kind !== ')') stop('callOpen', t.pos, t.end - t.pos, { name: word })
        const close = take()
        const info = FN.get(word)
        if (!info) {
          const near = closest(word, FUNCTIONS.map((f) => f.name))
          stop(near ? 'unknownFunctionNear' : 'unknownFunction', t.pos, t.end - t.pos, { name: word, near: near ?? '' })
        }
        if (count < info!.min || (info!.max != null && count > info!.max)) {
          stop('arity', t.pos, close.end - t.pos, { name: word, usage: info!.sig, given: count })
        }
        if (word === 'PARAM' && args[0]) {
          const a = args[0]
          if (a.kind !== 'str') stop('paramQuoted', a.pos, a.end - a.pos)
          else if (!ctx.params.includes(a.value)) {
            stop('noParam', a.pos, a.end - a.pos, { name: a.value })
          }
        }
        return
      }
      if (t.kind === 'end') stop('endsEarly', t.pos, 0)
      stop('unexpected', t.pos, t.end - t.pos, { what: src.slice(t.pos, t.end) })
    }

    if (tok().kind === 'end') stop('empty', 0, 0)
    or()
    const t = tok()
    if (t.kind !== 'end') stop(t.kind === ')' ? 'closeAlone' : 'missingOperator', t.pos, t.end - t.pos, { what: src.slice(t.pos, t.end) })
    return null
  } catch (e) {
    if (e instanceof Stop) return e.err
    throw e
  }
}

// what the check says of an expression not finished yet: something open at its end, nothing yet where a value goes
const UNFINISHED = new Set(['textOpen', 'fieldOpen', 'fieldEmpty', 'placeholderOpen', 'parenOpen', 'callOpen', 'endsEarly', 'empty'])
const NAME_MISTAKES = new Set(['unknownName', 'keywordAlone', 'missingOperator'])

/** Whether a mistake only says the expression is still being written, with the caret where
 *  it is: not finished yet, a name under the caret (`SU`, about to be `SUM(`), or the caret
 *  inside what the mistake is about. A person who stops writing is told (at the blur). */
export function stillWriting(err: ExprError, caret: number): boolean {
  if (UNFINISHED.has(err.code)) return true
  const end = err.pos + err.length
  return NAME_MISTAKES.has(err.code) ? caret >= err.pos && caret <= end : caret > err.pos && caret < end
}

/** The nearest name, when one is close enough to be the one meant. */
function closest(name: string, names: string[]): string | null {
  const low = name.toLowerCase()
  let best: string | null = null
  let bestScore = Infinity
  for (const n of names) {
    const d = distance(low, n.toLowerCase())
    if (d < bestScore) { best = n; bestScore = d }
  }
  return best != null && bestScore <= Math.max(1, Math.floor(name.length / 3)) ? best : null
}

function distance(a: string, b: string): number {
  const row = Array.from({ length: b.length + 1 }, (_, j) => j)
  for (let i = 1; i <= a.length; i++) {
    let prev = row[0]
    row[0] = i
    for (let j = 1; j <= b.length; j++) {
      const tmp = row[j]
      row[j] = Math.min(row[j] + 1, row[j - 1] + 1, prev + (a[i - 1] === b[j - 1] ? 0 : 1))
      prev = tmp
    }
  }
  return row[b.length]
}

/** An expression cut at its top-level `&`: the texts in quotes as they print, the rest as
 *  expressions — what the design view draws for a text (`"Page " & PAGE()`). */
export function concatParts(expr: string): { lit: boolean; text: string }[] {
  const toks = highlightTokens(expr.replace(/^\s*=/, ''))
  const parts: { lit: boolean; text: string }[] = []
  let depth = 0
  let buf: Token[] = []
  const flush = () => {
    const real = buf.filter((t) => t.kind !== 'space')
    if (real.length === 1 && real[0].kind === 'str' && real[0].text.length >= 2 && real[0].text.endsWith('"')) {
      parts.push({ lit: true, text: real[0].text.slice(1, -1).replaceAll('""', '"') })
    } else if (real.length) {
      parts.push({ lit: false, text: buf.map((t) => t.text).join('').trim() })
    }
    buf = []
  }
  for (const t of toks) {
    if (t.kind === 'punct' && t.text === '(') depth++
    else if (t.kind === 'punct' && t.text === ')') depth--
    if (depth === 0 && t.kind === 'op' && t.text === '&') flush()
    else buf.push(t)
  }
  flush()
  return parts
}

// ── renames ──────────────────────────────────────────────────────────────────
/** An expression with a dataset or a parameter renamed where the language names them: a
 *  dataset as the last argument of an aggregate (`SUM([amount], "orders")`, `COUNT("orders")`),
 *  a parameter in `PARAM("region")`. Texts elsewhere, fields and the rest of the expression
 *  are left as written, spaces and capitals included. */
export function renameInExpression(expr: string, what: { dataset?: [string, string]; param?: [string, string] }): string {
  const toks = highlightTokens(expr)
  const frames: { name: string | null; args: Token[][] }[] = []
  const swap = new Map<Token, string>()
  const quoted = (s: string) => `"${s.replaceAll('"', '""')}"`
  const textOf = (t: Token) => (t.kind === 'str' && t.text.length >= 2 && t.text.endsWith('"') ? t.text.slice(1, -1).replaceAll('""', '"') : null)
  function judge(f: { name: string | null; args: Token[][] }) {
    const last = f.args[f.args.length - 1]
    if (last.length !== 1) return
    const v = textOf(last[0])
    if (v == null) return
    const scoped = AGGREGATES.has(f.name ?? '') && (f.args.length >= 2 || f.name === 'COUNT')
    if (what.dataset && scoped && v === what.dataset[0]) swap.set(last[0], quoted(what.dataset[1]))
    if (what.param && f.name === 'PARAM' && f.args.length === 1 && v === what.param[0]) swap.set(last[0], quoted(what.param[1]))
  }
  let lastFn: string | null = null
  for (const t of toks) {
    if (t.kind === 'space') continue
    const top = frames[frames.length - 1]
    if (t.kind === 'punct' && t.text === '(') {
      top?.args[top.args.length - 1].push(t)
      frames.push({ name: lastFn, args: [[]] })
    } else if (t.kind === 'punct' && t.text === ')') {
      const f = frames.pop()
      if (f) judge(f)
      const outer = frames[frames.length - 1]
      outer?.args[outer.args.length - 1].push(t)
    } else if (t.kind === 'punct' && t.text === ',' && top) {
      top.args.push([])
    } else {
      top?.args[top.args.length - 1].push(t)
    }
    lastFn = t.kind === 'fn' ? t.text.toUpperCase() : null
  }
  return swap.size ? toks.map((t) => swap.get(t) ?? t.text).join('') : expr
}

// what is text, never an expression: a header, a title, a series' name, a KPI's label…
const NOT_EXPRESSIONS = new Set(['id', 'type', 'kind', 'asset', 'fit', 'dataset', 'header', 'title', 'name', 'color',
  'background', 'align', 'valign', 'weight', 'page_break', 'sort', 'dir', 'as', 'axis', 'format'])

/** The expressions of the whole report with a dataset or a parameter renamed where the
 *  language names them (renameInExpression): the items of every band, the calculated
 *  fields, the choices of the parameters. Changed in place. */
export function renameEverywhere(def: DDefinition, what: { dataset?: [string, string]; param?: [string, string] }) {
  const walk = (obj: any, kpi = false) => {
    if (Array.isArray(obj)) {
      obj.forEach((x, i) => { if (typeof x === 'string') obj[i] = renameInExpression(x, what); else walk(x) })
      return
    }
    if (!obj || typeof obj !== 'object') return
    for (const [k, v] of Object.entries(obj)) {
      if (typeof v === 'string') {
        if (!NOT_EXPRESSIONS.has(k) && !(kpi && k === 'label')) obj[k] = renameInExpression(v, what)
      } else if (k !== 'style') {
        walk(v)
      }
    }
  }
  for (const b of ['page_header', 'body', 'page_footer'] as const) {
    for (const it of def.sections[b].items) walk(it, it.type === 'kpi')
  }
  for (const d of def.datasets) for (const c of d.calculated ?? []) c.expr = renameInExpression(c.expr, what)
  for (const p of def.parameters) {
    if (p.choices && 'dataset' in p.choices) {
      p.choices.value = renameInExpression(p.choices.value, what)
      if (p.choices.label) p.choices.label = renameInExpression(p.choices.label, what)
    }
  }
}

// ── where the caret is: what to complete, which call it is inside ────────────
export type CompletionContext =
  | { kind: 'field'; from: number; prefix: string }
  | { kind: 'param'; from: number; prefix: string }
  | { kind: 'name'; from: number; prefix: string }
  | { kind: 'none' }

/** What the person is typing at `caret`: a field after an open `[`, a parameter's name
 *  inside `PARAM("`, a function's name; nothing inside a text or a placeholder. */
export function completionAt(text: string, caret: number): CompletionContext {
  const before = text.slice(0, caret)
  let inStr = false
  let strStart = -1
  let inField = false
  let fieldStart = -1
  let inPh = false
  for (let i = 0; i < before.length; i++) {
    const c = before[i]
    if (inStr) {
      if (c === '"') {
        if (before[i + 1] === '"') { i++; continue }
        inStr = false
      }
    } else if (inField) {
      if (c === ']') {
        if (before[i + 1] === ']') { i++; continue }
        inField = false
      }
    } else if (inPh) {
      if (c === '}') inPh = false
    } else if (c === '"') { inStr = true; strStart = i } else if (c === '[') { inField = true; fieldStart = i } else if (c === '{') inPh = true
  }
  if (inField) return { kind: 'field', from: fieldStart, prefix: before.slice(fieldStart + 1).replaceAll(']]', ']') }
  if (inStr) {
    if (/PARAM\s*\(\s*$/i.test(before.slice(0, strStart))) return { kind: 'param', from: strStart, prefix: before.slice(strStart + 1) }
    return { kind: 'none' }
  }
  if (inPh) return { kind: 'none' }
  const m = /[A-Za-z_][A-Za-z0-9_]*$/.exec(before)
  if (m && !/[\d.]$/.test(before.slice(0, m.index))) return { kind: 'name', from: m.index, prefix: m[0] }
  return { kind: 'none' }
}

/** The function call the caret is inside (the innermost), and which argument: the
 *  signature shown under the field. */
export function callAt(text: string, caret: number): { fn: FnInfo; arg: number } | null {
  const toks = highlightTokens(text.slice(0, caret))
  const stack: { name: string | null; arg: number }[] = []
  let lastFn: string | null = null
  for (const t of toks) {
    if (t.kind === 'fn') lastFn = t.text.toUpperCase()
    else if (t.kind === 'punct' && t.text === '(') { stack.push({ name: lastFn, arg: 0 }); lastFn = null } else if (t.kind === 'punct' && t.text === ')') stack.pop()
    else if (t.kind === 'punct' && t.text === ',' && stack.length) stack[stack.length - 1].arg++
    else if (t.kind !== 'space') lastFn = null
  }
  for (let i = stack.length - 1; i >= 0; i--) {
    const info = stack[i].name ? FN.get(stack[i].name!) : undefined
    if (info) return { fn: info, arg: stack[i].arg }
  }
  return null
}
