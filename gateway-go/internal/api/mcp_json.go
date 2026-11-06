package api

import (
	"encoding/binary"
	"strings"
	"unicode/utf8"
)

// jsonBytesText is what json.loads does with bytes before it parses them:
// s.decode(json.detect_encoding(s), 'surrogatepass'). ok=false for UnicodeDecodeError (a
// ValueError, the route's «Parse error»). A lone surrogate, which 'surrogatepass' lets through,
// becomes U+FFFD, as internal/pyjson writes one read from a \u escape.
func jsonBytesText(b []byte) (string, bool) {
	switch enc := jsonEncoding(b); enc {
	case "utf-32":
		if len(b)%4 != 0 {
			return "", false
		}
		order := binary.ByteOrder(binary.BigEndian)
		if b[0] == 0xff {
			order = binary.LittleEndian
		}
		return utf32Text(b[4:], order)
	case "utf-32-be":
		if len(b)%4 != 0 {
			return "", false
		}
		return utf32Text(b, binary.BigEndian)
	case "utf-32-le":
		if len(b)%4 != 0 {
			return "", false
		}
		return utf32Text(b, binary.LittleEndian)
	case "utf-16":
		order := binary.ByteOrder(binary.BigEndian)
		if b[0] == 0xff {
			order = binary.LittleEndian
		}
		return utf16Text(b[2:], order)
	case "utf-16-be":
		return utf16Text(b, binary.BigEndian)
	case "utf-16-le":
		return utf16Text(b, binary.LittleEndian)
	case "utf-8-sig":
		return utf8Text(b[3:])
	default:
		return utf8Text(b)
	}
}

// jsonEncoding is json.detect_encoding.
func jsonEncoding(b []byte) string {
	has := func(p ...byte) bool { return len(b) >= len(p) && string(b[:len(p)]) == string(p) }
	switch {
	case has(0, 0, 0xfe, 0xff), has(0xff, 0xfe, 0, 0):
		return "utf-32"
	case has(0xfe, 0xff), has(0xff, 0xfe):
		return "utf-16"
	case has(0xef, 0xbb, 0xbf):
		return "utf-8-sig"
	}
	if len(b) >= 4 {
		if b[0] == 0 {
			if b[1] != 0 {
				return "utf-16-be"
			}
			return "utf-32-be"
		}
		if b[1] == 0 {
			if b[2] != 0 || b[3] != 0 {
				return "utf-16-le"
			}
			return "utf-32-le"
		}
	} else if len(b) == 2 {
		if b[0] == 0 {
			return "utf-16-be"
		}
		if b[1] == 0 {
			return "utf-16-le"
		}
	}
	return "utf-8"
}

// utf8Text is a UTF-8 decode with 'surrogatepass': an encoded surrogate (ED A0..BF xx) is let
// through, anything else invalid fails.
func utf8Text(b []byte) (string, bool) {
	if utf8.Valid(b) {
		return string(b), true
	}
	var s strings.Builder
	for i := 0; i < len(b); {
		if b[i] == 0xed && i+2 < len(b) && b[i+1] >= 0xa0 && b[i+1] <= 0xbf && b[i+2] >= 0x80 && b[i+2] <= 0xbf {
			s.WriteRune(utf8.RuneError)
			i += 3
			continue
		}
		r, size := utf8.DecodeRune(b[i:])
		if r == utf8.RuneError && size <= 1 {
			return "", false
		}
		s.WriteRune(r)
		i += size
	}
	return s.String(), true
}

// utf16Text is a UTF-16 decode with 'surrogatepass': pairs joined, a lone surrogate let
// through; an odd byte at the end is «truncated data».
func utf16Text(b []byte, order binary.ByteOrder) (string, bool) {
	if len(b)%2 != 0 {
		return "", false
	}
	var s strings.Builder
	for i := 0; i < len(b); i += 2 {
		u := rune(order.Uint16(b[i:]))
		if u >= 0xd800 && u < 0xdc00 && i+3 < len(b) {
			if lo := rune(order.Uint16(b[i+2:])); lo >= 0xdc00 && lo < 0xe000 {
				s.WriteRune(0x10000 + (u-0xd800)<<10 + (lo - 0xdc00))
				i += 2
				continue
			}
		}
		if u >= 0xd800 && u < 0xe000 {
			s.WriteRune(utf8.RuneError)
			continue
		}
		s.WriteRune(u)
	}
	return s.String(), true
}

// utf32Text is a UTF-32 decode with 'surrogatepass': a surrogate let through, a value past
// U+10FFFF fails.
func utf32Text(b []byte, order binary.ByteOrder) (string, bool) {
	var s strings.Builder
	for i := 0; i < len(b); i += 4 {
		u := order.Uint32(b[i:])
		switch {
		case u > 0x10ffff:
			return "", false
		case u >= 0xd800 && u < 0xe000:
			s.WriteRune(utf8.RuneError)
		default:
			s.WriteRune(rune(u))
		}
	}
	return s.String(), true
}
