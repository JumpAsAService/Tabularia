package pyjson

import (
	"errors"
	"fmt"
	"math"
	"strconv"
	"strings"
	"unicode/utf16"
	"unicode/utf8"
)

// BigInt is an integer of the JSON text too large for an int64: Python's int has no
// limit, and json.dumps writes it back as it came.
type BigInt string

// Loads is json.loads: the objects as Object (the keys in the order they came; a key
// repeated keeps its first place and its last value, as a Python dict), the arrays as
// []any, the integers as int64 (BigInt past it), the other numbers as float64, NaN and
// Infinity accepted as Python accepts them. Bytes that are not UTF-8 are refused, as
// bytes.decode refuses them.
func Loads(raw []byte) (any, error) { return loads(raw, false) }

// LoadsNoConstants is json.loads(raw, parse_constant=…raise): NaN and Infinity refused.
func LoadsNoConstants(raw []byte) (any, error) { return loads(raw, true) }

func loads(raw []byte, noConstants bool) (any, error) {
	if !utf8.Valid(raw) {
		return nil, errors.New("the text is not UTF-8")
	}
	p := &parser{s: string(raw), noConstants: noConstants}
	p.space()
	v, err := p.value()
	if err != nil {
		return nil, err
	}
	p.space()
	if p.i != len(p.s) {
		return nil, p.fail("Extra data")
	}
	return v, nil
}

type parser struct {
	s           string
	i           int
	noConstants bool
}

func (p *parser) fail(what string) error { return fmt.Errorf("%s: char %d", what, p.i) }

// space is Python's WHITESPACE: blanks, tabs, line feeds and carriage returns only.
func (p *parser) space() {
	for p.i < len(p.s) {
		switch p.s[p.i] {
		case ' ', '\t', '\n', '\r':
			p.i++
		default:
			return
		}
	}
}

func (p *parser) value() (any, error) {
	if p.i >= len(p.s) {
		return nil, p.fail("Expecting value")
	}
	switch c := p.s[p.i]; {
	case c == '{':
		return p.object()
	case c == '[':
		return p.array()
	case c == '"':
		return p.str()
	case strings.HasPrefix(p.s[p.i:], "null"):
		p.i += 4
		return nil, nil
	case strings.HasPrefix(p.s[p.i:], "true"):
		p.i += 4
		return true, nil
	case strings.HasPrefix(p.s[p.i:], "false"):
		p.i += 5
		return false, nil
	case p.noConstants && (strings.HasPrefix(p.s[p.i:], "NaN") || strings.HasPrefix(p.s[p.i:], "Infinity") ||
		strings.HasPrefix(p.s[p.i:], "-Infinity")):
		return nil, p.fail("not a JSON value")
	case strings.HasPrefix(p.s[p.i:], "NaN"):
		p.i += 3
		return math.NaN(), nil
	case strings.HasPrefix(p.s[p.i:], "Infinity"):
		p.i += 8
		return math.Inf(1), nil
	case strings.HasPrefix(p.s[p.i:], "-Infinity"):
		p.i += 9
		return math.Inf(-1), nil
	case c == '-' || (c >= '0' && c <= '9'):
		return p.number()
	}
	return nil, p.fail("Expecting value")
}

func (p *parser) object() (any, error) {
	p.i++ // {
	obj := Object{}
	p.space()
	if p.i < len(p.s) && p.s[p.i] == '}' {
		p.i++
		return obj, nil
	}
	for {
		p.space()
		if p.i >= len(p.s) || p.s[p.i] != '"' {
			return nil, p.fail("Expecting property name enclosed in double quotes")
		}
		k, err := p.str()
		if err != nil {
			return nil, err
		}
		p.space()
		if p.i >= len(p.s) || p.s[p.i] != ':' {
			return nil, p.fail("Expecting ':' delimiter")
		}
		p.i++
		p.space()
		v, err := p.value()
		if err != nil {
			return nil, err
		}
		obj = obj.Set(k, v)
		p.space()
		if p.i >= len(p.s) {
			return nil, p.fail("Expecting ',' delimiter")
		}
		switch p.s[p.i] {
		case ',':
			p.i++
		case '}':
			p.i++
			return obj, nil
		default:
			return nil, p.fail("Expecting ',' delimiter")
		}
	}
}

