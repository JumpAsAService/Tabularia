package bayesexpr

import (
	"errors"
	"fmt"
	"math"
	"strconv"
	"strings"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescatalog"
)

// This file is the emission (docs/specs/bayes-expression-language.md, «Emission: Stan text from
// the typed AST»): the only way an expression reaches a Stan program. The text is written from the
// typed tree alone, never from the person's text: every identifier comes from the caller's
// EmitEnv and is checked before it is written, every function and operator from the catalogue,
// every number re-printed from its double. Everything is parenthesized, every binary operator has
// a space on each side, and the whole text is checked against the output's alphabet before it is
// returned. A failure is a bug of the caller (an identifier outside the rule, a tree Check did not
// make): Emit returns an error, never a text.

// EmitEnv is what stan-codegen gives for one emission: the program's identifiers for the nodes and
// for the derived data. Every string it returns must be an identifier of the program
// (^[A-Za-z][A-Za-z0-9_]*$, not ending in "__", not a word Stan reserves).
type EmitEnv interface {
	// Identifier is a node's Stan identifier, by the node id of Symbol.ID (the person's name, or a
	// generated one: answer 5).
	Identifier(nodeID string) string
	// IndexArray is the data array of a group index, by the index's node id, for the context
	// plate: the index itself when the context is its own plate, its lift (Up_<id>_<plate>) when
	// the indexing is lifted (Typed.Lifted, Checked.Lifts).
	IndexArray(indexID string, context Plate) string
	// LagArray is the data array of the rows k steps before, one per row the statement runs on
	// (Lag<k>), or that array taken at some of its positions (Lag<k>[Ppc_o1_rows]: an identifier
	// indexed by one, see RowsSubset).
	LagArray(k int) string
	// RowsSubset is the array of the rows the statement runs on when it is not every row
	// (Rows_lag for a statement with lag), "" otherwise. Emit asks it once per emission: when it is
	// not empty, every value over the context (a name, an indexing) is written on those rows,
	// x[Rows_lag], so every argument of a statement that runs on the lagged rows has their size,
	// lag or not. An expression with lag needs it. It may be an identifier indexed by one, the
	// positions predictive-checks names among a statement's rows (Rows_lag[Ppc_o1_rows]).
	RowsSubset() string
}

// ErrEmit is the error of every refused emission (errors.Is).
var ErrEmit = errors.New("bayesexpr: emit")

func emitError(what string) error { return fmt.Errorf("%w: %s", ErrEmit, what) }

// toVector is Stan's conversion of an array of integers to a vector (vector to_vector(array[] int)),
// from the catalogue's internal functions.
const toVector = "to_vector"

// binaryOps are the catalogue's binary operators: the Stan form of each, and its elementwise form.
var binaryOps = func() map[string]bayescatalog.Operator {
	m := map[string]bayescatalog.Operator{}
	for _, op := range bayescatalog.Declared().Language.Operators {
		if op.Kind == "binary" {
			m[op.Symbol] = op
		}
	}
	return m
}()

// callable are the names the output may call: the Stan names of the language's functions and
// to_vector when the catalogue lists it among its internal functions.
var callable = func() map[string]bool {
	m := map[string]bool{}
	for _, f := range functions {
		m[f.Stan] = true
	}
	for _, f := range bayescatalog.Declared().InternalFunctions {
		if f == toVector {
			m[f] = true
		}
	}
	return m
}()

// reservedIdentifiers are the words an identifier of the output may not be: Stan's reserved
// words, type names, block names, reserved functions, the C++ words it renames and the names of
// the model's C++ class (backend_member_words; checkIdentifier matches the patterns), and the
// words stan-codegen's output check refuses as tokens. A name the output may call is not an
// identifier either (a variable never shadows a function).
var reservedIdentifiers = func() map[string]bool {
	m := map[string]bool{}
	s := bayescatalog.StanLists()
	for _, list := range [][]string{s.ReservedWords, s.TypeNames, s.BlockNames, s.ReservedFunctions, s.RenamedBackendWords, s.BackendMemberWords,
		{"print", "reject", "fatal_error", "target", "include", "functions", "profile",
			"jacobian", "while", "if", "else", "return", "break", "continue"}} {
		for _, w := range list {
			m[w] = true
		}
	}
	return m
}()

