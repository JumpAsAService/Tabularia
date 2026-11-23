package bayesexpr

import (
	"errors"
	"fmt"
	"math"
	"strconv"
	"strings"
	"sync/atomic"
	"unicode/utf8"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescatalog"
)

// The vocabulary is the catalogue's: its limits, hints and confusables are read from it, never
// copied here.
var (
	catalogLimits = bayescatalog.GetLimits()
	hints         = bayescatalog.Declared().Language.Hints
	confusables   = confusableRunes(bayescatalog.Declared().Language.Confusables)
)

func confusableRunes(list []bayescatalog.Confusable) map[rune]bayescatalog.Confusable {
	m := make(map[rune]bayescatalog.Confusable, len(list))
	for _, c := range list {
		r, size := utf8.DecodeRuneInString(c.Char)
		if r != utf8.RuneError && size == len(c.Char) {
			m[r] = c
		}
	}
	return m
}

// lexerRuns counts the lexers started, so that a test can see that an input too long is refused
// before any token is read.
var lexerRuns atomic.Int64

// The input is compared byte by byte: no Unicode-aware class anywhere (a Unicode digit class
// matches Arabic-Indic digits, a Unicode space class matches U+00A0).
func isLower(c byte) bool { return 'a' <= c && c <= 'z' }
func isUpper(c byte) bool { return 'A' <= c && c <= 'Z' }
func isDigit(c byte) bool { return '0' <= c && c <= '9' }
func isSpace(c byte) bool { return c == ' ' || c == '\t' || c == '\n' || c == '\r' }
func isWord(c byte) bool  { return isLower(c) || isUpper(c) || isDigit(c) || c == '_' }

func codePoint(r rune) string { return fmt.Sprintf("U+%04X", r) }

// precheck runs before any token is read: the length in bytes first, then every byte ASCII and
// no control byte but tab, line feed and carriage return. It returns the first failure.
func precheck(src string, lim *bayescatalog.Limits) *Error {
	if len(src) > lim.MaxExpressionBytes {
		// Nothing of the input is read, not even to count its characters: the span is empty.
		return &Error{Code: CodeTooLong, Pos: 0, Len: 0}
	}
	for i := 0; i < len(src); i++ {
		c := src[i]
		if c >= 0x80 {
			// Every byte before is ASCII: the byte offset is the code point offset. An invalid
			// sequence is U+FFFD, the character Go's JSON decoder gives for a lone surrogate.
			r, _ := utf8.DecodeRuneInString(src[i:])
			args := map[string]string{"codepoint": codePoint(r)}
			if conf, ok := confusables[r]; ok {
				args["char"] = conf.Char
				args["ascii"] = conf.ASCII
			}
			return &Error{Code: CodeNonASCII, Pos: i, Len: 1, Args: args}
		}
		if (c < 0x20 && c != '\t' && c != '\n' && c != '\r') || c == 0x7f {
			return &Error{Code: CodeControl, Pos: i, Len: 1, Args: map[string]string{"codepoint": codePoint(rune(c))}}
		}
	}
	return nil
}

type tokKind uint8

const (
	tEOF tokKind = iota
	tName
	tNumber
	tPlus
	tMinus
	tStar
	tSlash
	tCaret
	tLParen
	tRParen
	tLBracket
	tRBracket
	tComma
)

// token is a span [pos, end) of the input, in bytes. A number carries its value and whether it
// was written with digits only (lag's k).
type token struct {
	kind   tokKind
	pos    int
	end    int
	num    float64
	digits bool
}

// lexer reads one token at a time, when the parser asks: the first error from the left is the
// one reported, lexical or syntactic.
type lexer struct {
	src string
	i   int
	lim *bayescatalog.Limits
}

func newLexer(src string, lim *bayescatalog.Limits) lexer {
	lexerRuns.Add(1)
	return lexer{src: src, lim: lim}
}

func (lx *lexer) next() (token, *Error) {
	src, n := lx.src, len(lx.src)
	for lx.i < n && isSpace(src[lx.i]) {
		lx.i++
	}
	if lx.i >= n {
		return token{kind: tEOF, pos: n, end: n}, nil
	}
	start := lx.i
	c := src[start]
	switch {
	case isLower(c):
		return lx.name(start)
	case isUpper(c):
		return token{}, lx.nameCase(start)
	case isDigit(c):
		return lx.number(start)
	case c == '/' && start+1 < n && (src[start+1] == '/' || src[start+1] == '*'):
		return token{}, &Error{Code: CodeComment, Pos: start, Len: 2}
	case c == '#':
		return token{}, &Error{Code: CodeHash, Pos: start, Len: 1}
	case c == '"' || c == '\'':
		return token{}, &Error{Code: CodeText, Pos: start, Len: 1}
	case c == '.' && start+1 < n && isDigit(src[start+1]):
		return token{}, lx.numberForm(start, start+1, "leading_dot")
	}
	// The operators of other languages come before the symbols: "**" is a hint, not two "*".
	if h, ok := longestHint(src[start:]); ok {
		return token{}, &Error{Code: CodeHint, Pos: start, Len: len(h.Token),
			Args: map[string]string{"hint": h.Message, "token": h.Token}}
	}
	kind := tEOF
	switch c {
	case '+':
		kind = tPlus
	case '-':
		kind = tMinus
	case '*':
		kind = tStar
	case '/':
		kind = tSlash
	case '^':
		kind = tCaret
	case '(':
		kind = tLParen
	case ')':
		kind = tRParen
	case '[':
		kind = tLBracket
	case ']':
		kind = tRBracket
	case ',':
		kind = tComma
	default:
		return token{}, &Error{Code: CodeSymbol, Pos: start, Len: 1, Args: map[string]string{"char": string(rune(c))}}
	}
	lx.i = start + 1
	return token{kind: kind, pos: start, end: start + 1}, nil
}

