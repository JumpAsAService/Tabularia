package pyd

import (
	"errors"
	"math/big"
	"strconv"
	"unicode/utf16"
	"unicode/utf8"
)

// JSONDecodeError is CPython's json.JSONDecodeError: FastAPI turns it into the
// `json_invalid` error, with Msg as ctx.error and Pos (a code-point index into the
// decoded text, not a byte offset) in the loc.
type JSONDecodeError struct {
	Msg string
	Pos int
}

func (e *JSONDecodeError) Error() string { return e.Msg + " at " + strconv.Itoa(e.Pos) }

// errBodyUnreadable is any other exception json.loads raises on a body (bytes that
// do not decode, an int beyond Python's 4300 digits, a nesting deeper than the C
// recursion limit): FastAPI answers 400 «There was an error parsing the body».
var errBodyUnreadable = errors.New("there was an error parsing the body")

// maxJSONDepth is where CPython's C scanner raises RecursionError: measured in the
// gateway's image, through FastAPI (testdata/make_cases.py, «depth» cases):
// the containers counted from the outermost one.
const maxJSONDepth = 9993

// pyIntMaxDigits is sys.get_int_max_str_digits(): int("1"*4301) raises ValueError.
const pyIntMaxDigits = 4300

// loadsJSON is json.loads(body) on bytes: the encoding guessed as
// json.detect_encoding does, decoded with 'surrogatepass', then scanned with the
// C scanner's rules (positions and messages).
func loadsJSON(body []byte) (any, error) {
	text, err := decodeJSONBytes(body)
	if err != nil {
		return nil, err
	}
	sc := scanner{s: text}
	idx := sc.skipWS(0)
	v, end, err := sc.scanOnce(idx, 0)
	if err != nil {
		var stop stopIteration
		if errors.As(err, &stop) {
			return nil, &JSONDecodeError{Msg: "Expecting value", Pos: int(stop)}
		}
		return nil, err
	}
	end = sc.skipWS(end)
	if end != len(text) {
		return nil, &JSONDecodeError{Msg: "Extra data", Pos: end}
	}
	return v, nil
}

// decodeJSONBytes is json.detect_encoding + bytes.decode(enc, 'surrogatepass').
func decodeJSONBytes(b []byte) ([]rune, error) {
	switch {
	case hasPrefix(b, 0x00, 0x00, 0xFE, 0xFF):
		return decodeUTF32(b[4:], true)
	case hasPrefix(b, 0xFF, 0xFE, 0x00, 0x00):
		return decodeUTF32(b[4:], false)
	case hasPrefix(b, 0xFE, 0xFF):
		return decodeUTF16(b[2:], true)
	case hasPrefix(b, 0xFF, 0xFE):
		return decodeUTF16(b[2:], false)
	case hasPrefix(b, 0xEF, 0xBB, 0xBF):
		return decodeUTF8(b[3:])
	}
	if len(b) >= 4 {
		if b[0] == 0 {
			if b[1] != 0 {
				return decodeUTF16(b, true)
			}
			return decodeUTF32(b, true)
		}
		if b[1] == 0 {
			if b[2] != 0 || b[3] != 0 {
				return decodeUTF16(b, false)
			}
			return decodeUTF32(b, false)
		}
	} else if len(b) == 2 {
		if b[0] == 0 {
			return decodeUTF16(b, true)
		}
		if b[1] == 0 {
			return decodeUTF16(b, false)
		}
	}
	return decodeUTF8(b)
}

func hasPrefix(b []byte, p ...byte) bool {
	if len(b) < len(p) {
		return false
	}
	for i, c := range p {
		if b[i] != c {
			return false
		}
	}
	return true
}

// decodeUTF8 is Python's strict UTF-8 decoder with 'surrogatepass': the encoded
// surrogates (ED A0..BF xx) pass, anything else invalid fails.
func decodeUTF8(b []byte) ([]rune, error) {
	out := make([]rune, 0, len(b))
	for i := 0; i < len(b); {
		r, w := utf8.DecodeRune(b[i:])
		if r == utf8.RuneError && w == 1 {
			if i+2 < len(b) && b[i] == 0xED && b[i+1] >= 0xA0 && b[i+1] <= 0xBF && b[i+2] >= 0x80 && b[i+2] <= 0xBF {
				out = append(out, 0xD000|rune(b[i+1]&0x3F)<<6|rune(b[i+2]&0x3F))
				i += 3
				continue
			}
			return nil, errBodyUnreadable
		}
		out = append(out, r)
		i += w
	}
	return out, nil
}

