// Cosa fa un nodo, in una riga, dai suoi parametri: «stato = evaso», «ricavo ↓»,
// «agente_id → ricavo, ordini, clienti». Prima il nodo diceva «1 parameters» e
// per sapere cosa faceva bisognava cliccarlo (audit 2026-10-11, F4). Solo valori
// — nomi di colonna, espressioni, numeri — e simboli: niente parole da tradurre.
// Una forma che non si conosce ricade su un riassunto generico dei valori.

const OPERATORI: Record<string, string> = {
  eq: '=', ne: '≠', neq: '≠', gt: '>', gte: '≥', ge: '≥', lt: '<', lte: '≤', le: '≤',
  contains: '∋', in: '∈', not_in: '∉', is_null: '= ∅', is_not_null: '≠ ∅', not_null: '≠ ∅',
  starts_with: '^', ends_with: '$', regex: '~',
}

const MAX_ELENCO = 3

function elenco(v: unknown): string {
  const a = Array.isArray(v) ? v : v == null || v === '' ? [] : [v]
  const testi = a.map((x) => (typeof x === 'object' && x !== null ? sintesi(x) : String(x))).filter(Boolean)
  return testi.length > MAX_ELENCO ? `${testi.slice(0, MAX_ELENCO).join(', ')} +${testi.length - MAX_ELENCO}` : testi.join(', ')
}

function coppie(m: unknown, freccia = '→'): string {
  if (!m || typeof m !== 'object' || Array.isArray(m)) return ''
  return elenco(Object.entries(m as Record<string, unknown>).map(([k, v]) => `${k} ${freccia} ${String(v)}`))
}

// un oggetto qualunque (un'aggregazione, una colonna calcolata): il suo nome, se ce l'ha
function sintesi(o: any): string {
  return o.alias ?? o.name ?? o.column ?? o.col ?? Object.values(o).filter((v) => typeof v !== 'object').join(' ')
}

export function opSummary(op: string, params: Record<string, any> | null | undefined): string {
  const p = params ?? {}
  switch (op) {
    case 'select': case 'reorder': case 'drop': case 'unique': case 'drop_nulls':
      return elenco(p.columns ?? p.subset ?? p.by)
    case 'rename': return coppie(p.mapping)
    case 'cast': return coppie(p.columns)
    case 'compute': {
      const cols = Array.isArray(p.columns) ? p.columns : []
      if (!cols.length) return ''
      const primo = `${cols[0].name} = ${cols[0].expr ?? ''}`.trim()
      return cols.length > 1 ? `${primo} +${cols.length - 1}` : primo
    }
    case 'sql': return String(p.query ?? '').replace(/\s+/g, ' ').trim()
    case 'filter': {
      if (!p.column) return ''
      const op2 = OPERATORI[p.operator] ?? p.operator ?? ''
      const valore = p.value == null || p.value === '' || /null/.test(p.operator ?? '') ? '' : ` ${Array.isArray(p.value) ? elenco(p.value) : p.value}`
      return `${p.column} ${op2}${valore}`.trim()
    }
    case 'sort': {
      // a direction each (a list), or one for all (flows saved before)
      const by = Array.isArray(p.by) ? p.by : p.by ? [p.by] : []
      const desc = (i: number) => (Array.isArray(p.descending) ? !!p.descending[i] : !!p.descending)
      return elenco(by.map((c: string, i: number) => `${c} ${desc(i) ? '↓' : '↑'}`))
    }
    case 'limit': return p.n != null ? `n = ${p.n}` : ''
    case 'sample': return p.n != null ? `n = ${p.n}` : p.fraction != null ? `${Math.round(Number(p.fraction) * 100)}%` : ''
    case 'group_by': {
      const aggs = Array.isArray(p.aggregations) ? p.aggregations.map((a: any) => a.alias ?? `${a.func}(${a.column})`) : []
      const by = elenco(p.by)
      return aggs.length ? `${by} → ${elenco(aggs)}` : by
    }
    case 'pivot': {
      const righe = elenco(p.index), colonne = elenco(p.on)
      const valori = p.values ? `${p.func ?? ''}(${p.values})` : ''
      return [righe && colonne ? `${righe} × ${colonne}` : righe || colonne, valori].filter(Boolean).join(' · ')
    }
    case 'unpivot': return elenco(p.on ?? p.columns ?? p.value_vars)
    case 'fill_null': {
      const cols = elenco(p.columns ?? p.subset)
      const con = p.value ?? p.strategy
      return [cols, con != null ? `→ ${con}` : ''].filter(Boolean).join(' ')
    }
    default: {
      // forma sconosciuta: i valori, nell'ordine in cui arrivano
      return elenco(Object.values(p).filter((v) => v != null && v !== '' && !(Array.isArray(v) && !v.length)).map((v) =>
        Array.isArray(v) ? elenco(v) : typeof v === 'object' ? coppie(v) : String(v)))
    }
  }
}