// lexicalIdentifier reports a word of Stan's identifiers: ^[A-Za-z][A-Za-z0-9_]*$.
func lexicalIdentifier(s string) bool {
	if s == "" {
		return false
	}
	for i := 0; i < len(s); i++ {
		c := s[i]
		letter := c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z'
		if !letter && (i == 0 || !(c >= '0' && c <= '9' || c == '_')) {
			return false
		}
	}
	return true
}

// checkRows refuses a set of rows the caller gives (RowsSubset, LagArray) that is not an identifier,
// nor an identifier indexed by one (Rows_lag[Ppc_o1_rows]).
func checkRows(what, s string) error {
	base, rest, indexed := strings.Cut(s, "[")
	if !indexed {
		return checkIdentifier(what, s)
	}
	inner, ok := strings.CutSuffix(rest, "]")
	if !ok {
		return emitError(what + ": not an identifier nor one indexed by one: " + strconv.Quote(s))
	}
	if err := checkIdentifier(what, base); err != nil {
		return err
	}
	return checkIdentifier(what, inner)
}

// checkIdentifier refuses a string the caller gives that is not an identifier the output may hold.
func checkIdentifier(what, s string) error {
	if !lexicalIdentifier(s) {
		return emitError(what + ": not an identifier: " + strconv.Quote(s))
	}
	if strings.HasSuffix(s, "__") || reservedIdentifiers[s] || callable[s] || bayescatalog.StanLists().BackendMember(s) != "" {
		return emitError(what + ": a word Stan reserves: " + strconv.Quote(s))
	}
	for _, suffix := range bayescatalog.StanLists().ReservedSuffixes {
		if strings.HasSuffix(s, suffix) {
			return emitError(what + ": a suffix Stan reserves: " + strconv.Quote(s))
		}
	}
	return nil
}

// Emit writes the Stan text of a typed expression, its identifiers given by env.
//
//	number v        strconv.FormatFloat(v, 'g', -1, 64), ".0" appended without "." nor "e"
//	name            Identifier(id), id[RowsSubset] over the context on a subset of the rows
//	x[g]            (x[IndexArray(g, context)]), (x[g_array[RowsSubset]]) on a subset; x alone
//	                when the context is g's target
//	lag(x, k)       (x[LagArray(k)])
//	f(e, …)         the catalogue's Stan name of f, (e, …)
//	-e              (-e)
//	e1 op e2        (e1 op e2), op elementwise (.* ./ .^) as the operands' shapes require
//
// An integer leaf that enters an operation or a call (Typed.Convert) is made real on the leaf:
// (1.0 * n) for a single value, to_vector(n) over the context; so the text never divides two
// integers nor computes with Stan's integers. An expression that is one integer leaf is written as
// it is (an integer slot).
func Emit(c *Checked, env EmitEnv) (string, error) {
	if c == nil || c.Tree == nil || env == nil {
		return "", emitError("no tree or no environment")
	}
	e := &emitter{env: env, ctx: c.Context, maxLag: int64(catalogLimits.MaxLag), budget: catalogLimits.MaxExpressionNodes}
	if s := env.RowsSubset(); s != "" {
		if err := checkRows("RowsSubset", s); err != nil {
			return "", err
		}
		e.subset = s
	}
	if err := e.emit(c.Tree); err != nil {
		return "", err
	}
	out := e.b.String()
	if err := checkOutput(out); err != nil {
		return "", err
	}
	return out, nil
}

type emitter struct {
	env    EmitEnv
	ctx    Plate
	subset string
	maxLag int64
	budget int // the nodes left: a tree Check made has at most max_expression_nodes
	b      strings.Builder
}

