package bayesexpr

import (
	"encoding/json"
	"errors"
	"math"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"testing"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescatalog"
)

// corpusEmitEnv is the corpus's emission environment: a node's identifier is its name, an index
// on the context's own plate is its own array, a lifted one Up_<id>_<plate>, the lags Lag<k>,
// and an expression with lag runs on Rows_lag (as stan-codegen writes them).
type corpusEmitEnv struct {
	spec   envSpec
	subset string
}

func newCorpusEmitEnv(spec envSpec, c *Checked) corpusEmitEnv {
	e := corpusEmitEnv{spec: spec}
	if len(c.Lags) > 0 {
		e.subset = "Rows_lag"
	}
	return e
}

func (e corpusEmitEnv) node(id string) envNode {
	for _, n := range e.spec.Nodes {
		if n.ID == id {
			return n
		}
	}
	return envNode{}
}

func (e corpusEmitEnv) Identifier(id string) string { return e.node(id).Name }

func (e corpusEmitEnv) IndexArray(id string, ctx Plate) string {
	n := e.node(id)
	if n.Plate == ctx {
		return n.Name
	}
	return "Up_" + id + "_" + strings.ReplaceAll(string(ctx), ":", "_")
}

func (e corpusEmitEnv) LagArray(k int) string { return "Lag" + strconv.Itoa(k) }
func (e corpusEmitEnv) RowsSubset() string    { return e.subset }

// gives reports whether s is a string corpusEmitEnv can return for its spec, for verifyOutput.
func (e corpusEmitEnv) gives(s string) bool {
	if s == "Rows_lag" {
		return true
	}
	if k, ok := strings.CutPrefix(s, "Lag"); ok {
		n, err := strconv.Atoi(k)
		return err == nil && n >= 1 && n <= bayescatalog.GetLimits().MaxLag && strconv.Itoa(n) == k
	}
	for _, n := range e.spec.Nodes {
		if s == n.Name || strings.HasPrefix(s, "Up_"+n.ID+"_") {
			return true
		}
	}
	return false
}

// The test's own reading of the output, independent of checkOutput.
var (
	outputAlphabet = regexp.MustCompile(`^[A-Za-z0-9_ .()\[\],+\-*/^]*$`)
	outputToken    = regexp.MustCompile(`^(?:[0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?|[A-Za-z_][A-Za-z0-9_]*|\.[*/^]|[-+*/^()\[\], ])`)
	identifierRE   = regexp.MustCompile(`^[A-Za-z][A-Za-z0-9_]*$`)
	testForbidden  = []string{"//", "/*", "*/", "#", `"`, "'", ";", "{", "}", "~", "<", ">", "=", "\n", "\r", "\t"}
	testTokenWords = []string{"print", "reject", "fatal_error", "target", "include", "functions", "profile",
		"jacobian", "while", "if", "else", "return", "break", "continue"}
)

// testCallable are the names the output may call, read from the catalogue here.
func testCallable() map[string]bool {
	m := map[string]bool{"to_vector": slices.Contains(bayescatalog.Declared().InternalFunctions, "to_vector")}
	for _, f := range bayescatalog.Declared().Language.Functions {
		if !f.Apart {
			m[f.Stan] = true
		}
	}
	return m
}

