// The expression language of the Bayesian model editor as the browser reads it: the lexer, the
// parser, the syntax tree and Format, a mirror of the gateway's (gateway-go/internal/bayesexpr,
// docs/specs/bayes-expression-language.md). The gateway is the one authority; this copy gives
// the person feedback while typing, and the tests make it give the same answer as the Go reading
// on every syntactic case of the shared corpus: the same error code, position, length and
// arguments, the same Format, the same JSON form of the tree.
//
// The input is compared by character code, never by a pattern: no regular expression class
// (a Unicode digit class matches Arabic-Indic digits, a Unicode space class matches U+00A0).
// The limits, the hints and the confusables come from the catalogue, never copied here.
//
// Pure functions, nothing of Nuxt or the DOM. Every export starts with `bayes`, `BAYES_` or
// `Bayes`: a composable's exports are auto-imported everywhere.

import { BAYES_CATALOG, bayesFunction, type BayesCatalog, type BayesConfusable, type BayesHint } from '~/composables/bayesCatalog'

/** The id of an error; the browser chooses the message `bayes.expr.<code without "expr.">`. */
export type BayesExprCode =
  | 'expr.too_long' | 'expr.non_ascii' | 'expr.control'
  | 'expr.number_form' | 'expr.number_range' | 'expr.number_underflow' | 'expr.name_case'
  | 'expr.name_long' | 'expr.name_reserved' | 'expr.comment' | 'expr.hash' | 'expr.text'
  | 'expr.hint' | 'expr.symbol'
  | 'expr.empty' | 'expr.unexpected' | 'expr.unclosed' | 'expr.call_empty' | 'expr.too_deep'
  | 'expr.too_big' | 'expr.lag_argument'
  // The type checker's (bayesExprCheck.ts).
  | 'expr.unknown_name' | 'expr.time_value' | 'expr.not_number' | 'expr.index_alone' | 'expr.needs_index'
  | 'expr.wrong_plate' | 'expr.not_index' | 'expr.index_scalar' | 'expr.index_target' | 'expr.index_context'
  | 'expr.unknown_function' | 'expr.not_function' | 'expr.arity' | 'expr.lag_not_series' | 'expr.lag_k'

/** The first error of an expression; pos and len count code points (here, ASCII: code units). */
export interface BayesExprError {
  code: BayesExprCode
  pos: number
  len: number
  args?: Record<string, string>
}

/** A stretch of the input, in code points. */
export interface BayesExprSpan { pos: number, len: number }

/**
 * pos and len take in the parentheses around the node; `at` is its own span without them (set
 * only when it had some); `sub` are the spans of an indexing's two names (x, g) and of a lag's
 * name and number, for the type checker's errors. None of them is part of the JSON form.
 */
export type BayesExprNode = { pos: number, len: number, at?: BayesExprSpan, sub?: [BayesExprSpan, BayesExprSpan] } & (
  | { kind: 'num', num: number, digits: boolean }
  | { kind: 'name', name: string }
  | { kind: 'index', name: string, by: string }
  | { kind: 'call', name: string, args: BayesExprNode[] }
  | { kind: 'lag', name: string, k: number }
  | { kind: 'neg', args: [BayesExprNode] }
  | { kind: 'binary', op: '+' | '-' | '*' | '/' | '^', args: [BayesExprNode, BayesExprNode] }
)

export type BayesExprResult =
  | { ok: true, node: BayesExprNode }
  | { ok: false, error: BayesExprError }

const LAG = 'lag'

// The characters are compared by code, byte by byte.
const isLower = (c: number) => c >= 97 && c <= 122
const isUpper = (c: number) => c >= 65 && c <= 90
const isDigit = (c: number) => c >= 48 && c <= 57
const isSpace = (c: number) => c === 32 || c === 9 || c === 10 || c === 13
const isWord = (c: number) => isLower(c) || isUpper(c) || isDigit(c) || c === 95

const codePointText = (cp: number) => 'U+' + cp.toString(16).toUpperCase().padStart(4, '0')

/** The length of a string in UTF-8 bytes (a lone surrogate is U+FFFD, three bytes). */
function utf8Length (s: string): number {
  let n = 0
  for (let i = 0; i < s.length; i++) {
    const c = s.charCodeAt(i)
    if (c < 0x80) n += 1
    else if (c < 0x800) n += 2
    else if (c >= 0xd800 && c <= 0xdbff && i + 1 < s.length && s.charCodeAt(i + 1) >= 0xdc00 && s.charCodeAt(i + 1) <= 0xdfff) {
      n += 4
      i++
    } else n += 3
  }
  return n
}

