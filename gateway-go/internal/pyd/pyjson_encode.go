package pyd

import (
	"errors"
	"fmt"
	"math"
	"math/big"
	"strconv"
	"strings"
	"unicode/utf8"
)

// errNotJSON is the ValueError Starlette's JSONResponse raises on what json.dumps
// (allow_nan=False) or .encode("utf-8") refuse: NaN and the infinities, lone
// surrogates, and a raw body that is not UTF-8 (jsonable_encoder decodes bytes).
var errNotJSON = errors.New("value not representable in JSON")

// dumpsJSON is Starlette's JSONResponse.render: json.dumps(ensure_ascii=False,
// allow_nan=False, separators=(",", ":")) and then UTF-8.
func dumpsJSON(v any) ([]byte, error) {
	return encoder{}.append(nil, v)
}

// dumpsLenient is json.dumps(allow_nan=True, ensure_ascii=False) encoded with
// 'surrogatepass': what Python writes when nothing may fail.
func dumpsLenient(v any) ([]byte, error) {
	return encoder{lenient: true}.append(nil, v)
}

type encoder struct {
	lenient bool
}

func (en encoder) append(b []byte, v any) ([]byte, error) {
	switch x := v.(type) {
	case nil:
		return append(b, "null"...), nil
	case bool:
		if x {
			return append(b, "true"...), nil
		}
		return append(b, "false"...), nil
	case int:
		return strconv.AppendInt(b, int64(x), 10), nil
	case int64:
		return strconv.AppendInt(b, x, 10), nil
	case *big.Int:
		return x.Append(b, 10), nil
	case float64:
		switch {
		case !math.IsNaN(x) && !math.IsInf(x, 0):
			return append(b, pyFloatRepr(x)...), nil
		case !en.lenient:
			return nil, errNotJSON
		case math.IsNaN(x):
			return append(b, "NaN"...), nil
		case x > 0:
			return append(b, "Infinity"...), nil
		}
		return append(b, "-Infinity"...), nil
	case string:
		return en.appendString(b, x)
	case RawBytes:
		if !utf8.Valid(x) {
			return nil, errNotJSON
		}
		return en.appendString(b, string(x))
	case []any:
		b = append(b, '[')
		for i, e := range x {
			if i > 0 {
				b = append(b, ',')
			}
			var err error
			if b, err = en.append(b, e); err != nil {
				return nil, err
			}
		}
		return append(b, ']'), nil
	case *Object:
		b = append(b, '{')
		for i, k := range x.keys {
			if i > 0 {
				b = append(b, ',')
			}
			var err error
			if b, err = en.appendString(b, k); err != nil {
				return nil, err
			}
			b = append(b, ':')
			if b, err = en.append(b, x.vals[k]); err != nil {
				return nil, err
			}
		}
		return append(b, '}'), nil
	case Loc:
		return en.append(b, []any(x))
	}
	panic(fmt.Sprintf("pyd: cannot encode %T", v))
}

// appendString is c_encode_basestring (ensure_ascii=False): only the quote, the
// backslash and the C0 controls are escaped, everything else goes out as is. A
// lone surrogate cannot become UTF-8 (lenient: it goes out as WTF-8).
func (en encoder) appendString(b []byte, s string) ([]byte, error) {
	if !en.lenient && !utf8.ValidString(s) {
		return nil, errNotJSON
	}
	b = append(b, '"')
	for i := 0; i < len(s); i++ {
		c := s[i]
		switch {
		case c == '"':
			b = append(b, '\\', '"')
		case c == '\\':
			b = append(b, '\\', '\\')
		case c == '\n':
			b = append(b, '\\', 'n')
		case c == '\r':
			b = append(b, '\\', 'r')
		case c == '\t':
			b = append(b, '\\', 't')
		case c == '\b':
			b = append(b, '\\', 'b')
		case c == '\f':
			b = append(b, '\\', 'f')
		case c < 0x20:
			b = append(b, fmt.Sprintf("\\u%04x", c)...)
		default:
			b = append(b, c)
		}
	}
	return append(b, '"'), nil
}

// pyFloatRepr is Python's repr(float): the shortest digits that round-trip, in
// positional notation when the exponent is in [-4, 16), else "d.ddde+XX"; an
// integral value keeps its ".0".
func pyFloatRepr(f float64) string {
	if math.IsInf(f, 1) {
		return "inf"
	}
	if math.IsInf(f, -1) {
		return "-inf"
	}
	if math.IsNaN(f) {
		return "nan"
	}
	e := strconv.FormatFloat(f, 'e', -1, 64) // e.g. "-1.2345e+17"
	sign := ""
	if e[0] == '-' {
		sign = "-"
		e = e[1:]
	}
	mant, expStr, _ := strings.Cut(e, "e")
	exp, _ := strconv.Atoi(expStr)
	digits := strings.Replace(mant, ".", "", 1)
	if exp < -4 || exp >= 16 {
		m := digits[:1]
		if len(digits) > 1 {
			m += "." + digits[1:]
		}
		es := "+"
		if exp < 0 {
			es = "-"
			exp = -exp
		}
		return fmt.Sprintf("%s%se%s%02d", sign, m, es, exp)
	}
	if exp < 0 {
		return sign + "0." + strings.Repeat("0", -exp-1) + digits
	}
	if len(digits) <= exp+1 {
		return sign + digits + strings.Repeat("0", exp+1-len(digits)) + ".0"
	}
	return sign + digits[:exp+1] + "." + digits[exp+1:]
}

// pyRepr is Python's repr() of the values a Literal or an enum can hold, as
// pydantic writes them in `expected`.
func pyRepr(v any) string {
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
	case *big.Int:
		return x.String()
	case float64:
		return pyFloatRepr(x)
	case string:
		return pyStrRepr(x)
	}
	return fmt.Sprint(v)
}

// pyStrRepr is repr(str) for printable text: single quotes unless the text holds
// a single quote and no double one.
func pyStrRepr(s string) string {
	q := byte('\'')
	if strings.Contains(s, "'") && !strings.Contains(s, "\"") {
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
		default:
			b.WriteRune(r)
		}
	}
	b.WriteByte(q)
	return b.String()
}