// verifyOutput checks an emitted text: the alphabet, none of the forbidden sequences, balanced and
// nested parentheses and brackets, a space on each side of every binary operator, every name
// followed by "(" a catalogue function and every other one a string the environment gave (an
// identifier, not a word Stan reserves).
func verifyOutput(t *testing.T, src, out string, given func(string) bool) {
	t.Helper()
	if out == "" || !outputAlphabet.MatchString(out) {
		t.Fatalf("%q: %q outside the alphabet", src, out)
	}
	for _, f := range testForbidden {
		if strings.Contains(out, f) {
			t.Fatalf("%q: %q holds %q", src, out, f)
		}
	}
	callable := testCallable()
	var stack []string
	prev := ""
	for i := 0; i < len(out); {
		tok := outputToken.FindString(out[i:])
		if tok == "" {
			t.Fatalf("%q: %q: no token at %d", src, out, i)
		}
		next := out[i+len(tok):]
		switch {
		case tok == "(" || tok == "[":
			stack = append(stack, tok)
		case tok == ")" || tok == "]":
			want := map[string]string{")": "(", "]": "["}[tok]
			if len(stack) == 0 || stack[len(stack)-1] != want {
				t.Fatalf("%q: %q: unbalanced at %d", src, out, i)
			}
			stack = stack[:len(stack)-1]
		case tok[0] == '_' || tok[0] >= 'A' && tok[0] <= 'Z' || tok[0] >= 'a' && tok[0] <= 'z':
			if strings.HasPrefix(next, "(") {
				if !callable[tok] {
					t.Fatalf("%q: %q calls %q", src, out, tok)
				}
			} else if !given(tok) || !identifierRE.MatchString(tok) || strings.HasSuffix(tok, "__") ||
				slices.Contains(testTokenWords, tok) || callable[tok] {
				t.Fatalf("%q: %q names %q, not from the environment", src, out, tok)
			}
		case tok[0] >= '0' && tok[0] <= '9':
			if next != "" && (wordByte(next[0]) || next[0] == '.') {
				t.Fatalf("%q: %q: a number glued to %q", src, out, next)
			}
		case strings.Contains("+-*/^", tok[len(tok)-1:]) && tok != " ":
			unary := tok == "-" && prev == "("
			if !unary && (prev != " " || !strings.HasPrefix(next, " ")) {
				t.Fatalf("%q: %q: operator %q without its spaces at %d", src, out, tok, i)
			}
		}
		prev = tok
		i += len(tok)
	}
	if len(stack) != 0 {
		t.Fatalf("%q: %q: unbalanced", src, out)
	}
}

func emitCorpus(t *testing.T, spec envSpec, c *Checked) string {
	t.Helper()
	ee := newCorpusEmitEnv(spec, c)
	out, err := Emit(c, ee)
	if err != nil {
		t.Fatalf("%s: %v", Format(c.Tree.Node), err)
	}
	verifyOutput(t, Format(c.Tree.Node), out, ee.gives)
	return out
}

// specOf is the spec of a case's environment.
func specOf(t *testing.T, envs map[string]envSpec, raw json.RawMessage) envSpec {
	t.Helper()
	var ref struct {
		Use string `json:"use"`
	}
	if err := json.Unmarshal(raw, &ref); err != nil {
		t.Fatal(err)
	}
	return envs[ref.Use]
}

// TestCorpusEmit: every accepted typed case of the handwritten corpus has its Stan text, and Emit
// gives it.
func TestCorpusEmit(t *testing.T) {
	envs := readEnvs(t)
	n := 0
	for _, c := range readCorpus(t) {
		if c.Env == nil || !c.Expect.OK {
			continue
		}
		env := caseEnv(t, envs, c.Env)
		checked, err := CheckText(c.Input, env)
		if err != nil {
			continue // TestCorpusTypes says it
		}
		n++
		got := emitCorpus(t, specOf(t, envs, c.Env), checked)
		if c.Expect.Stan == "" {
			t.Errorf("%s: %q: no expected stan (it gives %q)", c.ID, c.Input, got)
		} else if got != c.Expect.Stan {
			t.Errorf("%s: %q: stan %q, want %q", c.ID, c.Input, got, c.Expect.Stan)
		}
	}
	if n < 90 {
		t.Errorf("%d emitted cases, want at least 90", n)
	}
}

// fixedEmitEnv answers every question with the same strings.
type fixedEmitEnv struct{ id, index, lag, subset string }

func (e fixedEmitEnv) Identifier(string) string        { return e.id }
func (e fixedEmitEnv) IndexArray(string, Plate) string { return e.index }
func (e fixedEmitEnv) LagArray(int) string             { return e.lag }
func (e fixedEmitEnv) RowsSubset() string              { return e.subset }

