package pyd

import (
	"strings"
	"unicode/utf8"
)

// Query is a query string as Starlette reads it: urllib.parse.parse_qsl with
// keep_blank_values=True over the raw bytes taken as Latin-1.
type Query struct {
	pairs [][2]string
}

// ParseQuery parses r.URL.RawQuery. Unlike url.ParseQuery it never fails: a bad
// %-escape stays as written, invalid UTF-8 becomes U+FFFD, as in Python.
func ParseQuery(raw string) *Query {
	q := &Query{}
	for _, nv := range strings.Split(latin1(raw), "&") {
		if nv == "" {
			continue
		}
		name, value, _ := strings.Cut(nv, "=")
		q.pairs = append(q.pairs, [2]string{unquotePlus(name), unquotePlus(value)})
	}
	return q
}

// Get is QueryParams.get: the last value of the name.
func (q *Query) Get(name string) (string, bool) {
	for i := len(q.pairs) - 1; i >= 0; i-- {
		if q.pairs[i][0] == name {
			return q.pairs[i][1], true
		}
	}
	return "", false
}

// latin1 is bytes.decode("latin-1"): every byte one code point.
func latin1(s string) string {
	var b strings.Builder
	for i := 0; i < len(s); i++ {
		b.WriteRune(rune(s[i]))
	}
	return b.String()
}

// unquotePlus is parse_qsl's replace('+', ' ') then unquote(errors='replace'):
// the ASCII runs are %-decoded to bytes and decoded as UTF-8, the rest kept.
func unquotePlus(s string) string {
	s = strings.ReplaceAll(s, "+", " ")
	if !strings.Contains(s, "%") {
		return s
	}
	var out strings.Builder
	for len(s) > 0 {
		i := 0
		for i < len(s) && s[i] < 0x80 {
			i++
		}
		if i > 0 {
			out.WriteString(decodeUTF8Replace(unquoteToBytes(s[:i])))
			s = s[i:]
			continue
		}
		_, w := utf8.DecodeRuneInString(s)
		out.WriteString(s[:w])
		s = s[w:]
	}
	return out.String()
}

func unhex(c byte) (byte, bool) {
	switch {
	case c >= '0' && c <= '9':
		return c - '0', true
	case c >= 'a' && c <= 'f':
		return c - 'a' + 10, true
	case c >= 'A' && c <= 'F':
		return c - 'A' + 10, true
	}
	return 0, false
}

// unquoteToBytes decodes %XX, leaving a '%' without two hex digits as it is.
func unquoteToBytes(s string) []byte {
	b := make([]byte, 0, len(s))
	for i := 0; i < len(s); i++ {
		if s[i] == '%' && i+2 < len(s) {
			if h, ok := unhex(s[i+1]); ok {
				if l, ok := unhex(s[i+2]); ok {
					b = append(b, h<<4|l)
					i += 2
					continue
				}
			}
		}
		b = append(b, s[i])
	}
	return b
}

// decodeUTF8Replace is bytes.decode("utf-8", "replace"): one U+FFFD for each
// maximal invalid subsequence, as CPython (and the Unicode standard) do.
func decodeUTF8Replace(b []byte) string {
	var out strings.Builder
	for i := 0; i < len(b); {
		r, w := utf8.DecodeRune(b[i:])
		if r != utf8.RuneError || w > 1 {
			out.WriteRune(r)
			i += w
			continue
		}
		out.WriteRune(utf8.RuneError)
		i += invalidPrefixLen(b[i:])
	}
	return out.String()
}

// invalidPrefixLen is how many bytes one replacement character covers: the lead
// byte plus the continuation bytes that were still acceptable for it.
func invalidPrefixLen(b []byte) int {
	c := b[0]
	var need int
	lo, hi := byte(0x80), byte(0xBF)
	switch {
	case c >= 0xC2 && c <= 0xDF:
		need = 1
	case c == 0xE0:
		need, lo = 2, 0xA0
	case c >= 0xE1 && c <= 0xEC, c == 0xEE, c == 0xEF:
		need = 2
	case c == 0xED:
		need, hi = 2, 0x9F
	case c == 0xF0:
		need, lo = 3, 0x90
	case c >= 0xF1 && c <= 0xF3:
		need = 3
	case c == 0xF4:
		need, hi = 3, 0x8F
	default:
		return 1
	}
	n := 1
	for k := 0; k < need && n < len(b); k++ {
		cb := b[n]
		if k == 0 {
			if cb < lo || cb > hi {
				break
			}
		} else if cb < 0x80 || cb > 0xBF {
			break
		}
		n++
	}
	return n
}

// Items is QueryParams.multi_items(): every name and value, in order.
func (q *Query) Items() [][2]string { return q.pairs }