// shape checks a node's type: a real or an integer, a single value or one over the context.
func (e *emitter) shape(t *Typed) error {
	if t.Type.Elem != ElemReal && t.Type.Elem != ElemInt {
		return emitError("a value of type " + t.Type.String())
	}
	if t.Type.Plate != "" && t.Type.Plate != e.ctx {
		return emitError("a value over " + strconv.Quote(string(t.Type.Plate)) + " in the context " + strconv.Quote(string(e.ctx)))
	}
	return nil
}

func (e *emitter) identifier(id string) (string, error) {
	s := e.env.Identifier(id)
	return s, checkIdentifier("Identifier("+strconv.Quote(id)+")", s)
}

func (e *emitter) emit(t *Typed) error {
	if t == nil || t.Node == nil {
		return emitError("a node without its syntax")
	}
	if e.budget--; e.budget < 0 {
		return emitError("more nodes than max_expression_nodes")
	}
	if err := e.shape(t); err != nil {
		return err
	}
	n := t.Node
	leaf := n.Kind == KindNum || n.Kind == KindName || n.Kind == KindIndex || n.Kind == KindLag
	if len(t.Args) != len(n.Args) {
		return emitError("a node whose operands are not its syntax's")
	}
	// Only an integer leaf is converted, and its conversion is told by Convert.
	if t.Convert && (t.Type.Elem != ElemInt && n.Kind != KindLag || !leaf || n.Kind == KindNum) {
		return emitError("a conversion of a value that is not an integer leaf")
	}
	if !leaf && t.Type.Elem != ElemReal {
		return emitError("an operation that is not real")
	}
	switch n.Kind {
	case KindNum:
		return e.number(n.Num)
	case KindName:
		return e.name(t)
	case KindIndex:
		return e.index(t)
	case KindLag:
		return e.lag(t)
	case KindCall:
		return e.call(t)
	case KindNeg:
		if len(t.Args) != 1 {
			return emitError("a unary minus without one operand")
		}
		e.b.WriteString("(-")
		if err := e.operand(t.Args[0]); err != nil {
			return err
		}
		e.b.WriteByte(')')
		return nil
	case KindBinary:
		return e.binary(t)
	}
	return emitError("a node of no kind")
}

// operand writes an operand of an operation or an argument of a call: an integer there is a leaf
// the checker marked for conversion.
func (e *emitter) operand(t *Typed) error {
	if t != nil && t.Type.Elem == ElemInt && !t.Convert {
		return emitError("an integer operand not converted")
	}
	return e.emit(t)
}

// number is a literal re-printed from its double: every literal is a real, so a number without
// "." nor exponent gets ".0" (2 is 2.0; 1e15 is 1e+15, 1e-5 is 1e-05).
func (e *emitter) number(v float64) error {
	if math.IsNaN(v) || math.IsInf(v, 0) || math.Signbit(v) {
		return emitError("a literal that is not a finite number >= 0")
	}
	s := strconv.FormatFloat(v, 'g', -1, 64)
	if !strings.ContainsAny(s, ".e") {
		s += ".0"
	}
	e.b.WriteString(s)
	return nil
}

// convert writes x made real when the leaf is converted: (1.0 * x) for a single value,
// to_vector(x) for an array of integers over the context.
func (e *emitter) convert(t *Typed, x string) {
	switch {
	case !t.Convert:
		e.b.WriteString(x)
	case t.Type.Plate == "":
		e.b.WriteString("(1.0 * " + x + ")")
	default:
		e.b.WriteString(toVector + "(" + x + ")")
	}
}

// name: the node's identifier; a value over the context on a subset of the rows is taken on it.
func (e *emitter) name(t *Typed) error {
	if t.Sym == nil {
		return emitError("a name without its node")
	}
	x, err := e.identifier(t.Sym.ID)
	if err != nil {
		return err
	}
	if t.Type.Plate != "" && e.subset != "" {
		x += "[" + e.subset + "]"
	}
	e.convert(t, x)
	return nil
}