// An identifier of the caller outside the rule is a bug: Emit refuses it, whichever question it
// answers.
func TestEmitRefusesIdentifiers(t *testing.T) {
	envs := readEnvs(t)
	series := newTestEnv(t, envs["series"], "rows")
	c := check(t, series, "mu_region[region] + phi * lag(y, 1)")
	good := fixedEmitEnv{id: "x", index: "g", lag: "Lag1", subset: "Rows_lag"}
	if _, err := Emit(c, good); err != nil {
		t.Fatal(err)
	}
	// A generated identifier ending in _impl is no member of the model's C++ class (E4, finding 2).
	if _, err := Emit(c, fixedEmitEnv{id: "V_impl", index: "g", lag: "Lag1", subset: "Rows_lag"}); err != nil {
		t.Errorf("V_impl: %v", err)
	}
	bad := []string{"", "target", "print", "lower", "for", "vector", "model", "functions", "include", "x__", "lp__",
		"_x", "1x", "x y", "x;y", "x}", "x//", "x/*", "x.y", "x[1]", "x(", "a\nb", "\u00e9", "x_lpdf", "x_lupmf",
		"and", "STAN_MAJOR", "jacobian", "profile", "exp", "to_vector", "inv_logit",
		"return", "void", "tuple", "break", "continue", "complex_vector",
		"while", "if", "else", "log_prob", "write_array", "errno", "model_model", "get_any_sizedtypes", "my_impl"}
	for _, s := range bad {
		for i, env := range []fixedEmitEnv{
			{id: s, index: "g", lag: "Lag1", subset: "Rows_lag"},
			{id: "x", index: s, lag: "Lag1", subset: "Rows_lag"},
			{id: "x", index: "g", lag: s, subset: "Rows_lag"},
			{id: "x", index: "g", lag: "Lag1", subset: s},
		} {
			if s == "" && i == 3 {
				continue // no subset: the lag refuses it, below
			}
			out, err := Emit(c, env)
			if err == nil || !errors.Is(err, ErrEmit) || out != "" {
				t.Errorf("%q as answer %d: %q, %v", s, i, out, err)
			}
		}
	}
	if _, err := Emit(c, fixedEmitEnv{id: "x", index: "g", lag: "Lag1"}); !errors.Is(err, ErrEmit) {
		t.Errorf("a lag without the subset: %v", err)
	}
	// The rows (RowsSubset, LagArray) may be an identifier indexed by one, the positions
	// predictive-checks names among a statement's rows (lot P1), and nothing more.
	out, err := Emit(c, fixedEmitEnv{id: "x", index: "g", lag: "Lag1[Ppc_rows]", subset: "Rows_lag[Ppc_rows]"})
	if want := "((x[g[Rows_lag[Ppc_rows]]]) + (x * (x[Lag1[Ppc_rows]])))"; err != nil || out != want {
		t.Errorf("indexed rows: %q, %v; want %q", out, err, want)
	}
	for _, s := range []string{"x[y][z]", "x[y]z", "x[]", "[x]", "x[print]", "print[x]", "x[y", "x[y]]", "x[1]", "x[y z]",
		"x[y_lpdf]", "x[ y]", "x[(y)]", "x[y[z]]", "x[y;]"} {
		for i, env := range []fixedEmitEnv{
			{id: "x", index: "g", lag: s, subset: "Rows_lag"},
			{id: "x", index: "g", lag: "Lag1", subset: s},
		} {
			out, err := Emit(c, env)
			if err == nil || !errors.Is(err, ErrEmit) || out != "" {
				t.Errorf("%q as rows %d: %q, %v", s, i, out, err)
			}
		}
	}
}

// The subset of the rows applies to every value over the context, lag or not, so that every
// argument of a statement on the lagged rows has their size; single values are not taken on it.
func TestEmitSubset(t *testing.T) {
	envs := readEnvs(t)
	series := newTestEnv(t, envs["series"], "rows")
	ee := corpusEmitEnv{spec: envs["series"], subset: "Rows_lag"}
	for src, want := range map[string]string{
		"exp(trend + sigma)":                "exp((trend[Rows_lag] + sigma))",
		"u * mu_region[region]":             "(to_vector(u[Rows_lag]) .* (mu_region[region[Rows_lag]]))",
		"u":                                 "u[Rows_lag]",
		"lag(u, 2) + phi":                   "(to_vector(u[Lag2]) + phi)",
		"sigma":                             "sigma",
		"y - lag(y, 1)":                     "(y[Rows_lag] - (y[Lag1]))",
		"mu_region[region] / lag(level, 3)": "((mu_region[region[Rows_lag]]) ./ (level[Lag3]))",
	} {
		out, err := Emit(check(t, series, src), ee)
		if err != nil || out != want {
			t.Errorf("%s: %q, %v; want %q", src, out, err, want)
		}
	}
	// Without lag and without a subset, every row.
	out, err := Emit(check(t, series, "trend + u"), corpusEmitEnv{spec: envs["series"]})
	if err != nil || out != "(trend + to_vector(u))" {
		t.Errorf("%q, %v", out, err)
	}
}