class ExprFail {
  error: BayesExprError
  constructor (error: BayesExprError) {
    this.error = error
  }
}

const fail = (code: BayesExprCode, pos: number, len: number, args?: Record<string, string>): never => {
  throw new ExprFail(args ? { code, pos, len, args } : { code, pos, len })
}

type TokKind = 'eof' | 'name' | 'number' | '+' | '-' | '*' | '/' | '^' | '(' | ')' | '[' | ']' | ','

interface Token {
  kind: TokKind
  pos: number
  end: number
  num: number
  digits: boolean
}

const tok = (kind: TokKind, pos: number, end: number, num = 0, digits = false): Token => ({ kind, pos, end, num, digits })

interface Vocabulary {
  limits: Record<string, number>
  hints: BayesHint[]
  confusables: Map<number, BayesConfusable>
  lagApart: boolean
}

const vocabularies = new WeakMap<BayesCatalog, Vocabulary>()

function vocabulary (catalog: BayesCatalog): Vocabulary {
  let v = vocabularies.get(catalog)
  if (!v) {
    const confusables = new Map<number, BayesConfusable>()
    for (const c of catalog.language.confusables) {
      const cp = parseInt(c.codepoint.replace(/^U\+/, ''), 16)
      if (Number.isFinite(cp)) confusables.set(cp, c)
    }
    const lag = bayesFunction(LAG, catalog)
    v = {
      limits: catalog.limits,
      hints: catalog.language.hints,
      confusables,
      lagApart: Boolean(lag && lag.apart && lag.arguments === 2),
    }
    vocabularies.set(catalog, v)
  }
  return v
}

/** The checks before any token: the length in bytes, then every character ASCII and no control. */
function precheck (src: string, voc: Vocabulary): void {
  if (utf8Length(src) > voc.limits.max_expression_bytes) fail('expr.too_long', 0, 0)
  for (let i = 0; i < src.length; i++) {
    const c = src.charCodeAt(i)
    if (c >= 0x80) {
      let cp = src.codePointAt(i) as number
      if (cp >= 0xd800 && cp <= 0xdfff) cp = 0xfffd
      const args: Record<string, string> = { codepoint: codePointText(cp) }
      const conf = voc.confusables.get(cp)
      if (conf) {
        if (conf.direction) args.direction = 'true'
        else {
          args.char = conf.char
          args.ascii = conf.ascii
        }
      }
      fail('expr.non_ascii', i, 1, args)
    }
    if ((c < 0x20 && c !== 9 && c !== 10 && c !== 13) || c === 0x7f) {
      fail('expr.control', i, 1, { codepoint: codePointText(c) })
    }
  }
}

/** Go's strconv.FormatFloat(v, 'g', -1, 64): the shortest text that reads back to the same double. */
export function bayesFormatNumber (v: number): string {
  if (v === 0) return '0'
  const [mantissa, e] = Math.abs(v).toExponential().split('e')
  const digits = mantissa.replace('.', '')
  const exp = parseInt(e, 10)
  const sign = v < 0 ? '-' : ''
  if (exp < -4 || exp >= 6) {
    const frac = digits.length > 1 ? '.' + digits.slice(1) : ''
    const mag = Math.abs(exp)
    return `${sign}${digits[0]}${frac}e${exp < 0 ? '-' : '+'}${mag < 10 ? '0' : ''}${mag}`
  }
  if (exp >= 0) {
    if (digits.length <= exp + 1) return sign + digits + '0'.repeat(exp + 1 - digits.length)
    return `${sign}${digits.slice(0, exp + 1)}.${digits.slice(exp + 1)}`
  }
  return `${sign}0.${'0'.repeat(-exp - 1)}${digits}`
}

class Lexer {
  i = 0
  private src: string
  private voc: Vocabulary
  constructor (src: string, voc: Vocabulary) {
    this.src = src
    this.voc = voc
  }

