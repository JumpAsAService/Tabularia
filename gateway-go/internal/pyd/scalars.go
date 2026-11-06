package pyd

import (
	"math"
	"math/big"
	"strconv"
	"strings"
	"unicode"
)

// trimRust is Rust's str::trim (the Unicode White_Space property, which is what
// Go's unicode.IsSpace implements): pydantic-core trims numbers with it.
func trimRust(s string) string { return strings.TrimFunc(s, unicode.IsSpace) }

// strToInt is pydantic-core's str_as_int: trimmed, parsed as an integer, else
// with Python's underscores, else with a fractional part made only of zeros
// ("5.00" is 5, "5." and "5.5" are not).
func strToInt(input any, raw string) (any, *Error) {
	s := trimRust(raw)
	v, ok := parseIntText(s)
	if !ok {
		if i := strings.IndexByte(s, '.'); i >= 0 && i+1 < len(s) && strings.Trim(s[i+1:], "0") == "" {
			v, ok = parseIntText(s[:i])
		}
	}
	if !ok {
		return nil, errIntParsing(input)
	}
	// the value goes to Python through its decimal text, which Python caps at 4300 digits
	if b, isBig := v.(*big.Int); isBig {
		digits := len(b.String())
		if b.Sign() < 0 {
			digits--
		}
		if digits > pyIntMaxDigits {
			return nil, errIntParsingSize(input)
		}
	}
	return v, nil
}

// parseIntText is _parse_str: Rust's i64 parse below 19 bytes, num-bigint's above,
// then the same text without underscores standing between two digits.
func parseIntText(s string) (any, bool) {
	if v, ok := parseIntPlain(s); ok {
		return v, true
	}
	if !strings.Contains(s, "_") {
		return nil, false
	}
	for i := 0; i < len(s); i++ {
		if s[i] == '_' && (i == 0 || i == len(s)-1 || !isASCIIDigit(s[i-1]) || !isASCIIDigit(s[i+1])) {
			return nil, false
		}
	}
	return parseIntPlain(strings.ReplaceAll(s, "_", ""))
}

func isASCIIDigit(c byte) bool { return c >= '0' && c <= '9' }

func parseIntPlain(s string) (any, bool) {
	if len(s) < 19 {
		// Rust's i64::from_str: an optional sign and at least one digit
		t := s
		if t != "" && (t[0] == '+' || t[0] == '-') {
			t = t[1:]
		}
		if t == "" {
			return nil, false
		}
		for i := 0; i < len(t); i++ {
			if !isASCIIDigit(t[i]) {
				return nil, false
			}
		}
		v, err := strconv.ParseInt(s, 10, 64)
		return v, err == nil
	}
	// num-bigint's BigInt::from_str: '-' for BigInt, then BigUint's own '+',
	// no leading underscore, the other underscores skipped
	t := s
	neg := false
	if strings.HasPrefix(t, "-") {
		neg = true
		t = t[1:]
	}
	if strings.HasPrefix(t, "+") && !strings.HasPrefix(t[1:], "+") {
		t = t[1:]
	}
	if t == "" || t[0] == '_' {
		return nil, false
	}
	var digits strings.Builder
	for i := 0; i < len(t); i++ {
		switch {
		case isASCIIDigit(t[i]):
			digits.WriteByte(t[i])
		case t[i] == '_':
		default:
			return nil, false
		}
	}
	if digits.Len() == 0 {
		return nil, false
	}
	b, _ := new(big.Int).SetString(digits.String(), 10)
	if neg {
		b.Neg(b)
	}
	return normInt(b), true
}