// longestHint is the longest entry of language.hints the rest of the input starts with.
func longestHint(rest string) (bayescatalog.Hint, bool) {
	var best bayescatalog.Hint
	for _, h := range hints {
		if h.Token != "" && len(h.Token) > len(best.Token) && strings.HasPrefix(rest, h.Token) {
			best = h
		}
	}
	return best, best.Token != ""
}

// name reads lower {lower | digit | "_"}.
func (lx *lexer) name(start int) (token, *Error) {
	src, n := lx.src, len(lx.src)
	i := start
	for i < n && (isLower(src[i]) || isDigit(src[i]) || src[i] == '_') {
		i++
	}
	if i < n && isUpper(src[i]) {
		return token{}, lx.nameCase(start)
	}
	switch {
	case i-start > lx.lim.MaxNameBytes:
		return token{}, &Error{Code: CodeNameLong, Pos: start, Len: i - start}
	case strings.HasSuffix(src[start:i], "__"):
		return token{}, &Error{Code: CodeNameReserved, Pos: start, Len: i - start}
	}
	lx.i = i
	return token{kind: tName, pos: start, end: i}, nil
}

// nameCase refuses a word with an upper-case letter, and gives it in lower case.
func (lx *lexer) nameCase(start int) *Error {
	src, n := lx.src, len(lx.src)
	end := start
	for end < n && isWord(src[end]) {
		end++
	}
	lower := []byte(src[start:end])
	for k, c := range lower {
		if isUpper(c) {
			lower[k] = c + ('a' - 'A')
		}
	}
	return &Error{Code: CodeNameCase, Pos: start, Len: end - start, Args: map[string]string{"name": string(lower)}}
}

// number reads digits ["." digits] [("e" | "E") ["+" | "-"] digits], then checks what follows
// it, its length, its range and that it does not vanish.
func (lx *lexer) number(start int) (token, *Error) {
	src, n := lx.src, len(lx.src)
	i := start
	for i < n && isDigit(src[i]) {
		i++
	}
	digitsOnly := true
	if i < n && src[i] == '.' {
		if i+1 >= n || !isDigit(src[i+1]) {
			return token{}, lx.numberForm(start, i+1, "trailing_dot")
		}
		digitsOnly = false
		for i++; i < n && isDigit(src[i]); i++ {
		}
	}
	if i < n && (src[i] == 'e' || src[i] == 'E') {
		j := i + 1
		if j < n && (src[j] == '+' || src[j] == '-') {
			j++
		}
		if j >= n || !isDigit(src[j]) {
			return token{}, lx.numberForm(start, j, "exponent")
		}
		digitsOnly = false
		for i = j; i < n && isDigit(src[i]); i++ {
		}
	}
	if i < n {
		switch c := src[i]; {
		case isLower(c) || isUpper(c) || c == '_':
			return token{}, lx.numberForm(start, i+1, "suffix")
		case c == '.':
			return token{}, lx.numberForm(start, i+1, "second_dot")
		}
	}
	text := src[start:i]
	if len(text) > lx.lim.MaxNumberChars {
		return token{}, &Error{Code: CodeNumberRange, Pos: start, Len: i - start, Args: map[string]string{"reason": "chars"}}
	}
	v, err := strconv.ParseFloat(text, 64)
	switch {
	case err != nil && !errors.Is(err, strconv.ErrRange):
		// Not reached: the text was read by the grammar above. Refused all the same.
		return token{}, lx.numberForm(start, i, "suffix")
	case math.IsInf(v, 0) || math.IsNaN(v) || math.Abs(v) > lx.lim.MaxAbsLiteral:
		return token{}, &Error{Code: CodeNumberRange, Pos: start, Len: i - start, Args: map[string]string{"reason": "magnitude"}}
	case v == 0 && nonZeroMantissa(text):
		// strconv reads 1e-400 as 0 without an error.
		return token{}, &Error{Code: CodeNumberUnderflow, Pos: start, Len: i - start}
	}
	lx.i = i
	return token{kind: tNumber, pos: start, end: i, num: v, digits: digitsOnly}, nil
}

// numberForm is a malformed number from start; the span runs over the letters, digits, "_" and
// "." that follow end, so that the whole word is shown.
func (lx *lexer) numberForm(start, end int, kind string) *Error {
	src, n := lx.src, len(lx.src)
	for end < n && (isWord(src[end]) || src[end] == '.') {
		end++
	}
	return &Error{Code: CodeNumberForm, Pos: start, Len: end - start, Args: map[string]string{"kind": kind}}
}

// nonZeroMantissa reports whether a digit before the exponent is not 0.
func nonZeroMantissa(text string) bool {
	for i := 0; i < len(text); i++ {
		c := text[i]
		if c == 'e' || c == 'E' {
			return false
		}
		if '1' <= c && c <= '9' {
			return true
		}
	}
	return false
}
