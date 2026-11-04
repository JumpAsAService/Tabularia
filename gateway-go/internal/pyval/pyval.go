// Package pyval runs over the values json.loads builds (pyjson: Object, []any, int64, Int,
// float64, string, bool, nil) the few operations of Python the gateway's code does on a JSON
// it does not trust (a flow's definition): `d.get(k)`, `d[k]`, iterating, hashing a value as
// a dict key, `str.strip()`, `int()`. Where Python raises (an AttributeError on a node that is
// not a dict, a KeyError, a TypeError on an unhashable key), these panic with an *Error
// carrying the exception's type and message, so that the callers that catch them (the dbt
// export says «its definition is not valid (KeyError: 'id')») say the same; Catch turns the
// panic back into an error.
package pyval

import (
	"fmt"
	"math"
	"math/big"
	"strconv"
	"strings"
	"unicode"
	"unicode/utf8"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pystr"
)

// Error is a Python exception: its class name and str(e).
type Error struct {
	Type string
	Msg  string
}

func (e *Error) Error() string { return e.Type + ": " + e.Msg }

// Raise panics with the exception.
func Raise(typ, format string, args ...any) {
	panic(&Error{Type: typ, Msg: fmt.Sprintf(format, args...)})
}

// Catch runs f; a panic with an *Error (or an error the caller raised with Throw) comes back
// as the error, any other panic goes on.
func Catch(f func()) (err error) {
	defer func() {
		if r := recover(); r != nil {
			switch x := r.(type) {
			case *Error:
				err = x
			case thrown:
				err = x.err
			default:
				panic(r)
			}
		}
	}()
	f()
	return nil
}

type thrown struct{ err error }

// Throw unwinds to the nearest Catch with err (an error of the caller's own: a refusal, a
// database error), as raising does in Python.
func Throw(err error) { panic(thrown{err}) }

// Check throws a non-nil error.
func Check(err error) {
	if err != nil {
		Throw(err)
	}
}

// TypeName is type(v).__name__.
func TypeName(v any) string {
	switch v.(type) {
	case nil:
		return "NoneType"
	case bool:
		return "bool"
	case int, int64, pyjson.Int:
		return "int"
	case float64:
		return "float"
	case string:
		return "str"
	case pyjson.Object:
		return "dict"
	case []any:
		return "list"
	}
	return fmt.Sprintf("%T", v)
}

// Get is v.get(key) (None when missing); v must be a dict.
func Get(v any, key string) any {
	o, ok := v.(pyjson.Object)
	if !ok {
		Raise("AttributeError", "'%s' object has no attribute 'get'", TypeName(v))
	}
	return o.Value(key)
}

// GetOr is v.get(key, def).
func GetOr(v any, key string, def any) any {
	o, ok := v.(pyjson.Object)
	if !ok {
		Raise("AttributeError", "'%s' object has no attribute 'get'", TypeName(v))
	}
	if x, has := o.Get(key); has {
		return x
	}
	return def
}

// Index is v[key] with a text key.
func Index(v any, key string) any {
	switch x := v.(type) {
	case pyjson.Object:
		if e, ok := x.Get(key); ok {
			return e
		}
		Raise("KeyError", "%s", pystr.Repr(key))
	case string:
		Raise("TypeError", "string indices must be integers, not 'str'")
	case []any:
		Raise("TypeError", "list indices must be integers or slices, not str")
	}
	Raise("TypeError", "'%s' object is not subscriptable", TypeName(v))
	return nil
}

// Iter is `for x in v`: a list's items, a dict's keys, a text's characters.
func Iter(v any) []any {
	switch x := v.(type) {
	case []any:
		return x
	case pyjson.Object:
		seen := map[string]bool{}
		out := []any{}
		for _, kv := range x {
			if !seen[kv.Key] {
				seen[kv.Key] = true
				out = append(out, kv.Key)
			}
		}
		return out
	case string:
		out := make([]any, 0, len(x))
		for _, r := range x {
			out = append(out, string(r))
		}
		return out
	}
	Raise("TypeError", "'%s' object is not iterable", TypeName(v))
	return nil
}

