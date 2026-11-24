// The Stan code tab of the editor of Bayesian models (docs/specs/bayes-graph-editor.md, «The Stan code,
// read only»), as pure functions: the tokenizer of the program's alphabet (the codegen spec's «The
// alphabet»: blocks, types, bounds, `~`, numbers, identifiers), the mapping table's rows (identifier,
// the node it came from, the label and its kind), the places of a node's identifiers in the program, and
// what the tab shows while the graph has an error (the stored program of the last saved version).
// Nothing of Vue, Nuxt, Monaco or the DOM: the tests run it in Node, the component and Monaco's
// tokens provider call it. Every export starts with `bayes` or `BAYES_` (the composables are auto-imported).

// ---------------------------------------------------------------- the tokenizer

/** What a token is; the editor's theme colours each by role. */
export type BayesStanClass =
  | 'block' | 'type' | 'bound' | 'keyword' | 'function' | 'number' | 'tilde' | 'generated' | 'name' | 'punct' | 'comment' | 'space'

export interface BayesStanToken {
  /** first character, 0-based */
  start: number
  /** one past the last character */
  end: number
  cls: BayesStanClass
}

const TYPES = new Set([
  'int', 'real', 'complex', 'vector', 'row_vector', 'matrix', 'array', 'simplex', 'sum_to_zero_vector', 'sum_to_zero_matrix', 'ordered',
  'positive_ordered', 'unit_vector', 'cholesky_factor_corr', 'cholesky_factor_cov', 'cov_matrix', 'corr_matrix', 'column_stochastic_matrix',
  'row_stochastic_matrix', 'complex_vector', 'complex_row_vector', 'complex_matrix', 'void', 'tuple',
])
const KEYWORDS = new Set(['for', 'in', 'while', 'if', 'else', 'return', 'break', 'continue', 'target', 'profile', 'print', 'reject', 'fatal_error', 'jacobian'])
const BOUNDS = new Set(['lower', 'upper', 'offset', 'multiplier'])
const BLOCK = /^(transformed data|transformed parameters|generated quantities|functions|parameters|data|model)(?![A-Za-z0-9_])/
const NUMBER = /^(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?/
const WORD = /^[A-Za-z_][A-Za-z0-9_]*/

/**
 * The tokens of one line, and whether a `/* … *\/` comment is still open at its end. A block's name
 * counts only at the start of a line (the program writes one per line); a bound's word only before `=`
 * (inside `<…>`); an identifier followed by `(` is a function or a distribution; one that starts with a
 * capital letter is one the generator made (`V_n7`, `Z_eff`, `N`, `Mode`: a person's name never does).
 */
export function bayesStanTokenizeLine (line: string, inComment = false): { tokens: BayesStanToken[], inComment: boolean } {
  const tokens: BayesStanToken[] = []
  const push = (start: number, end: number, cls: BayesStanClass) => {
    const last = tokens[tokens.length - 1]
    if (last && last.cls === cls && last.end === start && (cls === 'space' || cls === 'punct' || cls === 'comment')) last.end = end
    else tokens.push({ start, end, cls })
  }
  let i = 0
  while (i < line.length) {
    if (inComment) {
      const close = line.indexOf('*/', i)
      const end = close < 0 ? line.length : close + 2
      push(i, end, 'comment')
      i = end
      if (close >= 0) inComment = false
      continue
    }
    const rest = line.slice(i)
    const c = rest[0]
    if (c === ' ' || c === '\t') {
      let j = i + 1
      while (j < line.length && (line[j] === ' ' || line[j] === '\t')) j++
      push(i, j, 'space')
      i = j
      continue
    }
    if (rest.startsWith('//')) { push(i, line.length, 'comment'); i = line.length; continue }
    if (rest.startsWith('/*')) { inComment = true; continue }
    if (c === '~') { push(i, i + 1, 'tilde'); i++; continue }
    if (i === 0) {
      const b = BLOCK.exec(rest)
      if (b) { push(0, b[0].length, 'block'); i = b[0].length; continue }
    }
    const n = /^[0-9.]/.test(c) ? NUMBER.exec(rest) : null
    if (n) { push(i, i + n[0].length, 'number'); i += n[0].length; continue }
    const w = WORD.exec(rest)
    if (w) {
      const word = w[0]
      const after = line.slice(i + word.length)
      let cls: BayesStanClass
      if (BOUNDS.has(word) && /^\s*=/.test(after)) cls = 'bound'
      else if (TYPES.has(word)) cls = 'type'
      else if (KEYWORDS.has(word)) cls = 'keyword'
      else if (word === 'T' && after.startsWith('[')) cls = 'keyword'
      else if (/^\s*\(/.test(after)) cls = 'function'
      else if (/^[A-Z]/.test(word)) cls = 'generated'
      else cls = 'name'
      push(i, i + word.length, cls)
      i += word.length
      continue
    }
    push(i, i + 1, 'punct')
    i++
  }
  return { tokens, inComment }
}

/** The tokens of every line of a program. */
export function bayesStanTokenize (program: string): BayesStanToken[][] {
  let open = false
  return program.split('\n').map((line) => {
    const r = bayesStanTokenizeLine(line, open)
    open = r.inComment
    return r.tokens
  })
}

// ---------------------------------------------------------------- the mapping table

/** One identifier the program declares, as the gateway's mapping lists it (bayes-stan-codegen, «The mapping table»). */
export interface BayesMapEntry {
  stan: string
  block: string
  node_id: string | null
  component: string | null
  name: string | null
  label: string | null
  kind: string
  plate: string | null
  role: string | null
  renamed: string | null
}

/** The mapping the gateway sends (a list of entries), read with care: anything else is an empty table. */
export function bayesStanMapping (raw: unknown): BayesMapEntry[] {
  if (!Array.isArray(raw)) return []
  const str = (v: unknown): string | null => (typeof v === 'string' && v !== '' ? v : null)
  const out: BayesMapEntry[] = []
  for (const e of raw) {
    if (!e || typeof e !== 'object' || typeof (e as any).stan !== 'string' || !(e as any).stan) continue
    const o = e as Record<string, unknown>
    out.push({
      stan: o.stan as string,
      block: str(o.block) ?? '',
      node_id: str(o.node_id),
      component: str(o.component),
      name: str(o.name),
      label: str(o.label),
      kind: str(o.kind) ?? 'other',
      plate: str(o.plate),
      role: str(o.role),
      renamed: str(o.renamed),
    })
  }
  return out
}

/** The kinds of the mapping the interface has words for (`bayes.editor.stan.kinds.<kind>`); any other is «other». */
export const BAYES_STAN_KINDS = [
  'data', 'parameter', 'component', 'linear_predictor', 'transformed', 'state', 'correlation', 'series', 'size', 'mode', 'lift', 'internal', 'extension', 'other',
] as const

export function bayesStanKind (kind: string): string {
  return (BAYES_STAN_KINDS as readonly string[]).includes(kind) ? kind : 'other'
}

/** The node or box on the canvas an entry selects: its node, or the box a size belongs to; null when it has none. */
export function bayesMapTarget (e: BayesMapEntry, exists: (id: string) => boolean): string | null {
  const id = e.node_id ?? (e.kind === 'size' ? e.plate : null)
  return id && exists(id) ? id : null
}

/** The identifiers of one node or box in the program (its own, its components', its locals and sizes). */
export function bayesMapIdentifiers (entries: readonly BayesMapEntry[], id: string | null, exists: (id: string) => boolean): string[] {
  if (!id) return []
  return entries.filter(e => bayesMapTarget(e, exists) === id).map(e => e.stan)
}

// ---------------------------------------------------------------- the places of an identifier

export interface BayesStanUse {
  /** 1-based, as Monaco counts */
  line: number
  /** 1-based first column, and one past the last */
  start: number
  end: number
}

/** Every whole-word place of the identifiers in the program (never inside a longer name), in reading order. */
export function bayesStanUses (program: string, identifiers: readonly string[]): BayesStanUse[] {
  if (!identifiers.length) return []
  const wanted = new Set(identifiers)
  const out: BayesStanUse[] = []
  program.split('\n').forEach((line, i) => {
    for (const w of line.matchAll(/[A-Za-z_][A-Za-z0-9_]*/g)) {
      if (wanted.has(w[0])) out.push({ line: i + 1, start: (w.index ?? 0) + 1, end: (w.index ?? 0) + 1 + w[0].length })
    }
  })
  return out
}

/** The distinct lines of some places, ascending. */
export function bayesStanLines (uses: readonly BayesStanUse[]): number[] {
  return [...new Set(uses.map(u => u.line))].sort((a, b) => a - b)
}

// ---------------------------------------------------------------- what the tab shows

/** What the tab shows: the program of the graph as it is (`live`), the stored one of the last saved version, or nothing. */
export type BayesStanView = 'live' | 'stored' | 'none'

/**
 * While the graph has an error there is no program for it: the tab shows the last saved version's, as the
 * flows that pin it run it (`stored`), or says there is none for a model never saved. A version opened read
 * only shows its own stored program, never a new one. No error: the generated program, once there is one;
 * until then the stored one stands (the person sees a program at once, not an empty box).
 */
export function bayesStanView (s: { errors: number, readonly: boolean, hasLive: boolean, hasStored: boolean }): BayesStanView {
  if (s.readonly) return s.hasStored ? 'stored' : 'none'
  if (s.errors > 0) return s.hasStored ? 'stored' : 'none'
  if (s.hasLive) return 'live'
  return s.hasStored ? 'stored' : 'none'
}

/**
 * The part of the document the program is made from: everything but the drawing (`layout`), which never
 * enters it. Moving a node does not ask the gateway for the program again.
 */
export function bayesStanKey (doc: object | null): string {
  if (!doc) return ''
  const { layout: _layout, ...rest } = doc as Record<string, unknown>
  return JSON.stringify(rest)
}

// ---------------------------------------------------------------- colours for Monaco's theme

/** A CSS colour as the browser computes it (`rgb(…)`, `rgba(…)`, `color(srgb …)`) as `#rrggbb` or `#rrggbbaa`; null when unreadable. */
export function bayesCssToHex (css: string): string | null {
  const s = css.trim()
  const two = (n: number) => Math.max(0, Math.min(255, Math.round(n))).toString(16).padStart(2, '0')
  let m = /^rgba?\(\s*([\d.]+)[\s,]+([\d.]+)[\s,]+([\d.]+)(?:\s*[,/]\s*([\d.]+%?))?\s*\)$/.exec(s)
  let k = 1
  if (!m) {
    m = /^color\(\s*srgb\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)(?:\s*\/\s*([\d.]+%?))?\s*\)$/.exec(s)
    k = 255
  }
  if (m) {
    const alpha = m[4] === undefined ? 1 : m[4].endsWith('%') ? parseFloat(m[4]) / 100 : parseFloat(m[4])
    return `#${two(parseFloat(m[1]) * k)}${two(parseFloat(m[2]) * k)}${two(parseFloat(m[3]) * k)}${alpha < 1 ? two(alpha * 255) : ''}`
  }
  const h = /^#([0-9a-f]{3}|[0-9a-f]{6}|[0-9a-f]{8})$/i.exec(s)
  if (!h) return null
  const v = h[1].length === 3 ? [...h[1]].map(x => x + x).join('') : h[1]
  return `#${v.toLowerCase()}`
}

/** Whether a `#rrggbb` colour is light (the editor's base theme follows its background). */
export function bayesHexIsLight (hex: string): boolean {
  const v = /^#([0-9a-f]{6})/i.exec(hex)
  if (!v) return false
  const c = [0, 2, 4].map(i => parseInt(v[1].slice(i, i + 2), 16) / 255).map(x => (x <= 0.03928 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4))
  return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2] > 0.4
}

