package pyjson

import (
	"errors"
	"math"
	"strconv"
	"strings"
	"unicode/utf16"
	"unicode/utf8"
)

// Int is an integer of JSON beyond int64, kept as its digits: Python's ints have no limit
// and json.dumps writes them back as they were read.
type Int string

// Loads is json.loads: objects as Object (their keys in the order read; a key repeated keeps
// its first place and its last value, as dict(pairs) does), arrays as []any, integers as
// int64 (Int past it), other numbers as float64 (1e400 is +Inf, as float() reads it), NaN
// and ±Infinity accepted, control characters refused inside strings (strict=True).
func Loads(text string) (any, error) {
	p := &parser{s: text}
	p.space()
	v, err := p.value()
	if err != nil {
		return nil, err
	}
	p.space()
	if p.i != len(p.s) {
		return nil, errors.New("Extra data")
	}
	return v, nil
}

type parser struct {
	s string
	i int
}

var errSyntax = errors.New("Expecting value")

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
		return nil, errSyntax
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
	return nil, errSyntax
}

func digits(s string, i int) int {
	for i < len(s) && s[i] >= '0' && s[i] <= '9' {
		i++
	}
	return i
}

// number is json.scanner's NUMBER_RE: -?(0|[1-9]\d*)(\.\d+)?([eE][-+]?\d+)?
func (p *parser) number() (any, error) {
	start, i := p.i, p.i
	if p.s[i] == '-' {
		i++
	}
	if i >= len(p.s) || p.s[i] < '0' || p.s[i] > '9' {
		return nil, errSyntax
	}
	if p.s[i] == '0' {
		i++
	} else {
		i = digits(p.s, i)
	}
	integer := true
	if i+1 < len(p.s) && p.s[i] == '.' && p.s[i+1] >= '0' && p.s[i+1] <= '9' {
		i = digits(p.s, i+1)
		integer = false
	}
	if i < len(p.s) && (p.s[i] == 'e' || p.s[i] == 'E') {
		j := i + 1
		if j < len(p.s) && (p.s[j] == '+' || p.s[j] == '-') {
			j++
		}
		if k := digits(p.s, j); k > j {
			i, integer = k, false
		}
	}
	p.i = i
	text := p.s[start:i]
	if integer {
		if n, err := strconv.ParseInt(text, 10, 64); err == nil {
			return n, nil
		}
		t := strings.TrimLeft(strings.TrimPrefix(text, "-"), "0")
		if strings.HasPrefix(text, "-") {
			t = "-" + t
		}
		return Int(t), nil
	}
	f, err := strconv.ParseFloat(text, 64)
	if err != nil && !errors.Is(err, strconv.ErrRange) {
		return nil, err
	}
	return f, nil
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
			return nil, errors.New("Expecting property name enclosed in double quotes")
		}
		k, err := p.str()
		if err != nil {
			return nil, err
		}
		p.space()
		if p.i >= len(p.s) || p.s[p.i] != ':' {
			return nil, errors.New("Expecting ':' delimiter")
		}
		p.i++
		p.space()
		v, err := p.value()
		if err != nil {
			return nil, err
		}
		obj = obj.Set(k.(string), v)
		p.space()
		if p.i >= len(p.s) {
			return nil, errors.New("Expecting ',' delimiter")
		}
		if p.s[p.i] == '}' {
			p.i++
			return obj, nil
		}
		if p.s[p.i] != ',' {
			return nil, errors.New("Expecting ',' delimiter")
		}
		p.i++
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
			return nil, errors.New("Expecting ',' delimiter")
		}
		if p.s[p.i] == ']' {
			p.i++
			return out, nil
		}
		if p.s[p.i] != ',' {
			return nil, errors.New("Expecting ',' delimiter")
		}
		p.i++
	}
}

func hex4(s string) (rune, bool) {
	if len(s) < 4 {
		return 0, false
	}
	n, err := strconv.ParseUint(s[:4], 16, 32)
	return rune(n), err == nil
}

func (p *parser) str() (any, error) {
	p.i++ // "
	var b strings.Builder
	for {
		if p.i >= len(p.s) {
			return nil, errors.New("Unterminated string")
		}
		c := p.s[p.i]
		switch {
		case c == '"':
			p.i++
			return b.String(), nil
		case c < 0x20:
			return nil, errors.New("Invalid control character")
		case c == '\\':
			if p.i+1 >= len(p.s) {
				return nil, errors.New("Unterminated string")
			}
			e := p.s[p.i+1]
			p.i += 2
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
				r, ok := hex4(p.s[p.i:])
				if !ok {
					return nil, errors.New("Invalid \\uXXXX escape")
				}
				p.i += 4
				if utf16.IsSurrogate(r) && strings.HasPrefix(p.s[p.i:], `\u`) {
					if r2, ok := hex4(p.s[p.i+2:]); ok {
						if d := utf16.DecodeRune(r, r2); d != utf8.RuneError {
							p.i += 6
							r = d
						}
					}
				}
				b.WriteRune(r)
			default:
				return nil, errors.New("Invalid \\escape")
			}
		default:
			r, size := utf8.DecodeRuneInString(p.s[p.i:])
			b.WriteRune(r)
			p.i += size
		}
	}
}