// Truthy is bool(v).
func Truthy(v any) bool { return pystr.Truthy(v) }

// Or is `a or b`.
func Or(a, b any) any {
	if Truthy(a) {
		return a
	}
	return b
}

// Eq is Python's ==.
func Eq(a, b any) bool { return pystr.Eq(a, b) }

// Str is str(v).
func Str(v any) string { return pystr.Str(v) }

// Strip is v.strip() (whitespace as Python reads it); v must be a text.
func Strip(v any) string {
	s, ok := v.(string)
	if !ok {
		Raise("AttributeError", "'%s' object has no attribute 'strip'", TypeName(v))
	}
	return pystr.Strip(s)
}

// Key is the identity of v as a dict key or a set member: equal values (1, 1.0, True) give
// the same key, as their hash and == do; a list or a dict is unhashable.
func Key(v any) string {
	switch x := v.(type) {
	case nil:
		return "N"
	case string:
		return "s" + x
	case bool:
		if x {
			return "i1"
		}
		return "i0"
	case int:
		return "i" + strconv.Itoa(x)
	case int64:
		return "i" + strconv.FormatInt(x, 10)
	case pyjson.Int:
		return "i" + string(x)
	case float64:
		if !math.IsInf(x, 0) && !math.IsNaN(x) && x == math.Trunc(x) {
			b, _ := new(big.Float).SetFloat64(x).Int(nil)
			return "i" + b.String()
		}
		return "f" + strconv.FormatFloat(x, 'g', -1, 64)
	case Tuple:
		parts := make([]string, len(x))
		for i, e := range x {
			k := Key(e)
			parts[i] = strconv.Itoa(len(k)) + ":" + k
		}
		return "t" + strings.Join(parts, "")
	}
	Raise("TypeError", "unhashable type: '%s'", TypeName(v))
	return ""
}

// Tuple is a Python tuple used as a dict key.
type Tuple []any

// Dict is a Python dict with keys of any hashable value: a key set again keeps its first place.
type Dict struct {
	keys []any
	vals []any
	at   map[string]int
}

// NewDict is an empty dict.
func NewDict() *Dict { return &Dict{at: map[string]int{}} }

// Set is d[k] = v.
func (d *Dict) Set(k, v any) {
	h := Key(k)
	if i, ok := d.at[h]; ok {
		d.vals[i] = v
		return
	}
	d.at[h] = len(d.keys)
	d.keys = append(d.keys, k)
	d.vals = append(d.vals, v)
}

// SetDefault is d.setdefault(k, v).
func (d *Dict) SetDefault(k, v any) any {
	if x, ok := d.Lookup(k); ok {
		return x
	}
	d.Set(k, v)
	return v
}

// Lookup is the value of k and whether it is there.
func (d *Dict) Lookup(k any) (any, bool) {
	i, ok := d.at[Key(k)]
	if !ok {
		return nil, false
	}
	return d.vals[i], true
}

// Get is d.get(k).
func (d *Dict) Get(k any) any {
	v, _ := d.Lookup(k)
	return v
}

// Has is `k in d`.
func (d *Dict) Has(k any) bool {
	_, ok := d.Lookup(k)
	return ok
}

// Delete is d.pop(k, None).
func (d *Dict) Delete(k any) {
	h := Key(k)
	i, ok := d.at[h]
	if !ok {
		return
	}
	d.keys = append(d.keys[:i:i], d.keys[i+1:]...)
	d.vals = append(d.vals[:i:i], d.vals[i+1:]...)
	delete(d.at, h)
	for j := i; j < len(d.keys); j++ {
		d.at[Key(d.keys[j])] = j
	}
}