// index: x[g] as x indexed by the index's array for the context (its lift when lifted), taken on
// the subset of the rows when there is one.
func (e *emitter) index(t *Typed) error {
	if t.Sym == nil || t.By == nil || t.Type.Plate == "" {
		return emitError("an indexing without its nodes or its context")
	}
	x, err := e.identifier(t.Sym.ID)
	if err != nil {
		return err
	}
	if t.Sym.Type.Plate == e.ctx {
		// The context is the index's target (alpha_school[school] per school): x itself, written
		// as a name; the index does not appear.
		if t.Lifted {
			return emitError("an indexing on its own target marked lifted")
		}
		if e.subset != "" {
			x += "[" + e.subset + "]"
		}
		e.convert(t, x)
		return nil
	}
	arr := e.env.IndexArray(t.By.ID, e.ctx)
	if err := checkIdentifier("IndexArray("+strconv.Quote(t.By.ID)+")", arr); err != nil {
		return err
	}
	if e.subset != "" {
		arr += "[" + e.subset + "]"
	}
	x += "[" + arr + "]"
	if !t.Convert {
		x = "(" + x + ")"
	}
	e.convert(t, x)
	return nil
}

// lag: lag(x, k) as x on the rows k steps before; it needs the statement's subset of the rows (the
// rows that have every lag), on which LagArray is defined.
func (e *emitter) lag(t *Typed) error {
	if t.Sym == nil || t.Type.Plate == "" || t.Type.Elem != ElemReal {
		return emitError("a lag without its node or its context")
	}
	if t.Convert != (t.Sym.Type.Elem == ElemInt) {
		return emitError("a lag whose conversion is not its node's")
	}
	if e.subset == "" {
		return emitError("a lag without RowsSubset")
	}
	k := t.Node.K
	if k < 1 || k > e.maxLag {
		return emitError("a lag outside 1…max_lag")
	}
	x, err := e.identifier(t.Sym.ID)
	if err != nil {
		return err
	}
	arr := e.env.LagArray(int(k))
	if err := checkRows("LagArray("+strconv.FormatInt(k, 10)+")", arr); err != nil {
		return err
	}
	x += "[" + arr + "]"
	if !t.Convert {
		x = "(" + x + ")"
	}
	e.convert(t, x)
	return nil
}

// call: the catalogue's Stan name of the function, never the person's.
func (e *emitter) call(t *Typed) error {
	f, ok := functions[t.Node.Name]
	if !ok || !callable[f.Stan] || len(t.Args) != f.Arguments {
		return emitError("a call outside the catalogue")
	}
	if !lexicalIdentifier(f.Stan) {
		return emitError("the catalogue's function is not an identifier: " + strconv.Quote(f.Stan))
	}
	e.b.WriteString(f.Stan + "(")
	for i, a := range t.Args {
		if i > 0 {
			e.b.WriteString(", ")
		}
		if err := e.operand(a); err != nil {
			return err
		}
	}
	e.b.WriteByte(')')
	return nil
}

// binary: (e1 op e2), the elementwise form when Stan needs it: * between two vectors, / by a
// vector, ^ with a vector on either side (+ and - are the same on vectors).
func (e *emitter) binary(t *Typed) error {
	op, ok := binaryOps[t.Node.Op]
	if !ok || len(t.Args) != 2 || t.Args[0] == nil || t.Args[1] == nil {
		return emitError("an operator outside the catalogue")
	}
	l, r := t.Args[0].Type.Plate != "", t.Args[1].Type.Plate != ""
	text := op.Stan
	switch t.Node.Op {
	case "*":
		if l && r {
			text = op.Elementwise
		}
	case "/":
		if r {
			text = op.Elementwise
		}
	case "^":
		if l || r {
			text = op.Elementwise
		}
	}
	switch text {
	case "+", "-", "*", "/", "^", ".*", "./", ".^":
	default:
		return emitError("an operator outside the output's alphabet")
	}
	e.b.WriteByte('(')
	if err := e.operand(t.Args[0]); err != nil {
		return err
	}
	e.b.WriteString(" " + text + " ")
	if err := e.operand(t.Args[1]); err != nil {
		return err
	}
	e.b.WriteByte(')')
	return nil
}

