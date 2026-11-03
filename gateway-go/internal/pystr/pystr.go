// Package pystr writes values read from JSON (pyjson.Loads) as Python's str() and repr() do,
// for the messages the Python gateway builds with them (`f"… «{str(kind)[:20]}»"`), and cuts
// text by characters as Python's slices do.
package pystr

import (
	"fmt"
	"math"
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