// strToFloat is pydantic-core's str_as_float: Rust's f64 parse of the trimmed
// text, else of the untrimmed text without its underscores (refused when it starts
// or ends with one, or holds two in a row).
func strToFloat(input any, s string) (float64, *Error) {
	if f, ok := parseRustFloat(trimRust(s)); ok {
		return f, nil
	}
	if !strings.HasPrefix(s, "_") && !strings.HasSuffix(s, "_") && !strings.Contains(s, "__") {
		if f, ok := parseRustFloat(strings.ReplaceAll(s, "_", "")); ok {
			return f, nil
		}
	}
	return 0, errFloatParsing(input)
}

// parseRustFloat is f64::from_str: [+-]? (inf | infinity | nan | digits[.digits] |
// .digits) ([eE][+-]?digits)?, case-insensitive specials, overflow to infinity.
func parseRustFloat(s string) (float64, bool) {
	t := s
	neg := false
	if t != "" && (t[0] == '+' || t[0] == '-') {
		neg = t[0] == '-'
		t = t[1:]
	}
	switch strings.ToLower(t) {
	case "inf", "infinity":
		if neg {
			return math.Inf(-1), true
		}
		return math.Inf(1), true
	case "nan":
		return math.NaN(), true
	}
	i := 0
	intDigits := 0
	for i < len(t) && isASCIIDigit(t[i]) {
		i++
		intDigits++
	}
	fracDigits := 0
	if i < len(t) && t[i] == '.' {
		i++
		for i < len(t) && isASCIIDigit(t[i]) {
			i++
			fracDigits++
		}
	}
	if intDigits == 0 && fracDigits == 0 {
		return 0, false
	}
	if i < len(t) && (t[i] == 'e' || t[i] == 'E') {
		i++
		if i < len(t) && (t[i] == '+' || t[i] == '-') {
			i++
		}
		expDigits := 0
		for i < len(t) && isASCIIDigit(t[i]) {
			i++
			expDigits++
		}
		if expDigits == 0 {
			return 0, false
		}
	}
	if i != len(t) {
		return 0, false
	}
	f, err := strconv.ParseFloat(s, 64)
	if err != nil {
		if ne, ok := err.(*strconv.NumError); !ok || ne.Err != strconv.ErrRange {
			return 0, false
		}
	}
	return f, true
}

// boolWords are the strings pydantic reads as booleans (any case, untrimmed).
var boolWords = map[string]bool{
	"0": false, "off": false, "f": false, "false": false, "n": false, "no": false,
	"1": true, "on": true, "t": true, "true": true, "y": true, "yes": true,
}

func strToBool(input any, s string) (bool, *Error) {
	if b, ok := boolWords[strings.ToLower(s)]; ok {
		return b, nil
	}
	return false, errBoolParsing(input)
}

// floatToInt is pydantic's int from a float in lax mode: whole numbers strictly
// inside the i64 range only (-2**63 itself is refused).
func floatToInt(input any, f float64) (any, *Error) {
	if math.IsNaN(f) || math.IsInf(f, 0) {
		return nil, errFinite(input)
	}
	if f != math.Trunc(f) {
		return nil, errIntFromFloat(input)
	}
	if !floatInI64(f) {
		return nil, errIntParsingSize(input)
	}
	return int64(f), nil
}

// intToFloat is Python's float(int): rounded, an int beyond the float range refused.
func intToFloat(input any, v any) (float64, *Error) {
	switch x := v.(type) {
	case int64:
		return float64(x), nil
	case *big.Int:
		f, _ := new(big.Float).SetInt(x).Float64()
		if math.IsInf(f, 0) {
			return 0, errFloatType(input)
		}
		return f, nil
	}
	return 0, errFloatType(input)
}

// floatInI64 is pydantic-core's range test for a float read as an int.
func floatInI64(f float64) bool { return f < 9223372036854775808.0 && f > -9223372036854775808.0 }

// errStringUnicode is what pydantic-core says when it needs a str as UTF-8 (to
// parse it, or to measure or match it) and the str holds a lone surrogate.
func errStringUnicode(v any) *Error {
	return newErr("string_unicode", "Input should be a valid string, unable to parse raw data as a unicode string", v)
}