// Numbers are re-printed from their double, never copied: every literal a real.
func TestEmitNumbers(t *testing.T) {
	env := newTestEnv(t, readEnvs(t)["schools"], "")
	ee := corpusEmitEnv{spec: readEnvs(t)["schools"]}
	for src, want := range map[string]string{
		"2": "2.0", "0": "0.0", "007": "7.0", "0.5": "0.5", "1.50": "1.5", "100000": "100000.0", "1e5": "100000.0",
		"1e6": "1e+06", "1e15": "1e+15", "0.0001": "0.0001", "0.00001": "1e-05", "1e-7": "1e-07",
		"123456789": "1.23456789e+08", "0.1": "0.1", "2.5E-3": "0.0025", "2.2250738585072014e-308": "2.2250738585072014e-308",
		"999999999999999": "9.99999999999999e+14", "3.141592653589793238": "3.141592653589793",
	} {
		out, err := Emit(check(t, env, src), ee)
		if err != nil || out != want {
			t.Errorf("%s: %q, %v; want %q", src, out, err, want)
		}
	}
}

// Trees that Check does not make are refused, never written: Emit trusts nothing of a tree but
// what it checks again.
func TestEmitRefusesTreesCheckDidNotMake(t *testing.T) {
	schools := readEnvs(t)["schools"]
	env := newTestEnv(t, schools, "rows")
	ee := corpusEmitEnv{spec: schools}
	fresh := func(src string) *Checked { return check(t, env, src) }
	cases := map[string]func() *Checked{
		"nil tree":   func() *Checked { return &Checked{} },
		"nan":        func() *Checked { c := fresh("2"); c.Tree.Node.Num = math.NaN(); return c },
		"inf":        func() *Checked { c := fresh("2"); c.Tree.Node.Num = math.Inf(1); return c },
		"negative":   func() *Checked { c := fresh("2"); c.Tree.Node.Num = -2; return c },
		"minus zero": func() *Checked { c := fresh("2"); c.Tree.Node.Num = math.Copysign(0, -1); return c },
		"no symbol":  func() *Checked { c := fresh("score"); c.Tree.Sym = nil; return c },
		"no by":      func() *Checked { c := fresh("alpha_school[school]"); c.Tree.By = nil; return c },
		"plate":      func() *Checked { c := fresh("score"); c.Tree.Type.Plate = "p1"; return c },
		"index type": func() *Checked { c := fresh("score"); c.Tree.Type.Elem = ElemIndex; return c },
		"int operand not converted": func() *Checked {
			c := fresh("count + 1")
			c.Tree.Args[0].Convert = false
			return c
		},
		"real converted": func() *Checked { c := fresh("score + 1"); c.Tree.Args[0].Convert = true; return c },
		"int operation":  func() *Checked { c := fresh("score + 1"); c.Tree.Type.Elem = ElemInt; return c },
		"operator": func() *Checked {
			c := fresh("score + 1")
			c.Tree.Node = &Node{Kind: KindBinary, Op: "%", Args: c.Tree.Node.Args}
			return c
		},
		"function": func() *Checked {
			c := fresh("exp(score)")
			c.Tree.Node = &Node{Kind: KindCall, Name: "print", Args: c.Tree.Node.Args}
			return c
		},
		"arity": func() *Checked {
			c := fresh("exp(score)")
			c.Tree.Args = append(c.Tree.Args, c.Tree.Args[0])
			c.Tree.Node = &Node{Kind: KindCall, Name: "exp", Args: append(c.Tree.Node.Args, c.Tree.Node.Args[0])}
			return c
		},
		"operands": func() *Checked { c := fresh("score + 1"); c.Tree.Args = c.Tree.Args[:1]; return c },
		"no node":  func() *Checked { c := fresh("score + 1"); c.Tree.Args[1] = nil; return c },
		"kind":     func() *Checked { c := fresh("score"); c.Tree.Node = &Node{Kind: 99}; return c },
		"too many nodes": func() *Checked {
			c := fresh("score")
			for range catalogLimits.MaxExpressionNodes {
				c.Tree = &Typed{Node: &Node{Kind: KindNeg, Args: []*Node{c.Tree.Node}}, Type: c.Tree.Type, Args: []*Typed{c.Tree}}
			}
			return c
		},
	}
	for name, mk := range cases {
		if out, err := Emit(mk(), ee); !errors.Is(err, ErrEmit) || out != "" {
			t.Errorf("%s: %q, %v", name, out, err)
		}
	}
	if _, err := Emit(nil, ee); !errors.Is(err, ErrEmit) {
		t.Errorf("nil: %v", err)
	}
	if _, err := Emit(fresh("score"), nil); !errors.Is(err, ErrEmit) {
		t.Errorf("nil env: %v", err)
	}
	series := readEnvs(t)["series"]
	senv := newTestEnv(t, series, "rows")
	sub := corpusEmitEnv{spec: series, subset: "Rows_lag"}
	for name, mk := range map[string]func() *Checked{
		"k zero":       func() *Checked { c := check(t, senv, "lag(y, 1)"); c.Tree.Node.K = 0; return c },
		"k past max":   func() *Checked { c := check(t, senv, "lag(y, 1)"); c.Tree.Node.K = 61; return c },
		"int lag kept": func() *Checked { c := check(t, senv, "lag(u, 1)"); c.Tree.Convert = false; return c },
		"real lag converted": func() *Checked {
			c := check(t, senv, "lag(y, 1)")
			c.Tree.Convert = true
			return c
		},
		"scalar lag": func() *Checked { c := check(t, senv, "lag(y, 1)"); c.Tree.Type.Plate = ""; return c },
	} {
		if out, err := Emit(mk(), sub); !errors.Is(err, ErrEmit) || out != "" {
			t.Errorf("%s: %q, %v", name, out, err)
		}
	}
}

