package pyjson

import (
	"fmt"
	"strconv"
	"strings"
	"unicode"
)

// Str is Python's str() of a value json.loads gave (Loads): a string as it is, None,
// True and False by name, a float as repr writes it, a dict or a list as their repr.
func Str(v any) string {
	if s, ok := v.(string); ok {
		return s
	}
	return Repr(v)
}

// Repr is Python's repr() of a value json.loads gave.
func Repr(v any) string {
	switch x := v.(type) {
	case nil:
		return "None"
	case bool:
		if x {
			return "True"
		}
		return "False"
	case string:
		return reprString(x)
	case int64:
		return strconv.FormatInt(x, 10)
	case int:
		return strconv.Itoa(x)
	case BigInt:
		return string(x)
	case float64:
		switch r := FloatRepr(x); r {
		case "Infinity":
			return "inf"
		case "-Infinity":
			return "-inf"
		case "NaN":
			return "nan"
		default:
			return r
		}
	case Object:
		parts := make([]string, len(x))
		for i, kv := range x {
			parts[i] = reprString(kv.Key) + ": " + Repr(kv.Value)
		}
		return "{" + strings.Join(parts, ", ") + "}"
	case []any:
		parts := make([]string, len(x))
		for i, e := range x {
			parts[i] = Repr(e)
		}
		return "[" + strings.Join(parts, ", ") + "]"
	}
	return fmt.Sprint(v)
}

// reprString is repr(str): single quotes unless the text holds one and no double quote.
func reprString(s string) string {
	q := byte('\'')
	if strings.ContainsRune(s, '\'') && !strings.ContainsRune(s, '"') {
		q = '"'
	}
	var b strings.Builder
	b.WriteByte(q)
	for _, r := range s {
		switch {
		case r == rune(q) || r == '\\':
			b.WriteByte('\\')
			b.WriteRune(r)
		case r == '\n':
			b.WriteString(`\n`)
		case r == '\r':
			b.WriteString(`\r`)
		case r == '\t':
			b.WriteString(`\t`)
		case r < 0x20 || r == 0x7f:
			fmt.Fprintf(&b, `\x%02x`, r)
		case r < 0x80 || unicode.IsPrint(r):
			b.WriteRune(r)
		case r <= 0xff:
			fmt.Fprintf(&b, `\x%02x`, r)
		case r <= 0xffff:
			fmt.Fprintf(&b, `\u%04x`, r)
		default:
			fmt.Fprintf(&b, `\U%08x`, r)
		}
	}
	b.WriteByte(q)
	return b.String()
}

// Truthy is Python's bool() of a value json.loads gave.
func Truthy(v any) bool {
	switch x := v.(type) {
	case nil:
		return false
	case bool:
		return x
	case string:
		return x != ""
	case int64:
		return x != 0
	case int:
		return x != 0
	case BigInt:
		return true
	case float64:
		return x != 0
	case Object:
		return len(x) > 0
	case []any:
		return len(x) > 0
	}
	return v != nil
}

// Or is Python's `a or b`.
func Or(a, b any) any {
	if Truthy(a) {
		return a
	}
	return b
}

// IsInt is isinstance(v, int) without bool (the checks that exclude it, or where a
// boolean id is no id).
func IsInt(v any) (int64, bool) {
	n, ok := v.(int64)
	return n, ok
}

// Obj is v as a dict, or nil.
func Obj(v any) Object {
	o, _ := v.(Object)
	return o
}

// Value is d.get(key): the value, nil when missing.
func (o Object) Value(key string) any {
	v, _ := o.Get(key)
	return v
}
