// A value typed in a box (filter value, fill value, a free "is one of" value), read
// the way its column stores values. Text stays text: "00144" is a code, not 144, and
// "true" is a word. Numbers become numbers and true/false booleans only on columns of
// those types. Dates stay ISO text, which every engine converts.
//
// An empty box gives `undefined`: nothing is sent, and the engine refuses an operator
// left without a value instead of keeping zero rows. To mean the empty string, type
// "" with its quotes. A {{placeholder}} stays as typed (it is filled per iteration).
//
// Until the column's type is known (no column chosen, columns not loaded yet) the
// text is read as before: JSON when it parses, otherwise as typed.

const TEXT = /^(String|Utf8|Categorical|Enum)/
const NUMBER = /^(U?Int\d+|Float\d+|Decimal)/

function scalar(v: unknown, dtype: string): unknown {
  if (TEXT.test(dtype)) {
    if (typeof v !== 'string') return String(v)
    return v.trim() === '""' ? '' : v
  }
  if (NUMBER.test(dtype)) {
    const t = String(v).trim()
    const n = Number(t)
    return t !== '' && Number.isFinite(n) ? n : v
  }
  if (dtype === 'Boolean') {
    const t = String(v).trim().toLowerCase()
    return t === 'true' ? true : t === 'false' ? false : v
  }
  return v
}

export function typedValue(raw: string | null | undefined, dtype: string | null | undefined): unknown {
  const text = raw ?? ''
  const t = text.trim()
  if (t === '') return undefined
  if (t.includes('{{')) return text
  if (!dtype) {
    try {
      return JSON.parse(t)
    } catch {
      return text
    }
  }
  // a list ([a, b]) for between, or for an "is one of" written by hand
  if (t.startsWith('[')) {
    try {
      const list = JSON.parse(t)
      if (Array.isArray(list)) return list.map((v) => scalar(v, dtype))
    } catch {
      /* not a list: read as one value */
    }
  }
  return scalar(text, dtype)
}

/** The text a box shows for a stored value: the inverse of `typedValue`. */
export function typedValueText(v: unknown): string {
  if (v === null || v === undefined) return ''
  if (v === '') return '""'
  return typeof v === 'string' ? v : JSON.stringify(v)
}