  next (): Token {
    const { src } = this
    const n = src.length
    while (this.i < n && isSpace(src.charCodeAt(this.i))) this.i++
    if (this.i >= n) return tok('eof', n, n)
    const start = this.i
    const c = src.charCodeAt(start)
    const c1 = start + 1 < n ? src.charCodeAt(start + 1) : -1
    if (isLower(c)) return this.name(start)
    if (isUpper(c)) return this.nameCase(start)
    if (isDigit(c)) return this.number(start)
    if (c === 47 && (c1 === 47 || c1 === 42)) fail('expr.comment', start, 2)
    if (c === 35) fail('expr.hash', start, 1)
    if (c === 34 || c === 39) fail('expr.text', start, 1)
    if (c === 46 && isDigit(c1)) this.numberForm(start, start + 1, 'leading_dot')
    // The operators of other languages come before the symbols: "**" is a hint, not two "*".
    const rest = src.slice(start)
    let best: BayesHint | undefined
    for (const h of this.voc.hints) {
      if (h.token !== '' && (!best || h.token.length > best.token.length) && rest.startsWith(h.token)) best = h
    }
    if (best) fail('expr.hint', start, best.token.length, { hint: best.message, token: best.token })
    const kinds: Record<number, TokKind> = { 43: '+', 45: '-', 42: '*', 47: '/', 94: '^', 40: '(', 41: ')', 91: '[', 93: ']', 44: ',' }
    const kind = kinds[c]
    if (!kind) fail('expr.symbol', start, 1, { char: String.fromCharCode(c) })
    this.i = start + 1
    return tok(kind, start, start + 1)
  }

  private name (start: number): Token {
    const { src } = this
    const n = src.length
    let i = start
    while (i < n) {
      const c = src.charCodeAt(i)
      if (!(isLower(c) || isDigit(c) || c === 95)) break
      i++
    }
    if (i < n && isUpper(src.charCodeAt(i))) this.nameCase(start)
    if (i - start > this.voc.limits.max_name_bytes) fail('expr.name_long', start, i - start)
    if (src.slice(start, i).endsWith('__')) fail('expr.name_reserved', start, i - start)
    this.i = i
    return tok('name', start, i)
  }

  private nameCase (start: number): never {
    const { src } = this
    let end = start
    while (end < src.length && isWord(src.charCodeAt(end))) end++
    return fail('expr.name_case', start, end - start, { name: src.slice(start, end).toLowerCase() })
  }

  private number (start: number): Token {
    const { src } = this
    const n = src.length
    let i = start
    while (i < n && isDigit(src.charCodeAt(i))) i++
    let digitsOnly = true
    if (i < n && src.charCodeAt(i) === 46) {
      if (i + 1 >= n || !isDigit(src.charCodeAt(i + 1))) this.numberForm(start, i + 1, 'trailing_dot')
      digitsOnly = false
      for (i++; i < n && isDigit(src.charCodeAt(i)); i++);
    }
    if (i < n && (src.charCodeAt(i) === 101 || src.charCodeAt(i) === 69)) {
      let j = i + 1
      if (j < n && (src.charCodeAt(j) === 43 || src.charCodeAt(j) === 45)) j++
      if (j >= n || !isDigit(src.charCodeAt(j))) this.numberForm(start, j, 'exponent')
      digitsOnly = false
      for (i = j; i < n && isDigit(src.charCodeAt(i)); i++);
    }
    if (i < n) {
      const c = src.charCodeAt(i)
      if (isLower(c) || isUpper(c) || c === 95) this.numberForm(start, i + 1, 'suffix')
      else if (c === 46) this.numberForm(start, i + 1, 'second_dot')
    }
    const text = src.slice(start, i)
    if (text.length > this.voc.limits.max_number_chars) fail('expr.number_range', start, i - start, { reason: 'chars' })
    const v = Number(text)
    if (!Number.isFinite(v) || Math.abs(v) > this.voc.limits.max_abs_literal) {
      fail('expr.number_range', start, i - start, { reason: 'magnitude' })
    }
    // Number reads 1e-400 as 0 without an error.
    if (v === 0 && nonZeroMantissa(text)) fail('expr.number_underflow', start, i - start)
    this.i = i
    return tok('number', start, i, v, digitsOnly)
  }

  /** A malformed number: the span runs over the letters, digits, "_" and "." that follow `end`. */
  private numberForm (start: number, end: number, kind: string): never {
    const { src } = this
    while (end < src.length && (isWord(src.charCodeAt(end)) || src.charCodeAt(end) === 46)) end++
    return fail('expr.number_form', start, end - start, { kind })
  }
}

function nonZeroMantissa (text: string): boolean {
  for (let i = 0; i < text.length; i++) {
    const c = text.charCodeAt(i)
    if (c === 101 || c === 69) return false
    if (c >= 49 && c <= 57) return true
  }
  return false
}

const end = (n: BayesExprNode) => n.pos + n.len

function binary (op: Token, left: BayesExprNode, right: BayesExprNode): BayesExprNode {
  return { kind: 'binary', op: op.kind as '+' | '-' | '*' | '/' | '^', args: [left, right], pos: left.pos, len: end(right) - left.pos }
}