// decodeUTF16 keeps lone surrogates ('surrogatepass') and pairs the others.
func decodeUTF16(b []byte, bigEndian bool) ([]rune, error) {
	if len(b)%2 != 0 {
		return nil, errBodyUnreadable
	}
	u := make([]uint16, len(b)/2)
	for i := range u {
		if bigEndian {
			u[i] = uint16(b[2*i])<<8 | uint16(b[2*i+1])
		} else {
			u[i] = uint16(b[2*i+1])<<8 | uint16(b[2*i])
		}
	}
	out := make([]rune, 0, len(u))
	for i := 0; i < len(u); i++ {
		c := rune(u[i])
		if utf16.IsSurrogate(c) && c < 0xDC00 && i+1 < len(u) && u[i+1] >= 0xDC00 && u[i+1] <= 0xDFFF {
			out = append(out, utf16.DecodeRune(c, rune(u[i+1])))
			i++
			continue
		}
		out = append(out, c)
	}
	return out, nil
}

func decodeUTF32(b []byte, bigEndian bool) ([]rune, error) {
	if len(b)%4 != 0 {
		return nil, errBodyUnreadable
	}
	out := make([]rune, 0, len(b)/4)
	for i := 0; i < len(b); i += 4 {
		var c uint32
		if bigEndian {
			c = uint32(b[i])<<24 | uint32(b[i+1])<<16 | uint32(b[i+2])<<8 | uint32(b[i+3])
		} else {
			c = uint32(b[i+3])<<24 | uint32(b[i+2])<<16 | uint32(b[i+1])<<8 | uint32(b[i])
		}
		if c > 0x10FFFF {
			return nil, errBodyUnreadable
		}
		out = append(out, rune(c))
	}
	return out, nil
}

// stopIteration is the C scanner's StopIteration(idx): json.loads reports it as
// «Expecting value» at idx.
type stopIteration int

func (s stopIteration) Error() string { return "stop iteration" }

type scanner struct {
	s []rune
}

func isJSONWS(r rune) bool { return r == ' ' || r == '\t' || r == '\n' || r == '\r' }

func (sc *scanner) skipWS(i int) int {
	for i < len(sc.s) && isJSONWS(sc.s[i]) {
		i++
	}
	return i
}

func (sc *scanner) errAt(msg string, pos int) error { return &JSONDecodeError{Msg: msg, Pos: pos} }

// has reports whether the text at i spells word (the named constants).
func (sc *scanner) has(i int, word string) bool {
	n := 0
	for _, c := range word {
		if i+n >= len(sc.s) || sc.s[i+n] != c {
			return false
		}
		n++
	}
	return true
}

// scanOnce is _json.c scan_once_unicode.
func (sc *scanner) scanOnce(idx, depth int) (any, int, error) {
	if idx >= len(sc.s) {
		return nil, 0, stopIteration(idx)
	}
	switch sc.s[idx] {
	case '"':
		return sc.scanString(idx + 1)
	case '{':
		if depth+1 > maxJSONDepth {
			return nil, 0, errBodyUnreadable
		}
		return sc.parseObject(idx+1, depth+1)
	case '[':
		if depth+1 > maxJSONDepth {
			return nil, 0, errBodyUnreadable
		}
		return sc.parseArray(idx+1, depth+1)
	case 'n':
		if sc.has(idx, "null") {
			return nil, idx + 4, nil
		}
	case 't':
		if sc.has(idx, "true") {
			return true, idx + 4, nil
		}
	case 'f':
		if sc.has(idx, "false") {
			return false, idx + 5, nil
		}
	case 'N':
		if sc.has(idx, "NaN") {
			return nan(), idx + 3, nil
		}
	case 'I':
		if sc.has(idx, "Infinity") {
			return posInf(), idx + 8, nil
		}
	case '-':
		if sc.has(idx, "-Infinity") {
			return negInf(), idx + 9, nil
		}
	}
	return sc.matchNumber(idx)
}

func isDigit(r rune) bool { return r >= '0' && r <= '9' }

// matchNumber is _match_number_unicode: the longest JSON number at start, with
// its backtracking on a dangling exponent.
func (sc *scanner) matchNumber(start int) (any, int, error) {
	s := sc.s
	end := len(s) - 1
	idx := start
	if s[idx] == '-' {
		idx++
		if idx > end {
			return nil, 0, stopIteration(start)
		}
	}
	switch {
	case s[idx] >= '1' && s[idx] <= '9':
		idx++
		for idx <= end && isDigit(s[idx]) {
			idx++
		}
	case s[idx] == '0':
		idx++
	default:
		return nil, 0, stopIteration(start)
	}
	isFloat := false
	if idx < end && s[idx] == '.' && isDigit(s[idx+1]) {
		isFloat = true
		idx += 2
		for idx <= end && isDigit(s[idx]) {
			idx++
		}
	}
	if idx < end && (s[idx] == 'e' || s[idx] == 'E') {
		eStart := idx
		idx++
		if idx < end && (s[idx] == '-' || s[idx] == '+') {
			idx++
		}
		for idx <= end && isDigit(s[idx]) {
			idx++
		}
		if isDigit(s[idx-1]) {
			isFloat = true
		} else {
			idx = eStart
		}
	}
	text := string(s[start:idx])
	if isFloat {
		f, err := strconv.ParseFloat(text, 64)
		if err != nil && !errors.Is(err, strconv.ErrRange) {
			return nil, 0, errBodyUnreadable
		}
		return f, idx, nil
	}
	digits := len(text)
	if text[0] == '-' {
		digits--
	}
	if digits > pyIntMaxDigits {
		return nil, 0, errBodyUnreadable
	}
	if i, err := strconv.ParseInt(text, 10, 64); err == nil {
		return i, idx, nil
	}
	b, _ := new(big.Int).SetString(text, 10)
	return b, idx, nil
}