func (p *parser) array() (any, error) {
	p.i++ // [
	out := []any{}
	p.space()
	if p.i < len(p.s) && p.s[p.i] == ']' {
		p.i++
		return out, nil
	}
	for {
		p.space()
		v, err := p.value()
		if err != nil {
			return nil, err
		}
		out = append(out, v)
		p.space()
		if p.i >= len(p.s) {
			return nil, p.fail("Expecting ',' delimiter")
		}
		switch p.s[p.i] {
		case ',':
			p.i++
		case ']':
			p.i++
			return out, nil
		default:
			return nil, p.fail("Expecting ',' delimiter")
		}
	}
}

func (p *parser) number() (any, error) {
	start := p.i
	if p.s[p.i] == '-' {
		p.i++
	}
	digits := func() int {
		n := 0
		for p.i < len(p.s) && p.s[p.i] >= '0' && p.s[p.i] <= '9' {
			p.i++
			n++
		}
		return n
	}
	if p.i < len(p.s) && p.s[p.i] == '0' {
		p.i++
	} else if digits() == 0 {
		p.i = start
		return nil, p.fail("Expecting value")
	}
	integer := true
	if p.i < len(p.s) && p.s[p.i] == '.' {
		save := p.i
		p.i++
		if digits() == 0 { // «1.» is the integer 1 followed by extra data, in Python
			p.i = save
		} else {
			integer = false
		}
	}
	if p.i < len(p.s) && (p.s[p.i] == 'e' || p.s[p.i] == 'E') {
		save := p.i
		p.i++
		if p.i < len(p.s) && (p.s[p.i] == '+' || p.s[p.i] == '-') {
			p.i++
		}
		if digits() == 0 {
			p.i = save
		} else {
			integer = false
		}
	}
	text := p.s[start:p.i]
	if integer {
		if n, err := strconv.ParseInt(text, 10, 64); err == nil {
			return n, nil
		}
		return BigInt(text), nil
	}
	f, err := strconv.ParseFloat(text, 64)
	if err != nil { // out of range: ±inf, as float() gives
		var ne *strconv.NumError
		if errors.As(err, &ne) && errors.Is(ne.Err, strconv.ErrRange) {
			return f, nil
		}
		return nil, err
	}
	return f, nil
}

func (p *parser) str() (string, error) {
	p.i++ // "
	var b strings.Builder
	for {
		if p.i >= len(p.s) {
			return "", p.fail("Unterminated string starting at")
		}
		c := p.s[p.i]
		switch {
		case c == '"':
			p.i++
			return b.String(), nil
		case c < 0x20:
			return "", p.fail("Invalid control character at")
		case c == '\\':
			p.i++
			if p.i >= len(p.s) {
				return "", p.fail("Unterminated string starting at")
			}
			e := p.s[p.i]
			p.i++
			switch e {
			case '"', '\\', '/':
				b.WriteByte(e)
			case 'b':
				b.WriteByte('\b')
			case 'f':
				b.WriteByte('\f')
			case 'n':
				b.WriteByte('\n')
			case 'r':
				b.WriteByte('\r')
			case 't':
				b.WriteByte('\t')
			case 'u':
				r, ok := p.hex4()
				if !ok {
					return "", p.fail("Invalid \\uXXXX escape")
				}
				if utf16.IsSurrogate(r) && r < 0xdc00 && strings.HasPrefix(p.s[p.i:], `\u`) {
					save := p.i
					p.i += 2
					if r2, ok := p.hex4(); ok && r2 >= 0xdc00 && r2 <= 0xdfff {
						r = utf16.DecodeRune(r, r2)
					} else {
						p.i = save
					}
				}
				b.WriteRune(r) // a lone surrogate becomes U+FFFD (Python keeps it)
			default:
				return "", p.fail("Invalid \\escape")
			}
		default:
			r, size := utf8.DecodeRuneInString(p.s[p.i:])
			b.WriteRune(r)
			p.i += size
		}
	}
}

func (p *parser) hex4() (rune, bool) {
	if p.i+4 > len(p.s) {
		return 0, false
	}
	n, err := strconv.ParseUint(p.s[p.i:p.i+4], 16, 32)
	if err != nil {
		return 0, false
	}
	p.i += 4
	return rune(n), true
}

// MarshalJSON writes the object as Starlette's JSONResponse does (compact, the keys in
// their order, no ASCII escaping), so an Object can be a route's answer.
func (o Object) MarshalJSON() ([]byte, error) {
	s, err := Dumps(o, Options{Compact: true})
	return []byte(s), err
}
