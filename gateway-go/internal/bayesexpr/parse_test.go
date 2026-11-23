package bayesexpr

import (
	"encoding/json"
	"errors"
	"math"
	"strconv"
	"strings"
	"testing"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescatalog"
)

func parseErr(t *testing.T, src string) *Error {
	t.Helper()
	_, err := Parse(src)
	var e *Error
	if !errors.As(err, &e) {
		t.Fatalf("%q: want an *Error, got %v", src, err)
	}
	return e
}

func mustParse(t *testing.T, src string) *Node {
	t.Helper()
	n, err := Parse(src)
	if err != nil {
		t.Fatalf("%q: %v", src, err)
	}
	return n
}

func TestTooLongIsRefusedBeforeTheLexer(t *testing.T) {
	lim := bayescatalog.GetLimits()
	before := lexerRuns.Load()
	for _, src := range []string{
		strings.Repeat("a", lim.MaxExpressionBytes+1),
		strings.Repeat("é", lim.MaxExpressionBytes/2+1),
		strings.Repeat("(", 100000),
	} {
		if e := parseErr(t, src); e.Code != CodeTooLong || e.Pos != 0 || e.Len != 0 {
			t.Errorf("%d bytes: %v", len(src), e)
		}
	}
	if got := lexerRuns.Load(); got != before {
		t.Fatalf("the lexer ran %d times on inputs too long", got-before)
	}
	mustParse(t, "a"+strings.Repeat(" ", lim.MaxExpressionBytes-1))
	if got := lexerRuns.Load(); got != before+1 {
		t.Fatalf("the lexer ran %d times on an input at the limit, want 1", got-before)
	}
}

// The limits are the catalogue's: one past each is refused with its code, whatever the numbers.
func TestLimitsAreTheCatalogues(t *testing.T) {
	lim := bayescatalog.GetLimits()
	nest := lim.MaxExpressionNesting
	mustParse(t, strings.Repeat("(", nest)+"a"+strings.Repeat(")", nest))
	if e := parseErr(t, strings.Repeat("(", nest+1)+"a"+strings.Repeat(")", nest+1)); e.Code != CodeTooDeep || e.Pos != nest {
		t.Errorf("nesting: %v", e)
	}
	mustParse(t, strings.Repeat("a", lim.MaxNameBytes))
	if e := parseErr(t, strings.Repeat("a", lim.MaxNameBytes+1)); e.Code != CodeNameLong {
		t.Errorf("name: %v", e)
	}
	mustParse(t, strings.Repeat("0", lim.MaxNumberChars-1)+"1")
	if e := parseErr(t, strings.Repeat("0", lim.MaxNumberChars)+"1"); e.Code != CodeNumberRange || e.Args["reason"] != "chars" {
		t.Errorf("number chars: %v", e)
	}
	// A sum of k names has 2k-1 nodes.
	k := (lim.MaxExpressionNodes + 1) / 2
	_, st, perr := parse("a"+strings.Repeat("+a", k-1), &lim)
	if perr != nil || st.nodes != 2*k-1 {
		t.Errorf("%d nodes: %v %+v", 2*k-1, perr, st)
	}
	if e := parseErr(t, "a"+strings.Repeat("+a", lim.MaxExpressionNodes/2)); e.Code != CodeTooBig {
		t.Errorf("nodes: %v", e)
	}
	mustParse(t, strconv.FormatFloat(lim.MaxAbsLiteral, 'g', -1, 64))
	above := strconv.FormatFloat(math.Nextafter(lim.MaxAbsLiteral, math.Inf(1)), 'f', -1, 64)
	if e := parseErr(t, above); e.Code != CodeNumberRange || e.Args["reason"] != "magnitude" {
		t.Errorf("%s: %v", above, e)
	}
}

// Smaller limits than the catalogue's, so that every guard is reached on short inputs.
func TestGuardsWithSmallLimits(t *testing.T) {
	lim := bayescatalog.GetLimits()
	lim.MaxExpressionBytes, lim.MaxExpressionNesting, lim.MaxExpressionNodes = 20, 2, 5
	lim.MaxNameBytes, lim.MaxNumberChars, lim.MaxAbsLiteral = 3, 3, 100
	for _, c := range []struct {
		src  string
		code Code
		pos  int
	}{
		{strings.Repeat("a", 21), CodeTooLong, 0},
		{"((a))", "", 0},
		{"(((a)))", CodeTooDeep, 2},
		{"-(-a)", CodeTooDeep, 2},
		{"a^b^c", "", 0},
		{"a^b^c^d", CodeTooDeep, 5},
		{"(x[g])", "", 0},
		{"((x[g]))", CodeTooDeep, 3},
		{"f(g(h(a)))", CodeTooDeep, 5},
		{"a+b+c", "", 0},
		{"a+b+c+d", CodeTooBig, 5},
		{"abc", "", 0},
		{"abcd", CodeNameLong, 0},
		{"100", "", 0},
		{"101", CodeNumberRange, 0},
		{"1000", CodeNumberRange, 0},
	} {
		_, _, e := parse(c.src, &lim)
		switch {
		case c.code == "" && e != nil:
			t.Errorf("%q: %v", c.src, e)
		case c.code != "" && (e == nil || e.Code != c.code || e.Pos != c.pos):
			t.Errorf("%q: got %v, want %s at %d", c.src, e, c.code, c.pos)
		}
	}
}