// checkOutput, the last check, refuses on its own what the emitter could never write.
func TestCheckOutput(t *testing.T) {
	for _, s := range []string{
		"(a + b)", "(a .* b)", "((1.0 * n) ./ x)", "to_vector(n[Rows_lag])", "(1e+15 + 2.0)", "exp((-a))",
		"(x[Up_n5_p1])", "(2.0 .^ x)", "1e-05", "inv_logit(a)",
	} {
		if err := checkOutput(s); err != nil {
			t.Errorf("%q: %v", s, err)
		}
	}
	for _, s := range []string{
		"", "a;b", "a // b", "a/*b", "a*/b", "(a", "a)", "(a]", "[a)", "print(a)", "target(a)", "x(a)",
		"x.1", "a . b", "a.", "1e", "1.", "1.e5", "2i", "1e5x", "1..2", "_a", "a\nb", "a\tb", "a=b", "a~b",
		"{a}", "a<b", "#a", "\"a\"", "'a'", "a\\b", "a:b", "a?b", "\u00e9", "1exp(a)", "1e+", "1e+x",
	} {
		if err := checkOutput(s); !errors.Is(err, ErrEmit) {
			t.Errorf("%q: accepted", s)
		}
	}
}

// The catalogue gives every name the emitter calls and writes.
func TestEmitCatalogue(t *testing.T) {
	if !callable[toVector] || !slices.Contains(bayescatalog.Declared().InternalFunctions, toVector) {
		t.Errorf("to_vector is not among the catalogue's internal functions")
	}
	for _, f := range bayescatalog.Declared().Language.Functions {
		if f.Apart {
			continue
		}
		if !callable[f.Stan] || !lexicalIdentifier(f.Stan) || checkIdentifier("", f.Stan) == nil {
			t.Errorf("%s: Stan name %q", f.Name, f.Stan)
		}
	}
	for _, op := range []string{"+", "-", "*", "/", "^"} {
		if _, ok := binaryOps[op]; !ok {
			t.Errorf("%s: not a binary operator of the catalogue", op)
		}
	}
	if binaryOps["*"].Elementwise != ".*" || binaryOps["/"].Elementwise != "./" || binaryOps["^"].Elementwise != ".^" {
		t.Errorf("elementwise forms: %+v", binaryOps)
	}
}