// Len is len(d).
func (d *Dict) Len() int { return len(d.keys) }

// Keys are the keys in order.
func (d *Dict) Keys() []any { return d.keys }

// Values are the values in order.
func (d *Dict) Values() []any { return d.vals }

// Int is int(v) for what json.loads gives: an int, a bool, a float cut towards zero, a text
// of an integer. A TypeError or a ValueError otherwise.
func Int(v any) int64 {
	switch x := v.(type) {
	case bool:
		if x {
			return 1
		}
		return 0
	case int64:
		return x
	case int:
		return int64(x)
	case pyjson.Int: // past int64: Python's int has no limit, the callers only compare it
		if strings.HasPrefix(string(x), "-") {
			return math.MinInt64
		}
		return math.MaxInt64
	case float64:
		if math.IsInf(x, 0) {
			Raise("OverflowError", "cannot convert float infinity to integer")
		}
		if math.IsNaN(x) {
			Raise("ValueError", "cannot convert float NaN to integer")
		}
		if x >= 9.2e18 {
			return math.MaxInt64
		}
		if x <= -9.2e18 {
			return math.MinInt64
		}
		return int64(x)
	case string:
		if n, ok := parseInt(x); ok {
			return n
		}
		Raise("ValueError", "invalid literal for int() with base 10: %s", pystr.Repr(x))
	}
	Raise("TypeError", "int() argument must be a string, a bytes-like object or a real number, not '%s'", TypeName(v))
	return 0
}

// parseInt is int(text): blanks around, a sign, digits (Unicode decimal digits too) with
// single underscores between them.
func parseInt(s string) (int64, bool) {
	s = pystr.Strip(s)
	neg := false
	if s != "" && (s[0] == '+' || s[0] == '-') {
		neg = s[0] == '-'
		s = s[1:]
	}
	if s == "" || s[0] == '_' || s[len(s)-1] == '_' || strings.Contains(s, "__") {
		return 0, false
	}
	var b strings.Builder
	for _, r := range s {
		if r == '_' {
			continue
		}
		if !unicode.Is(unicode.Nd, r) {
			return 0, false
		}
		b.WriteRune(rune('0' + digitValue(r)))
	}
	n, err := strconv.ParseInt(b.String(), 10, 64)
	if err != nil { // past int64: see Int
		n = math.MaxInt64
	}
	if neg {
		n = -n
	}
	return n, true
}

// digitValue is the value of a Unicode decimal digit (the Nd blocks run 0 to 9).
func digitValue(r rune) int {
	for _, rt := range unicode.Nd.R16 {
		if uint16(r) >= rt.Lo && uint16(r) <= rt.Hi && r <= 0xFFFF {
			return int(uint16(r)-rt.Lo) % 10
		}
	}
	for _, rt := range unicode.Nd.R32 {
		if uint32(r) >= rt.Lo && uint32(r) <= rt.Hi {
			return int(uint32(r)-rt.Lo) % 10
		}
	}
	return 0
}

// Float is float(v): a number, a bool, a text of a number. A TypeError or a ValueError otherwise.
func Float(v any) float64 {
	switch x := v.(type) {
	case bool:
		if x {
			return 1
		}
		return 0
	case int64:
		return float64(x)
	case int:
		return float64(x)
	case pyjson.Int:
		f, _ := new(big.Float).SetString(string(x))
		r, _ := f.Float64()
		return r
	case float64:
		return x
	case string:
		t := strings.ReplaceAll(pystr.Strip(x), "_", "")
		switch strings.ToLower(strings.TrimLeft(t, "+-")) {
		case "inf", "infinity", "nan":
			f, _ := strconv.ParseFloat(t, 64)
			return f
		}
		if f, err := strconv.ParseFloat(t, 64); err == nil && !strings.ContainsAny(t, "xXpP") {
			return f
		}
		Raise("ValueError", "could not convert string to float: %s", pystr.Repr(x))
	}
	Raise("TypeError", "float() argument must be a string or a real number, not '%s'", TypeName(v))
	return 0
}