class Parser {
  private lx: Lexer
  private tok!: Token
  private depth = 0
  private nodes = 0
  private src: string
  private voc: Vocabulary

  constructor (src: string, voc: Vocabulary) {
    this.src = src
    this.voc = voc
    this.lx = new Lexer(src, voc)
  }

  input (): BayesExprNode {
    this.advance()
    if (this.tok.kind === 'eof') fail('expr.empty', 0, 0)
    const n = this.sum()
    if (this.tok.kind !== 'eof') this.unexpected('operator')
    return n
  }

  private advance () { this.tok = this.lx.next() }

  private unexpected (expected: string): never {
    return fail('expr.unexpected', this.tok.pos, this.tok.end - this.tok.pos, { expected })
  }

  private unclosed (open: Token): never {
    return fail('expr.unclosed', open.pos, open.end - open.pos, { bracket: open.kind === '[' ? '[' : '(' })
  }

  private count (t: Token) {
    if (this.nodes >= this.voc.limits.max_expression_nodes) fail('expr.too_big', t.pos, t.end - t.pos)
    this.nodes++
  }

  private enter (t: Token) {
    if (this.depth >= this.voc.limits.max_expression_nesting) fail('expr.too_deep', t.pos, t.end - t.pos)
    this.depth++
  }

  private sum (): BayesExprNode {
    let left = this.product()
    while (this.tok.kind === '+' || this.tok.kind === '-') {
      const op = this.tok
      this.count(op)
      this.advance()
      left = binary(op, left, this.product())
    }
    return left
  }

  private product (): BayesExprNode {
    let left = this.signed()
    while (this.tok.kind === '*' || this.tok.kind === '/') {
      const op = this.tok
      this.count(op)
      this.advance()
      left = binary(op, left, this.signed())
    }
    return left
  }

  private signed (): BayesExprNode {
    if (this.tok.kind !== '-') return this.power()
    const minus = this.tok
    this.enter(minus)
    this.count(minus)
    this.advance()
    const x = this.signed()
    this.depth--
    return { kind: 'neg', args: [x], pos: minus.pos, len: end(x) - minus.pos }
  }

  private power (): BayesExprNode {
    const base = this.atom()
    if (this.tok.kind !== '^') return base
    const caret = this.tok
    this.enter(caret)
    this.count(caret)
    this.advance()
    const exp = this.signed()
    this.depth--
    return binary(caret, base, exp)
  }

  private atom (): BayesExprNode {
    const t = this.tok
    switch (t.kind) {
      case 'number':
        this.count(t)
        this.advance()
        return { kind: 'num', num: t.num, digits: t.digits, pos: t.pos, len: t.end - t.pos }
      case 'name': {
        this.count(t)
        this.advance()
        if (this.tok.kind === '(') return this.call(t)
        if (this.tok.kind === '[') return this.index(t)
        return { kind: 'name', name: this.src.slice(t.pos, t.end), pos: t.pos, len: t.end - t.pos }
      }
      case '(': {
        this.enter(t)
        this.advance()
        const inner = this.sum()
        if (this.tok.kind === 'eof') this.unclosed(t)
        if (this.tok.kind !== ')') this.unexpected('paren')
        const closing = this.tok
        this.advance()
        this.depth--
        // The node's span takes in its parentheses; its own span stays the first one.
        if (!inner.at) inner.at = { pos: inner.pos, len: inner.len }
        inner.pos = t.pos
        inner.len = closing.end - t.pos
        return inner
      }
    }
    return this.unexpected('value')
  }

  private call (name: Token): BayesExprNode {
    const open = this.tok
    const fn = this.src.slice(name.pos, name.end)
    this.enter(open)
    this.advance()
    if (this.tok.kind === ')') fail('expr.call_empty', name.pos, this.tok.end - name.pos, { name: fn })
    const args: BayesExprNode[] = []
    for (;;) {
      args.push(this.sum())
      if (this.tok.kind === ',') {
        this.advance()
        continue
      }
      if (this.tok.kind === ')') break
      if (this.tok.kind === 'eof') this.unclosed(open)
      this.unexpected('argument')
    }
    const closing = this.tok
    this.advance()
    this.depth--
    const n: BayesExprNode = { kind: 'call', name: fn, args, pos: name.pos, len: closing.end - name.pos }
    return fn === LAG && this.voc.lagApart ? lag(n) : n
  }

