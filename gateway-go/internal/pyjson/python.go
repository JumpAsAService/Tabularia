package pyjson

import (
	"strconv"
	"strings"
)

// Equal is Python's `==` between two values Loads returned (or built alike): numbers by
// value (1 == 1.0, True == 1), dicts whatever the order of their keys, lists item by item.
func Equal(a, b any) bool {
	if x, ok := number(a); ok {
		if y, ok := number(b); ok {
			return x == y
		}
		return false
	}
	switch x := a.(type) {
	case nil:
		return b == nil
	case string:
		y, ok := b.(string)
		return ok && x == y
	case Object:
		y, ok := b.(Object)
		if !ok || len(dedup(x)) != len(dedup(y)) {
			return false
		}
		for _, kv := range x {
			w, has := y.Get(kv.Key)
			if !has || !Equal(kv.Value, w) {
				return false
			}
		}
		return true
	case []any:
		y, ok := b.([]any)
		if !ok || len(x) != len(y) {
			return false
		}
		for i := range x {
			if !Equal(x[i], y[i]) {
				return false
			}
		}
		return true
	case []string:
		y, ok := b.([]string)
		if !ok || len(x) != len(y) {
			return false
		}
		for i := range x {
			if x[i] != y[i] {
				return false
			}
		}
		return true
	}
	return false
}

func dedup(o Object) map[string]bool {
	m := make(map[string]bool, len(o))
	for _, kv := range o {
		m[kv.Key] = true
	}
	return m
}

func number(v any) (float64, bool) {
	switch x := v.(type) {
	case bool:
		if x {
			return 1, true
		}
		return 0, true
	case int:
		return float64(x), true
	case int64:
		return float64(x), true
	case float64:
		return x, true
	}
	return 0, false
}

// Str is Python's str() of a JSON value: a text as it is, the others as repr writes them.
func Str(v any) string {
	if s, ok := v.(string); ok {
		return s
	}
	return Repr(v)
}

// Repr is Python's repr() of a JSON value.
func Repr(v any) string {
	switch x := v.(type) {
	case nil:
		return "None"
	case bool:
		if x {
			return "True"
		}
		return "False"
	case int:
		return strconv.Itoa(x)
	case int64:
		return strconv.FormatInt(x, 10)
	case Int: // an integer past int64, as Loads read it
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
	case string:
		return reprString(x)
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
	return ""
}

// reprString is repr(str): single quotes unless the text holds one and no double quote.
func reprString(s string) string {
	quote := byte('\'')
	if strings.Contains(s, "'") && !strings.Contains(s, `"`) {
		quote = '"'
	}
	var b strings.Builder
	b.WriteByte(quote)
	for _, r := range s {
		switch {
		case r == '\\':
			b.WriteString(`\\`)
		case r == rune(quote):
			b.WriteByte('\\')
			b.WriteRune(r)
		case r == '\n':
			b.WriteString(`\n`)
		case r == '\r':
			b.WriteString(`\r`)
		case r == '\t':
			b.WriteString(`\t`)
		case r < 0x20 || r == 0x7f:
			b.WriteString(`\x` + strconv.FormatInt(int64(r)+0x100, 16)[1:])
		default:
			b.WriteRune(r)
		}
	}
	b.WriteByte(quote)
	return b.String()
}
