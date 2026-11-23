package bayesexpr

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"reflect"
	"slices"
	"strconv"
	"strings"
	"testing"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescatalog"
)

// corpusCase is one case of testdata/corpus/handwritten.json. A case without Env is read by
// Parse alone (lot E0); the type checker's cases carry an environment (lot E1, check_test.go).
type corpusCase struct {
	ID     string          `json:"id"`
	Input  string          `json:"input"`
	Env    json.RawMessage `json:"env,omitempty"`
	Note   string          `json:"note,omitempty"`
	Expect struct {
		OK     bool              `json:"ok"`
		AST    json.RawMessage   `json:"ast"`
		Format string            `json:"format"`
		Code   Code              `json:"code"`
		Pos    int               `json:"pos"`
		Len    int               `json:"len"`
		Args   map[string]string `json:"args"`
		// A typed case's, when accepted: the value's type and its interval.
		Type     string          `json:"type"`
		Interval json.RawMessage `json:"interval"`
		// Its Stan text (Go only: the mirror has no emitter), with the corpus's emission
		// environment (emit_test.go).
		Stan string `json:"stan"`
	} `json:"expect"`
}

func readCorpus(t testing.TB) []corpusCase {
	t.Helper()
	raw, err := os.ReadFile("testdata/corpus/handwritten.json")
	if err != nil {
		t.Fatal(err)
	}
	var file struct {
		Cases []corpusCase `json:"cases"`
	}
	if err := json.Unmarshal(raw, &file); err != nil {
		t.Fatal(err)
	}
	return file.Cases
}

// sameJSON compares two JSON texts as values (key order and spacing aside).
func sameJSON(t *testing.T, a, b []byte) bool {
	t.Helper()
	var x, y any
	if err := json.Unmarshal(a, &x); err != nil {
		t.Fatalf("%s: %v", a, err)
	}
	if err := json.Unmarshal(b, &y); err != nil {
		t.Fatalf("%s: %v", b, err)
	}
	return reflect.DeepEqual(x, y)
}

func TestCorpusSyntax(t *testing.T) {
	cases := readCorpus(t)
	seen := map[string]bool{}
	codes := map[Code]int{}
	for _, c := range cases {
		if seen[c.ID] {
			t.Errorf("%s: id twice", c.ID)
		}
		seen[c.ID] = true
		if c.Env != nil {
			continue
		}
		n, err := Parse(c.Input)
		if c.Expect.OK {
			if err != nil {
				t.Errorf("%s: %q: want ok, got %v", c.ID, c.Input, err)
				continue
			}
			got, _ := json.Marshal(n)
			if !sameJSON(t, got, c.Expect.AST) {
				t.Errorf("%s: %q: ast %s, want %s", c.ID, c.Input, got, c.Expect.AST)
			}
			if f := Format(n); f != c.Expect.Format {
				t.Errorf("%s: %q: format %q, want %q", c.ID, c.Input, f, c.Expect.Format)
			}
			continue
		}
		var e *Error
		if !errors.As(err, &e) {
			t.Errorf("%s: %q: want %s, got %v", c.ID, c.Input, c.Expect.Code, err)
			continue
		}
		codes[c.Expect.Code]++
		want := &Error{Code: c.Expect.Code, Pos: c.Expect.Pos, Len: c.Expect.Len, Args: c.Expect.Args}
		if e.Code != want.Code || e.Pos != want.Pos || e.Len != want.Len || !sameArgs(e.Args, want.Args) {
			t.Errorf("%s: %q: got %v, want %v", c.ID, c.Input, e, want)
		}
	}
	for _, code := range syntaxCodes {
		if codes[code] < 2 {
			t.Errorf("%s: %d cases in the corpus, want at least 2", code, codes[code])
		}
	}
	for code := range codes {
		if !slices.Contains(syntaxCodes, code) {
			t.Errorf("%s: not a code of Parse", code)
		}
	}
}

func sameArgs(a, b map[string]string) bool {
	if len(a) == 0 && len(b) == 0 {
		return true
	}
	return reflect.DeepEqual(a, b)
}

// The generated corpus (testdata/corpus/generated.json): inputs from a seeded generator, half
// grammar-driven (trees of the language over the corpus's environments, rendered with random
// spacing and parentheses), half mutations of the handwritten inputs (a piece inserted, deleted or
// replaced, from an alphabet weighted towards the symbols, the hints and the confusables), each
// with Go's results. go test regenerates it in memory and fails when the committed file differs;
// BAYES_CORPUS_WRITE=1 rewrites it (to be reviewed). The TypeScript mirror reads it too.

