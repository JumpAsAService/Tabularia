package bayesexpr

import (
	"encoding/json"
	"errors"
	"math"
	"os"
	"slices"
	"strconv"
	"testing"
	"unicode/utf8"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescatalog"
)

// countNodes is the number of nodes of a tree.
func countNodes(n *Node) int {
	c := 1
	for _, a := range n.Args {
		c += countNodes(a)
	}
	return c
}

// FuzzParse: Parse never panics; its work is bounded by the limits (tokens read at most one per
// byte, nesting and nodes within the catalogue's); an error has a code of Parse and a span inside
// the input; an accepted tree has at most max_expression_nodes nodes, its Format reads back to the
// same tree, with the same nodes and no deeper nesting, and every node's span is inside the input.
// Every accepted tree is then typed against the corpus's environments (a context per plate):
// Check gives a type over the context or a scalar, with an interval and no NaN, or an error with a
// code of Check and a span inside the input.
// The seeds are the inputs of both corpora and testdata/fuzz/FuzzParse.
func FuzzParse(f *testing.F) {
	for _, c := range readCorpus(f) {
		f.Add(c.Input)
	}
	for _, input := range readGeneratedInputs(f) {
		f.Add(input)
	}
	envs := readEnvs(f)
	lim := bayescatalog.GetLimits()
	relaxed := lim
	relaxed.MaxExpressionBytes = 1 << 20
	f.Fuzz(func(t *testing.T, src string) {
		n, st, e := parse(src, &lim)
		if e != nil {
			if n != nil {
				t.Fatalf("%q: a tree and an error", src)
			}
			if !slices.Contains(syntaxCodes, e.Code) {
				t.Fatalf("%q: unknown code %v", src, e)
			}
			if e.Pos < 0 || e.Len < 0 || e.Pos+e.Len > utf8.RuneCountInString(src) {
				t.Fatalf("%q: span outside the input: %v", src, e)
			}
			if e.Code != CodeTooLong && e.Pos+e.Len > len(src) {
				t.Fatalf("%q: span past the bytes: %v", src, e)
			}
			return
		}
		if len(src) > lim.MaxExpressionBytes {
			t.Fatalf("%d bytes accepted", len(src))
		}
		if st.tokens > len(src)+1 || st.maxDepth > lim.MaxExpressionNesting || st.nodes > lim.MaxExpressionNodes {
			t.Fatalf("%q: work beyond the limits: %+v", src, st)
		}
		if c := countNodes(n); c > st.nodes {
			t.Fatalf("%q: %d nodes in the tree, %d counted", src, c, st.nodes)
		}
		var spans func(n *Node)
		spans = func(n *Node) {
			if n.Pos < 0 || n.Len <= 0 || n.end() > len(src) {
				t.Fatalf("%q: span %d+%d", src, n.Pos, n.Len)
			}
			for _, a := range n.Args {
				if a.Pos < n.Pos || a.end() > n.end() {
					t.Fatalf("%q: a child's span outside its parent's", src)
				}
				spans(a)
			}
		}
		spans(n)
		text := Format(n)
		if len(text) > 3*len(src) {
			t.Fatalf("%q: Format is %d bytes", src, len(text))
		}
		m, st2, e2 := parse(text, &relaxed)
		if e2 != nil {
			t.Fatalf("%q: Format gives %q, refused: %v", src, text, e2)
		}
		a, err := json.Marshal(n)
		if err != nil {
			t.Fatal(err)
		}
		b, err := json.Marshal(m)
		if err != nil {
			t.Fatal(err)
		}
		if string(a) != string(b) {
			t.Fatalf("%q: %s, then from %q: %s", src, a, text, b)
		}
		if Format(m) != text {
			t.Fatalf("%q: Format is not stable: %q, then %q", src, text, Format(m))
		}
		if st2.nodes != st.nodes || st2.maxDepth > st.maxDepth {
			t.Fatalf("%q: Format changes the work: %+v, then %+v", src, st, st2)
		}
		for _, name := range []string{"schools", "series"} {
			spec := envs[name]
			for _, ctx := range append([]Plate{""}, plateIDs(spec)...) {
				fuzzCheck(t, src, n, newTestEnv(t, spec, ctx))
			}
		}
	})
}

// readGeneratedInputs are the inputs of the committed generated corpus.
func readGeneratedInputs(t testing.TB) []string {
	t.Helper()
	raw, err := os.ReadFile(generatedPath)
	if err != nil {
		t.Fatal(err)
	}
	var file struct {
		Cases []struct {
			Input string `json:"input"`
		} `json:"cases"`
	}
	if err := json.Unmarshal(raw, &file); err != nil {
		t.Fatal(err)
	}
	inputs := make([]string, 0, len(file.Cases))
	for _, c := range file.Cases {
		inputs = append(inputs, c.Input)
	}
	return inputs
}

func plateIDs(spec envSpec) []Plate {
	ids := make([]Plate, 0, len(spec.Plates))
	for _, p := range spec.Plates {
		ids = append(ids, p.ID)
	}
	return ids
}

func fuzzCheck(t *testing.T, src string, n *Node, env *testEnv) {
	c, err := Check(n, env)
	if err != nil {
		var e *Error
		if !errors.As(err, &e) || !slices.Contains(checkCodes, e.Code) {
			t.Fatalf("%q in %q: not a code of Check: %v", src, env.ctx, err)
		}
		if e.Pos < 0 || e.Len <= 0 || e.Pos+e.Len > len(src) {
			t.Fatalf("%q in %q: span outside the input: %v", src, env.ctx, e)
		}
		return
	}
	ty := c.Tree.Type
	if (ty.Elem != ElemReal && ty.Elem != ElemInt) || (ty.Plate != "" && ty.Plate != env.ctx) {
		t.Fatalf("%q in %q: type %s", src, env.ctx, ty)
	}
	i := c.Interval()
	if math.IsNaN(i.Lo) || math.IsNaN(i.Hi) || i.Lo > i.Hi || math.IsInf(i.Lo, 1) || math.IsInf(i.Hi, -1) {
		t.Fatalf("%q in %q: interval %+v", src, env.ctx, i)
	}
}