// Every hint and every confusable of the catalogue is recognised, with the catalogue's words.
func TestCatalogueVocabulary(t *testing.T) {
	lang := bayescatalog.Declared().Language
	if len(lang.Hints) == 0 || len(lang.Confusables) == 0 {
		t.Fatal("the catalogue has no hints or no confusables")
	}
	for _, h := range lang.Hints {
		e := parseErr(t, "a "+h.Token+" b")
		if e.Code != CodeHint || e.Pos != 2 || e.Len != len(h.Token) || e.Args["hint"] != h.Message || e.Args["token"] != h.Token {
			t.Errorf("hint %q: %v", h.Token, e)
		}
	}
	for _, c := range lang.Confusables {
		e := parseErr(t, "a"+c.Char+"b")
		if e.Code != CodeNonASCII || e.Pos != 1 || e.Len != 1 || e.Args["codepoint"] != c.CodePoint ||
			e.Args["char"] != c.Char || e.Args["ascii"] != c.ASCII {
			t.Errorf("confusable %s: %v", c.CodePoint, e)
		}
	}
	if !lagApart {
		t.Fatal("the catalogue does not declare lag apart with two arguments")
	}
}

// No byte outside the grammar's alphabet starts a token: every printable ASCII byte is either a
// token's first byte or refused with a lexical code.
func TestEveryASCIIByte(t *testing.T) {
	lexical := map[Code]bool{CodeControl: true, CodeComment: true, CodeHash: true, CodeText: true, CodeHint: true, CodeSymbol: true, CodeNameCase: true}
	for c := 0; c < 0x80; c++ {
		src := "a " + string(rune(c))
		_, err := Parse(src)
		var e *Error
		if !errors.As(err, &e) {
			if c != ' ' && c != '\t' && c != '\n' && c != '\r' {
				t.Errorf("%q accepted", src)
			}
			continue
		}
		accepted := strings.IndexByte("abcdefghijklmnopqrstuvwxyz0123456789+-*/^()[],", byte(c)) >= 0
		switch {
		case accepted && lexical[e.Code] && e.Code != CodeHint:
			t.Errorf("%q: %v for a byte of the grammar", src, e)
		case !accepted && !lexical[e.Code]:
			t.Errorf("%q: %v, want a lexical refusal", src, e)
		}
	}
}

// The span of every node is its own text, parentheses around it included.
func TestSpans(t *testing.T) {
	for src, want := range map[string][]string{
		"a + b * c":       {"a + b * c", "a", "b * c", "b", "c"},
		" (a + b) * -c ":  {"(a + b) * -c", "(a + b)", "a", "b", "-c", "c"},
		"exp(x[g]) ^ 2":   {"exp(x[g]) ^ 2", "exp(x[g])", "x[g]", "2"},
		"lag(y, 1) - 1.5": {"lag(y, 1) - 1.5", "lag(y, 1)", "1.5"},
	} {
		var got []string
		var walk func(n *Node)
		walk = func(n *Node) {
			got = append(got, src[n.Pos:n.end()])
			for _, a := range n.Args {
				walk(a)
			}
		}
		walk(mustParse(t, src))
		if strings.Join(got, "|") != strings.Join(want, "|") {
			t.Errorf("%q: spans %q, want %q", src, got, want)
		}
	}
}

func TestParseFormatRoundTripOnTheCorpus(t *testing.T) {
	relaxed := bayescatalog.GetLimits()
	relaxed.MaxExpressionBytes = 1 << 20
	for _, c := range readCorpus(t) {
		n, err := Parse(c.Input)
		if err != nil {
			continue
		}
		text := Format(n)
		m, _, e := parse(text, &relaxed)
		if e != nil {
			t.Errorf("%s: Format gives %q, which is refused: %v", c.ID, text, e)
			continue
		}
		a, _ := json.Marshal(n)
		b, _ := json.Marshal(m)
		if string(a) != string(b) || Format(m) != text {
			t.Errorf("%s: %s, then %s", c.ID, a, b)
		}
	}
}

func TestErrorText(t *testing.T) {
	if s := parseErr(t, "a b").Error(); s != "expr.unexpected at 2+1 map[expected:operator]" {
		t.Errorf("%q", s)
	}
	if s := parseErr(t, "").Error(); s != "expr.empty at 0+0" {
		t.Errorf("%q", s)
	}
	var e *Error
	if _, err := Parse("a"); err != nil || errors.As(err, &e) {
		t.Errorf("an accepted input has a nil error, got %#v", err)
	}
}