const (
	generatedPath     = "testdata/corpus/generated.json"
	generatedSeed     = 20261123
	generatedGrammar  = 1000
	generatedMutation = 1000
	// mutationMaxBytes: only the shorter handwritten inputs are mutated (the long ones are the
	// limits' cases, which the handwritten corpus holds already).
	mutationMaxBytes = 200
)

// picker gives the generator's choices: a number in [0, n).
type picker interface{ pick(n int) int }

// splitmix is SplitMix64: a source fixed forever, so the committed corpus is the same with any Go
// version.
type splitmix struct{ s uint64 }

func (r *splitmix) pick(n int) int {
	if n <= 1 {
		return 0
	}
	r.s += 0x9e3779b97f4a7c15
	z := r.s
	z = (z ^ (z >> 30)) * 0xbf58476d1ce4e5b9
	z = (z ^ (z >> 27)) * 0x94d049bb133111eb
	z ^= z >> 31
	return int(z % uint64(n))
}

// bytePicker reads the choices from the fuzzer's bytes; past their end every choice is 0.
type bytePicker struct {
	b []byte
	i int
}

func (p *bytePicker) pick(n int) int {
	if n <= 1 || p.i >= len(p.b) {
		return 0
	}
	v := int(p.b[p.i])
	p.i++
	if n > 256 && p.i < len(p.b) {
		v = v<<8 | int(p.b[p.i])
		p.i++
	}
	return v % n
}

// envChoice is an environment of the corpus with a context plate.
type envChoice struct {
	use string
	ctx Plate
}

// envChoices are every environment of the corpus with every context, in a fixed order.
func envChoices(envs map[string]envSpec) []envChoice {
	var out []envChoice
	for _, use := range []string{"schools", "series"} {
		out = append(out, envChoice{use, ""})
		for _, p := range envs[use].Plates {
			out = append(out, envChoice{use, p.ID})
		}
	}
	return out
}

// exprGen writes the text of a tree of the language over an environment: mostly names that type
// in the context, sometimes any name of the environment (so the type errors are met too).
type exprGen struct {
	p     picker
	names []string // bare names over the context or single values
	index []string // x[g] that type in the context
	lags  []string // the names lag takes in the context
	all   []string // every name of the environment
	nodes int
}

const (
	genMaxDepth = 5
	genMaxNodes = 40
)

var (
	genNumbers = []string{"0", "1", "2", "3", "0.5", "10", "2.5", "1e3", "2.5E-3", "1e15", "1e-7", "100000",
		"1000000", "0.0001", "0.00001", "007", "1.0", "123456789", "3.14159", "0.1", "1e-320", "999999999999999"}
	genSpaces = []string{"", "", "", " ", " ", " ", "  ", "\t", "\n", "\r\n"}
	genLagK   = []string{"1", "1", "1", "2", "2", "3", "12", "60", "001", "0", "61"}
)

func newExprGen(p picker, spec envSpec, env *testEnv) *exprGen {
	g := &exprGen{p: p}
	ctx := env.ctx
	number := func(n envNode) bool { return n.Type == "real" || n.Type == "int" }
	for _, n := range spec.Nodes {
		g.all = append(g.all, n.Name)
		if number(n) && (n.Plate == "" || n.Plate == ctx) {
			g.names = append(g.names, n.Name)
		}
		if number(n) && ctx != "" && n.Plate == ctx && env.IsSeries(ctx) && slices.Contains(lagRoles, n.Role) {
			g.lags = append(g.lags, n.Name)
		}
		if n.Type != "index" {
			continue
		}
		ok := ctx == n.Plate || ctx != "" && env.Encloses(n.Target, ctx) && env.Encloses(ctx, n.Plate)
		for _, x := range spec.Nodes {
			if ok && number(x) && x.Plate == n.Target {
				g.index = append(g.index, x.Name+"["+n.Name+"]")
			}
		}
	}
	return g
}

func (g *exprGen) one(list []string) string { return list[g.p.pick(len(list))] }
func (g *exprGen) sp() string               { return g.one(genSpaces) }

// name is a name of the list, or now and then any name of the environment.
func (g *exprGen) name(list []string) string {
	if len(list) == 0 || g.p.pick(40) == 0 {
		return g.one(g.all)
	}
	return g.one(list)
}

// wrap puts parentheses around a text of a looser level than its place needs, and now and then
// around any text.
func (g *exprGen) wrap(s string, lvl, min int) string {
	if lvl < min || g.p.pick(10) == 0 {
		return "(" + g.sp() + s + g.sp() + ")"
	}
	return s
}

