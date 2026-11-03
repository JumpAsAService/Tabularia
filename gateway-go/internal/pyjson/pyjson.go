// Package pyjson writes JSON exactly as Python's json.dumps does, for the text the Python
// gateway stores in the database (an audit event's detail, json.dumps(detail, default=str,
// ensure_ascii=False)) and so that both gateways leave the same bytes behind: the
// separators ", " and ": " (or "," and ":" when compact), the keys in the order they were
// added, the strings escaped as Python escapes them, and the floats as Python's repr writes
// them (1.0, 1e+16, 1000000000000000.0).
package pyjson

import (
	"fmt"
	"math"
	"reflect"
	"sort"
	"strconv"
	"strings"
	"time"
	"unicode/utf16"
)

// KV is one key of an Object.
type KV struct {
	Key   string
	Value any
}

// Object is a Python dict: its keys keep the order they were added in.
type Object []KV

// Set adds the key, or replaces its value where it already is (as `d[k] = v`).
func (o Object) Set(key string, value any) Object {
	for i := range o {
		if o[i].Key == key {
			o[i].Value = value
			return o
		}
	}
	return append(o, KV{key, value})
}

// Get returns a key's value.
func (o Object) Get(key string) (any, bool) {
	for _, kv := range o {
		if kv.Key == key {
			return kv.Value, true
		}
	}
	return nil, false
}

// Options are json.dumps' arguments the gateway uses.
type Options struct {
	EnsureASCII bool // ensure_ascii (Python's default is true)
	Compact     bool // separators=(",", ":")
	SortKeys    bool // sort_keys
}

// Dumps is json.dumps(v, …) with the options; values: nil, bool, ints, floats, string,
// Object, map[string]any (sorted keys, as Go maps have no order: use Object to keep one),
// slices, time.Time (default=str: str(datetime)), fmt.Stringer (default=str).
func Dumps(v any, o Options) (string, error) {
	var b strings.Builder
	if err := write(&b, v, o); err != nil {
		return "", err
	}
	return b.String(), nil
}

// Audit is json.dumps(detail, default=str, ensure_ascii=False), as services/audit.py
// writes an event's detail.
func Audit(v any) string {
	s, err := Dumps(v, Options{})
	if err != nil {
		return fmt.Sprintf("%q", err.Error())
	}
	return s
}

