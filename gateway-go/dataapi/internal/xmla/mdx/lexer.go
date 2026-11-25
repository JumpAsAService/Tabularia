package mdx

import (
	"strings"
	"unicode"
	"unicode/utf8"
)

type tokKind int

const (
	tEOF     tokKind = iota
	tIdent           // bare word: keyword, function, unbracketed name
	tBracket         // [name], text already unescaped
	tNumber          // as written
	tString          // "..." or '...', text already unescaped
	tPunct
)

type token struct {
	kind     tokKind
	text     string
	off, end int
}

func (t token) is(p string) bool { return t.kind == tPunct && t.text == p }

func (t token) kw(w string) bool { return t.kind == tIdent && strings.EqualFold(t.text, w) }

// describe names a token in a message without echoing a huge text back.
func (t token) describe() string {
	switch t.kind {
	case tEOF:
		return "the end of the statement"
	case tBracket:
		return "[" + clip(t.text) + "]"
	case tString:
		return "a string"
	}
	return "\"" + clip(t.text) + "\""
}

func clip(s string) string {
	if len(s) > 40 {
		s = s[:40]
		for !utf8.ValidString(s) {
			s = s[:len(s)-1]
		}
		return s + "..."
	}
	return s
}

// lexer is on demand: a lexical error must not hide an earlier construct that
// the parser would have refused by name. Errors travel as panics of *Error,
// recovered in Parse.
type lexer struct {
	src string
	pos int
}

func (l *lexer) fail(code, name string, off int, format string, args ...any) {
	panic(newError(l.src, code, name, off, format, args...))
}

func isIdentStart(r rune) bool { return r == '_' || unicode.IsLetter(r) }
func isIdentPart(r rune) bool  { return r == '_' || unicode.IsLetter(r) || unicode.IsDigit(r) }
func isDigit(r rune) bool      { return r >= '0' && r <= '9' }

func (l *lexer) skipSpace() {
	for l.pos < len(l.src) {
		r, w := utf8.DecodeRuneInString(l.src[l.pos:])
		switch {
		case r != utf8.RuneError && unicode.IsSpace(r):
			l.pos += w
		case strings.HasPrefix(l.src[l.pos:], "--"), strings.HasPrefix(l.src[l.pos:], "//"):
			i := strings.IndexByte(l.src[l.pos:], '\n')
			if i < 0 {
				l.pos = len(l.src)
			} else {
				l.pos += i + 1
			}
		case strings.HasPrefix(l.src[l.pos:], "/*"):
			i := strings.Index(l.src[l.pos+2:], "*/")
			if i < 0 {
				l.fail(CodeSyntax, "", l.pos, "unterminated comment")
			}
			l.pos += 2 + i + 2
		default:
			return
		}
	}
}

func (l *lexer) next() token {
	l.skipSpace()
	start := l.pos
	if start >= len(l.src) {
		return token{kind: tEOF, off: start, end: start}
	}
	r, w := utf8.DecodeRuneInString(l.src[start:])
	if r == utf8.RuneError && w == 1 {
		l.fail(CodeSyntax, "", start, "invalid UTF-8 in the statement")
	}
	switch {
	case r == '[':
		return l.bracket()
	case r == '"' || r == '\'':
		return l.quoted(r)
	case isDigit(r):
		return l.number()
	case isIdentStart(r):
		for l.pos < len(l.src) {
			c, cw := utf8.DecodeRuneInString(l.src[l.pos:])
			if !isIdentPart(c) {
				break
			}
			l.pos += cw
		}
		return token{kind: tIdent, text: l.src[start:l.pos], off: start, end: l.pos}
	}
	n := 1
	switch r {
	case '(', ')', '{', '}', ',', '.', '*', '+', '-', '/', ':', '=', '&', ';':
	case '<':
		if strings.HasPrefix(l.src[start:], "<=") || strings.HasPrefix(l.src[start:], "<>") {
			n = 2
		}
	case '>':
		if strings.HasPrefix(l.src[start:], ">=") {
			n = 2
		}
	default:
		l.fail(CodeSyntax, "", start, "unexpected character %q", r)
	}
	l.pos = start + n
	return token{kind: tPunct, text: l.src[start:l.pos], off: start, end: l.pos}
}

// bracket reads [name]; "]]" is an escaped "]".
func (l *lexer) bracket() token {
	start := l.pos
	var b strings.Builder
	i := start + 1
	for i < len(l.src) {
		c := l.src[i]
		if c == ']' {
			if i+1 < len(l.src) && l.src[i+1] == ']' {
				b.WriteByte(']')
				i += 2
				continue
			}
			if b.Len() == 0 {
				l.fail(CodeSyntax, "", start, "empty name between brackets")
			}
			l.pos = i + 1
			return token{kind: tBracket, text: b.String(), off: start, end: l.pos}
		}
		b.WriteByte(c)
		i++
	}
	l.fail(CodeSyntax, "", start, "unterminated [ name")
	return token{}
}

// quoted reads "..." or '...'; a doubled quote is an escaped quote.
func (l *lexer) quoted(q rune) token {
	start := l.pos
	var b strings.Builder
	i := start + 1
	for i < len(l.src) {
		c := l.src[i]
		if c == byte(q) {
			if i+1 < len(l.src) && l.src[i+1] == byte(q) {
				b.WriteByte(c)
				i += 2
				continue
			}
			l.pos = i + 1
			return token{kind: tString, text: b.String(), off: start, end: l.pos}
		}
		b.WriteByte(c)
		i++
	}
	l.fail(CodeSyntax, "", start, "unterminated string")
	return token{}
}

// number reads digits [. digits] [e[+-]digits]. A letter glued to it is an error.
func (l *lexer) number() token {
	start := l.pos
	s := l.src
	i := start
	for i < len(s) && isDigit(rune(s[i])) {
		i++
	}
	if i+1 < len(s) && s[i] == '.' && isDigit(rune(s[i+1])) {
		i++
		for i < len(s) && isDigit(rune(s[i])) {
			i++
		}
	}
	if i < len(s) && (s[i] == 'e' || s[i] == 'E') {
		j := i + 1
		if j < len(s) && (s[j] == '+' || s[j] == '-') {
			j++
		}
		if j < len(s) && isDigit(rune(s[j])) {
			for j < len(s) && isDigit(rune(s[j])) {
				j++
			}
			i = j
		}
	}
	if i < len(s) {
		if c, _ := utf8.DecodeRuneInString(s[i:]); isIdentStart(c) {
			l.fail(CodeSyntax, "", start, "malformed number")
		}
	}
	l.pos = i
	return token{kind: tNumber, text: s[start:i], off: start, end: i}
}