// text is the text of a tree and its level (ast.go's levels).
func (g *exprGen) text(depth int) (string, int) {
	g.nodes++
	k := g.p.pick(12)
	if depth >= genMaxDepth || g.nodes >= genMaxNodes {
		k %= 4
	}
	switch k {
	case 0:
		return g.one(genNumbers), levelAtom
	case 1:
		return g.name(g.names), levelAtom
	case 2:
		if len(g.index) == 0 && g.p.pick(8) != 0 {
			return g.name(g.names), levelAtom
		}
		ix := g.name(g.index)
		if !strings.Contains(ix, "[") {
			ix += "[" + g.one(g.all) + "]"
		}
		x, by, _ := strings.Cut(strings.TrimSuffix(ix, "]"), "[")
		return x + g.sp() + "[" + g.sp() + by + g.sp() + "]", levelAtom
	case 3:
		if len(g.lags) == 0 && g.p.pick(16) != 0 {
			return g.name(g.names), levelAtom
		}
		return "lag(" + g.sp() + g.name(g.lags) + g.sp() + "," + g.sp() + g.one(genLagK) + g.sp() + ")", levelAtom
	case 4:
		s, l := g.text(depth + 1)
		return "-" + g.sp() + g.wrap(s, l, levelSigned), levelSigned
	case 5, 6, 7, 8, 9:
		op := []string{"+", "-", "*", "/", "^"}[k-5]
		lvl, left, right := levelSum, levelSum, levelProduct
		switch op {
		case "*", "/":
			lvl, left, right = levelProduct, levelProduct, levelSigned
		case "^":
			lvl, left, right = levelPower, levelAtom, levelSigned
		}
		a, la := g.text(depth + 1)
		b, lb := g.text(depth + 1)
		return g.wrap(a, la, left) + g.sp() + op + g.sp() + g.wrap(b, lb, right), lvl
	case 10:
		fs := bayescatalog.Declared().Language.Functions
		f := fs[g.p.pick(len(fs))]
		n := f.Arguments
		if f.Apart || g.p.pick(16) == 0 {
			n = 1 + g.p.pick(3)
		}
		args := make([]string, n)
		for i := range args {
			s, _ := g.text(depth + 1)
			args[i] = g.sp() + s + g.sp()
		}
		return f.Name + "(" + strings.Join(args, ",") + ")", levelAtom
	}
	s, _ := g.text(depth + 1)
	return "(" + g.sp() + s + g.sp() + ")", levelAtom
}

// mutationPieces is the alphabet of the mutations, weighted towards the symbols, the hints and the
// confusables.
var mutationPieces = []string{
	"+", "-", "*", "/", "^", "(", ")", "[", "]", ",", " ", ".",
	"+", "-", "*", "/", "^", "(", ")", "[", "]", ",", " ", ".",
	"**", "%", "%/%", "=", "==", "<-", ";", "~", "<", ">", "!", "&", "|", "?", ":", ".*", "./", ".^", "\\", "{", "}",
	"//", "/*", "#", "\"", "'",
	"\u2212", "\u2013", "\u00d7", "\u00b7", "\u00f7", "\u00a0", "\u2009", "\u200b", "\ufeff", "\uff11", "\u0430",
	"\u03b1", "\u202e", "\u2066",
	"a", "x", "e", "E", "i", "_", "__", "0", "1", "9", "A", "Z", "\t", "\n", "\x01", "\x7f",
	"mu", "school", "lag", "exp(", "y", "1e400", "1e-400",
}

// mutate inserts, deletes or replaces one to three pieces of s, by code point.
func mutate(p picker, s string) string {
	r := []rune(s)
	for range 1 + p.pick(3) {
		at := 0
		if len(r) > 0 {
			at = p.pick(len(r) + 1)
		}
		piece := []rune(mutationPieces[p.pick(len(mutationPieces))])
		switch p.pick(3) {
		case 0:
			r = slices.Insert(r, at, piece...)
		case 1:
			if at < len(r) {
				r = slices.Delete(r, at, at+1)
			}
		default:
			if at < len(r) {
				r = slices.Replace(r, at, at+1, piece...)
			} else {
				r = append(r, piece...)
			}
		}
	}
	return string(r)
}

type generatedCase struct {
	ID    string `json:"id"`
	Input string `json:"input"`
	Env   struct {
		Use     string `json:"use"`
		Context Plate  `json:"context"`
	} `json:"env"`
	Expect any `json:"expect"`
}

type generatedOK struct {
	OK       bool            `json:"ok"`
	AST      json.RawMessage `json:"ast"`
	Format   string          `json:"format"`
	Type     string          `json:"type"`
	Interval json.RawMessage `json:"interval"`
	Stan     string          `json:"stan"`
}

type generatedErr struct {
	Code Code              `json:"code"`
	Pos  int               `json:"pos"`
	Len  int               `json:"len"`
	Args map[string]string `json:"args,omitempty"`
}