// Less is a < b for what may be sorted: numbers with numbers, texts with texts; a TypeError
// otherwise.
func Less(a, b any) bool {
	fa, aNum := pystr.Number(a)
	fb, bNum := pystr.Number(b)
	if ba, ok := a.(bool); ok {
		fa, aNum = b2f(ba), true
	}
	if bb, ok := b.(bool); ok {
		fb, bNum = b2f(bb), true
	}
	if aNum && bNum {
		return fa < fb
	}
	sa, aStr := a.(string)
	sb, bStr := b.(string)
	if aStr && bStr {
		return lessText(sa, sb)
	}
	if ta, ok := a.(Tuple); ok {
		if tb, ok := b.(Tuple); ok {
			for i := 0; i < len(ta) && i < len(tb); i++ {
				if Eq(ta[i], tb[i]) {
					continue
				}
				return Less(ta[i], tb[i])
			}
			return len(ta) < len(tb)
		}
	}
	Raise("TypeError", "'<' not supported between instances of '%s' and '%s'", TypeName(a), TypeName(b))
	return false
}

func b2f(b bool) float64 {
	if b {
		return 1
	}
	return 0
}

// lessText compares two texts by code point, as Python does (Go's < compares UTF-8 bytes,
// which agrees on valid text).
func lessText(a, b string) bool {
	for a != "" && b != "" {
		ra, na := utf8.DecodeRuneInString(a)
		rb, nb := utf8.DecodeRuneInString(b)
		if ra != rb {
			return ra < rb
		}
		a, b = a[na:], b[nb:]
	}
	return a == "" && b != ""
}

// IsList is isinstance(v, list); IsDict is isinstance(v, dict); IsStr is isinstance(v, str).
func IsList(v any) bool { _, ok := v.([]any); return ok }
func IsDict(v any) bool { _, ok := v.(pyjson.Object); return ok }
func IsStr(v any) bool  { _, ok := v.(string); return ok }

// Len is len(v) for a text (code points), a list, a dict.
func Len(v any) int {
	switch x := v.(type) {
	case string:
		return utf8.RuneCountInString(x)
	case []any:
		return len(x)
	case pyjson.Object:
		return len(Iter(x))
	}
	Raise("TypeError", "object of type '%s' has no len()", TypeName(v))
	return 0
}

// List is list(v).
func List(v any) []any {
	items := Iter(v)
	return append([]any{}, items...)
}

// DictOf is dict(v) for what json.loads gives: a dict copied, a list of pairs.
func DictOf(v any) pyjson.Object {
	switch x := v.(type) {
	case pyjson.Object:
		return append(pyjson.Object{}, x...)
	case []any, string:
		out := pyjson.Object{}
		for i, e := range Iter(x) {
			var pair []any
			switch p := e.(type) {
			case []any:
				pair = p
			case string:
				pair = Iter(p)
			case pyjson.Object:
				pair = Iter(p)
			default:
				Raise("TypeError", "cannot convert dictionary update sequence element #%d to a sequence", i)
			}
			if len(pair) != 2 {
				Raise("ValueError", "dictionary update sequence element #%d has length %d; 2 is required", i, len(pair))
			}
			Key(pair[0])
			out = out.Set(JSONKey(pair[0]), pair[1])
		}
		return out
	}
	Raise("TypeError", "'%s' object is not iterable", TypeName(v))
	return nil
}

// JSONKey is the key json.dumps writes for a dict key: a text as it is, the others as JSON.
func JSONKey(k any) string {
	switch x := k.(type) {
	case string:
		return x
	case nil:
		return "null"
	case bool:
		if x {
			return "true"
		}
		return "false"
	case float64:
		return pyjson.FloatRepr(x)
	}
	return pystr.Str(k)
}
