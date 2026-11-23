package bayesexpr

import (
	"encoding/json"
	"slices"
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
// The seeds are the inputs of the handwritten corpus and testdata/fuzz/FuzzParse.
func FuzzParse(f *testing.F) {
	for _, c := range readCorpus(f) {
		f.Add(c.Input)
	}
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
	})
}