  private index (name: Token): BayesExprNode {
    const open = this.tok
    this.enter(open)
    this.advance()
    const by = this.tok
    if (by.kind === 'eof') this.unclosed(open)
    if (by.kind !== 'name') this.unexpected('index_name')
    this.advance()
    if (this.tok.kind === 'eof') this.unclosed(open)
    if (this.tok.kind !== ']') this.unexpected('bracket')
    const closing = this.tok
    this.advance()
    this.depth--
    return {
      kind: 'index', name: this.src.slice(name.pos, name.end), by: this.src.slice(by.pos, by.end),
      pos: name.pos, len: closing.end - name.pos,
      sub: [{ pos: name.pos, len: name.end - name.pos }, { pos: by.pos, len: by.end - by.pos }],
    }
  }
}

/** lag(x, k): x a bare name, k a literal written in digits only. */
function lag (call: Extract<BayesExprNode, { kind: 'call' }>): BayesExprNode {
  const span = (n: BayesExprNode): never => fail('expr.lag_argument', n.pos, n.len)
  if (call.args.length !== 2) span(call)
  const [x, k] = call.args
  if (x.kind !== 'name') span(x)
  if (k.kind !== 'num' || !k.digits) span(k)
  return {
    kind: 'lag', name: (x as { name: string }).name, k: (k as { num: number }).num, pos: call.pos, len: call.len,
    sub: [x.at ?? { pos: x.pos, len: x.len }, k.at ?? { pos: k.pos, len: k.len }],
  }
}

/** Reads one expression; the error, when there is one, is the first from the left. */
export function bayesParseExpr (src: string, catalog: BayesCatalog = BAYES_CATALOG): BayesExprResult {
  const voc = vocabulary(catalog)
  try {
    precheck(src, voc)
    return { ok: true, node: new Parser(src, voc).input() }
  } catch (e) {
    if (e instanceof ExprFail) return { ok: false, error: e.error }
    throw e
  }
}

// The levels of the grammar, from the loosest.
const LEVEL_SUM = 0
const LEVEL_PRODUCT = 1
const LEVEL_SIGNED = 2
const LEVEL_POWER = 3
const LEVEL_ATOM = 4

function level (n: BayesExprNode): number {
  if (n.kind === 'binary') {
    if (n.op === '+' || n.op === '-') return LEVEL_SUM
    if (n.op === '*' || n.op === '/') return LEVEL_PRODUCT
    return LEVEL_POWER
  }
  return n.kind === 'neg' ? LEVEL_SIGNED : LEVEL_ATOM
}

function formatInto (n: BayesExprNode, min: number): string {
  if (level(n) < min) return '(' + formatInto(n, LEVEL_SUM) + ')'
  switch (n.kind) {
    case 'num': return bayesFormatNumber(n.num)
    case 'name': return n.name
    case 'index': return `${n.name}[${n.by}]`
    case 'lag': return `${LAG}(${n.name}, ${n.k})`
    case 'call': return `${n.name}(${n.args.map(a => formatInto(a, LEVEL_SUM)).join(', ')})`
    case 'neg': return '-' + formatInto(n.args[0], LEVEL_SIGNED)
    case 'binary': {
      let left = LEVEL_SUM
      let right = LEVEL_PRODUCT
      if (n.op === '*' || n.op === '/') [left, right] = [LEVEL_PRODUCT, LEVEL_SIGNED]
      else if (n.op === '^') [left, right] = [LEVEL_ATOM, LEVEL_SIGNED]
      return `${formatInto(n.args[0], left)} ${n.op} ${formatInto(n.args[1], right)}`
    }
  }
}

/**
 * The canonical text of a tree: minimal parentheses by the precedence of the grammar, one space
 * around each binary operator, none after a unary minus, numbers in their shortest form. It can
 * be longer than the person's text.
 */
export function bayesFormatExpr (n: BayesExprNode): string {
  return formatInto(n, LEVEL_SUM)
}

/** The JSON form of a tree (the same in Go and TypeScript); spans are not part of it. */
export function bayesExprJson (n: BayesExprNode): unknown {
  switch (n.kind) {
    case 'num': return { num: bayesFormatNumber(n.num) }
    case 'name': return { name: n.name }
    case 'index': return { index: n.name, by: n.by }
    case 'call': return { call: n.name, args: n.args.map(bayesExprJson) }
    case 'lag': return { lag: n.name, k: n.k }
    case 'neg': return { neg: bayesExprJson(n.args[0]) }
    case 'binary': return { op: n.op, args: n.args.map(bayesExprJson) }
  }
}