// scanString is scanstring_unicode (strict): end is the index after the quote.
func (sc *scanner) scanString(end int) (any, int, error) {
	s := sc.s
	n := len(s)
	begin := end - 1
	var out []byte
	for {
		next := end
		var c rune
		for ; next < n; next++ {
			c = s[next]
			if c == '"' || c == '\\' {
				break
			}
			if c <= 0x1f {
				return nil, 0, sc.errAt("Invalid control character at", next)
			}
		}
		if next == n {
			return nil, 0, sc.errAt("Unterminated string starting at", begin)
		}
		for _, r := range s[end:next] {
			out = appendWTF8(out, r)
		}
		if c == '"' {
			return string(out), next + 1, nil
		}
		next++
		if next == n {
			return nil, 0, sc.errAt("Unterminated string starting at", begin)
		}
		c = s[next]
		if c != 'u' {
			end = next + 1
			switch c {
			case '"', '\\', '/':
			case 'b':
				c = '\b'
			case 'f':
				c = '\f'
			case 'n':
				c = '\n'
			case 'r':
				c = '\r'
			case 't':
				c = '\t'
			default:
				return nil, 0, sc.errAt("Invalid \\escape", end-2)
			}
			out = appendWTF8(out, c)
			continue
		}
		next++
		end = next + 4
		if end >= n {
			return nil, 0, sc.errAt("Invalid \\uXXXX escape", next-1)
		}
		cp, ok := hex4(s[next:end])
		if !ok {
			return nil, 0, sc.errAt("Invalid \\uXXXX escape", end-5)
		}
		next = end
		if cp >= 0xD800 && cp <= 0xDBFF && end+6 < n && s[next] == '\\' && s[next+1] == 'u' {
			end += 6
			c2, ok := hex4(s[next+2 : end])
			if !ok {
				return nil, 0, sc.errAt("Invalid \\uXXXX escape", end-5)
			}
			if c2 >= 0xDC00 && c2 <= 0xDFFF {
				cp = utf16.DecodeRune(cp, c2)
			} else {
				end -= 6
			}
		}
		out = appendWTF8(out, cp)
	}
}

func hex4(rs []rune) (rune, bool) {
	var c rune
	for _, d := range rs {
		c <<= 4
		switch {
		case d >= '0' && d <= '9':
			c |= d - '0'
		case d >= 'a' && d <= 'f':
			c |= d - 'a' + 10
		case d >= 'A' && d <= 'F':
			c |= d - 'A' + 10
		default:
			return 0, false
		}
	}
	return c, true
}

// parseObject is _parse_object_unicode (no hooks): idx is after the '{'.
func (sc *scanner) parseObject(idx, depth int) (any, int, error) {
	s := sc.s
	end := len(s) - 1
	obj := NewObject()
	idx = sc.skipWS(idx)
	if idx > end || s[idx] != '}' {
		for {
			if idx > end || s[idx] != '"' {
				return nil, 0, sc.errAt("Expecting property name enclosed in double quotes", idx)
			}
			k, next, err := sc.scanString(idx + 1)
			if err != nil {
				return nil, 0, err
			}
			idx = sc.skipWS(next)
			if idx > end || s[idx] != ':' {
				return nil, 0, sc.errAt("Expecting ':' delimiter", idx)
			}
			idx = sc.skipWS(idx + 1)
			v, next, err := sc.scanOnce(idx, depth)
			if err != nil {
				return nil, 0, err
			}
			obj.Set(k.(string), v)
			idx = sc.skipWS(next)
			if idx <= end && s[idx] == '}' {
				break
			}
			if idx > end || s[idx] != ',' {
				return nil, 0, sc.errAt("Expecting ',' delimiter", idx)
			}
			idx = sc.skipWS(idx + 1)
		}
	}
	return obj, idx + 1, nil
}

// parseArray is _parse_array_unicode: idx is after the '['.
func (sc *scanner) parseArray(idx, depth int) (any, int, error) {
	s := sc.s
	end := len(s) - 1
	arr := []any{}
	idx = sc.skipWS(idx)
	if idx > end || s[idx] != ']' {
		for {
			v, next, err := sc.scanOnce(idx, depth)
			if err != nil {
				return nil, 0, err
			}
			arr = append(arr, v)
			idx = sc.skipWS(next)
			if idx <= end && s[idx] == ']' {
				break
			}
			if idx > end || s[idx] != ',' {
				return nil, 0, sc.errAt("Expecting ',' delimiter", idx)
			}
			idx = sc.skipWS(idx + 1)
		}
	}
	return arr, idx + 1, nil
}