// generate builds the generated corpus's text: every case typed and, when accepted, emitted and
// checked by verifyOutput.
func generate(t *testing.T) []byte {
	envs := readEnvs(t)
	choices := envChoices(envs)
	var hand []corpusCase
	for _, c := range readCorpus(t) {
		if len(c.Input) <= mutationMaxBytes {
			hand = append(hand, c)
		}
	}
	r := &splitmix{s: generatedSeed}
	var cases []generatedCase
	add := func(id, input string, ch envChoice) {
		c := generatedCase{ID: id, Input: input}
		c.Env.Use, c.Env.Context = ch.use, ch.ctx
		spec := envs[ch.use]
		checked, err := CheckText(input, newTestEnv(t, spec, ch.ctx))
		if err != nil {
			var e *Error
			if !errors.As(err, &e) {
				t.Fatalf("%s: %q: %v", id, input, err)
			}
			c.Expect = generatedErr{Code: e.Code, Pos: e.Pos, Len: e.Len, Args: e.Args}
		} else {
			ast, _ := json.Marshal(checked.Tree.Node)
			iv, _ := json.Marshal(checked.Interval().Catalog())
			c.Expect = generatedOK{OK: true, AST: ast, Format: Format(checked.Tree.Node), Type: checked.Tree.Type.String(),
				Interval: iv, Stan: emitCorpus(t, spec, checked)}
		}
		cases = append(cases, c)
	}
	for i := range generatedGrammar {
		ch := choices[r.pick(len(choices))]
		spec := envs[ch.use]
		g := newExprGen(r, spec, newTestEnv(t, spec, ch.ctx))
		s, _ := g.text(0)
		add(fmt.Sprintf("gen.grammar.%04d", i+1), g.sp()+s+g.sp(), ch)
	}
	for i := range generatedMutation {
		src := hand[r.pick(len(hand))]
		ch := choices[r.pick(len(choices))]
		if src.Env != nil {
			var ref struct {
				Use     string `json:"use"`
				Context Plate  `json:"context"`
			}
			if err := json.Unmarshal(src.Env, &ref); err != nil {
				t.Fatal(err)
			}
			ch = envChoice{ref.Use, ref.Context}
		}
		add(fmt.Sprintf("gen.mutation.%04d", i+1), mutate(r, src.Input), ch)
	}
	var b bytes.Buffer
	b.WriteString("{\n  \"about\": ")
	about, _ := json.Marshal("The expression language's generated cases (docs/specs/bayes-expression-language.md, «Testing strategy»), " +
		"written by TestCorpusGenerated with BAYES_CORPUS_WRITE=1 from a fixed seed: half trees of the grammar over the " +
		"handwritten corpus's environments, half mutations of its inputs. Each case is typed in its env ({use, context}, " +
		"as in handwritten.json); expect is {code, pos, len, args} or {ok, ast, format, type, interval, stan}, stan " +
		"being Go's only (the emission environment of the Go tests). Read by the Go tests and by the TypeScript mirror.")
	b.Write(about)
	b.WriteString(",\n  \"seed\": " + strconv.Itoa(generatedSeed) + ",\n  \"cases\": [\n")
	for i, c := range cases {
		var line bytes.Buffer
		enc := json.NewEncoder(&line)
		enc.SetEscapeHTML(false)
		if err := enc.Encode(c); err != nil {
			t.Fatal(err)
		}
		b.WriteString("    " + strings.TrimSuffix(line.String(), "\n"))
		if i < len(cases)-1 {
			b.WriteByte(',')
		}
		b.WriteByte('\n')
	}
	b.WriteString("  ]\n}\n")
	return b.Bytes()
}

// TestCorpusGenerated: the committed generated corpus is what the generator gives now.
func TestCorpusGenerated(t *testing.T) {
	got := generate(t)
	if os.Getenv("BAYES_CORPUS_WRITE") == "1" {
		if err := os.WriteFile(generatedPath, got, 0o644); err != nil {
			t.Fatal(err)
		}
		return
	}
	want, err := os.ReadFile(generatedPath)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(got, want) {
		gl, wl := strings.Split(string(got), "\n"), strings.Split(string(want), "\n")
		for i := 0; i < len(gl) && i < len(wl); i++ {
			if gl[i] != wl[i] {
				t.Fatalf("%s differs at line %d (BAYES_CORPUS_WRITE=1 rewrites it):\n got %.300s\nwant %.300s", generatedPath, i+1, gl[i], wl[i])
			}
		}
		t.Fatalf("%s differs in length: %d lines, want %d", generatedPath, len(gl), len(wl))
	}
}
