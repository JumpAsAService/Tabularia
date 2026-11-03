// Package pystr writes values read from JSON (pyjson.Loads) as Python's str() and repr() do,
// for the messages the Python gateway builds with them (`f"… «{str(kind)[:20]}»"`), and cuts
// text by characters as Python's slices do.
package pystr

import (
	"fmt"
	"math"
	"math/big"
	"strings"
	"unicode"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
)

// Str is str(v).
func Str(v any) string {
	if s, ok := v.(string); ok {
		return s
	}
	return Repr(v)
}

// Repr is repr(v).
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
	case int:
		return fmt.Sprint(x)
	case int64:
		return fmt.Sprint(x)
	case pyjson.Int:
		return string(x)
	case float64:
		switch {
		case math.IsInf(x, 1):
			return "inf"
		case math.IsInf(x, -1):
			return "-inf"
		case math.IsNaN(x):
			return "nan"
		}
		return pyjson.FloatRepr(x)
	case []any:
		parts := make([]string, len(x))
		for i, e := range x {
			parts[i] = Repr(e)
		}
		return "[" + strings.Join(parts, ", ") + "]"
	case pyjson.Object:
		parts := make([]string, len(x))
		for i, kv := range x {
			parts[i] = reprString(kv.Key) + ": " + Repr(kv.Value)
		}
		return "{" + strings.Join(parts, ", ") + "}"
	}
	return fmt.Sprint(v)
}

// reprString is unicode_repr: single quotes unless the text holds one and no double quote.
func reprString(s string) string {
	quote := '\''
	if strings.ContainsRune(s, '\'') && !strings.ContainsRune(s, '"') {
		quote = '"'
	}
	var b strings.Builder
	b.WriteRune(quote)
	for _, r := range s {
		switch {
		case r == quote || r == '\\':
			b.WriteRune('\\')
			b.WriteRune(r)
		case r == '\t':
			b.WriteString(`\t`)
		case r == '\n':
			b.WriteString(`\n`)
		case r == '\r':
			b.WriteString(`\r`)
		case r < ' ' || r == 0x7f:
			fmt.Fprintf(&b, `\x%02x`, r)
		case r < 0x7f:
			b.WriteRune(r)
		case unicode.IsPrint(r):
			b.WriteRune(r)
		case r <= 0xff:
			fmt.Fprintf(&b, `\x%02x`, r)
		case r <= 0xffff:
			fmt.Fprintf(&b, `\u%04x`, r)
		default:
			fmt.Fprintf(&b, `\U%08x`, r)
		}
	}
	b.WriteRune(quote)
	return b.String()
}

// Cut is s[:n] by characters.
func Cut(s string, n int) string {
	if len(s) <= n {
		return s
	}
	i := 0
	for pos := range s {
		if i == n {
			return s[:pos]
		}
		i++
	}
	return s
}

// Len is len(s): its characters.
func Len(s string) int { return len([]rune(s)) }

// IsSpace is str.isspace() of a character: what strip() removes.
func IsSpace(r rune) bool {
	switch r {
	case ' ', '\t', '\n', '\v', '\f', '\r', 0x1c, 0x1d, 0x1e, 0x1f, 0x85, 0xa0, 0x1680, 0x2028, 0x2029, 0x202f, 0x205f, 0x3000:
		return true
	}
	return r >= 0x2000 && r <= 0x200a
}

// Strip is str.strip().
func Strip(s string) string { return strings.TrimFunc(s, IsSpace) }

// Lower is str.lower() for the texts compared here.
func Lower(s string) string { return strings.ToLower(s) }

// Capitalized helpers of reports.py: Within is _within, Sentence is sentence.

// Within is _within: a message put after «The dataset «x»: », its first letter in lower case
// when the second is.
func Within(m string) string {
	r := []rune(m)
	if len(r) >= 2 && unicode.IsLower(r[1]) {
		return strings.ToLower(string(r[:1])) + string(r[1:])
	}
	return m
}