/** The contrast ratio of two `#rrggbb` colours (WCAG): 1 to 21; 1 when either is unreadable. */
export function bayesHexContrast (a: string, b: string): number {
  const lum = (hex: string): number | null => {
    const v = /^#([0-9a-f]{6})/i.exec(hex)
    if (!v) return null
    const c = [0, 2, 4].map(i => parseInt(v[1].slice(i, i + 2), 16) / 255).map(x => (x <= 0.03928 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4))
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
  }
  const la = lum(a)
  const lb = lum(b)
  if (la === null || lb === null) return 1
  return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05)
}

/**
 * The theme token a class of token is drawn in: the first of its candidates that reads at 4.5:1 on the code's
 * background (`color` and `bg` are what the browser computed for them), the text colour at the last. A theme
 * whose accent is too pale on its background keeps the code legible and loses only that class's hue.
 */
export function bayesStanPick (cands: readonly string[], colorOf: (token: string) => string | null, bg: string): string | null {
  for (const token of cands) {
    const c = colorOf(token)
    if (c && bayesHexContrast(c, bg) >= 4.5) return c
  }
  return colorOf(cands[cands.length - 1])
}

/** The theme tokens each class of token reads (`var(--x)` names), and what is bold or italic: the mockup's colours by role. */
export const BAYES_STAN_PALETTE: Record<Exclude<BayesStanClass, 'space'>, { token: string, alt?: string, style?: 'bold' | 'italic' }> = {
  block: { token: '--accent-hi', style: 'bold' },
  type: { token: '--violet' },
  bound: { token: '--muted', style: 'italic' },
  keyword: { token: '--violet' },
  function: { token: '--accent', alt: '--accent-hi' },
  number: { token: '--warning' },
  tilde: { token: '--accent-2' },
  generated: { token: '--muted' },
  name: { token: '--text' },
  punct: { token: '--text' },
  comment: { token: '--muted', style: 'italic' },
}
