package bayescodegen

import (
	"math"
	"strconv"
	"strings"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescatalog"
)

// This file is the block writer (docs/specs/bayes-stan-codegen.md, «Layout of the text», «Code
// style»): two spaces a level, one line per statement or declaration, "{" at the end of a block's
// line and "}" alone, "\n" after every line. It has one way to write a call and one way to write a
// sampling statement, and each refuses a name the catalogue does not list. Numbers are re-printed
// from their doubles.

// callable are the names a program may call: every distribution's density and generators, the
// language's functions (their Stan names) and the catalogue's internal functions.
var callable = func() map[string]bool {
	m := map[string]bool{}
	c := bayescatalog.Declared()
	for _, d := range c.Distributions {
		for _, n := range []string{d.Stan.Density, d.Stan.RNG, d.Stan.RNGInverseLink} {
			if n != "" {
				m[n] = true
			}
		}
	}
	for _, f := range c.Language.Functions {
		if f.Stan != "" {
			m[f.Stan] = true
		}
	}
	for _, f := range c.InternalFunctions {
		m[f] = true
	}
	return m
}()

// statements are the names a program may write after "~": every distribution's statement.
var statements = func() map[string]bool {
	m := map[string]bool{}
	for _, d := range bayescatalog.Declared().Distributions {
		if d.Stan.Statement != "" {
			m[d.Stan.Statement] = true
		}
	}
	return m
}()

type writer struct {
	b     strings.Builder
	depth int
	err   error
}

// line writes one line at the current depth. A line is one statement or declaration: never empty,
// never with a line feed, a leading or a trailing space.
func (w *writer) line(s string) {
	if w.err != nil {
		return
	}
	if s == "" || strings.ContainsAny(s, "\n\r\t") || s[0] == ' ' || s[len(s)-1] == ' ' {
		w.err = bug("a line that is not one line: %s", strconv.Quote(s))
		return
	}
	w.b.WriteString(strings.Repeat("  ", w.depth))
	w.b.WriteString(s)
	w.b.WriteByte('\n')
}

// open writes "<head> {" and goes one level in.
func (w *writer) open(head string) {
	w.line(head + " {")
	w.depth++
}

// openNested writes a nested block's "{" alone and goes one level in.
func (w *writer) openNested() {
	w.line("{")
	w.depth++
}

// close writes "}" one level out.
func (w *writer) close() {
	if w.depth == 0 {
		if w.err == nil {
			w.err = bug("a block closed that was not open")
		}
		return
	}
	w.depth--
	w.line("}")
}

// text is what was written, or the first error.
func (w *writer) text() (string, error) {
	if w.err != nil {
		return "", w.err
	}
	if w.depth != 0 {
		return "", bug("a block left open")
	}
	return w.b.String(), nil
}

// call writes a call of a Stan function or density. The name comes from the catalogue, never from
// the graph: a name the allow-list does not hold is a bug of the codegen, and no program is returned.
func call(name string, args ...string) (string, error) {
	if !callable[name] {
		return "", bug("%q is not a function the catalogue lists", name)
	}
	return name + "(" + strings.Join(args, ", ") + ")", nil
}

// sampling writes "lhs ~ dist(args)" with the truncation trunc ("" or " T[…]"), without the ";".
func sampling(lhs string, d bayescatalog.Distribution, args []string, trunc string) (string, error) {
	name := d.Stan.Statement
	if !statements[name] {
		return "", bug("%q is not a statement the catalogue lists", name)
	}
	return lhs + " ~ " + name + "(" + strings.Join(args, ", ") + ")" + trunc, nil
}

// realLiteral is a real number as the program writes it: the shortest text that reads back to the
// same double (the language's formatter), ".0" appended when it has neither "." nor an exponent,
// the sign kept; -0 is 0. A number that is not finite is a bug (the validator refuses it).
func realLiteral(v float64) (string, error) {
	if math.IsNaN(v) || math.IsInf(v, 0) {
		return "", bug("a number that is not finite")
	}
	if v == 0 {
		v = 0 // -0 is 0
	}
	s := strconv.FormatFloat(v, 'g', -1, 64)
	if !strings.ContainsAny(s, ".e") {
		s += ".0"
	}
	return s, nil
}