// outputByte reports the bytes of the output's alphabet: ^[A-Za-z0-9_ .()\[\],+\-*/^]*$.
func outputByte(c byte) bool {
	switch {
	case c >= 'a' && c <= 'z', c >= 'A' && c <= 'Z', c >= '0' && c <= '9':
		return true
	}
	return strings.IndexByte("_ .()[],+-*/^", c) >= 0
}

// checkOutput is the last check of every emitted text, independent of how it was written: the
// alphabet, none of //, /*, */, parentheses and brackets balanced and nested, and every name
// followed by "(" a name the output may call.
func checkOutput(s string) error {
	if s == "" {
		return emitError("an empty text")
	}
	var open []byte
	for i := 0; i < len(s); i++ {
		c := s[i]
		if !outputByte(c) {
			return emitError("a byte outside the output's alphabet: " + strconv.QuoteToASCII(string(c)))
		}
		if i+1 < len(s) {
			switch s[i : i+2] {
			case "//", "/*", "*/":
				return emitError("a comment in the output")
			}
		}
		switch c {
		case '(', '[':
			open = append(open, c)
		case ')', ']':
			want := byte('(')
			if c == ']' {
				want = '['
			}
			if len(open) == 0 || open[len(open)-1] != want {
				return emitError("unbalanced parentheses in the output")
			}
			open = open[:len(open)-1]
		}
		if c >= '0' && c <= '9' && (i == 0 || !wordByte(s[i-1])) {
			j, err := scanNumber(s, i)
			if err != nil {
				return err
			}
			i = j - 1
			continue
		}
		// A point is in a number or starts an elementwise operator: never a tuple's x.1.
		if c == '.' && (i+1 == len(s) || strings.IndexByte("*/^", s[i+1]) < 0) {
			return emitError("a point outside a number or an elementwise operator")
		}
		if wordByte(c) && (i == 0 || !wordByte(s[i-1])) {
			j := i
			for j < len(s) && wordByte(s[j]) {
				j++
			}
			if c == '_' {
				return emitError("a name starting with _ in the output")
			}
			if j < len(s) && s[j] == '(' && !callable[s[i:j]] {
				return emitError("a call outside the catalogue: " + strconv.Quote(s[i:j]))
			}
			i = j - 1
		}
	}
	if len(open) != 0 {
		return emitError("unbalanced parentheses in the output")
	}
	return nil
}

// scanNumber reads the number at s[i]: digits, [. digits], [e [+-] digits], and nothing of a
// name or another point after it. It returns the offset after the number.
func scanNumber(s string, i int) (int, error) {
	digits := func(j int) int {
		for j < len(s) && s[j] >= '0' && s[j] <= '9' {
			j++
		}
		return j
	}
	j := digits(i)
	if j < len(s) && s[j] == '.' {
		if k := digits(j + 1); k > j+1 {
			j = k
		} else {
			return 0, emitError("a number without its decimals")
		}
	}
	if j < len(s) && (s[j] == 'e' || s[j] == 'E') {
		k := j + 1
		if k < len(s) && (s[k] == '+' || s[k] == '-') {
			k++
		}
		if m := digits(k); m > k {
			j = m
		} else {
			return 0, emitError("a number without its exponent")
		}
	}
	if j < len(s) && (wordByte(s[j]) || s[j] == '.') {
		return 0, emitError("a number followed by a name or a point")
	}
	return j, nil
}

func wordByte(c byte) bool {
	return c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9' || c == '_'
}