// Sentence is sentence: a message alone, its first letter in capitals.
func Sentence(m string) string {
	r := []rune(m)
	if len(r) >= 1 && unicode.IsLower(r[0]) {
		return strings.ToUpper(string(r[:1])) + string(r[1:])
	}
	return m
}

// ── values read from JSON, as Python sees them ───────────────────────────────

// Number is a JSON number as float (bool is not one here: callers exclude it as
// `isinstance(v, bool)` does), and whether it is one.
func Number(v any) (float64, bool) {
	switch x := v.(type) {
	case int64:
		return float64(x), true
	case int:
		return float64(x), true
	case float64:
		return x, true
	case pyjson.Int:
		f, _ := new(big.Float).SetString(string(x))
		if f == nil {
			return 0, false
		}
		r, _ := f.Float64()
		return r, true
	}
	return 0, false
}

// IsInt is isinstance(v, int) and not a bool.
func IsInt(v any) bool {
	switch v.(type) {
	case int64, int, pyjson.Int:
		return true
	}
	return false
}

// Truthy is bool(v).
func Truthy(v any) bool {
	switch x := v.(type) {
	case nil:
		return false
	case bool:
		return x
	case string:
		return x != ""
	case []any:
		return len(x) > 0
	case pyjson.Object:
		return len(x) > 0
	case pyjson.Int:
		return true
	}
	if f, ok := Number(v); ok {
		return f != 0
	}
	return true
}

// Eq is Python's ==: numbers by value (True == 1, 1 == 1.0), lists item by item, dicts by
// their keys whatever the order.
func Eq(a, b any) bool {
	if fa, ok := a.(float64); ok && math.IsNaN(fa) {
		return false
	}
	if fb, ok := b.(float64); ok && math.IsNaN(fb) {
		return false
	}
	na, aNum := numeric(a)
	nb, bNum := numeric(b)
	if aNum || bNum {
		if aNum && bNum {
			return na.Cmp(nb) == 0
		}
		return false
	}
	switch x := a.(type) {
	case nil:
		return b == nil
	case string:
		y, ok := b.(string)
		return ok && x == y
	case []any:
		y, ok := b.([]any)
		if !ok || len(x) != len(y) {
			return false
		}
		for i := range x {
			if !Eq(x[i], y[i]) {
				return false
			}
		}
		return true
	case pyjson.Object:
		y, ok := b.(pyjson.Object)
		if !ok || len(x) != len(y) {
			return false
		}
		for _, kv := range x {
			o, ok := y.Get(kv.Key)
			if !ok || !Eq(kv.Value, o) {
				return false
			}
		}
		return true
	}
	if ea, ok := a.(interface{ Equal(any) bool }); ok {
		return ea.Equal(b)
	}
	return false
}

// numeric is a bool, an int or a float as an exact number; NaN is never equal.
func numeric(v any) (*big.Float, bool) {
	switch x := v.(type) {
	case bool:
		if x {
			return big.NewFloat(1), true
		}
		return big.NewFloat(0), true
	case int64:
		return new(big.Float).SetInt64(x), true
	case int:
		return new(big.Float).SetInt64(int64(x)), true
	case pyjson.Int:
		f, _, err := big.ParseFloat(string(x), 10, 4096, big.ToNearestEven)
		if err != nil {
			return nil, false
		}
		return f, true
	case float64:
		if math.IsInf(x, 0) {
			return new(big.Float).SetInf(x < 0), true
		}
		return new(big.Float).SetFloat64(x), true
	}
	return nil, false
}

// In is `v in options`.
func In(v any, options ...any) bool {
	for _, o := range options {
		if Eq(v, o) {
			return true
		}
	}
	return false
}

// Hashable is false for what Python cannot put in a set or look up in a dict (a list, a dict):
// a TypeError, a 500.
func Hashable(v any) bool {
	switch v.(type) {
	case []any, pyjson.Object:
		return false
	}
	return true
}
