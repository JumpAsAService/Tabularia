// What a node does, in one line, from its parameters: «stato = evaso», «ricavo ↓»,
// «agente_id → ricavo, ordini, clienti». Before, the node said «1 parameters» and to know
// what it did one had to click it (audit 2026-10-11, F4). Only values — column names,
// expressions, numbers — and symbols: no words to translate. A shape not known falls back
// on a generic summary of the values.

const OPERATORS: Record<string, string> = {
  eq: '=', ne: '≠', neq: '≠', gt: '>', gte: '≥', ge: '≥', lt: '<', lte: '≤', le: '≤',
  contains: '∋', in: '∈', not_in: '∉', is_null: '= ∅', is_not_null: '≠ ∅', not_null: '≠ ∅',
  starts_with: '^', ends_with: '$', regex: '~',
}

const MAX_LISTING = 3

function listing(v: unknown): string {
  const a = Array.isArray(v) ? v : v == null || v === '' ? [] : [v]
  const texts = a.map((x) => (typeof x === 'object' && x !== null ? summary(x) : String(x))).filter(Boolean)
  return texts.length > MAX_LISTING ? `${texts.slice(0, MAX_LISTING).join(', ')} +${texts.length - MAX_LISTING}` : texts.join(', ')
}

function pairs(m: unknown, arrow = '→'): string {
  if (!m || typeof m !== 'object' || Array.isArray(m)) return ''
  return listing(Object.entries(m as Record<string, unknown>).map(([k, v]) => `${k} ${arrow} ${String(v)}`))
}

// any object (an aggregation, a computed column): its name, if it has one
function summary(o: any): string {
  return o.alias ?? o.name ?? o.column ?? o.col ?? Object.values(o).filter((v) => typeof v !== 'object').join(' ')
}

export function opSummary(op: string, params: Record<string, any> | null | undefined): string {
  const p = params ?? {}
  switch (op) {
    case 'select': case 'reorder': case 'drop': case 'unique': case 'drop_nulls':
      return listing(p.columns ?? p.subset ?? p.by)
    case 'rename': return pairs(p.mapping)
    case 'cast': return pairs(p.columns)
    case 'compute': {
      const cols = Array.isArray(p.columns) ? p.columns : []
      if (!cols.length) return ''
      const first = `${cols[0].name} = ${cols[0].expr ?? ''}`.trim()
      return cols.length > 1 ? `${first} +${cols.length - 1}` : first
    }
    case 'sql': return String(p.query ?? '').replace(/\s+/g, ' ').trim()
    case 'filter': {
      if (!p.column) return ''
      const op2 = OPERATORS[p.operator] ?? p.operator ?? ''
      const value = p.value == null || p.value === '' || /null/.test(p.operator ?? '') ? '' : ` ${Array.isArray(p.value) ? listing(p.value) : p.value}`
      return `${p.column} ${op2}${value}`.trim()
    }
    case 'sort': {
      // a direction each (a list), or one for all (flows saved before)
      const by = Array.isArray(p.by) ? p.by : p.by ? [p.by] : []
      const desc = (i: number) => (Array.isArray(p.descending) ? !!p.descending[i] : !!p.descending)
      return listing(by.map((c: string, i: number) => `${c} ${desc(i) ? '↓' : '↑'}`))
    }
    case 'limit': return p.n != null ? `n = ${p.n}` : ''
    case 'sample': return p.n != null ? `n = ${p.n}` : p.fraction != null ? `${Math.round(Number(p.fraction) * 100)}%` : ''
    case 'group_by': {
      const aggs = Array.isArray(p.aggregations) ? p.aggregations.map((a: any) => a.alias ?? `${a.func}(${a.column})`) : []
      const by = listing(p.by)
      return aggs.length ? `${by} → ${listing(aggs)}` : by
    }
    case 'pivot': {
      const rows = listing(p.index), columns = listing(p.on)
      const values = p.values ? `${p.func ?? ''}(${p.values})` : ''
      return [rows && columns ? `${rows} × ${columns}` : rows || columns, values].filter(Boolean).join(' · ')
    }
    case 'unpivot': return listing(p.on ?? p.columns ?? p.value_vars)
    case 'fill_null': {
      const cols = listing(p.columns ?? p.subset)
      const withValue = p.value ?? p.strategy
      return [cols, withValue != null ? `→ ${withValue}` : ''].filter(Boolean).join(' ')
    }
    default: {
      // unknown shape: the values, in the order they arrive
      return listing(Object.values(p).filter((v) => v != null && v !== '' && !(Array.isArray(v) && !v.length)).map((v) =>
        Array.isArray(v) ? listing(v) : typeof v === 'object' ? pairs(v) : String(v)))
    }
  }
}