// fuzzEmitEnv gives generated identifiers, V_<n> for the n-th node it is asked for, and records
// every string it gives: the output may name nothing else.
type fuzzEmitEnv struct {
	ids    map[string]string
	given  map[string]bool
	subset string
}

func newFuzzEmitEnv(subset string) *fuzzEmitEnv {
	e := &fuzzEmitEnv{ids: map[string]string{}, given: map[string]bool{}, subset: subset}
	if subset != "" {
		e.given[subset] = true
	}
	return e
}

func (e *fuzzEmitEnv) give(s string) string { e.given[s] = true; return s }

func (e *fuzzEmitEnv) Identifier(id string) string {
	if s, ok := e.ids[id]; ok {
		return s
	}
	e.ids[id] = "V_" + strconv.Itoa(len(e.ids)+1)
	return e.give(e.ids[id])
}

func (e *fuzzEmitEnv) IndexArray(id string, ctx Plate) string {
	return e.give("Ix_" + e.Identifier(id) + "_" + strconv.Itoa(len(ctx)))
}

func (e *fuzzEmitEnv) LagArray(k int) string { return e.give("Lag" + strconv.Itoa(k)) }
func (e *fuzzEmitEnv) RowsSubset() string    { return e.subset }

// hostileEmitEnv answers with strings the fuzzer chooses: Stan's words, broken identifiers, pieces
// of the fuzzer's bytes. Emit either refuses them or writes a text where every name is one of
// them and a valid identifier.
type hostileEmitEnv struct {
	p     picker
	seed  []byte
	given map[string]bool
}

var hostileAnswers = []string{"ok_name", "target", "print", "a;b", "x__", "_x", "a b", "a/*", "a//", "1a", "", "lower",
	"x_lpdf", "V_1", "Lag1", "Rows_lag", "a\nb", "é", "x}", "x.1", "x[1]", "exp", "to_vector", "N", "a(", "a)", "A"}

func (e *hostileEmitEnv) answer() string {
	var s string
	if k := e.p.pick(len(hostileAnswers) + 1); k < len(hostileAnswers) || len(e.seed) == 0 {
		s = hostileAnswers[k%len(hostileAnswers)]
	} else {
		i := e.p.pick(len(e.seed))
		s = string(e.seed[i : i+e.p.pick(len(e.seed)-i+1)])
	}
	e.given[s] = true
	return s
}

func (e *hostileEmitEnv) Identifier(string) string        { return e.answer() }
func (e *hostileEmitEnv) IndexArray(string, Plate) string { return e.answer() }
func (e *hostileEmitEnv) LagArray(int) string             { return e.answer() }
func (e *hostileEmitEnv) RowsSubset() string              { return e.answer() }

// FuzzEmit: a tree of the grammar built from the fuzzer's bytes (corpus_test.go's generator, over
// the corpus's environments), typed, then emitted twice.
//
//   - With generated identifiers (V_<n>; Rows_lag when the expression has lag, or now and then
//     another subset): Emit never fails on a typed tree; its text is in the output's alphabet, has
//     none of the forbidden sequences, balanced parentheses, a space on each side of every binary
//     operator, calls only catalogue functions and names only what the environment gave; Emit is
//     deterministic.
//   - With hostile answers: Emit refuses, or its text passes the same checks, every name a valid
//     identifier the environment gave.
//
// Emit never panics. The seeds are fixed byte strings and testdata/fuzz/FuzzEmit.
func FuzzEmit(f *testing.F) {
	f.Add([]byte{})
	f.Add([]byte("schools rows a + b * x"))
	r := &splitmix{s: 1}
	for n := 1; n <= 24; n++ {
		b := make([]byte, 8*n)
		for i := range b {
			b[i] = byte(r.pick(256))
		}
		f.Add(b)
	}
	envs := readEnvs(f)
	choices := envChoices(envs)
	f.Fuzz(func(t *testing.T, seed []byte) {
		p := &bytePicker{b: seed}
		ch := choices[p.pick(len(choices))]
		spec := envs[ch.use]
		env := newTestEnv(t, spec, ch.ctx)
		g := newExprGen(p, spec, env)
		src, _ := g.text(0)
		c, err := CheckText(src, env)
		if err != nil {
			return
		}
		subset := ""
		if len(c.Lags) > 0 {
			subset = "Rows_lag"
		} else if ch.ctx != "" && p.pick(4) == 0 {
			subset = "Rows_first"
		}
		fe := newFuzzEmitEnv(subset)
		out, err := Emit(c, fe)
		if err != nil {
			t.Fatalf("%q in %q: %v", src, ch.ctx, err)
		}
		verifyOutput(t, src, out, func(s string) bool { return fe.given[s] })
		again, err := Emit(c, newFuzzEmitEnv(subset))
		if err != nil || again != out {
			t.Fatalf("%q: not deterministic: %q, then %q (%v)", src, out, again, err)
		}
		he := &hostileEmitEnv{p: p, seed: seed, given: map[string]bool{}}
		hout, err := Emit(c, he)
		if err != nil {
			if !errors.Is(err, ErrEmit) || hout != "" {
				t.Fatalf("%q: %q, %v", src, hout, err)
			}
			return
		}
		verifyOutput(t, src, hout, func(s string) bool { return he.given[s] })
	})
}