func write(b *strings.Builder, v any, o Options) error {
	item, kv := ", ", ": "
	if o.Compact {
		item, kv = ",", ":"
	}
	switch x := v.(type) {
	case nil:
		b.WriteString("null")
	case bool:
		if x {
			b.WriteString("true")
		} else {
			b.WriteString("false")
		}
	case string:
		writeString(b, x, o.EnsureASCII)
	case int:
		b.WriteString(strconv.Itoa(x))
	case int32:
		b.WriteString(strconv.FormatInt(int64(x), 10))
	case int64:
		b.WriteString(strconv.FormatInt(x, 10))
	case uint64:
		b.WriteString(strconv.FormatUint(x, 10))
	case BigInt:
		b.WriteString(string(x))
	case float32:
		b.WriteString(FloatRepr(float64(x)))
	case float64:
		b.WriteString(FloatRepr(x))
	case Object:
		keys := x
		if o.SortKeys {
			keys = append(Object(nil), x...)
			sort.SliceStable(keys, func(i, j int) bool { return keys[i].Key < keys[j].Key })
		}
		b.WriteByte('{')
		for i, p := range keys {
			if i > 0 {
				b.WriteString(item)
			}
			writeString(b, p.Key, o.EnsureASCII)
			b.WriteString(kv)
			if err := write(b, p.Value, o); err != nil {
				return err
			}
		}
		b.WriteByte('}')
	case map[string]any:
		keys := make([]string, 0, len(x))
		for k := range x {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		obj := make(Object, 0, len(keys))
		for _, k := range keys {
			obj = append(obj, KV{k, x[k]})
		}
		return write(b, obj, o)
	case time.Time:
		writeString(b, DatetimeStr(x), o.EnsureASCII)
	case fmt.Stringer:
		writeString(b, x.String(), o.EnsureASCII)
	default:
		rv := reflect.ValueOf(v)
		if rv.Kind() == reflect.Slice || rv.Kind() == reflect.Array {
			b.WriteByte('[')
			for i := 0; i < rv.Len(); i++ {
				if i > 0 {
					b.WriteString(item)
				}
				if err := write(b, rv.Index(i).Interface(), o); err != nil {
					return err
				}
			}
			b.WriteByte(']')
			return nil
		}
		if rv.Kind() == reflect.Pointer {
			if rv.IsNil() {
				b.WriteString("null")
				return nil
			}
			return write(b, rv.Elem().Interface(), o)
		}
		return fmt.Errorf("pyjson: %T is not JSON serializable", v)
	}
	return nil
}

// writeString escapes as Python's json module: \" \\ \n \r \t \b \f, every other control
// character as \u00XX, and with ensure_ascii every non-ASCII character as \uXXXX (UTF-16,
// surrogate pairs past the BMP). '/' and '<' are left as they are.
func writeString(b *strings.Builder, s string, ascii bool) {
	b.WriteByte('"')
	for _, r := range s {
		switch r {
		case '"':
			b.WriteString(`\"`)
		case '\\':
			b.WriteString(`\\`)
		case '\n':
			b.WriteString(`\n`)
		case '\r':
			b.WriteString(`\r`)
		case '\t':
			b.WriteString(`\t`)
		case '\b':
			b.WriteString(`\b`)
		case '\f':
			b.WriteString(`\f`)
		default:
			switch {
			case r < 0x20 || (ascii && r == 0x7f):
				fmt.Fprintf(b, `\u%04x`, r)
			case ascii && r > 0x7f:
				if r > 0xffff {
					hi, lo := utf16.EncodeRune(r)
					fmt.Fprintf(b, `\u%04x\u%04x`, hi, lo)
				} else {
					fmt.Fprintf(b, `\u%04x`, r)
				}
			default:
				b.WriteRune(r)
			}
		}
	}
	b.WriteByte('"')
}

// FloatRepr is Python's repr(float): the shortest digits that read back the same, fixed
// notation for exponents from -4 to 15 (always with a decimal point), scientific otherwise
// with a two-digit exponent at least; Infinity, -Infinity and NaN as json.dumps writes them.
func FloatRepr(f float64) string {
	switch {
	case math.IsInf(f, 1):
		return "Infinity"
	case math.IsInf(f, -1):
		return "-Infinity"
	case math.IsNaN(f):
		return "NaN"
	}
	if f == 0 {
		if math.Signbit(f) {
			return "-0.0"
		}
		return "0.0"
	}
	// the shortest digits and the decimal exponent, from the scientific form
	e := strconv.FormatFloat(f, 'e', -1, 64) // e.g. -1.2345e+16
	sign := ""
	if e[0] == '-' {
		sign, e = "-", e[1:]
	}
	mant, expPart, _ := strings.Cut(e, "e")
	exp, _ := strconv.Atoi(expPart)
	digits := strings.Replace(mant, ".", "", 1)
	if exp < -4 || exp >= 16 {
		m := digits[:1]
		if len(digits) > 1 {
			m += "." + digits[1:]
		}
		es := "+"
		if exp < 0 {
			es, exp = "-", -exp
		}
		return fmt.Sprintf("%s%se%s%02d", sign, m, es, exp)
	}
	var s string
	switch {
	case exp < 0:
		s = "0." + strings.Repeat("0", -exp-1) + digits
	case exp+1 >= len(digits):
		s = digits + strings.Repeat("0", exp+1-len(digits)) + ".0"
	default:
		s = digits[:exp+1] + "." + digits[exp+1:]
	}
	return sign + s
}

// DatetimeStr is str(datetime) of a naive one, as default=str writes it: a space between
// date and time, the microseconds only when there are some.
func DatetimeStr(t time.Time) string {
	if t.Nanosecond()/1000 != 0 {
		return t.Format("2006-01-02 15:04:05.000000")
	}
	return t.Format("2006-01-02 15:04:05")
}

// Isoformat is datetime.isoformat() of a naive UTC datetime made aware (schemas/models.py
// `_utc_iso`): «2026-11-03T18:00:00+00:00», the microseconds only when there are some.
func Isoformat(t time.Time) string {
	t = t.UTC()
	if t.Nanosecond()/1000 != 0 {
		return t.Format("2006-01-02T15:04:05.000000") + "+00:00"
	}
	return t.Format("2006-01-02T15:04:05") + "+00:00"
}
