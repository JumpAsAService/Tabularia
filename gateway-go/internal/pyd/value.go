package pyd

import (
	"math"
	"math/big"
	"strings"
	"unicode"
	"unicode/utf8"
)

// Object is a Python dict as json.loads builds it: a repeated key keeps the
// position of its first occurrence and the value of its last one.
type Object struct {
	keys []string
	vals map[string]any
}

// NewObject returns an empty dict.
func NewObject() *Object { return &Object{vals: map[string]any{}} }

// Set adds or replaces a key, Python-style: a replaced key does not move.
func (o *Object) Set(k string, v any) {
	if _, ok := o.vals[k]; !ok {
		o.keys = append(o.keys, k)
	}
	o.vals[k] = v
}

// Get returns a key's value and whether it is there.
func (o *Object) Get(k string) (any, bool) {
	v, ok := o.vals[k]
	return v, ok
}

// Keys returns the keys in order (the slice must not be changed).
func (o *Object) Keys() []string { return o.keys }

// Len is the number of keys.
func (o *Object) Len() int { return len(o.keys) }

// RawBytes is a body FastAPI did not parse (a content type that is not JSON): the
// model receives the bytes, and the 422 echoes them decoded as UTF-8.
type RawBytes []byte

// isInt reports whether v is a Python int (bool excluded, as pydantic does in
// strict mode).
func isInt(v any) bool {
	switch v.(type) {
	case int64, *big.Int:
		return true
	}
	return false
}

// normInt returns a *big.Int as int64 when it fits, so equal values compare equal.
func normInt(b *big.Int) any {
	if b.IsInt64() {
		return b.Int64()
	}
	return b
}

// toBig widens an int value.
func toBig(v any) *big.Int {
	switch x := v.(type) {
	case int64:
		return big.NewInt(x)
	case *big.Int:
		return x
	}
	return nil
}

// cmpIntFloat compares a Python int with a float the way Python does: exactly,
// not by first rounding the int to a float.
func cmpIntFloat(i any, f float64) int {
	if math.IsInf(f, 1) {
		return -1
	}
	if math.IsInf(f, -1) {
		return 1
	}
	bf := new(big.Float).SetPrec(0).SetInt(toBig(i))
	return bf.Cmp(new(big.Float).SetFloat64(f))
}

// pyLen is Python's len() of a str: code points, a lone surrogate (WTF-8) being one.
func pyLen(s string) int {
	n := 0
	for i := 0; i < len(s); {
		_, w := decodeWTF8(s[i:])
		i += w
		n++
	}
	return n
}

// decodeWTF8 decodes one code point, accepting the 3-byte encoding of a surrogate
// that standard UTF-8 refuses. Invalid bytes count one each.
func decodeWTF8(s string) (rune, int) {
	r, w := utf8.DecodeRuneInString(s)
	if r == utf8.RuneError && w == 1 && len(s) >= 3 && s[0] == 0xED && s[1] >= 0xA0 && s[1] <= 0xBF && s[2] >= 0x80 && s[2] <= 0xBF {
		return rune(0xD000 | rune(s[1]&0x3F)<<6 | rune(s[2]&0x3F)), 3
	}
	return r, w
}

// appendWTF8 encodes a code point, surrogates included.
func appendWTF8(b []byte, r rune) []byte {
	if r >= 0xD800 && r <= 0xDFFF {
		return append(b, 0xED, byte(0x80|(r>>6)&0x3F), byte(0x80|r&0x3F))
	}
	return utf8.AppendRune(b, r)
}

// hasSurrogate reports whether a string holds a lone surrogate: Python can keep
// it in a str but cannot encode it as UTF-8.
func hasSurrogate(s string) bool {
	for i := 0; i < len(s); {
		r, w := decodeWTF8(s[i:])
		if r >= 0xD800 && r <= 0xDFFF {
			return true
		}
		i += w
	}
	return false
}

func nan() float64    { return math.NaN() }
func posInf() float64 { return math.Inf(1) }
func negInf() float64 { return math.Inf(-1) }

// Len is Python's len() of a str, for validators that count as pydantic does.
func Len(s string) int { return pyLen(s) }

// Strip is Python's str.strip(): the whitespace of str.isspace(), which also counts
// the separators U+001C..U+001F that Go's unicode.IsSpace leaves out.
func Strip(s string) string {
	return strings.TrimFunc(s, func(r rune) bool {
		return unicode.IsSpace(r) || (r >= 0x1C && r <= 0x1F)
	})
}

// Lower is Python's str.lower() where it differs from Go's: 'İ' becomes "i̇".
func Lower(s string) string {
	return strings.ToLower(strings.ReplaceAll(s, "İ", "i̇"))
}
